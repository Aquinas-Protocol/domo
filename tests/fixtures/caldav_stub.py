"""In-memory stub of the slice of caldav we use.

Just enough surface for ``src.calendar_client.CalendarClient`` to work without
talking to iCloud:

- ``StubPrincipal`` exposing ``calendars()``.
- ``StubCalendar`` exposing ``name``, ``url``, ``search``, ``event_by_uid``,
  ``save_event``, ``get_properties``.
- ``StubEvent`` exposing ``url``, ``etag``, ``data``, ``vobject_instance``,
  ``save``, ``delete``.

Helper :func:`make_vcalendar` builds a real ``vobject.iCalendar`` so the
client's parsing code is exercised end-to-end.
"""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytz
import vobject
from caldav.elements import dav as caldav_dav
from caldav.lib.error import ETagMismatchError, NotFoundError

GETETAG_TAG = caldav_dav.GetEtag.tag


def _coerce_for_vobject(value: date | datetime) -> date | datetime:
    """vobject only knows pytz/dateutil tzinfos; coerce ZoneInfo to UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return pytz.UTC.localize(value)
        return value.astimezone(pytz.UTC)
    return value


def make_vcalendar(
    *,
    uid: str | None = None,
    title: str = "Test Event",
    dtstart: datetime | date,
    dtend: datetime | date,
    description: str | None = None,
    location: str | None = None,
    transparent: bool = False,
    user_email: str | None = None,
    partstat: str | None = None,
) -> Any:
    """Build a vobject iCalendar with one VEVENT for tests."""
    cal = vobject.iCalendar()
    cal.add("prodid").value = "-//domo-tests//"
    ev = cal.add("vevent")
    ev.add("uid").value = uid or str(uuid.uuid4())
    ev.add("summary").value = title
    ev.add("dtstart").value = _coerce_for_vobject(dtstart)
    ev.add("dtend").value = _coerce_for_vobject(dtend)
    ev.add("dtstamp").value = datetime.now(pytz.UTC)
    if description:
        ev.add("description").value = description
    if location:
        ev.add("location").value = location
    if transparent:
        ev.add("transp").value = "TRANSPARENT"
    if user_email and partstat:
        att = ev.add("attendee")
        att.value = f"mailto:{user_email}"
        att.params["PARTSTAT"] = [partstat]
    return cal


class StubEvent:
    """In-memory mimic of caldav 3.x ``CalendarObjectResource``.

    ``etag`` is exposed as a property reading ``props[GETETAG_TAG]`` to match
    caldav's actual structure — this is what ``CalendarClient`` writes
    through when applying ``expected_etag`` as ``If-Match``. ``save()`` /
    ``delete()`` simulate iCloud's 412 by raising
    :class:`ETagMismatchError` when ``If-Match`` is set and disagrees with
    the server-side etag.
    """

    def __init__(self, vcal: Any, url: str, etag: str = "etag-1"):
        self._vcal = vcal
        self.url = url
        # Server-side ("real") etag — what caldav 3.x's auto If-Match would
        # be compared against on the iCloud side.
        self._server_etag = etag
        self.props: dict[str, Any] = {GETETAG_TAG: etag}
        self.data = vcal.serialize()
        self._deleted = False
        self._loaded_count = 0

    @property
    def etag(self) -> str | None:
        # Match caldav 3.x: ev.etag reads from props[{DAV:}getetag].
        return self.props.get(GETETAG_TAG)

    @etag.setter
    def etag(self, value: str | None) -> None:
        self.props[GETETAG_TAG] = value

    @property
    def vobject_instance(self):
        return vobject.readOne(self.data)

    def load(self) -> "StubEvent":
        # Mimic caldav.load(): direct GET refreshes etag from response Etag
        # header. Test code observes ``_loaded_count`` to assert load was called.
        self._loaded_count += 1
        self.props[GETETAG_TAG] = self._server_etag
        return self

    def _check_if_match(self) -> None:
        """If caller staged an If-Match etag in props, verify against server."""
        applied = self.props.get(GETETAG_TAG)
        if applied is not None and applied != self._server_etag:
            raise ETagMismatchError(
                f"412 Precondition Failed: If-Match {applied!r} != current {self._server_etag!r}"
            )

    def save(self) -> None:
        self._check_if_match()
        self._vcal = vobject.readOne(self.data)
        self._server_etag = f"etag-{int(time.time() * 1000)}"
        self.props[GETETAG_TAG] = self._server_etag

    def delete(self) -> None:
        self._check_if_match()
        self._deleted = True


class StubCalendar:
    def __init__(
        self,
        *,
        name: str,
        url: str,
        read_only: bool = False,
        color: str | None = None,
    ):
        self.name = name
        self.url = url
        self._read_only = read_only
        self._color = color
        self._events: list[StubEvent] = []

    def add_event_from_vcal(self, vcal: Any, *, etag: str = "etag-1") -> StubEvent:
        uid = vcal.vevent.uid.value
        ev = StubEvent(vcal, url=f"{self.url}/{uid}.ics", etag=etag)
        self._events.append(ev)
        return ev

    def search(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        event: bool = True,
        expand: bool = True,
    ) -> list[StubEvent]:
        out: list[StubEvent] = []
        for ev in self._events:
            if ev._deleted:
                continue
            vcal = ev.vobject_instance
            if not vcal.vevent_list:
                continue
            ev_start = vcal.vevent_list[0].dtstart.value
            ev_end_obj = getattr(vcal.vevent_list[0], "dtend", None)
            ev_end = ev_end_obj.value if ev_end_obj else ev_start
            if start is not None and ev_end is not None:
                cmp_end = _to_datetime(ev_end)
                if cmp_end <= start:
                    continue
            if end is not None and ev_start is not None:
                cmp_start = _to_datetime(ev_start)
                if cmp_start >= end:
                    continue
            out.append(ev)
        return out

    def event_by_uid(self, uid: str) -> StubEvent:
        for ev in self._events:
            if ev._deleted:
                continue
            vcal = ev.vobject_instance
            if vcal.vevent_list and vcal.vevent_list[0].uid.value == uid:
                return ev
        raise NotFoundError(f"event {uid} not found")

    def save_event(self, ical_str: str) -> StubEvent:
        new_vcal = vobject.readOne(ical_str)
        return self.add_event_from_vcal(new_vcal)

    def get_properties(self, props: list[Any]) -> dict[str, Any]:
        # CalendarClient renders ``str(result)`` and checks for the substrings
        # 'write' / 'read'. Returning a dict with one of those tokens is enough.
        if self._read_only:
            return {"_priv": "<read>"}
        return {"_priv": "<write-content>"}


class StubPrincipal:
    def __init__(self, calendars: list[StubCalendar]):
        self._calendars = calendars

    def calendars(self) -> list[StubCalendar]:
        return list(self._calendars)


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)
