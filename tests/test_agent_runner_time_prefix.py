"""Tests for the per-turn current-time prefix injected by AgentRunner.send.

The system prompt is built once at runner construction; without a per-turn
clock stamp, agents asked to schedule "tomorrow at 10am" frequently produce
the wrong date because their date prior is stale.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.agent_runner import _current_time_prefix


def test_prefix_format_uses_local_tz_and_weekday():
    fixed = datetime(2026, 4, 30, 9, 11, tzinfo=ZoneInfo("America/Chicago"))
    out = _current_time_prefix(now=fixed, tz_name="America/Chicago")
    assert out.startswith("[Current time: Thursday 2026-04-30 09:11")
    assert out.endswith("]")
    # Time-zone abbreviation depends on DST (CDT in April, CST in winter); just
    # assert it's a 2-5 char alpha block ending the bracketed payload.
    inner = out[len("[Current time: "):-1]
    parts = inner.split()
    assert len(parts) == 4  # weekday, date, time, tz
    assert parts[3].isalpha()


def test_prefix_attaches_tz_to_naive_datetime():
    naive = datetime(2026, 4, 30, 9, 11)  # no tzinfo
    out = _current_time_prefix(now=naive, tz_name="America/Chicago")
    assert "Thursday 2026-04-30 09:11" in out


def test_prefix_uses_now_when_no_argument():
    out = _current_time_prefix()
    # Just sanity-check the shape; we don't pin the value.
    assert out.startswith("[Current time: ")
    assert out.endswith("]")
    assert "20" in out  # year prefix


def test_prefix_distinct_across_minutes():
    a = _current_time_prefix(now=datetime(2026, 4, 30, 9, 11, tzinfo=ZoneInfo("America/Chicago")))
    b = _current_time_prefix(now=datetime(2026, 4, 30, 9, 12, tzinfo=ZoneInfo("America/Chicago")))
    assert a != b


def test_prefix_respects_alternate_tz():
    fixed = datetime(2026, 4, 30, 14, 11, tzinfo=ZoneInfo("UTC"))
    out_utc = _current_time_prefix(now=fixed, tz_name="UTC")
    out_chi = _current_time_prefix(now=fixed, tz_name="America/Chicago")
    # Same moment, two zones — the rendered local hour must differ.
    assert "14:11" in out_utc
    assert "09:11" in out_chi


@pytest.mark.parametrize(
    "moment, expected_substr",
    [
        (datetime(2026, 1, 1, 0, 0, tzinfo=ZoneInfo("America/Chicago")), "Thursday 2026-01-01 00:00"),
        (datetime(2026, 7, 4, 23, 59, tzinfo=ZoneInfo("America/Chicago")), "Saturday 2026-07-04 23:59"),
    ],
)
def test_prefix_renders_known_dates(moment, expected_substr):
    assert expected_substr in _current_time_prefix(now=moment, tz_name="America/Chicago")
