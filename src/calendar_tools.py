"""MCP wrappers around :class:`src.calendar_client.CalendarClient`.

Two distinct MCP servers are exposed:

- ``calendar_read`` — list_calendars, list_events, get_event, find_free_time.
  Registered for every agent so Maine, Hermes, and Intel can all read.
- ``calendar_write`` — create_event, update_event, delete_event.
  Registered only for ``main`` and ``comms`` (Maine + Hermes).

The two servers share one underlying :class:`CalendarClient` and one
``_ListCalendarsCache`` (60s TTL on the calendar discovery; cleared by every
write so renamed/added calendars surface promptly after Maine creates one).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from src.calendar_client import (
    AmbiguousCalendarError,
    AuthError,
    CalendarClient,
    CalendarClientError,
    ConflictError,
    NotFound,
    RangeTooLargeError,
    ReadOnlyCalendarError,
    list_to_dicts,
)

log = logging.getLogger(__name__)


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _none_if_empty(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _csv_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    return parts or None


_PRINCIPAL_PATH_RE = re.compile(r"https?://[^\s\"']+")


def _sanitize(text: str) -> str:
    """Strip iCloud principal hrefs from error messages before agents see them."""
    return _PRINCIPAL_PATH_RE.sub("<url>", text)


def _format_error(exc: Exception) -> str:
    if isinstance(exc, AuthError):
        return "iCloud authentication failed — check ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD"
    if isinstance(exc, AmbiguousCalendarError):
        return _sanitize(str(exc))
    if isinstance(exc, ReadOnlyCalendarError):
        return _sanitize(str(exc))
    if isinstance(exc, NotFound):
        return _sanitize(str(exc))
    if isinstance(exc, ConflictError):
        return "event was modified concurrently; refetch and retry"
    if isinstance(exc, RangeTooLargeError):
        return _sanitize(str(exc))
    if isinstance(exc, NotImplementedError):
        return _sanitize(str(exc))
    if isinstance(exc, ValueError):
        return _sanitize(str(exc))
    if isinstance(exc, CalendarClientError):
        return f"caldav error: {_sanitize(str(exc))}"
    return f"caldav error: {_sanitize(f'{type(exc).__name__}: {exc}')}"


def _dump(payload: Any) -> str:
    return json.dumps(payload, default=str, indent=2, sort_keys=False)


# --------------------------- list_calendars cache ---------------------------


class _ListCalendarsCache:
    def __init__(self, ttl_s: float = 60.0):
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._cached_at: float | None = None
        self._value: list[dict[str, Any]] | None = None

    def get(self) -> list[dict[str, Any]] | None:
        with self._lock:
            if self._value is None or self._cached_at is None:
                return None
            if (time.monotonic() - self._cached_at) > self._ttl_s:
                return None
            return self._value

    def set(self, value: list[dict[str, Any]]) -> None:
        with self._lock:
            self._value = list(value)
            self._cached_at = time.monotonic()

    def clear(self) -> None:
        with self._lock:
            self._value = None
            self._cached_at = None


# --------------------------- read context ---------------------------


class CalendarReadContext:
    def __init__(self, client: CalendarClient, cache: _ListCalendarsCache | None = None):
        self.client = client
        self.cache = cache or _ListCalendarsCache()

    async def list_calendars(self) -> dict[str, Any]:
        try:
            cached = self.cache.get()
            if cached is not None:
                return _ok(_dump(cached))
            cals = await asyncio.to_thread(self.client.list_calendars)
            data = list_to_dicts(cals)
            self.cache.set(data)
            return _ok(_dump(data))
        except Exception as e:
            log.exception("list_calendars failed")
            return _err(_format_error(e))

    async def list_events(
        self,
        *,
        calendar_id: str | None = None,
        calendar_name: str | None = None,
        start: str | None = None,
        end: str | None = None,
        search: str | None = None,
        force_unbounded: bool = False,
    ) -> dict[str, Any]:
        try:
            events = await asyncio.to_thread(
                self.client.list_events,
                calendar_id=_none_if_empty(calendar_id),
                calendar_name=_none_if_empty(calendar_name),
                start=_none_if_empty(start),
                end=_none_if_empty(end),
                search=_none_if_empty(search),
                force_unbounded=bool(force_unbounded),
            )
            return _ok(_dump(list_to_dicts(events)))
        except Exception as e:
            log.exception("list_events failed")
            return _err(_format_error(e))

    async def get_event(self, *, event_id: str) -> dict[str, Any]:
        try:
            event = await asyncio.to_thread(self.client.get_event, event_id)
            return _ok(_dump(list_to_dicts([event])[0]))
        except Exception as e:
            log.exception("get_event failed")
            return _err(_format_error(e))

    async def find_free_time(
        self,
        *,
        start: str,
        end: str,
        duration_minutes: int,
        calendars: str | None = None,
        include_subscribed: bool = False,
        include_declined: bool = False,
    ) -> dict[str, Any]:
        try:
            slots = await asyncio.to_thread(
                self.client.find_free_time,
                start=start,
                end=end,
                duration_minutes=int(duration_minutes),
                calendar_filter=_csv_list(calendars),
                include_subscribed=bool(include_subscribed),
                include_declined=bool(include_declined),
            )
            return _ok(_dump(list_to_dicts(slots)))
        except Exception as e:
            log.exception("find_free_time failed")
            return _err(_format_error(e))

    def as_mcp_server(self):
        ctx = self

        @tool(
            "list_calendars",
            "List every iCloud calendar visible to the integration. Each "
            "result has an opaque calendar_id (preferred for write tools), "
            "human name, color, and read_only flag. Subscribed/holiday "
            "calendars are tagged is_subscribed=true. Cached for 60s.",
            {},
        )
        async def list_calendars(_args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.list_calendars()

        @tool(
            "list_events",
            "List events on one or more iCloud calendars. Defaults to the "
            "next 14 days when start/end are omitted. Ranges over 90 days "
            "are rejected unless force_unbounded=true. Recurring events are "
            "expanded to per-occurrence rows; each row carries an event_id "
            "you can pass to update_event/delete_event/get_event. Naive ISO "
            "timestamps are read as CRON_TZ local time. Pass calendar_id "
            "(opaque, from list_calendars) OR calendar_name to scope to a "
            "single calendar; both omitted searches every visible calendar.",
            {
                "calendar_id": str,
                "calendar_name": str,
                "start": str,
                "end": str,
                "search": str,
                "force_unbounded": bool,
            },
        )
        async def list_events(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.list_events(
                calendar_id=args.get("calendar_id"),
                calendar_name=args.get("calendar_name"),
                start=args.get("start"),
                end=args.get("end"),
                search=args.get("search"),
                force_unbounded=bool(args.get("force_unbounded", False)),
            )

        @tool(
            "get_event",
            "Fetch a single event by its opaque event_id. Returns the full "
            "row including a freshly-loaded etag and last_modified. To use "
            "optimistic concurrency on a subsequent update_event / "
            "delete_event call, prefer this tool over list_events: list "
            "etags can be null on iCloud, while get_event always returns a "
            "live etag. Pass that etag as expected_etag on the write to "
            "have iCloud reject the change if the event was modified by "
            "someone else in the meantime.",
            {"event_id": str},
        )
        async def get_event(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.get_event(event_id=args.get("event_id", ""))

        @tool(
            "find_free_time",
            "Find contiguous free intervals of at least duration_minutes "
            "within [start, end]. Skips events marked transparent (e.g. "
            "all-day reminders) and events the user has declined. By "
            "default ignores subscribed/holiday calendars; pass "
            "include_subscribed=true to fold them in. calendars is an "
            "optional comma-separated list of calendar ids or names.",
            {
                "start": str,
                "end": str,
                "duration_minutes": int,
                "calendars": str,
                "include_subscribed": bool,
                "include_declined": bool,
            },
        )
        async def find_free_time(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.find_free_time(
                start=args.get("start", ""),
                end=args.get("end", ""),
                duration_minutes=int(args.get("duration_minutes", 0)),
                calendars=args.get("calendars"),
                include_subscribed=bool(args.get("include_subscribed", False)),
                include_declined=bool(args.get("include_declined", False)),
            )

        return create_sdk_mcp_server(
            name="calendar_read",
            version="0.1.0",
            tools=[list_calendars, list_events, get_event, find_free_time],
        )


# --------------------------- write context ---------------------------


class CalendarWriteContext:
    def __init__(self, client: CalendarClient, cache: _ListCalendarsCache | None = None):
        self.client = client
        self.cache = cache or _ListCalendarsCache()

    def _invalidate(self) -> None:
        self.cache.clear()

    async def create_event(
        self,
        *,
        calendar_id: str | None = None,
        calendar_name: str | None = None,
        title: str,
        start: str,
        end: str,
        description: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        try:
            event = await asyncio.to_thread(
                self.client.create_event,
                calendar_id=_none_if_empty(calendar_id),
                calendar_name=_none_if_empty(calendar_name),
                title=title,
                start=start,
                end=end,
                description=_none_if_empty(description),
                location=_none_if_empty(location),
            )
            self._invalidate()
            return _ok(_dump(list_to_dicts([event])[0]))
        except Exception as e:
            log.exception("create_event failed")
            return _err(_format_error(e))

    async def update_event(
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
    ) -> dict[str, Any]:
        try:
            event = await asyncio.to_thread(
                self.client.update_event,
                event_id=event_id,
                expected_etag=_none_if_empty(expected_etag),
                scope=scope or "auto",
                title=_none_if_empty(title),
                start=_none_if_empty(start),
                end=_none_if_empty(end),
                description=_none_if_empty(description),
                location=_none_if_empty(location),
            )
            self._invalidate()
            return _ok(_dump(list_to_dicts([event])[0]))
        except Exception as e:
            log.exception("update_event failed")
            return _err(_format_error(e))

    async def delete_event(
        self,
        *,
        event_id: str,
        expected_etag: str | None = None,
        scope: str = "auto",
    ) -> dict[str, Any]:
        try:
            msg = await asyncio.to_thread(
                self.client.delete_event,
                event_id=event_id,
                expected_etag=_none_if_empty(expected_etag),
                scope=scope or "auto",
            )
            self._invalidate()
            return _ok(msg)
        except Exception as e:
            log.exception("delete_event failed")
            return _err(_format_error(e))

    def as_mcp_server(self):
        ctx = self

        @tool(
            "create_event",
            "Create a new event on an iCloud calendar. Provide either "
            "calendar_id (preferred, opaque, from list_calendars) or "
            "calendar_name (rejected if multiple calendars share the name). "
            "title is required. start and end are ISO-8601; naive "
            "timestamps are read as CRON_TZ local time. Subscribed / "
            "read-only calendars are rejected. attendees are not yet "
            "supported (deferred to v2).",
            {
                "calendar_id": str,
                "calendar_name": str,
                "title": str,
                "start": str,
                "end": str,
                "description": str,
                "location": str,
            },
        )
        async def create_event(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.create_event(
                calendar_id=args.get("calendar_id"),
                calendar_name=args.get("calendar_name"),
                title=args.get("title", ""),
                start=args.get("start", ""),
                end=args.get("end", ""),
                description=args.get("description"),
                location=args.get("location"),
            )

        @tool(
            "update_event",
            "Update fields on an existing event. event_id comes from a "
            "prior list_events / get_event / create_event call. scope "
            "controls how recurring events are handled: 'auto' (default) "
            "modifies just the occurrence the event_id refers to if it is "
            "an expanded instance, otherwise the whole series; 'instance' "
            "always modifies just one occurrence; 'series' always modifies "
            "the master. For optimistic concurrency, fetch the event with "
            "get_event first (its etag is reliable; list_events etags can "
            "be null on iCloud), then pass that etag as expected_etag — "
            "iCloud rejects the change with a refetch-and-retry error if "
            "anyone else modified the event between read and write. Only "
            "the provided fields change.",
            {
                "event_id": str,
                "expected_etag": str,
                "scope": str,
                "title": str,
                "start": str,
                "end": str,
                "description": str,
                "location": str,
            },
        )
        async def update_event(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.update_event(
                event_id=args.get("event_id", ""),
                expected_etag=args.get("expected_etag"),
                scope=args.get("scope", "auto"),
                title=args.get("title"),
                start=args.get("start"),
                end=args.get("end"),
                description=args.get("description"),
                location=args.get("location"),
            )

        @tool(
            "delete_event",
            "Delete an event. scope='auto' deletes just the occurrence if "
            "event_id is an expanded instance, otherwise the whole series; "
            "'instance' / 'series' force the choice. For optimistic "
            "concurrency, fetch the event with get_event first to get a "
            "reliable etag, then pass it as expected_etag — iCloud rejects "
            "the delete with a refetch-and-retry error if the event "
            "changed between read and write.",
            {"event_id": str, "expected_etag": str, "scope": str},
        )
        async def delete_event(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.delete_event(
                event_id=args.get("event_id", ""),
                expected_etag=args.get("expected_etag"),
                scope=args.get("scope", "auto"),
            )

        return create_sdk_mcp_server(
            name="calendar_write",
            version="0.1.0",
            tools=[create_event, update_event, delete_event],
        )
