"""Tests for src/calendar_tools.py and src/calendar_client.py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.calendar_client import (
    AmbiguousCalendarError,
    CalendarClient,
    NotFound,
    RangeTooLargeError,
    ReadOnlyCalendarError,
    _normalize_href,
)
from src.calendar_tokens import (
    decode_calendar_id,
    decode_event_id,
    encode_calendar_id,
    encode_event_id,
)
from src.calendar_tools import (
    CalendarReadContext,
    CalendarWriteContext,
    _ListCalendarsCache,
)
from tests.fixtures.caldav_stub import (
    StubCalendar,
    StubPrincipal,
    make_vcalendar,
)


USER_EMAIL = "user@example.com"
TZ = "America/Chicago"
TZ_INFO = ZoneInfo(TZ)


def _is_error(result: dict) -> bool:
    return bool(result.get("isError"))


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _payload(result: dict):
    """Return parsed JSON content from a tool ok result."""
    return json.loads(_text(result))


def _client(
    *,
    calendars: list[StubCalendar] | None = None,
    allowed: list[str] | None = None,
    readonly: list[str] | None = None,
    user_email: str = USER_EMAIL,
) -> CalendarClient:
    if calendars is None:
        calendars = [
            StubCalendar(name="Personal", url="https://example.com/cal/personal/"),
            StubCalendar(name="Work", url="https://example.com/cal/work/"),
        ]
    return CalendarClient(
        username=user_email,
        password="dummy",
        url="https://example.com/",
        allowed_calendars=allowed,
        readonly_calendars=readonly,
        token_secret=None,
        user_email=user_email,
        tz_name=TZ,
        principal=StubPrincipal(calendars),
    )


# ---------------- href normalization ----------------


def test_normalize_href_strips_default_https_port():
    assert (
        _normalize_href("https://caldav.icloud.com:443/12345/calendars/work/")
        == "https://caldav.icloud.com/12345/calendars/work/"
    )


def test_normalize_href_strips_default_http_port():
    assert (
        _normalize_href("http://example.com:80/cal/")
        == "http://example.com/cal/"
    )


def test_normalize_href_preserves_non_default_ports():
    assert (
        _normalize_href("https://caldav.icloud.com:8443/x/")
        == "https://caldav.icloud.com:8443/x/"
    )


def test_normalize_href_passes_through_when_no_port():
    assert (
        _normalize_href("https://caldav.icloud.com/x/")
        == "https://caldav.icloud.com/x/"
    )


def test_normalize_href_handles_empty():
    assert _normalize_href("") == ""


@pytest.mark.asyncio
async def test_get_event_does_not_call_event_by_uid():
    """Regression: caldav 3.x's ``cal.event_by_uid`` issues a UID-only REPORT
    that iCloud rejects with 412. Our client must use a time-range REPORT
    instead. Sentinel: a stub whose ``event_by_uid`` always raises 412 still
    lets get/delete succeed, proving we don't call that path.
    """
    from caldav.lib.error import ReportError

    class No412Calendar(StubCalendar):
        def event_by_uid(self, uid):  # type: ignore[override]
            raise ReportError("ReportError at '412 Precondition Failed")

    cal = No412Calendar(name="Work", url="https://example.com/cal/work/")
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    cal.add_event_from_vcal(
        make_vcalendar(
            uid="evt1",
            title="Survivor",
            dtstart=start,
            dtend=start + timedelta(minutes=30),
        )
    )
    client = _client(calendars=[cal])
    read_ctx = CalendarReadContext(client)
    write_ctx = CalendarWriteContext(client)

    listed = _payload(await read_ctx.list_events())
    event_id = listed[0]["event_id"]

    fetched = _payload(await read_ctx.get_event(event_id=event_id))
    assert fetched["title"] == "Survivor"

    deleted = await write_ctx.delete_event(event_id=event_id, scope="series")
    assert not _is_error(deleted), _text(deleted)


@pytest.mark.asyncio
async def test_create_then_delete_survives_url_port_mutation():
    """Regression: caldav can rewrite cal.url to include ``:443`` after a
    request, even though discovery returned the unported form. Pre-fix this
    broke the create→delete round-trip with "calendar not found for this
    event_id". The fix uses the discovery-time href as the canonical key.
    """

    class PortMutatingCalendar(StubCalendar):
        """After the first ``url`` access, return a ``:443`` flavored URL."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._reads = 0
            self._original_url = kwargs["url"]

        @property  # type: ignore[override]
        def url(self) -> str:  # type: ignore[override]
            self._reads += 1
            if self._reads == 1:
                return self._original_url
            # After discovery, simulate caldav mutating url to include :443.
            return self._original_url.replace("https://", "https://").replace(
                "/cal/", ":443/cal/"
            )

        @url.setter
        def url(self, value: str) -> None:
            self._original_url = value

    cal = PortMutatingCalendar(name="Work", url="https://example.com/cal/work/")
    client = _client(calendars=[cal])
    write_ctx = CalendarWriteContext(client)
    read_ctx = CalendarReadContext(client)
    start = datetime.now(TZ_INFO) + timedelta(hours=1)

    create_result = await write_ctx.create_event(
        calendar_name="Work",
        title="Round trip",
        start=start.isoformat(),
        end=(start + timedelta(minutes=30)).isoformat(),
    )
    assert not _is_error(create_result), _text(create_result)
    payload = _payload(create_result)
    event_id = payload["event_id"]

    fetched = _payload(await read_ctx.get_event(event_id=event_id))
    assert fetched["title"] == "Round trip"

    delete_result = await write_ctx.delete_event(event_id=event_id, scope="series")
    assert not _is_error(delete_result), _text(delete_result)


# ---------------- token round-trip ----------------


def test_calendar_id_round_trip_unsigned():
    href = "https://example.com/cal/personal/"
    token = encode_calendar_id(href)
    assert decode_calendar_id(token) == href


def test_calendar_id_round_trip_signed():
    href = "https://example.com/cal/personal/"
    token = encode_calendar_id(href, secret="topsecret")
    assert decode_calendar_id(token, secret="topsecret") == href


def test_calendar_id_signature_mismatch_rejected():
    href = "https://example.com/cal/personal/"
    token = encode_calendar_id(href, secret="topsecret")
    with pytest.raises(ValueError, match="signature"):
        decode_calendar_id(token, secret="wrong-secret")


def test_event_id_round_trip_with_recurrence_and_etag():
    token = encode_event_id(
        calendar_href="https://example.com/cal/work/",
        uid="abc123",
        recurrence_id="2026-04-29T15:00:00",
        etag="etag-99",
    )
    payload = decode_event_id(token)
    assert payload["calendar_href"] == "https://example.com/cal/work/"
    assert payload["uid"] == "abc123"
    assert payload["recurrence_id"] == "2026-04-29T15:00:00"
    assert payload["etag"] == "etag-99"


def test_event_id_round_trip_minimal():
    token = encode_event_id(
        calendar_href="https://example.com/cal/work/", uid="abc123"
    )
    payload = decode_event_id(token)
    assert payload == {
        "calendar_href": "https://example.com/cal/work/",
        "uid": "abc123",
    }


# ---------------- list_calendars ----------------


@pytest.mark.asyncio
async def test_list_calendars_returns_all_visible():
    client = _client()
    ctx = CalendarReadContext(client)
    result = await ctx.list_calendars()
    assert not _is_error(result)
    cals = _payload(result)
    names = {c["name"] for c in cals}
    assert names == {"Personal", "Work"}
    for c in cals:
        # Round-trip the id.
        assert decode_calendar_id(c["calendar_id"]).endswith("/")


@pytest.mark.asyncio
async def test_list_calendars_honors_allowlist():
    cals = [
        StubCalendar(name="Personal", url="https://example.com/cal/personal/"),
        StubCalendar(name="Work", url="https://example.com/cal/work/"),
        StubCalendar(name="Holidays", url="https://example.com/cal/holidays/"),
    ]
    client = _client(calendars=cals, allowed=["Personal", "Work"])
    ctx = CalendarReadContext(client)
    result = await ctx.list_calendars()
    names = {c["name"] for c in _payload(result)}
    assert names == {"Personal", "Work"}


@pytest.mark.asyncio
async def test_readonly_env_forces_calendar_read_only():
    """Names listed in ICLOUD_READONLY_CALENDARS are forced to read_only=True
    even though the stub reports them as writable. This is the operator-side
    guard for shared family calendars when caldav can't detect the privilege
    server-side."""
    cals = [
        StubCalendar(name="Personal", url="https://example.com/cal/personal/"),
        StubCalendar(name="Family", url="https://example.com/cal/family/"),
    ]
    client = _client(calendars=cals, readonly=["Family"])
    ctx = CalendarReadContext(client)
    by_name = {c["name"]: c for c in _payload(await ctx.list_calendars())}
    assert by_name["Personal"]["read_only"] is False
    assert by_name["Family"]["read_only"] is True
    assert by_name["Family"]["is_subscribed"] is True


@pytest.mark.asyncio
async def test_readonly_env_blocks_writes_to_forced_calendar():
    cals = [StubCalendar(name="Family", url="https://example.com/cal/family/")]
    client = _client(calendars=cals, readonly=["Family"])
    write_ctx = CalendarWriteContext(client)
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    result = await write_ctx.create_event(
        calendar_name="Family",
        title="Should fail",
        start=start.isoformat(),
        end=(start + timedelta(minutes=30)).isoformat(),
    )
    assert _is_error(result)
    assert "read-only" in _text(result)


@pytest.mark.asyncio
async def test_list_calendars_marks_read_only():
    cals = [
        StubCalendar(name="Personal", url="https://example.com/cal/personal/"),
        StubCalendar(
            name="Holidays",
            url="https://example.com/cal/holidays/",
            read_only=True,
        ),
    ]
    client = _client(calendars=cals)
    ctx = CalendarReadContext(client)
    by_name = {c["name"]: c for c in _payload(await ctx.list_calendars())}
    assert by_name["Personal"]["read_only"] is False
    assert by_name["Holidays"]["read_only"] is True
    assert by_name["Holidays"]["is_subscribed"] is True


@pytest.mark.asyncio
async def test_list_calendars_caches_for_60s():
    client = _client()
    cache = _ListCalendarsCache()
    ctx = CalendarReadContext(client, cache=cache)
    await ctx.list_calendars()
    cached = cache.get()
    assert cached is not None
    assert len(cached) == 2


# ---------------- list_events ----------------


@pytest.mark.asyncio
async def test_list_events_returns_events_from_all_calendars():
    personal = StubCalendar(name="Personal", url="https://example.com/cal/personal/")
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    now = datetime.now(TZ_INFO)
    personal.add_event_from_vcal(
        make_vcalendar(
            uid="p1",
            title="Lunch",
            dtstart=now + timedelta(hours=1),
            dtend=now + timedelta(hours=2),
        )
    )
    work.add_event_from_vcal(
        make_vcalendar(
            uid="w1",
            title="Standup",
            dtstart=now + timedelta(hours=3),
            dtend=now + timedelta(hours=4),
        )
    )
    client = _client(calendars=[personal, work])
    ctx = CalendarReadContext(client)
    events = _payload(await ctx.list_events())
    titles = sorted(e["title"] for e in events)
    assert titles == ["Lunch", "Standup"]


@pytest.mark.asyncio
async def test_list_events_search_filters_title():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    now = datetime.now(TZ_INFO)
    work.add_event_from_vcal(
        make_vcalendar(uid="a", title="Standup", dtstart=now, dtend=now + timedelta(hours=1))
    )
    work.add_event_from_vcal(
        make_vcalendar(
            uid="b",
            title="One-on-one",
            dtstart=now + timedelta(hours=2),
            dtend=now + timedelta(hours=3),
        )
    )
    client = _client(calendars=[work])
    ctx = CalendarReadContext(client)
    events = _payload(await ctx.list_events(search="standup"))
    assert [e["title"] for e in events] == ["Standup"]


@pytest.mark.asyncio
async def test_list_events_rejects_range_over_90_days():
    client = _client()
    ctx = CalendarReadContext(client)
    start = datetime.now(timezone.utc).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=120)).isoformat()
    result = await ctx.list_events(start=start, end=end)
    assert _is_error(result)
    assert "90-day cap" in _text(result)


@pytest.mark.asyncio
async def test_list_events_force_unbounded_allows_long_range():
    client = _client()
    ctx = CalendarReadContext(client)
    start = datetime.now(timezone.utc).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=120)).isoformat()
    result = await ctx.list_events(start=start, end=end, force_unbounded=True)
    assert not _is_error(result)


@pytest.mark.asyncio
async def test_list_events_unknown_calendar_name_errors():
    client = _client()
    ctx = CalendarReadContext(client)
    result = await ctx.list_events(calendar_name="Imaginary")
    assert _is_error(result)
    assert "not found" in _text(result)


@pytest.mark.asyncio
async def test_list_events_ambiguous_calendar_name_errors():
    cals = [
        StubCalendar(name="Personal", url="https://example.com/cal/personal/"),
        StubCalendar(name="Personal", url="https://example.com/cal/other-personal/"),
    ]
    client = _client(calendars=cals)
    ctx = CalendarReadContext(client)
    result = await ctx.list_events(calendar_name="Personal")
    assert _is_error(result)
    assert "matches" in _text(result)


# ---------------- get_event ----------------


@pytest.mark.asyncio
async def test_get_event_round_trips():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    now = datetime.now(TZ_INFO)
    work.add_event_from_vcal(
        make_vcalendar(
            uid="evt1",
            title="Standup",
            dtstart=now + timedelta(hours=1),
            dtend=now + timedelta(hours=2),
        )
    )
    client = _client(calendars=[work])
    ctx = CalendarReadContext(client)
    listed = _payload(await ctx.list_events())
    event_id = listed[0]["event_id"]

    fetched = _payload(await ctx.get_event(event_id=event_id))
    assert fetched["title"] == "Standup"


@pytest.mark.asyncio
async def test_get_event_missing_returns_error():
    client = _client()
    ctx = CalendarReadContext(client)
    fake = encode_event_id(
        calendar_href="https://example.com/cal/personal/", uid="ghost"
    )
    result = await ctx.get_event(event_id=fake)
    assert _is_error(result)
    assert "not found" in _text(result)


# ---------------- find_free_time ----------------


@pytest.mark.asyncio
async def test_find_free_time_empty_calendar_returns_full_range():
    client = _client()
    ctx = CalendarReadContext(client)
    start = datetime(2026, 4, 29, 9, 0, tzinfo=TZ_INFO)
    end = datetime(2026, 4, 29, 17, 0, tzinfo=TZ_INFO)
    slots = _payload(
        await ctx.find_free_time(
            start=start.isoformat(),
            end=end.isoformat(),
            duration_minutes=30,
        )
    )
    assert len(slots) == 1


@pytest.mark.asyncio
async def test_find_free_time_blocks_around_busy_event():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    busy_start = datetime(2026, 4, 29, 11, 0, tzinfo=TZ_INFO)
    busy_end = datetime(2026, 4, 29, 12, 0, tzinfo=TZ_INFO)
    work.add_event_from_vcal(
        make_vcalendar(
            uid="busy", title="Meeting", dtstart=busy_start, dtend=busy_end
        )
    )
    client = _client(calendars=[work])
    ctx = CalendarReadContext(client)
    slots = _payload(
        await ctx.find_free_time(
            start=datetime(2026, 4, 29, 9, 0, tzinfo=TZ_INFO).isoformat(),
            end=datetime(2026, 4, 29, 17, 0, tzinfo=TZ_INFO).isoformat(),
            duration_minutes=30,
        )
    )
    # Two gaps: 09:00–11:00 and 12:00–17:00.
    assert len(slots) == 2


@pytest.mark.asyncio
async def test_find_free_time_skips_transparent_events():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    work.add_event_from_vcal(
        make_vcalendar(
            uid="t",
            title="Reminder",
            dtstart=datetime(2026, 4, 29, 11, 0, tzinfo=TZ_INFO),
            dtend=datetime(2026, 4, 29, 12, 0, tzinfo=TZ_INFO),
            transparent=True,
        )
    )
    client = _client(calendars=[work])
    ctx = CalendarReadContext(client)
    slots = _payload(
        await ctx.find_free_time(
            start=datetime(2026, 4, 29, 9, 0, tzinfo=TZ_INFO).isoformat(),
            end=datetime(2026, 4, 29, 17, 0, tzinfo=TZ_INFO).isoformat(),
            duration_minutes=30,
        )
    )
    assert len(slots) == 1  # transparent event ignored, full range free


@pytest.mark.asyncio
async def test_find_free_time_skips_declined_events():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    work.add_event_from_vcal(
        make_vcalendar(
            uid="d",
            title="Skip me",
            dtstart=datetime(2026, 4, 29, 11, 0, tzinfo=TZ_INFO),
            dtend=datetime(2026, 4, 29, 12, 0, tzinfo=TZ_INFO),
            user_email=USER_EMAIL,
            partstat="DECLINED",
        )
    )
    client = _client(calendars=[work])
    ctx = CalendarReadContext(client)
    slots = _payload(
        await ctx.find_free_time(
            start=datetime(2026, 4, 29, 9, 0, tzinfo=TZ_INFO).isoformat(),
            end=datetime(2026, 4, 29, 17, 0, tzinfo=TZ_INFO).isoformat(),
            duration_minutes=30,
        )
    )
    assert len(slots) == 1


@pytest.mark.asyncio
async def test_find_free_time_includes_declined_when_flagged():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    work.add_event_from_vcal(
        make_vcalendar(
            uid="d",
            title="Skip me",
            dtstart=datetime(2026, 4, 29, 11, 0, tzinfo=TZ_INFO),
            dtend=datetime(2026, 4, 29, 12, 0, tzinfo=TZ_INFO),
            user_email=USER_EMAIL,
            partstat="DECLINED",
        )
    )
    client = _client(calendars=[work])
    ctx = CalendarReadContext(client)
    slots = _payload(
        await ctx.find_free_time(
            start=datetime(2026, 4, 29, 9, 0, tzinfo=TZ_INFO).isoformat(),
            end=datetime(2026, 4, 29, 17, 0, tzinfo=TZ_INFO).isoformat(),
            duration_minutes=30,
            include_declined=True,
        )
    )
    assert len(slots) == 2  # now blocked by the declined event


@pytest.mark.asyncio
async def test_find_free_time_skips_subscribed_calendars_by_default():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    holidays = StubCalendar(
        name="US Holidays",
        url="https://example.com/cal/holidays/",
        read_only=True,
    )
    holidays.add_event_from_vcal(
        make_vcalendar(
            uid="h",
            title="All-day holiday",
            dtstart=datetime(2026, 4, 29, 9, 0, tzinfo=TZ_INFO),
            dtend=datetime(2026, 4, 29, 17, 0, tzinfo=TZ_INFO),
        )
    )
    client = _client(calendars=[work, holidays])
    ctx = CalendarReadContext(client)
    slots = _payload(
        await ctx.find_free_time(
            start=datetime(2026, 4, 29, 9, 0, tzinfo=TZ_INFO).isoformat(),
            end=datetime(2026, 4, 29, 17, 0, tzinfo=TZ_INFO).isoformat(),
            duration_minutes=30,
        )
    )
    assert len(slots) == 1  # holiday calendar excluded
    slots_with = _payload(
        await ctx.find_free_time(
            start=datetime(2026, 4, 29, 9, 0, tzinfo=TZ_INFO).isoformat(),
            end=datetime(2026, 4, 29, 17, 0, tzinfo=TZ_INFO).isoformat(),
            duration_minutes=30,
            include_subscribed=True,
        )
    )
    assert slots_with == []  # blocked all day by holiday


# ---------------- create_event ----------------


@pytest.mark.asyncio
async def test_create_event_succeeds_on_writable_calendar():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    client = _client(calendars=[work])
    write_ctx = CalendarWriteContext(client)
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    end = start + timedelta(minutes=30)
    result = await write_ctx.create_event(
        calendar_name="Work",
        title="New Meeting",
        start=start.isoformat(),
        end=end.isoformat(),
    )
    assert not _is_error(result)
    payload = _payload(result)
    assert payload["title"] == "New Meeting"
    assert len(work._events) == 1


@pytest.mark.asyncio
async def test_create_event_rejected_on_read_only_calendar():
    sub = StubCalendar(
        name="Holidays",
        url="https://example.com/cal/holidays/",
        read_only=True,
    )
    client = _client(calendars=[sub])
    write_ctx = CalendarWriteContext(client)
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    result = await write_ctx.create_event(
        calendar_name="Holidays",
        title="Nope",
        start=start.isoformat(),
        end=(start + timedelta(minutes=30)).isoformat(),
    )
    assert _is_error(result)
    assert "read-only" in _text(result)


@pytest.mark.asyncio
async def test_create_event_missing_title_errors():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    client = _client(calendars=[work])
    write_ctx = CalendarWriteContext(client)
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    result = await write_ctx.create_event(
        calendar_name="Work",
        title="",
        start=start.isoformat(),
        end=(start + timedelta(minutes=30)).isoformat(),
    )
    assert _is_error(result)
    assert "title" in _text(result)


@pytest.mark.asyncio
async def test_create_event_invalidates_calendar_cache():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    client = _client(calendars=[work])
    cache = _ListCalendarsCache()
    read_ctx = CalendarReadContext(client, cache=cache)
    write_ctx = CalendarWriteContext(client, cache=cache)
    await read_ctx.list_calendars()
    assert cache.get() is not None
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    await write_ctx.create_event(
        calendar_name="Work",
        title="Cache buster",
        start=start.isoformat(),
        end=(start + timedelta(minutes=30)).isoformat(),
    )
    assert cache.get() is None


# ---------------- update_event / delete_event ----------------


@pytest.mark.asyncio
async def test_update_event_series_changes_title():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    work.add_event_from_vcal(
        make_vcalendar(
            uid="evt1",
            title="Old",
            dtstart=start,
            dtend=start + timedelta(minutes=30),
        )
    )
    client = _client(calendars=[work])
    read_ctx = CalendarReadContext(client)
    write_ctx = CalendarWriteContext(client)
    listed = _payload(await read_ctx.list_events())
    event_id = listed[0]["event_id"]

    result = await write_ctx.update_event(
        event_id=event_id, scope="series", title="Renamed"
    )
    assert not _is_error(result)
    after = _payload(await read_ctx.list_events())
    assert after[0]["title"] == "Renamed"


@pytest.mark.asyncio
async def test_two_consecutive_series_updates_do_not_crash_serialize():
    """Regression: a previous version wrote LAST-MODIFIED via _set_text on
    every series update. The first update added the property with a string
    value (vobject ``isNative=False``); after the server round-trip vobject
    re-parsed it to a datetime (``isNative=True``); the second update
    overwrote ``.value`` with a fresh string but left ``isNative=True``,
    causing ``vcal.serialize()`` to crash with
    ``'str' object has no attribute 'tzinfo'`` when transformFromNative ran.
    """
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    work.add_event_from_vcal(
        make_vcalendar(
            uid="evt1", title="v1",
            dtstart=start, dtend=start + timedelta(minutes=30),
        ),
        etag="etag-1",
    )
    client = _client(calendars=[work])
    write_ctx = CalendarWriteContext(client)
    read_ctx = CalendarReadContext(client)
    listed = _payload(await read_ctx.list_events())

    first = await write_ctx.update_event(
        event_id=listed[0]["event_id"], scope="series", title="v2"
    )
    assert not _is_error(first), _text(first)

    # Re-list (forces vobject re-parse from the stored ical text — same as
    # what happens after iCloud's REPORT response in production).
    listed_again = _payload(await read_ctx.list_events())

    second = await write_ctx.update_event(
        event_id=listed_again[0]["event_id"], scope="series", title="v3"
    )
    assert not _is_error(second), _text(second)


@pytest.mark.asyncio
async def test_update_event_etag_mismatch_rejected():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    work.add_event_from_vcal(
        make_vcalendar(
            uid="evt1",
            title="Old",
            dtstart=start,
            dtend=start + timedelta(minutes=30),
        )
    )
    client = _client(calendars=[work])
    read_ctx = CalendarReadContext(client)
    write_ctx = CalendarWriteContext(client)
    listed = _payload(await read_ctx.list_events())
    event_id = listed[0]["event_id"]

    result = await write_ctx.update_event(
        event_id=event_id,
        scope="series",
        title="Renamed",
        expected_etag="not-matching",
    )
    assert _is_error(result)
    assert "concurrently" in _text(result)


@pytest.mark.asyncio
async def test_delete_event_series_removes_event():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    work.add_event_from_vcal(
        make_vcalendar(
            uid="evt1",
            title="To delete",
            dtstart=start,
            dtend=start + timedelta(minutes=30),
        )
    )
    client = _client(calendars=[work])
    read_ctx = CalendarReadContext(client)
    write_ctx = CalendarWriteContext(client)
    listed = _payload(await read_ctx.list_events())
    event_id = listed[0]["event_id"]

    result = await write_ctx.delete_event(event_id=event_id, scope="series")
    assert not _is_error(result)
    after = _payload(await read_ctx.list_events())
    assert after == []


@pytest.mark.asyncio
async def test_update_event_instance_without_recurrence_errors():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    work.add_event_from_vcal(
        make_vcalendar(
            uid="evt1",
            title="One-off",
            dtstart=start,
            dtend=start + timedelta(minutes=30),
        )
    )
    client = _client(calendars=[work])
    read_ctx = CalendarReadContext(client)
    write_ctx = CalendarWriteContext(client)
    listed = _payload(await read_ctx.list_events())
    event_id = listed[0]["event_id"]

    result = await write_ctx.update_event(
        event_id=event_id, scope="instance", title="Renamed"
    )
    assert _is_error(result)
    assert "expanded occurrence" in _text(result)


@pytest.mark.asyncio
async def test_update_event_future_scope_not_implemented():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    work.add_event_from_vcal(
        make_vcalendar(
            uid="evt1",
            title="Series",
            dtstart=start,
            dtend=start + timedelta(minutes=30),
        )
    )
    client = _client(calendars=[work])
    read_ctx = CalendarReadContext(client)
    write_ctx = CalendarWriteContext(client)
    listed = _payload(await read_ctx.list_events())
    event_id = listed[0]["event_id"]

    result = await write_ctx.update_event(
        event_id=event_id, scope="future", title="Nope"
    )
    assert _is_error(result)
    assert "future" in _text(result).lower()


# ---------------- etag / optimistic concurrency ----------------


@pytest.mark.asyncio
async def test_get_event_calls_load_to_refresh_etag():
    """iCloud's REPORT often returns null etags; get_event must force a
    direct GET (ev.load()) so the returned etag is reliable for use as
    expected_etag on subsequent writes.
    """
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    work.add_event_from_vcal(
        make_vcalendar(
            uid="evt1", title="Round trip",
            dtstart=start, dtend=start + timedelta(minutes=30),
        ),
        etag="server-etag-1",
    )
    client = _client(calendars=[work])
    read_ctx = CalendarReadContext(client)

    listed = _payload(await read_ctx.list_events())
    event_id = listed[0]["event_id"]
    stub_event = work._events[0]
    loads_before = stub_event._loaded_count

    fetched = _payload(await read_ctx.get_event(event_id=event_id))
    assert stub_event._loaded_count == loads_before + 1
    assert fetched["etag"] == "server-etag-1"


@pytest.mark.asyncio
async def test_update_event_with_matching_etag_succeeds():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    work.add_event_from_vcal(
        make_vcalendar(
            uid="evt1", title="Original",
            dtstart=start, dtend=start + timedelta(minutes=30),
        ),
        etag="etag-current",
    )
    client = _client(calendars=[work])
    read_ctx = CalendarReadContext(client)
    write_ctx = CalendarWriteContext(client)

    fetched = _payload(await read_ctx.get_event(
        event_id=_payload(await read_ctx.list_events())[0]["event_id"]
    ))
    assert fetched["etag"] == "etag-current"

    result = await write_ctx.update_event(
        event_id=fetched["event_id"],
        scope="series",
        title="Renamed",
        expected_etag="etag-current",
    )
    assert not _is_error(result), _text(result)


@pytest.mark.asyncio
async def test_update_event_with_stale_etag_returns_conflict():
    """Caller's expected_etag predates a concurrent edit — server's If-Match
    rejects the write with 412, surfacing as a 'modified concurrently' error.
    """
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    work.add_event_from_vcal(
        make_vcalendar(
            uid="evt1", title="Original",
            dtstart=start, dtend=start + timedelta(minutes=30),
        ),
        etag="etag-current",
    )
    client = _client(calendars=[work])
    write_ctx = CalendarWriteContext(client)

    result = await write_ctx.update_event(
        event_id=_payload(
            await CalendarReadContext(client).list_events()
        )[0]["event_id"],
        scope="series",
        title="Won't land",
        expected_etag="etag-from-an-old-read",
    )
    assert _is_error(result)
    assert "concurrently" in _text(result)


@pytest.mark.asyncio
async def test_delete_event_with_stale_etag_returns_conflict():
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    work.add_event_from_vcal(
        make_vcalendar(
            uid="evt1", title="Doomed",
            dtstart=start, dtend=start + timedelta(minutes=30),
        ),
        etag="etag-current",
    )
    client = _client(calendars=[work])
    write_ctx = CalendarWriteContext(client)
    listed = _payload(await CalendarReadContext(client).list_events())

    result = await write_ctx.delete_event(
        event_id=listed[0]["event_id"],
        scope="series",
        expected_etag="etag-from-an-old-read",
    )
    assert _is_error(result)
    assert "concurrently" in _text(result)
    # Event should still be on the calendar.
    assert work._events[0]._deleted is False


@pytest.mark.asyncio
async def test_update_event_without_etag_does_not_set_if_match():
    """Calls without expected_etag should not stage any If-Match — the
    write just happens, no precondition check."""
    work = StubCalendar(name="Work", url="https://example.com/cal/work/")
    start = datetime.now(TZ_INFO) + timedelta(hours=1)
    work.add_event_from_vcal(
        make_vcalendar(
            uid="evt1", title="Original",
            dtstart=start, dtend=start + timedelta(minutes=30),
        ),
        etag="etag-current",
    )
    client = _client(calendars=[work])
    write_ctx = CalendarWriteContext(client)
    listed = _payload(await CalendarReadContext(client).list_events())

    # Manually corrupt the staged props to simulate "stale local state" —
    # without expected_etag, our code should not write GETETAG_TAG, so the
    # If-Match check should pass (the stub only enforces If-Match if props
    # match a non-server value).
    result = await write_ctx.update_event(
        event_id=listed[0]["event_id"],
        scope="series",
        title="Lands fine without etag",
    )
    assert not _is_error(result), _text(result)


# ---------------- as_mcp_server smoke ----------------


def test_read_context_as_mcp_server_constructs():
    ctx = CalendarReadContext(_client())
    assert ctx.as_mcp_server() is not None


def test_write_context_as_mcp_server_constructs():
    ctx = CalendarWriteContext(_client())
    assert ctx.as_mcp_server() is not None
