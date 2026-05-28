"""Unit tests for src/cron_store.py."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.cron_store import CronStore, _compute_next_run


def _job(**overrides):
    base = dict(
        name="daily-standup",
        cron_expr="0 9 * * *",
        target_agent="main",
        prompt="Run morning standup",
        description="agents check in",
        created_by="user",
    )
    base.update(overrides)
    return base


def test_create_returns_id_and_persists(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job())
    assert isinstance(jid, int) and jid > 0
    row = store.get(jid)
    assert row is not None
    assert row["name"] == "daily-standup"
    assert row["target_agent"] == "main"
    assert row["enabled"] == 1
    assert row["run_count"] == 0
    assert row["fail_count"] == 0
    assert row["next_run_at"] is not None


def test_create_computes_future_next_run(tmp_db: Path):
    store = CronStore(tmp_db)
    fixed = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    jid = store.create(**_job(cron_expr="0 9 * * *"), now_utc=fixed)
    next_dt = datetime.fromisoformat(store.get(jid)["next_run_at"])
    assert next_dt > fixed


def test_create_rejects_unknown_target_agent(tmp_db: Path):
    store = CronStore(tmp_db)
    with pytest.raises(ValueError, match="unknown target_agent"):
        store.create(**_job(target_agent="nobody"))


def test_create_rejects_invalid_cron_expr(tmp_db: Path):
    store = CronStore(tmp_db)
    with pytest.raises(ValueError, match="invalid cron_expr"):
        store.create(**_job(cron_expr="not a cron"))


def test_create_unique_name_constraint(tmp_db: Path):
    store = CronStore(tmp_db)
    store.create(**_job(name="dup-name"))
    with pytest.raises(sqlite3.IntegrityError):
        store.create(**_job(name="dup-name"))


def test_list_all_orders_newest_first(tmp_db: Path):
    store = CronStore(tmp_db)
    a = store.create(**_job(name="alpha"))
    b = store.create(**_job(name="beta"))
    rows = store.list_all()
    assert [r["id"] for r in rows] == [b, a]


def test_due_jobs_returns_only_enabled_overdue(tmp_db: Path):
    store = CronStore(tmp_db)
    fixed = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    overdue = store.create(**_job(name="overdue"), now_utc=fixed)
    store.update_next_run(overdue, "2026-04-27T09:00:00+00:00")
    future = store.create(**_job(name="future"), now_utc=fixed)
    store.update_next_run(future, "2030-01-01T00:00:00+00:00")
    disabled = store.create(**_job(name="disabled"), now_utc=fixed)
    store.update_next_run(disabled, "2026-04-27T09:00:00+00:00")
    store.set_enabled(disabled, False)

    due = store.due_jobs("2026-04-27T10:00:00+00:00")
    assert {r["name"] for r in due} == {"overdue"}


def test_set_enabled_toggles(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job())
    store.set_enabled(jid, False)
    assert store.get(jid)["enabled"] == 0
    store.set_enabled(jid, True)
    assert store.get(jid)["enabled"] == 1


def test_record_run_increments_counters_and_stamps_last_run(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job())
    assert store.get(jid)["last_run_at"] is None

    store.record_run(jid, success=True)
    row = store.get(jid)
    assert row["run_count"] == 1
    assert row["fail_count"] == 0
    assert row["last_run_at"] is not None

    store.record_run(jid, success=False)
    row = store.get(jid)
    assert row["run_count"] == 1
    assert row["fail_count"] == 1


def test_update_next_run_round_trips(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job())
    store.update_next_run(jid, "2030-01-01T00:00:00+00:00")
    assert store.get(jid)["next_run_at"] == "2030-01-01T00:00:00+00:00"


def test_update_recomputes_next_run_when_cron_changes(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job(cron_expr="0 9 * * *"))
    original = store.get(jid)["next_run_at"]
    store.update(jid, cron_expr="0 18 * * *")
    row = store.get(jid)
    assert row["next_run_at"] != original
    assert row["cron_expr"] == "0 18 * * *"


def test_update_validates_target_agent(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job())
    with pytest.raises(ValueError, match="unknown target_agent"):
        store.update(jid, target_agent="nobody")


def test_update_rejects_unknown_fields(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job())
    with pytest.raises(ValueError, match="unknown fields"):
        store.update(jid, secret="hax")


def test_update_validates_cron_expr(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job())
    with pytest.raises(ValueError, match="invalid cron_expr"):
        store.update(jid, cron_expr="bogus")


def test_delete_removes_row(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job())
    store.delete(jid)
    assert store.get(jid) is None


def test_compute_next_run_with_frozen_time():
    """_compute_next_run is callable in isolation with a fixed clock."""
    fixed = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
    next_iso = _compute_next_run("0 9 * * *", now_utc=fixed)
    assert datetime.fromisoformat(next_iso) > fixed


# ---------------- destination_channel_id + oneshot (v4) ----------------

def test_create_default_destination_is_null(tmp_db: Path):
    """Direct store.create() without a destination preserves NULL = legacy.

    Inbox-default resolution happens at the resolve_create_kwargs() layer,
    not in the store, so the morning-brief-style legacy rows stay untouched.
    """
    store = CronStore(tmp_db)
    jid = store.create(**_job())
    assert store.get(jid)["destination_channel_id"] is None


def test_create_with_destination_round_trips(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job(), destination_channel_id="123456789012345678")
    assert store.get(jid)["destination_channel_id"] == "123456789012345678"


def test_create_rejects_non_digit_destination(tmp_db: Path):
    store = CronStore(tmp_db)
    with pytest.raises(ValueError, match="digits-only"):
        store.create(**_job(), destination_channel_id="not-a-snowflake")


def test_create_oneshot_round_trips(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job(), oneshot=True)
    assert store.get(jid)["oneshot"] == 1


def test_create_default_oneshot_is_zero(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job())
    assert store.get(jid)["oneshot"] == 0


def test_create_with_explicit_next_run_at_uses_it(tmp_db: Path):
    store = CronStore(tmp_db)
    explicit = "2099-01-01T00:00:00+00:00"
    jid = store.create(**_job(), next_run_at=explicit)
    assert store.get(jid)["next_run_at"] == explicit


def test_update_destination_channel_id(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job())
    store.update(jid, destination_channel_id="123456789012345679")
    assert store.get(jid)["destination_channel_id"] == "123456789012345679"


def test_update_oneshot(tmp_db: Path):
    store = CronStore(tmp_db)
    jid = store.create(**_job())
    store.update(jid, oneshot=1)
    assert store.get(jid)["oneshot"] == 1


# ---------------- migration v4 ----------------

def test_migration_v4_adds_columns(tmp_db: Path):
    """conftest's tmp_db runs migrate(); confirm v4 columns exist."""
    import sqlite3 as _sqlite
    conn = _sqlite.connect(tmp_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cron_jobs)")}
    conn.close()
    assert "destination_channel_id" in cols
    assert "oneshot" in cols


def test_migration_v4_idempotent(tmp_db: Path):
    """Running migrate() again on a v4 DB is a no-op (no error)."""
    from src import store as _store
    _store.migrate(tmp_db)
    _store.migrate(tmp_db)  # second call must not raise
    import sqlite3 as _sqlite
    conn = _sqlite.connect(tmp_db)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == _store.CURRENT_VERSION


def test_migration_v4_recovers_from_partial_state(tmp_path: Path):
    """If a prior v4 attempt added one column then crashed before user_version
    was bumped (autocommit mode = each ALTER is its own transaction), the next
    boot's migration must add the missing column without failing on
    'duplicate column'."""
    import sqlite3 as _sqlite
    from src import store as _store

    db = tmp_path / "partial.sqlite"

    # Build a v3 schema by running the v1/v2/v3 migrations directly, then
    # simulate a partial v4: one column added but user_version still at 3.
    with _store.connect(db) as conn:
        _store._migration_v1(conn)
        _store._migration_v2(conn)
        _store._migration_v3(conn)
        conn.execute(
            "ALTER TABLE cron_jobs ADD COLUMN destination_channel_id TEXT"
        )
        _store._set_version(conn, 3)

    # Re-run migrate. v4 must add `oneshot` without crashing on the
    # already-present `destination_channel_id`.
    _store.migrate(db)

    with _sqlite.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cron_jobs)")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert "destination_channel_id" in cols
    assert "oneshot" in cols
    assert version == _store.CURRENT_VERSION
