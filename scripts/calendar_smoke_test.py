"""Manual sanity check for the iCloud CalDAV connection.

Run after populating .env. Prints calendars + today's events. Exits non-zero
on any failure so callers can detect a broken setup.

    python scripts/calendar_smoke_test.py
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

from src.calendar_client import CalendarClient, CalendarClientError  # noqa: E402


def main() -> int:
    # Windows console defaults to cp1252, which chokes on emoji / curly
    # apostrophes / non-Latin characters in calendar names. Force UTF-8.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    apple_id = os.getenv("ICLOUD_APPLE_ID", "").strip()
    app_password = os.getenv("ICLOUD_APP_PASSWORD", "").strip()
    if not apple_id or not app_password:
        print(
            "ERROR: ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD must be set in .env",
            file=sys.stderr,
        )
        return 1

    tz_name = os.getenv("CRON_TZ", "America/Chicago")
    allowed_raw = os.getenv("ICLOUD_ALLOWED_CALENDARS", "")
    allowed = [s.strip() for s in allowed_raw.split(",") if s.strip()] or None
    readonly_raw = os.getenv("ICLOUD_READONLY_CALENDARS", "")
    readonly = [s.strip() for s in readonly_raw.split(",") if s.strip()] or None

    client = CalendarClient(
        username=apple_id,
        password=app_password,
        allowed_calendars=allowed,
        readonly_calendars=readonly,
        token_secret=os.getenv("CALENDAR_TOKEN_SECRET") or None,
        user_email=apple_id,
        tz_name=tz_name,
    )

    try:
        cals = client.list_calendars()
    except CalendarClientError as e:
        print(f"ERROR: list_calendars failed: {e}", file=sys.stderr)
        return 2

    print(f"\nFound {len(cals)} calendar(s):")
    for info in cals:
        flags = []
        if info.read_only:
            flags.append("read-only")
        if info.is_subscribed:
            flags.append("subscribed")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        print(f"  - {info.name}{flag_str}")

    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    start = datetime.combine(today, time.min, tz).isoformat()
    end = datetime.combine(today + timedelta(days=1), time.min, tz).isoformat()

    try:
        events = client.list_events(start=start, end=end)
    except CalendarClientError as e:
        print(f"ERROR: list_events failed: {e}", file=sys.stderr)
        return 2

    print(f"\nToday ({today.isoformat()}) — {len(events)} event(s):")
    for rec in events:
        time_label = "all-day" if rec.all_day else f"{rec.start} → {rec.end}"
        print(f"  - {time_label}: {rec.title} ({rec.calendar_name})")

    print("\nSmoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
