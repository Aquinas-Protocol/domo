"""Sync CalDAV client for Apple iCloud Calendar.

This module is intentionally MCP-free and async-free. The MCP wrappers in
``calendar_tools.py`` and the standalone snapshot worker in
``scripts/calendar_mirror.py`` both consume this client.

The single :class:`CalendarClient` instance holds a long-lived
``caldav.DAVClient`` and lazy caches for the principal + calendar discovery.
A reentrant lock (``threading.RLock``) wraps every public method because
multiple Discord agents may dispatch to the same client through
``asyncio.to_thread`` concurrently.

Defaults / safety rails:
- ``list_events`` defaults to ``now..now + 14 days`` when no range is given.
- A range > 90 days is rejected unless ``force_unbounded=True``.
- Recurring events are expanded to per-occurrence rows
  (``cal.search(expand=True)``).
- Events are returned with ``etag``, ``last_modified``, ``transparent``,
  ``participation_status``, and ``recurrence_id`` so write tools can detect
  concurrent modifications and so ``find_free_time`` can filter correctly.

Recurrence write semantics (see ``update_event`` / ``delete_event``):
- ``scope="series"`` mutates the master VEVENT.
- ``scope="instance"`` adds an EXDATE to the master and creates an override
  event for the specific occurrence.
- ``scope="future"`` is reserved but currently raises ``NotImplementedError``;
  the safe fix is to delete + recreate from the boundary by hand. v2.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import caldav
import pytz
import vobject
from caldav.elements import dav as caldav_dav
from caldav.lib.error import (
    AuthorizationError,
    ETagMismatchError,
    NotFoundError,
)

# caldav 3.x reads ``ev.etag`` from ``ev.props[GETETAG_TAG]`` and auto-sends
# ``If-Match: <etag>`` on save/delete when that key is set. We use this to
# implement optimistic concurrency without parsing CalDAV protocol XML.
GETETAG_TAG = caldav_dav.GetEtag.tag

from src.calendar_tokens import (
    decode_calendar_id,
    decode_event_id,
    encode_calendar_id,
    encode_event_id,
)

log = logging.getLogger(__name__)

DEFAULT_RANGE_DAYS = 14
MAX_RANGE_DAYS = 90
ICLOUD_URL = "https://caldav.icloud.com/"


# --------------------------- public dataclasses ---------------------------


@dataclass(frozen=True)
class CalendarInfo:
    calendar_id: str
    name: str
    color: str | None
    read_only: bool
    is_subscribed: bool


@dataclass(frozen=True)
class EventDict:
    event_id: str
    calendar_id: str
    calendar_name: str
    title: str
    start: str  # ISO-8601 (date for all-day, datetime otherwise)
    end: str
    all_day: bool
    location: str | None
    description: str | None
    etag: str | None
    last_modified: str | None
    transparent: bool
    participation_status: str | None
    recurrence_id: str | None
    recurring: bool


@dataclass(frozen=True)
class Slot:
    start: str
    end: str


# --------------------------- error types ---------------------------


class CalendarClientError(Exception):
    """Base class — every public method raises a subclass on failure."""


class AuthError(CalendarClientError):
    pass


class NotFound(CalendarClientError):
    pass


class ReadOnlyCalendarError(CalendarClientError):
    pass


class AmbiguousCalendarError(CalendarClientError):
    pass


class ConflictError(CalendarClientError):
    """Raised on etag mismatch — caller should refetch and retry."""


class RangeTooLargeError(CalendarClientError):
    pass


# --------------------------- internal helpers ---------------------------


def _serialize_dt(value: Any, tz_name: str) -> str:
    """Render a vobject date/datetime to ISO-8601, attaching tz_name to naive."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo(tz_name))
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _vobject_dt(dt: datetime) -> datetime:
    """Convert a datetime to a UTC datetime tagged with pytz.UTC.

    vobject (0.9.x) only knows how to serialize tzinfo objects from pytz /
    dateutil; ``zoneinfo.ZoneInfo("America/Chicago")`` raises 'Unable to
    guess TZID' on ``cal.serialize()``. Coercing to UTC sidesteps the
    problem and is fully iCalendar-compliant.
    """
    if dt.tzinfo is None:
        return pytz.UTC.localize(dt)
    return dt.astimezone(pytz.UTC)


def _is_all_day(start: Any) -> bool:
    return isinstance(start, date) and not isinstance(start, datetime)


def _aware(dt: datetime, tz_name: str) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt


def _parse_iso(text: str, tz_name: str) -> datetime:
    """Parse ISO-8601, treating naive timestamps as local in tz_name."""
    text = text.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"could not parse {text!r} as ISO timestamp: {e}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt


_DEFAULT_PORT_RE = re.compile(r"^(https?)://([^/]+?):(?:443|80)(?=/|$)")


def _normalize_href(href: str) -> str:
    """Drop default ports so ``https://x`` and ``https://x:443`` compare equal.

    iCloud's CalDAV responses sometimes carry the explicit ``:443`` port and
    sometimes don't. Without normalization, an event_id encoded after a
    response that included ``:443`` won't match the calendar dict key set at
    discovery time (which omitted it), and event lookups fail with a
    "calendar not found for this event_id" error.
    """
    if not href:
        return href
    return _DEFAULT_PORT_RE.sub(r"\1://\2", href)


def _href_of(obj: Any) -> str:
    """Get the absolute URL string from a caldav object's url attribute, normalized."""
    url = getattr(obj, "url", None)
    if url is None:
        return ""
    return _normalize_href(str(url))


def _safe_text(obj: Any) -> str:
    """Get .value off a vobject component property; return '' if missing."""
    if obj is None:
        return ""
    val = getattr(obj, "value", None)
    if val is None:
        return ""
    return str(val)


def _master_vevent(vcal: Any) -> Any:
    """Return the master VEVENT from a parsed vobject Calendar.

    The master is the first VEVENT without RECURRENCE-ID. Overrides have
    RECURRENCE-ID and refer to a specific instance.
    """
    events = list(getattr(vcal, "vevent_list", []) or [])
    for ve in events:
        if not hasattr(ve, "recurrence_id"):
            return ve
    return events[0] if events else None


def _attendee_partstat(vevent: Any, user_email: str) -> str | None:
    """Look up the user's PARTSTAT on the event's ATTENDEE list."""
    if not user_email:
        return None
    needle = user_email.lower()
    for att in getattr(vevent, "attendee_list", []) or []:
        val = (getattr(att, "value", "") or "").lower()
        if val.startswith("mailto:"):
            val = val[len("mailto:"):]
        if val == needle:
            params = getattr(att, "params", {}) or {}
            partstat = params.get("PARTSTAT")
            if isinstance(partstat, list):
                return partstat[0] if partstat else None
            return partstat
    return None


def _privilege_writable(cal: Any) -> bool | None:
    """Best-effort: True/False if we can determine, None if undetermined.

    Order of resolution:
    1. ``cal._read_only`` (test stubs and explicit overrides win).
    2. caldav's ``CurrentUserPrivilegeSet`` property — present in caldav 1.x,
       dropped in 3.x. We probe rather than import unconditionally.
    3. URL heuristic — iCloud subscribed calendars carry ``subscriptions`` in
       their href.
    4. ``None`` (unknown — caller defaults to writable; iCloud rejects with
       an error message that we surface verbatim).
    """
    explicit = getattr(cal, "_read_only", None)
    if explicit is not None:
        return not bool(explicit)

    try:
        from caldav.elements import dav  # type: ignore[attr-defined]

        priv_cls = getattr(dav, "CurrentUserPrivilegeSet", None)
        if priv_cls is not None:
            result = cal.get_properties([priv_cls()])
            if result:
                rendered = str(result)
                if "write" in rendered:
                    return True
                if "read" in rendered:
                    return False
    except Exception:
        pass

    href = _href_of(cal).lower()
    if "subscriptions" in href or "holidays" in href:
        return False
    return None


# --------------------------- the client ---------------------------


class CalendarClient:
    def __init__(
        self,
        *,
        username: str,
        password: str,
        url: str = ICLOUD_URL,
        allowed_calendars: list[str] | None = None,
        readonly_calendars: list[str] | None = None,
        token_secret: str | None = None,
        user_email: str | None = None,
        tz_name: str = "America/Chicago",
        timeout_s: int = 30,
        principal: Any | None = None,
    ):
        if not username or not password:
            raise ValueError("username and password are required")
        self._url = url
        self._username = username
        self._password = password
        self._allowed = set(allowed_calendars or []) or None
        # ``readonly_calendars`` forces selected calendars to ``read_only=True``
        # regardless of what server-side privilege detection returns. Required
        # because caldav 3.x dropped the privilege-set element we previously
        # used; this lets the operator declare "agents must not mutate these"
        # for shared / family / informational calendars.
        self._forced_readonly = set(readonly_calendars or []) or None
        self._token_secret = token_secret
        self._user_email = (user_email or username).strip()
        self._tz_name = tz_name
        self._timeout_s = timeout_s

        self._lock = threading.RLock()
        self._client: caldav.DAVClient | None = None
        # ``principal`` is a test-injection hook: passing one bypasses
        # ``caldav.DAVClient`` discovery so unit tests can drive the client
        # against an in-memory stub.
        self._principal: Any | None = principal
        self._calendars_by_href: dict[str, Any] = {}
        self._calendar_info_by_href: dict[str, CalendarInfo] = {}

    # ------------- discovery -------------

    def _ensure_client(self) -> caldav.DAVClient:
        if self._client is None:
            self._client = caldav.DAVClient(
                url=self._url,
                username=self._username,
                password=self._password,
                timeout=self._timeout_s,
            )
        return self._client

    def _ensure_principal(self) -> Any:
        if self._principal is not None:
            return self._principal
        try:
            self._principal = self._ensure_client().principal()
        except AuthorizationError as e:
            raise AuthError(
                "iCloud authentication failed — check ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD"
            ) from e
        return self._principal

    def _ensure_calendars(self) -> dict[str, Any]:
        if self._calendars_by_href:
            return self._calendars_by_href
        principal = self._ensure_principal()
        try:
            calendars = principal.calendars()
        except AuthorizationError as e:
            raise AuthError(str(e)) from e
        for cal in calendars:
            try:
                name = (cal.name or "").strip() or "Unnamed"
            except Exception:
                name = "Unnamed"
            if self._allowed is not None and name not in self._allowed:
                continue
            href = _href_of(cal)
            self._calendars_by_href[href] = cal
            forced_ro = bool(self._forced_readonly and name in self._forced_readonly)
            writable = _privilege_writable(cal)
            read_only = forced_ro or (writable is False)
            # Mark forced-read-only calendars as subscribed so they are
            # excluded from ``find_free_time`` defaults — useful for shared
            # family calendars whose events shouldn't auto-block the user.
            is_subscribed = read_only
            color = None
            try:
                # iCloud surfaces ``calendar-color`` (Apple) on calendars
                from caldav.elements import ical

                props = cal.get_properties([ical.CalendarColor()])
                if props:
                    color = next(iter(props.values()), None)
                    if isinstance(color, str):
                        color = color.strip() or None
            except Exception:
                color = None
            self._calendar_info_by_href[href] = CalendarInfo(
                calendar_id=encode_calendar_id(href, secret=self._token_secret),
                name=name,
                color=color,
                read_only=read_only,
                is_subscribed=is_subscribed,
            )
        return self._calendars_by_href

    def _resolve_calendar(
        self,
        *,
        calendar_id: str | None = None,
        calendar_name: str | None = None,
    ) -> tuple[Any, CalendarInfo]:
        cals = self._ensure_calendars()
        if calendar_id:
            href = decode_calendar_id(calendar_id, secret=self._token_secret)
            cal = cals.get(href)
            if cal is None:
                raise NotFound(f"calendar not found for given calendar_id")
            return cal, self._calendar_info_by_href[href]
        if calendar_name:
            matches = [
                (href, info)
                for href, info in self._calendar_info_by_href.items()
                if info.name == calendar_name
            ]
            if not matches:
                names = sorted(info.name for info in self._calendar_info_by_href.values())
                raise NotFound(
                    f"calendar {calendar_name!r} not found; available: {names}"
                )
            if len(matches) > 1:
                raise AmbiguousCalendarError(
                    f"calendar name {calendar_name!r} matches {len(matches)} calendars; use calendar_id"
                )
            href, info = matches[0]
            return cals[href], info
        raise ValueError("calendar_id or calendar_name is required")

    # ------------- public API -------------

    def list_calendars(self) -> list[CalendarInfo]:
        with self._lock:
            self._ensure_calendars()
            return list(self._calendar_info_by_href.values())

    def list_events(
        self,
        *,
        calendar_id: str | None = None,
        calendar_name: str | None = None,
        start: str | None = None,
        end: str | None = None,
        search: str | None = None,
        force_unbounded: bool = False,
    ) -> list[EventDict]:
        with self._lock:
            range_start, range_end = self._compute_range(start, end, force_unbounded)
            cals_to_search: list[tuple[Any, CalendarInfo]]
            if calendar_id or calendar_name:
                cal, info = self._resolve_calendar(
                    calendar_id=calendar_id, calendar_name=calendar_name
                )
                cals_to_search = [(cal, info)]
            else:
                self._ensure_calendars()
                cals_to_search = [
                    (self._calendars_by_href[h], self._calendar_info_by_href[h])
                    for h in self._calendars_by_href
                ]
            needle = (search or "").strip().lower()
            out: list[EventDict] = []
            for cal, info in cals_to_search:
                try:
                    raw_events = cal.search(
                        start=range_start,
                        end=range_end,
                        event=True,
                        expand=True,
                    )
                except NotFoundError:
                    continue
                for ev in raw_events:
                    rec = self._event_to_dict(ev, info)
                    if rec is None:
                        continue
                    if needle:
                        haystack = " ".join(
                            (rec.title, rec.location or "", rec.description or "")
                        ).lower()
                        if needle not in haystack:
                            continue
                    out.append(rec)
            out.sort(key=lambda r: (r.start, r.title))
            return out

    def get_event(self, event_id: str) -> EventDict:
        with self._lock:
            payload = decode_event_id(event_id, secret=self._token_secret)
            cal, info = self._find_calendar_by_href(payload["calendar_href"])
            ev = self._fetch_event_for_uid(cal, payload["uid"])
            if ev is None:
                raise NotFound("event not found (may have been deleted)")
            # iCloud's calendar-query REPORT often omits per-event etags. A
            # direct GET via load() pulls the response's Etag header into
            # ev.props, which is what we need for reliable optimistic
            # concurrency on the next update/delete.
            try:
                ev.load()
            except NotFoundError:
                raise NotFound("event not found (may have been deleted)")
            except Exception:
                log.exception(
                    "get_event: ev.load() failed for uid=%s", payload["uid"]
                )
            rec = self._event_to_dict(ev, info, force_recurrence_id=payload.get("recurrence_id"))
            if rec is None:
                raise NotFound("event not found (may have been deleted)")
            return rec

    def find_free_time(
        self,
        *,
        start: str,
        end: str,
        duration_minutes: int,
        calendar_filter: list[str] | None = None,  # ids or names
        include_subscribed: bool = False,
        include_declined: bool = False,
    ) -> list[Slot]:
        with self._lock:
            range_start = _parse_iso(start, self._tz_name)
            range_end = _parse_iso(end, self._tz_name)
            if range_end <= range_start:
                raise ValueError("end must be after start")
            duration = timedelta(minutes=int(duration_minutes))
            if duration <= timedelta(0):
                raise ValueError("duration_minutes must be positive")

            self._ensure_calendars()
            allowed_hrefs = self._select_calendars(
                calendar_filter=calendar_filter,
                include_subscribed=include_subscribed,
            )
            busy: list[tuple[datetime, datetime]] = []
            for href in allowed_hrefs:
                cal = self._calendars_by_href[href]
                info = self._calendar_info_by_href[href]
                try:
                    raw_events = cal.search(
                        start=range_start,
                        end=range_end,
                        event=True,
                        expand=True,
                    )
                except NotFoundError:
                    continue
                for ev in raw_events:
                    rec = self._event_to_dict(ev, info)
                    if rec is None:
                        continue
                    if rec.transparent:
                        continue
                    if not include_declined and (rec.participation_status or "").upper() == "DECLINED":
                        continue
                    s, e = self._event_bounds(rec, self._tz_name)
                    s = max(s, range_start)
                    e = min(e, range_end)
                    if e > s:
                        busy.append((s, e))

            return self._gaps(range_start, range_end, busy, duration)

    def create_event(
        self,
        *,
        calendar_id: str | None = None,
        calendar_name: str | None = None,
        title: str,
        start: str,
        end: str,
        description: str | None = None,
        location: str | None = None,
    ) -> EventDict:
        if not title.strip():
            raise ValueError("title is required")
        with self._lock:
            cal, info = self._resolve_calendar(
                calendar_id=calendar_id, calendar_name=calendar_name
            )
            if info.read_only:
                raise ReadOnlyCalendarError(
                    f"calendar {info.name!r} is read-only (likely subscribed)"
                )
            dtstart = _parse_iso(start, self._tz_name)
            dtend = _parse_iso(end, self._tz_name)
            if dtend <= dtstart:
                raise ValueError("end must be after start")

            ical = self._build_ical(
                uid=str(uuid.uuid4()),
                title=title,
                dtstart=dtstart,
                dtend=dtend,
                description=description,
                location=location,
            )
            try:
                ev = cal.save_event(ical)
            except AuthorizationError as e:
                raise AuthError(str(e)) from e
            rec = self._event_to_dict(ev, info)
            if rec is None:
                raise CalendarClientError("created event could not be re-read")
            return rec

    def update_event(
        self,
        *,
        event_id: str,
        expected_etag: str | None = None,
        scope: str = "auto",
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
        description: str | None = None,
        location: str | None = None,
    ) -> EventDict:
        with self._lock:
            payload = decode_event_id(event_id, secret=self._token_secret)
            cal, info = self._find_calendar_by_href(payload["calendar_href"])
            if info.read_only:
                raise ReadOnlyCalendarError(
                    f"calendar {info.name!r} is read-only (likely subscribed)"
                )

            ev = self._fetch_event_for_uid(cal, payload["uid"])
            if ev is None:
                raise NotFound("event not found (may have been deleted)")

            self._apply_expected_etag(ev, expected_etag)

            chosen_scope = self._resolve_scope(scope, payload.get("recurrence_id"))
            if chosen_scope == "future":
                raise NotImplementedError(
                    "scope='future' is not yet supported; use 'instance' or 'series'"
                )

            vcal = ev.vobject_instance
            master = _master_vevent(vcal)
            if master is None:
                raise CalendarClientError("event has no VEVENT body")

            if chosen_scope == "instance":
                rid = payload.get("recurrence_id")
                if not rid:
                    raise ValueError(
                        "scope='instance' requires an event_id from an expanded occurrence"
                    )
                self._exdate_master(master, rid)
                override = self._build_override(
                    master=master,
                    recurrence_id=rid,
                    title=title,
                    start=start,
                    end=end,
                    description=description,
                    location=location,
                )
                vcal.add(override)
            else:  # series
                if title is not None:
                    self._set_text(master, "summary", title)
                if start is not None:
                    self._set_dt(master, "dtstart", _parse_iso(start, self._tz_name))
                if end is not None:
                    self._set_dt(master, "dtend", _parse_iso(end, self._tz_name))
                if description is not None:
                    self._set_text(master, "description", description)
                if location is not None:
                    self._set_text(master, "location", location)
                # Don't write LAST-MODIFIED. iCloud sets its own server-side
                # timestamp. Writing it via _set_text seeds a string value;
                # on the next read, vobject parses that back to a datetime
                # with isNative=True, and a subsequent _set_text overwrite
                # leaves a string in a native-flagged slot, crashing
                # vcal.serialize() on the next update with
                # ``'str' object has no attribute 'tzinfo'``.

            try:
                ev.data = vcal.serialize()
                ev.save()
            except AuthorizationError as e:
                raise AuthError(str(e)) from e
            except ETagMismatchError as e:
                raise ConflictError(
                    "event was modified concurrently; refetch and retry"
                ) from e

            rec = self._event_to_dict(ev, info, force_recurrence_id=payload.get("recurrence_id"))
            if rec is None:
                raise CalendarClientError("updated event could not be re-read")
            return rec

    def delete_event(
        self,
        *,
        event_id: str,
        expected_etag: str | None = None,
        scope: str = "auto",
    ) -> str:
        with self._lock:
            payload = decode_event_id(event_id, secret=self._token_secret)
            cal, info = self._find_calendar_by_href(payload["calendar_href"])
            if info.read_only:
                raise ReadOnlyCalendarError(
                    f"calendar {info.name!r} is read-only (likely subscribed)"
                )

            ev = self._fetch_event_for_uid(cal, payload["uid"])
            if ev is None:
                raise NotFound("event not found (may have been deleted)")

            self._apply_expected_etag(ev, expected_etag)

            chosen_scope = self._resolve_scope(scope, payload.get("recurrence_id"))
            if chosen_scope == "future":
                raise NotImplementedError(
                    "scope='future' is not yet supported; use 'instance' or 'series'"
                )

            if chosen_scope == "instance":
                rid = payload.get("recurrence_id")
                if not rid:
                    raise ValueError(
                        "scope='instance' requires an event_id from an expanded occurrence"
                    )
                vcal = ev.vobject_instance
                master = _master_vevent(vcal)
                if master is None:
                    raise CalendarClientError("event has no VEVENT body")
                self._exdate_master(master, rid)
                # Remove any existing override for this RECURRENCE-ID.
                self._strip_override(vcal, rid)
                ev.data = vcal.serialize()
                try:
                    ev.save()
                except AuthorizationError as e:
                    raise AuthError(str(e)) from e
                except ETagMismatchError as e:
                    raise ConflictError(
                        "event was modified concurrently; refetch and retry"
                    ) from e
                return f"deleted occurrence {rid} of event {payload['uid']}"
            else:  # series
                try:
                    ev.delete()
                except AuthorizationError as e:
                    raise AuthError(str(e)) from e
                except ETagMismatchError as e:
                    raise ConflictError(
                        "event was modified concurrently; refetch and retry"
                    ) from e
                return f"deleted event {payload['uid']}"

    # ------------- internals: events -------------

    def _compute_range(
        self, start: str | None, end: str | None, force_unbounded: bool
    ) -> tuple[datetime, datetime]:
        now = datetime.now(timezone.utc)
        if start is None and end is None:
            return now, now + timedelta(days=DEFAULT_RANGE_DAYS)
        s = _parse_iso(start, self._tz_name) if start else now
        e = (
            _parse_iso(end, self._tz_name)
            if end
            else s + timedelta(days=DEFAULT_RANGE_DAYS)
        )
        if e <= s:
            raise ValueError("end must be after start")
        if not force_unbounded and (e - s) > timedelta(days=MAX_RANGE_DAYS):
            raise RangeTooLargeError(
                f"requested range exceeds {MAX_RANGE_DAYS}-day cap; "
                f"pass force_unbounded=true to override"
            )
        return s, e

    def _select_calendars(
        self,
        *,
        calendar_filter: list[str] | None,
        include_subscribed: bool,
    ) -> list[str]:
        """Return list of hrefs after filtering."""
        all_hrefs = list(self._calendars_by_href.keys())
        if calendar_filter:
            wanted = set(calendar_filter)
            selected: list[str] = []
            for href in all_hrefs:
                info = self._calendar_info_by_href[href]
                if info.calendar_id in wanted or info.name in wanted:
                    selected.append(href)
            return selected
        if include_subscribed:
            return all_hrefs
        return [
            h
            for h in all_hrefs
            if not self._calendar_info_by_href[h].is_subscribed
        ]

    def _find_calendar_by_href(self, href: str) -> tuple[Any, CalendarInfo]:
        cals = self._ensure_calendars()
        normalized = _normalize_href(href)
        cal = cals.get(normalized)
        if cal is None:
            raise NotFound("calendar not found for this event_id")
        return cal, self._calendar_info_by_href[normalized]

    def _fetch_event_for_uid(self, cal: Any, uid: str) -> Any | None:
        """Fetch an event by UID using a time-range REPORT.

        We deliberately don't call ``cal.event_by_uid(uid)`` — caldav 3.x
        implements that as a calendar-query REPORT with a UID-only filter
        and no time-range, which iCloud rejects with
        ``412 Precondition Failed``. The workaround is to issue a
        time-bounded REPORT (which iCloud accepts) and filter by UID in
        memory. The window covers ``[-30d, +365d]`` from now, which is
        wide enough for any event a user is plausibly going to operate
        on shortly after creating or seeing it.
        """
        now = datetime.now(timezone.utc)
        try:
            events = cal.search(
                start=now - timedelta(days=30),
                end=now + timedelta(days=365),
                event=True,
                expand=False,
            )
        except NotFoundError:
            return None
        except Exception:
            log.exception("UID search REPORT failed for cal=%s uid=%s", _href_of(cal), uid)
            return None
        for ev in events:
            try:
                vcal = ev.vobject_instance
                if not vcal or not getattr(vcal, "vevent_list", None):
                    continue
                ev_uid = _safe_text(getattr(vcal.vevent_list[0], "uid", None))
                if ev_uid == uid:
                    return ev
            except Exception:
                continue
        return None

    def _apply_expected_etag(self, ev: Any, expected: str | None) -> None:
        """Stage ``expected_etag`` as the ``If-Match`` for the next write.

        caldav 3.x's ``CalendarObjectResource`` reads ``self.etag`` from
        ``self.props[{DAV:}getetag]`` and auto-attaches ``If-Match:`` to the
        next ``save()`` / ``delete()``. By writing the caller's
        ``expected_etag`` here, we get server-enforced optimistic
        concurrency: a concurrent edit causes iCloud to return 412 and
        caldav raises :class:`ETagMismatchError`, which the public method
        re-raises as :class:`ConflictError`.

        We deliberately do NOT pre-validate against the locally cached etag
        — REPORT responses from iCloud often omit per-event etags, so a
        client-side check would silently pass. Pushing the precondition to
        the server is the only reliable path.
        """
        if not expected:
            return
        if not hasattr(ev, "props") or ev.props is None:
            try:
                ev.props = {}
            except Exception:
                return
        try:
            ev.props[GETETAG_TAG] = expected
        except Exception:
            log.warning("could not stage If-Match etag on event")

    def _safe_etag(self, ev: Any) -> str | None:
        etag = getattr(ev, "etag", None)
        if etag:
            return str(etag)
        return None

    def _event_to_dict(
        self,
        ev: Any,
        info: CalendarInfo,
        *,
        force_recurrence_id: str | None = None,
    ) -> EventDict | None:
        try:
            vcal = ev.vobject_instance
        except Exception:
            log.warning("failed to parse event %s", _href_of(ev))
            return None
        # When expand=True iCalendar ships a single VEVENT per occurrence;
        # otherwise pick the master.
        events = list(getattr(vcal, "vevent_list", []) or [])
        if not events:
            return None
        vevent = events[0]
        for ve in events:
            if not hasattr(ve, "recurrence_id"):
                vevent = ve
                break

        title = _safe_text(getattr(vevent, "summary", None)) or "(no title)"
        dtstart = getattr(getattr(vevent, "dtstart", None), "value", None)
        dtend = getattr(getattr(vevent, "dtend", None), "value", None)
        if dtstart is None:
            return None
        if dtend is None:
            dtend = dtstart  # zero-length event, rare
        all_day = _is_all_day(dtstart)
        rid_value: str | None = force_recurrence_id
        if rid_value is None:
            rid_obj = getattr(vevent, "recurrence_id", None)
            if rid_obj is not None:
                rid_value = _serialize_dt(rid_obj.value, self._tz_name)
        recurring = hasattr(vevent, "rrule") or rid_value is not None
        transp = _safe_text(getattr(vevent, "transp", None)).upper() == "TRANSPARENT"
        partstat = _attendee_partstat(vevent, self._user_email)
        last_modified = None
        lm_obj = getattr(vevent, "last_modified", None)
        if lm_obj is not None:
            last_modified = _serialize_dt(lm_obj.value, "UTC")
        etag = self._safe_etag(ev)
        uid = _safe_text(getattr(vevent, "uid", None))
        if not uid:
            return None
        location = _safe_text(getattr(vevent, "location", None)) or None
        description = _safe_text(getattr(vevent, "description", None)) or None

        # Use the calendar's discovery-time href (round-tripped through
        # info.calendar_id) rather than the live cal.url — caldav can mutate
        # the latter to include ``:443`` after a request, breaking lookups.
        canonical_href = decode_calendar_id(
            info.calendar_id, secret=self._token_secret
        )
        return EventDict(
            event_id=encode_event_id(
                calendar_href=canonical_href,
                uid=uid,
                recurrence_id=rid_value,
                etag=etag,
                secret=self._token_secret,
            ),
            calendar_id=info.calendar_id,
            calendar_name=info.name,
            title=title,
            start=_serialize_dt(dtstart, self._tz_name),
            end=_serialize_dt(dtend, self._tz_name),
            all_day=all_day,
            location=location,
            description=description,
            etag=etag,
            last_modified=last_modified,
            transparent=transp,
            participation_status=partstat,
            recurrence_id=rid_value,
            recurring=recurring,
        )

    def _event_bounds(
        self, rec: EventDict, tz_name: str
    ) -> tuple[datetime, datetime]:
        if rec.all_day:
            d_s = date.fromisoformat(rec.start)
            d_e = date.fromisoformat(rec.end) if rec.end else d_s + timedelta(days=1)
            tz = ZoneInfo(tz_name)
            return datetime.combine(d_s, time.min, tz), datetime.combine(d_e, time.min, tz)
        s = _aware(datetime.fromisoformat(rec.start), tz_name)
        e = _aware(datetime.fromisoformat(rec.end), tz_name)
        return s, e

    def _gaps(
        self,
        start: datetime,
        end: datetime,
        busy: list[tuple[datetime, datetime]],
        duration: timedelta,
    ) -> list[Slot]:
        if not busy:
            if (end - start) >= duration:
                return [Slot(start=start.isoformat(), end=end.isoformat())]
            return []
        merged: list[tuple[datetime, datetime]] = []
        for s, e in sorted(busy):
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        out: list[Slot] = []
        cursor = start
        for s, e in merged:
            if s > cursor and (s - cursor) >= duration:
                out.append(Slot(start=cursor.isoformat(), end=s.isoformat()))
            cursor = max(cursor, e)
        if end > cursor and (end - cursor) >= duration:
            out.append(Slot(start=cursor.isoformat(), end=end.isoformat()))
        return out

    # ------------- internals: writes -------------

    def _resolve_scope(self, scope: str, recurrence_id: str | None) -> str:
        s = (scope or "auto").lower()
        if s == "auto":
            return "instance" if recurrence_id else "series"
        if s in ("instance", "series", "future"):
            return s
        raise ValueError(f"unknown scope {scope!r}; valid: auto / instance / series / future")

    def _build_ical(
        self,
        *,
        uid: str,
        title: str,
        dtstart: datetime,
        dtend: datetime,
        description: str | None,
        location: str | None,
    ) -> str:
        cal = vobject.iCalendar()
        cal.add("prodid").value = "-//domo//caldav//EN"
        ev = cal.add("vevent")
        ev.add("uid").value = uid
        ev.add("summary").value = title
        ev.add("dtstart").value = _vobject_dt(dtstart)
        ev.add("dtend").value = _vobject_dt(dtend)
        ev.add("dtstamp").value = datetime.now(pytz.UTC)
        if description:
            ev.add("description").value = description
        if location:
            ev.add("location").value = location
        return cal.serialize()

    def _exdate_master(self, master: Any, recurrence_id_iso: str) -> None:
        """Append an EXDATE entry to the master event."""
        rid_dt = _vobject_dt(_parse_iso(recurrence_id_iso, self._tz_name))
        ex = master.add("exdate")
        ex.value = [rid_dt]

    def _build_override(
        self,
        *,
        master: Any,
        recurrence_id: str,
        title: str | None,
        start: str | None,
        end: str | None,
        description: str | None,
        location: str | None,
    ) -> Any:
        rid_dt = _vobject_dt(_parse_iso(recurrence_id, self._tz_name))
        ov = vobject.newFromBehavior("vevent")
        ov.add("uid").value = _safe_text(getattr(master, "uid", None))
        ov.add("recurrence-id").value = rid_dt
        ov.add("summary").value = title or _safe_text(getattr(master, "summary", None))
        ov.add("dtstart").value = (
            _vobject_dt(_parse_iso(start, self._tz_name)) if start else rid_dt
        )
        if end:
            ov.add("dtend").value = _vobject_dt(_parse_iso(end, self._tz_name))
        else:
            master_dtend = getattr(getattr(master, "dtend", None), "value", None)
            master_dtstart = getattr(getattr(master, "dtstart", None), "value", None)
            if isinstance(master_dtstart, datetime) and isinstance(master_dtend, datetime):
                ov.add("dtend").value = rid_dt + (master_dtend - master_dtstart)
            elif master_dtend is not None:
                ov.add("dtend").value = master_dtend
        if description is not None:
            ov.add("description").value = description
        elif hasattr(master, "description"):
            ov.add("description").value = _safe_text(master.description)
        if location is not None:
            ov.add("location").value = location
        elif hasattr(master, "location"):
            ov.add("location").value = _safe_text(master.location)
        ov.add("dtstamp").value = datetime.now(pytz.UTC)
        return ov

    def _strip_override(self, vcal: Any, recurrence_id_iso: str) -> None:
        rid_dt = _parse_iso(recurrence_id_iso, self._tz_name)
        keep = []
        for ve in list(getattr(vcal, "vevent_list", []) or []):
            rid = getattr(ve, "recurrence_id", None)
            if rid is None:
                keep.append(ve)
                continue
            existing = rid.value
            if isinstance(existing, datetime) and existing == rid_dt:
                continue
            keep.append(ve)
        # vobject does not have a clean removeChild for components; rebuild.
        for ve in list(getattr(vcal, "vevent_list", []) or []):
            vcal.remove(ve)
        for ve in keep:
            vcal.add(ve)

    def _set_text(self, vevent: Any, name: str, value: str) -> None:
        if hasattr(vevent, name):
            getattr(vevent, name).value = value
        else:
            vevent.add(name).value = value

    def _set_dt(self, vevent: Any, name: str, value: datetime) -> None:
        coerced = _vobject_dt(value)
        if hasattr(vevent, name):
            getattr(vevent, name).value = coerced
        else:
            vevent.add(name).value = coerced


# --------------------------- helpers exported for tools layer ---------------------------


def info_to_dict(info: CalendarInfo) -> dict[str, Any]:
    return asdict(info)


def event_to_dict(rec: EventDict) -> dict[str, Any]:
    return asdict(rec)


def slot_to_dict(slot: Slot) -> dict[str, Any]:
    return asdict(slot)


def list_to_dicts(items: Iterable[Any]) -> list[dict[str, Any]]:
    return [asdict(x) for x in items]
