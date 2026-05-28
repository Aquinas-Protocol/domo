"""Unit tests for src/store.py — Ledger lifecycle and queries."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.store import Ledger, SessionStore


def _setup(db: Path) -> tuple[SessionStore, Ledger]:
    store = SessionStore(db)
    ledger = Ledger(db)
    # FK requires a sessions row first.
    store.upsert("main", session_id=None, cwd=None, parent_key=None)
    store.upsert("research", session_id=None, cwd=None, parent_key="main")
    return store, ledger


def test_enqueue_creates_queued_row(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    lid = ledger.enqueue(
        discord_key="main", agent_name="main",
        persona_version="abc123", triggered_by="user",
    )
    rows = ledger.query()
    assert len(rows) == 1
    assert rows[0]["id"] == lid
    assert rows[0]["status"] == "queued"
    assert rows[0]["agent_name"] == "main"


def test_lifecycle_queued_running_completed(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    lid = ledger.enqueue(
        discord_key="main", agent_name="main",
        persona_version="abc", triggered_by="user",
    )
    ledger.mark_running(lid)
    ledger.mark_completed(lid, summary="done", claude_session_id="sess1", cost_usd=0.0123)

    rows = ledger.query(status="completed")
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "completed"
    assert r["summary"] == "done"
    assert r["cost_usd"] == 0.0123
    assert r["total_tokens"] is None
    assert r["started_at"] is not None
    assert r["completed_at"] is not None


def test_failed_lifecycle(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    lid = ledger.enqueue(
        discord_key="main", agent_name="main",
        persona_version="abc", triggered_by="user",
    )
    ledger.mark_running(lid)
    ledger.mark_failed(lid, "BoomError: kaboom")
    rows = ledger.query(status="failed")
    assert len(rows) == 1
    assert "BoomError" in rows[0]["error_summary"]


def test_cancelled_lifecycle(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    lid = ledger.enqueue(
        discord_key="main", agent_name="main",
        persona_version="abc", triggered_by="user",
    )
    ledger.mark_cancelled(lid, "user requested")
    rows = ledger.query(status="cancelled")
    assert len(rows) == 1
    assert "user requested" in rows[0]["error_summary"]


def test_parent_child_chain(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    parent = ledger.enqueue(
        discord_key="main", agent_name="main",
        persona_version="abc", triggered_by="user",
    )
    child = ledger.enqueue(
        discord_key="research", agent_name="research",
        persona_version="def", triggered_by="delegation:main",
        parent_ledger_id=parent,
    )
    ledger.mark_completed(parent, "parent done", "s1", 0.001)
    ledger.mark_completed(child, "child done", "s2", 0.002)

    rows = ledger.query()
    by_id = {r["id"]: r for r in rows}
    assert by_id[child]["parent_ledger_id"] == parent
    assert by_id[parent]["parent_ledger_id"] is None


def test_filter_by_agent(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    ledger.enqueue("main", "main", "v1", "user")
    ledger.enqueue("research", "research", "v2", "delegation:main")
    ledger.enqueue("research", "research", "v2", "user")

    main_rows = ledger.query(agent="main")
    research_rows = ledger.query(agent="research")
    assert len(main_rows) == 1
    assert len(research_rows) == 2


def test_status_filter_returns_running(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    lid_running = ledger.enqueue("main", "main", "v1", "user")
    ledger.mark_running(lid_running)
    lid_done = ledger.enqueue("main", "main", "v1", "user")
    ledger.mark_running(lid_done)
    ledger.mark_completed(lid_done, "ok", "s", 0.0)

    running = ledger.query(status="running")
    assert len(running) == 1
    assert running[0]["id"] == lid_running


def test_persona_version_recorded(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    ledger.enqueue("main", "main", "hash-aaa", "user")
    rows = ledger.query()
    assert rows[0]["persona_version"] is not None  # column persists


def test_enqueue_without_preexisting_session_row_fails(tmp_db: Path):
    """Regression: ledger.enqueue requires the sessions row to exist first
    (FK constraint). Documents the contract the runner has to honor by
    pre-upserting before its first enqueue.
    """
    ledger = Ledger(tmp_db)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.enqueue(
            discord_key="never_seen_runner_key",
            agent_name="research",
            persona_version="abc",
            triggered_by="user",
        )


def test_filter_by_triggered_by_prefix(tmp_db: Path):
    """Regression: triggered_by_prefix supports cron-only history queries.

    Both auto-fired (`cron:{id}`) and manual (`cron:{id}`) cron runs share the
    `cron:` prefix, so /api/cron/history can use a single LIKE clause.
    """
    _, ledger = _setup(tmp_db)
    ledger.enqueue("main", "main", "v1", triggered_by="user")
    ledger.enqueue("main", "main", "v1", triggered_by="cron:7")
    ledger.enqueue("main", "main", "v1", triggered_by="cron:42")
    ledger.enqueue("main", "main", "v1", triggered_by="delegation:main")

    cron_rows = ledger.query(triggered_by_prefix="cron:")
    assert len(cron_rows) == 2
    triggers = {r["triggered_by"] for r in cron_rows}
    assert triggers == {"cron:7", "cron:42"}

    user_rows = ledger.query(triggered_by_prefix="user")
    assert len(user_rows) == 1
    assert user_rows[0]["triggered_by"] == "user"


def test_usage_summary_resets_daily_cost_at_central_midnight(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    tz = ZoneInfo("America/Chicago")
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = (today_start - timedelta(minutes=15)).astimezone(timezone.utc).isoformat()
    today = (today_start + timedelta(minutes=15)).astimezone(timezone.utc).isoformat()

    old_id = ledger.enqueue("main", "main", "v1", triggered_by="user")
    ledger.mark_completed(old_id, "old", "sess-old", 5.0, 1_000)
    new_id = ledger.enqueue("main", "main", "v1", triggered_by="user")
    ledger.mark_completed(new_id, "new", "sess-new", 0.55, 2_000)

    with sqlite3.connect(tmp_db) as c:
        c.execute("UPDATE task_ledger SET created_at=? WHERE id=?", (yesterday, old_id))
        c.execute("UPDATE task_ledger SET created_at=? WHERE id=?", (today, new_id))

    summary = ledger.usage_summary()

    assert summary["daily_cost_usd"] == 0.55
    assert summary["daily_runs"] == 1
    assert summary["lifetime_cost_usd"] == 5.55
    assert summary["lifetime_tokens"] == 3_000
    assert summary["lifetime_token_rows"] == 2


def test_legacy_operator_renamed_to_main_on_migration(tmp_path: Path):
    """v1 -> v2 migration should rename a pre-existing 'operator' session row."""
    from src.store import connect, migrate, _migration_v1, _set_version

    db = tmp_path / "legacy.sqlite"
    # Apply v1 only, then insert legacy row.
    with connect(db) as c:
        _migration_v1(c)
        _set_version(c, 1)
        c.execute(
            "INSERT INTO sessions(discord_key, claude_session_id, created_at, last_used_at) "
            "VALUES('operator', 'sess-legacy', '2026-04-24', '2026-04-24')"
        )
    # Now run full migrate (will apply v2).
    migrate(db)
    with connect(db) as c:
        row = c.execute(
            "SELECT discord_key, claude_session_id FROM sessions WHERE claude_session_id='sess-legacy'"
        ).fetchone()
    assert row is not None
    assert row["discord_key"] == "main"
