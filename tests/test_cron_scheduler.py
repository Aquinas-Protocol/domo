"""Tests for src/cron_scheduler.py — _run_to_completion, _run_job, _tick_once."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.cron_scheduler import (
    _run_job,
    _run_to_completion,
    _tick_once,
    _wrap_prompt_for_destination,
)
from src.cron_store import CronStore


@dataclass
class FakeEvent:
    kind: str


class StubRunner:
    def __init__(self, events: list[FakeEvent]):
        self.events = events
        self.calls: list[tuple[str, str]] = []

    async def send(self, prompt: str, *, triggered_by: str = "user", **_):
        self.calls.append((prompt, triggered_by))
        for ev in self.events:
            yield ev


class StubRegistry:
    def __init__(self, runner: StubRunner):
        self.runner = runner
        self.keys: list[tuple[str, str]] = []

    def get_or_create(self, key: str, agent_name: str) -> StubRunner:
        self.keys.append((key, agent_name))
        return self.runner

    @asynccontextmanager
    async def background_slot(self, agent_name: str):
        yield


class StubLedger:
    def __init__(self, queued: list | None = None, running: list | None = None):
        self.queued = queued or []
        self.running = running or []

    def query(
        self, agent=None, status=None, since_minutes=240, limit=20,
        triggered_by_prefix=None,
    ):
        if triggered_by_prefix in {"cron:", "mission:"}:
            if status == "queued":
                return list(self.queued) if triggered_by_prefix == "cron:" else []
            if status == "running":
                return list(self.running) if triggered_by_prefix == "cron:" else []
        return []


def _now_at(yyyymmdd: str, hh: int, mm: int = 0) -> datetime:
    y, m, d = (int(p) for p in yyyymmdd.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _seed_due(store: CronStore, name: str, now: datetime) -> int:
    jid = store.create(
        name=name, cron_expr="0 9 * * *", target_agent="main",
        prompt=f"do {name}", now_utc=now,
    )
    store.update_next_run(jid, "2026-04-27T11:00:00+00:00")
    return jid


# ---------------- _wrap_prompt_for_destination ----------------

def test_wrap_prompt_unwraps_for_null_destination():
    """agent_task jobs land in the store with NULL dest. The scheduler must
    pass the prompt through verbatim — no 'post to channel X' wrapping —
    so Maine actually executes the prompt as a normal agent task."""
    job = {"prompt": "do the thing", "destination_channel_id": None}
    assert _wrap_prompt_for_destination(job) == "do the thing"


def test_wrap_prompt_wraps_for_explicit_destination():
    """Default reminder mode wraps with the verbatim-post directive."""
    job = {"prompt": "the message", "destination_channel_id": "1234567890"}
    wrapped = _wrap_prompt_for_destination(job)
    assert "1234567890" in wrapped
    assert "the message" in wrapped
    assert "VERBATIM" in wrapped


# ---------------- _run_to_completion ----------------

@pytest.mark.asyncio
async def test_run_to_completion_with_final():
    runner = StubRunner([FakeEvent("text"), FakeEvent("final")])
    saw_final, saw_error = await _run_to_completion(runner, "hi", "cron:1")
    assert saw_final is True
    assert saw_error is False
    assert runner.calls == [("hi", "cron:1")]


@pytest.mark.asyncio
async def test_run_to_completion_with_error_event():
    runner = StubRunner([FakeEvent("text"), FakeEvent("error")])
    saw_final, saw_error = await _run_to_completion(runner, "hi", "cron:1")
    assert saw_final is False
    assert saw_error is True


@pytest.mark.asyncio
async def test_run_to_completion_no_final_no_error():
    runner = StubRunner([FakeEvent("text")])
    saw_final, saw_error = await _run_to_completion(runner, "hi", "cron:1")
    assert saw_final is False
    assert saw_error is False


# ---------------- _run_job records the right counter ----------------

@pytest.mark.asyncio
async def test_run_job_marks_success_on_final(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(
        name="x", cron_expr="0 9 * * *", target_agent="main", prompt="go",
    )
    runner = StubRunner([FakeEvent("text"), FakeEvent("final")])
    await _run_job(runner, store.get(jid), store)
    row = store.get(jid)
    assert row["run_count"] == 1
    assert row["fail_count"] == 0


@pytest.mark.asyncio
async def test_run_job_marks_failure_on_error_event(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(
        name="x", cron_expr="0 9 * * *", target_agent="main", prompt="go",
    )
    runner = StubRunner([FakeEvent("error")])
    await _run_job(runner, store.get(jid), store)
    row = store.get(jid)
    assert row["run_count"] == 0
    assert row["fail_count"] == 1


@pytest.mark.asyncio
async def test_run_job_marks_failure_when_no_final(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(
        name="x", cron_expr="0 9 * * *", target_agent="main", prompt="go",
    )
    runner = StubRunner([FakeEvent("text")])
    await _run_job(runner, store.get(jid), store)
    assert store.get(jid)["fail_count"] == 1


# ---------------- _tick_once ----------------

@pytest.mark.asyncio
async def test_tick_fires_due_job_and_updates_next_run(tmp_db: Path):
    store = CronStore(tmp_db)
    fixed = _now_at("2026-04-27", 12, 0)
    jid = _seed_due(store, "due-job", fixed)
    original_next = store.get(jid)["next_run_at"]

    runner = StubRunner([FakeEvent("final")])
    registry = StubRegistry(runner)
    tasks = await _tick_once(store, registry, StubLedger(), now_fn=lambda: fixed)
    await asyncio.gather(*tasks)

    assert len(tasks) == 1
    assert registry.keys == [(f"cron:{jid}", "main")]
    assert runner.calls == [("do due-job", f"cron:{jid}")]
    assert store.get(jid)["next_run_at"] != original_next
    assert store.get(jid)["run_count"] == 1


@pytest.mark.asyncio
async def test_tick_does_not_fire_disabled(tmp_db: Path):
    store = CronStore(tmp_db)
    fixed = _now_at("2026-04-27", 12, 0)
    jid = _seed_due(store, "off", fixed)
    store.set_enabled(jid, False)

    runner = StubRunner([FakeEvent("final")])
    tasks = await _tick_once(
        store, StubRegistry(runner), StubLedger(), now_fn=lambda: fixed
    )
    assert tasks == []


@pytest.mark.asyncio
async def test_tick_caps_due_batch_at_max_concurrent(tmp_db: Path):
    """5 jobs due, MAX_CONCURRENT=2, active=0 → exactly 2 fire."""
    store = CronStore(tmp_db)
    fixed = _now_at("2026-04-27", 12, 0)
    for i in range(5):
        _seed_due(store, f"j{i}", fixed)

    runner = StubRunner([FakeEvent("final")])
    with patch("src.cron_scheduler.CRON_MAX_CONCURRENT", 2):
        tasks = await _tick_once(
            store, StubRegistry(runner), StubLedger(), now_fn=lambda: fixed
        )
    await asyncio.gather(*tasks)
    assert len(tasks) == 2


@pytest.mark.asyncio
async def test_tick_counts_queued_plus_running(tmp_db: Path):
    """active = queued + running. cap=3, 2 queued + 1 running → slots=0."""
    store = CronStore(tmp_db)
    fixed = _now_at("2026-04-27", 12, 0)
    _seed_due(store, "x", fixed)

    runner = StubRunner([FakeEvent("final")])
    ledger = StubLedger(
        queued=[{"id": 1}, {"id": 2}], running=[{"id": 3}],
    )
    with patch("src.cron_scheduler.CRON_MAX_CONCURRENT", 3), \
         patch("src.cron_scheduler.CRON_AUTO_PAUSE", False):
        tasks = await _tick_once(
            store, StubRegistry(runner), ledger, now_fn=lambda: fixed
        )
    assert tasks == []


@pytest.mark.asyncio
async def test_tick_auto_pause_skips_when_at_cap(tmp_db: Path):
    """auto_pause stops scheduling when active >= cap."""
    store = CronStore(tmp_db)
    fixed = _now_at("2026-04-27", 12, 0)
    _seed_due(store, "x", fixed)

    runner = StubRunner([FakeEvent("final")])
    ledger = StubLedger(
        running=[{"id": 1}, {"id": 2}, {"id": 3}],
    )
    with patch("src.cron_scheduler.CRON_MAX_CONCURRENT", 3), \
         patch("src.cron_scheduler.CRON_AUTO_PAUSE", True):
        tasks = await _tick_once(
            store, StubRegistry(runner), ledger, now_fn=lambda: fixed
        )
    assert tasks == []


@pytest.mark.asyncio
async def test_tick_no_due_jobs_returns_empty(tmp_db: Path):
    """No due jobs → no work, no ledger query needed."""
    store = CronStore(tmp_db)
    fixed = _now_at("2026-04-27", 12, 0)
    # Create a job whose next_run_at is in the future (default behavior).
    store.create(
        name="future", cron_expr="0 9 * * *", target_agent="main",
        prompt="go", now_utc=fixed,
    )
    runner = StubRunner([FakeEvent("final")])
    tasks = await _tick_once(
        store, StubRegistry(runner), StubLedger(), now_fn=lambda: fixed
    )
    assert tasks == []


# ---------------- _wrap_prompt_for_destination ----------------

def test_wrap_prompt_returns_raw_when_destination_is_null():
    job = {"id": 1, "prompt": "do the thing", "destination_channel_id": None}
    assert _wrap_prompt_for_destination(job) == "do the thing"


def test_wrap_prompt_returns_raw_when_destination_missing():
    """Defensive: a job dict that doesn't include the key still works."""
    job = {"id": 1, "prompt": "do the thing"}
    assert _wrap_prompt_for_destination(job) == "do the thing"


def test_wrap_prompt_includes_prompt_and_post_to_channel_when_destination_set():
    job = {
        "id": 1, "prompt": "remind me to drink water",
        "destination_channel_id": "123456789012345678",
    }
    wrapped = _wrap_prompt_for_destination(job)
    # Reminder text is fenced between --- markers (visually distinct so the
    # agent posts it verbatim rather than treating it as task framing).
    assert "---\nremind me to drink water\n---" in wrapped
    assert "post_to_channel" in wrapped
    assert "123456789012345678" in wrapped
    # Identity is hardcoded to main — Maine owns #inbox, Intel/Hermes may not.
    assert 'agent="main"' in wrapped
    # Verbatim directive is what stops Maine from elaborating.
    assert "VERBATIM" in wrapped


def test_wrap_prompt_reminder_style_drops_verbatim_directive():
    """style='reminder' must NOT carry the VERBATIM directive — that's the
    whole point of the flag. It should still fence the original text + name
    the destination + emit a post_to_channel call."""
    job = {
        "id": 1, "prompt": "take out the trash",
        "destination_channel_id": "123456789012345678",
        "style": "reminder",
    }
    wrapped = _wrap_prompt_for_destination(job)
    assert "---\ntake out the trash\n---" in wrapped
    assert "post_to_channel" in wrapped
    assert "123456789012345678" in wrapped
    assert 'agent="main"' in wrapped
    assert "VERBATIM" not in wrapped
    # Sanity: the directive should mention emoji / flavor language so Maine
    # knows this is the lighten-up path, not the literal-post path.
    assert "emoji" in wrapped.lower()


def test_wrap_prompt_unknown_style_falls_back_to_verbatim():
    """Defensive: a row with a missing/unknown style key should behave like
    verbatim — never silently strip the directive on bad data."""
    job = {
        "id": 1, "prompt": "x",
        "destination_channel_id": "123456789012345678",
        "style": "garbage",
    }
    assert "VERBATIM" in _wrap_prompt_for_destination(job)


# ---------------- _run_job: prompt wrapping + oneshot disable ----------------

@pytest.mark.asyncio
async def test_run_job_uses_wrapped_prompt_when_destination_set(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(
        name="x", cron_expr="0 9 * * *", target_agent="main",
        prompt="check status",
        destination_channel_id="123456789012345678",
    )
    runner = StubRunner([FakeEvent("final")])
    await _run_job(runner, store.get(jid), store)
    assert len(runner.calls) == 1
    sent_prompt, _ = runner.calls[0]
    assert "---\ncheck status\n---" in sent_prompt
    assert "post_to_channel" in sent_prompt
    assert "123456789012345678" in sent_prompt


@pytest.mark.asyncio
async def test_run_job_uses_raw_prompt_when_destination_null(tmp_db: Path):
    """Legacy rows (e.g. morning-brief) keep posting via their embedded
    instruction; the scheduler must NOT prepend extra wrapping for them."""
    store = CronStore(tmp_db)
    jid = store.create(
        name="legacy", cron_expr="0 9 * * *", target_agent="main",
        prompt="run the morning brief and post_to_channel(...) yourself",
    )
    runner = StubRunner([FakeEvent("final")])
    await _run_job(runner, store.get(jid), store)
    sent_prompt, _ = runner.calls[0]
    assert sent_prompt == "run the morning brief and post_to_channel(...) yourself"


@pytest.mark.asyncio
async def test_run_job_oneshot_disables_after_fire(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(
        name="x", cron_expr="0 9 * * *", target_agent="main",
        prompt="go", oneshot=True,
    )
    assert store.get(jid)["enabled"] == 1
    runner = StubRunner([FakeEvent("final")])
    await _run_job(runner, store.get(jid), store)
    assert store.get(jid)["enabled"] == 0


@pytest.mark.asyncio
async def test_run_job_oneshot_disables_even_on_failure(tmp_db: Path):
    """Oneshot must disable regardless of run outcome — otherwise a failing
    one-time reminder would refire on every tick until manually disabled."""
    store = CronStore(tmp_db)
    jid = store.create(
        name="x", cron_expr="0 9 * * *", target_agent="main",
        prompt="go", oneshot=True,
    )
    runner = StubRunner([FakeEvent("error")])
    await _run_job(runner, store.get(jid), store)
    assert store.get(jid)["enabled"] == 0
    assert store.get(jid)["fail_count"] == 1


@pytest.mark.asyncio
async def test_run_job_non_oneshot_stays_enabled(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(
        name="x", cron_expr="0 9 * * *", target_agent="main", prompt="go",
    )
    runner = StubRunner([FakeEvent("final")])
    await _run_job(runner, store.get(jid), store)
    assert store.get(jid)["enabled"] == 1
