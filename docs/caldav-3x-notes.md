# caldav 3.x + iCloud notes

Practical reference for anyone touching `src/calendar_client.py`,
`src/calendar_tools.py`, or the `scripts/calendar_*.py` workers.

The integration uses the `caldav` Python library against
`https://caldav.icloud.com/`. caldav 3.x renamed or removed several APIs we
relied on in 1.x, and iCloud's CalDAV implementation has quirks the library
does not paper over. This file collects everything we learned shipping the
integration so the next person doesn't have to learn it from a 412.

## Dependencies

```
caldav>=3.2
vobject>=0.9   # 1.x pulled vobject transitively; 3.x does not
```

Both are declared explicitly in `pyproject.toml`. `vobject` is still load-
bearing because the parsing path uses `event.vobject_instance`; if you remove
it, plan a migration to the `icalendar` library that caldav now ships with.

## API differences vs caldav 1.x

| 1.x | 3.x |
|---|---|
| `event.icalendar_instance` returned a vobject component | now returns `icalendar.Calendar`. Use `event.vobject_instance` for vobject. |
| `dav.CurrentUserPrivilegeSet` was importable | removed from `caldav.elements.dav`. We probe via `getattr` and fall back. |
| `cal.event_by_uid(uid)` worked against iCloud | issues a UID-only REPORT under the hood, which iCloud rejects with 412. Don't call it. |
| vobject was a transitive dep | not anymore. Declare it yourself. |
| `event.etag` was its own attribute | now a property reading `event.props[{DAV:}getetag]`. |

## iCloud quirks

### 1. UID-only REPORTs return 412 Precondition Failed

`cal.event_by_uid(uid)` and `cal.search(uid=uid)` (with no time-range)
construct a calendar-query REPORT with only a UID filter. iCloud rejects
these unconditionally:

```
caldav.lib.error.ReportError: ReportError at '412 Precondition Failed
```

**Workaround:** issue a time-range bounded REPORT and filter by UID in
memory. `src/calendar_client.py::CalendarClient._fetch_event_for_uid`
does this with a `[-30d, +365d]` window — wide enough for any event a user
plausibly operates on shortly after seeing it. If you need older or further-
future events, widen the window or move to a multi-step search.

### 2. Per-event etags often missing from REPORT responses

iCloud's calendar-query REPORT responses frequently omit `<getetag>` per
event. caldav populates `ev.etag` from whatever the response carries, so
events fetched via `list_events` may have `etag = None`.

**Workaround:** call `ev.load()` after fetching. This issues a direct GET
against `ev.url`; iCloud's GET response always carries an `Etag` header,
which caldav writes into `ev.props[{DAV:}getetag]`.
`src/calendar_client.py::CalendarClient.get_event` does this so the
`EventDict.etag` returned by that tool is reliable for use as
`expected_etag` on a subsequent update/delete.

`list_events` deliberately does **not** load every event — that would mean
one extra round-trip per event, which is unacceptable for cheap reads.
The tradeoff: agents must call `get_event` before write operations that
need precondition checking. Tool descriptions document this.

### 3. Calendar hrefs sometimes carry `:443`, sometimes not

iCloud's responses to a discovery PROPFIND will return calendar URLs like
`https://caldav.icloud.com/12345/calendars/work/`, but a follow-up request
on the same `Calendar` object can mutate `cal.url` to
`https://caldav.icloud.com:443/12345/calendars/work/`. The same href, but
not equal as strings.

**Workaround:** normalize hrefs at storage and lookup —
`src/calendar_client.py::_normalize_href` strips default ports (`:443` for
HTTPS, `:80` for HTTP). Apply it everywhere a href is used as a dict key
or compared.

### 4. If-Match works correctly when set

caldav 3.x reads `ev.etag` from `ev.props[{DAV:}getetag]` on every save and
delete and auto-attaches `If-Match: <etag>` to the request. iCloud honors
this and returns 412 on a stale etag, which caldav surfaces as
`ETagMismatchError`. We catch that and raise `ConflictError`.

To make `expected_etag` enforceable from the agent layer:

```python
ev.props[GETETAG_TAG] = expected_etag  # see _apply_expected_etag
ev.save()  # caldav adds If-Match automatically; iCloud enforces
```

This is the *only* reliable optimistic-concurrency path — a client-side
check is unsafe because etags from `list_events` may be null.

## vobject quirks

### 5. Can't serialize `datetime.timezone.utc`

vobject 0.9.x doesn't recognize `datetime.timezone.utc` as a tzinfo it can
emit a TZID for, and crashes with:

```
vobject.base.VObjectError: Unable to guess TZID for tzinfo UTC
```

**Workaround:** use `pytz.UTC` or `zoneinfo.ZoneInfo("UTC")`.
`src/calendar_client.py::_vobject_dt` coerces every datetime we hand to
vobject through `pytz.UTC.localize` / `astimezone(pytz.UTC)`.

### 6. Don't write strings into native-parsed datetime properties

When vobject parses an iCalendar text response, it converts properties
like `DTSTART` and `LAST-MODIFIED` into Python datetimes and flips the
ContentLine's `isNative` flag to `True`. If you then assign a string to
that property's `.value`, the flag stays `True` but the value is wrong.
On the next `vcal.serialize()`, vobject calls `transformFromNative()`
which expects datetime methods and crashes with:

```
AttributeError: 'str' object has no attribute 'tzinfo'
```

This bit us once with `LAST-MODIFIED` — see the regression test
`tests/test_calendar_tools.py::test_two_consecutive_series_updates_do_not_crash_serialize`.
The fix: don't write LAST-MODIFIED ourselves at all (iCloud sets its own
server-side timestamp). For other datetime-typed properties (`DTSTART`,
`DTEND`, `RECURRENCE-ID`), always use `_set_dt` (which writes a coerced
datetime), never `_set_text` (which writes a string).

If you need to write a datetime property:

```python
self._set_dt(master, "dtstart", _vobject_dt(value))
```

Never:

```python
self._set_text(master, "dtstart", value.strftime("..."))   # broken
```

## Optimistic concurrency pattern (agent-facing)

The contract for callers:

1. `get_event(event_id)` — returns the event with a populated `etag`.
2. `update_event(event_id, expected_etag=etag, ...)` — succeeds if no
   concurrent edit, otherwise returns `"event was modified concurrently;
   refetch and retry"`.
3. Same for `delete_event`.

`list_events` returns events whose `etag` may be null. Don't pass that
null etag to `expected_etag` — that disables the precondition. Always
fetch via `get_event` first if you need optimistic concurrency.

## File map — where each workaround lives

| Concern | Location |
|---|---|
| Time-range UID search | `src/calendar_client.py::CalendarClient._fetch_event_for_uid` |
| `ev.load()` for reliable etag | `src/calendar_client.py::CalendarClient.get_event` |
| Href port normalization | `src/calendar_client.py::_normalize_href`, used by `_href_of` and `_find_calendar_by_href` |
| If-Match push-down | `src/calendar_client.py::CalendarClient._apply_expected_etag` |
| `ETagMismatchError` → `ConflictError` | catch blocks in `update_event` / `delete_event` |
| pytz coercion for vobject | `src/calendar_client.py::_vobject_dt` |
| Privilege detection fallback chain | `src/calendar_client.py::_privilege_writable` |
| Forced read-only by config | `ICLOUD_READONLY_CALENDARS` env var → `CalendarClient(readonly_calendars=...)` |

## Regression tests

Each of these locks in a specific quirk's workaround. If you change the
relevant code and one of these breaks, you've probably reintroduced the
bug it catches.

| Test | Catches |
|---|---|
| `test_get_event_does_not_call_event_by_uid` | UID-only REPORT 412 |
| `test_create_then_delete_survives_url_port_mutation` | `:443` href mismatch |
| `test_two_consecutive_series_updates_do_not_crash_serialize` | `LAST-MODIFIED` string-write |
| `test_get_event_calls_load_to_refresh_etag` | Missing-etag-from-REPORT |
| `test_update_event_with_stale_etag_returns_conflict` | If-Match → 412 → ConflictError |
| `test_delete_event_with_stale_etag_returns_conflict` | Same, delete path |
| `test_normalize_href_*` (5 cases) | Port-normalization edge cases |

## Things still on the table (v2)

- **Recurrence `scope="future"`** is not implemented; raises
  `NotImplementedError`. Splitting an RRULE with a UNTIL boundary plus a
  new master from the boundary is fiddly and we deferred it.
- **Attendees on `create_event`** are not yet supported. iCloud's invite
  semantics need live testing — invitations may auto-fire emails to the
  attendee addresses we send, with surprising opt-out behavior. Defer
  until you can test against a throwaway address.
- **Log noise on legitimate `ConflictError`** — the catch block in
  `calendar_tools.py` logs at `ERROR` level even when 412 is the
  expected, well-behaved outcome. Could be downgraded to `WARNING` or
  `INFO`.
