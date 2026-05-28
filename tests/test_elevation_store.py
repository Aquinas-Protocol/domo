"""Unit tests for src/elevation_store.py.

Covers the row lifecycle, CAS-guarded transitions (the double-click race),
the boot-time expire sweep, and the in-memory asyncio.Event registry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.elevation_store import ElevationStore
from src.store import connect


def _create_pending(store: ElevationStore, *, uuid: str = "u-1") -> str:
    store.create(
        uuid=uuid,
        reason="enable Windows Search",
        command="Set-Service -Name WSearch -StartupType Automatic",
        command_sha256="abc123",
        cwd=None,
        requested_by="main",
        approval_timeout_s=120,
        command_timeout_s=60,
    )
    return uuid


def test_create_inserts_pending_row(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_pending(store)
    row = store.get("u-1")
    assert row is not None
    assert row["status"] == "pending"
    assert row["reason"] == "enable Windows Search"
    assert row["command_sha256"] == "abc123"
    assert row["approval_timeout_s"] == 120
    assert row["command_timeout_s"] == 60
    assert row["approved_by"] is None
    assert row["approved_at"] is None
    assert row["executed_at"] is None


def test_attach_message_sets_channel_and_message_ids(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_pending(store)
    store.attach_message("u-1", channel_id=42, message_id=99)
    row = store.get("u-1")
    assert row["channel_id"] == 42
    assert row["message_id"] == 99


def test_mark_approved_succeeds_from_pending(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_pending(store)
    assert store.mark_approved("u-1", approved_by="user-77") is True
    row = store.get("u-1")
    assert row["status"] == "approved"
    assert row["approved_by"] == "user-77"
    assert row["approved_at"] is not None


def test_mark_denied_succeeds_from_pending(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_pending(store)
    assert store.mark_denied("u-1", approved_by="user-77") is True
    assert store.get("u-1")["status"] == "denied"


def test_mark_timed_out_succeeds_from_pending(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_pending(store)
    assert store.mark_timed_out("u-1") is True
    row = store.get("u-1")
    assert row["status"] == "expired"
    assert row["timed_out_at"] is not None


def test_cas_double_click_resolves_to_one_winner(tmp_db: Path):
    """Two Approve clicks landing in the same microsecond — only one wins.

    SQLite serializes writes through the single connection in `connect()`, so
    by the time the second `mark_approved` runs the row is already 'approved'
    and the WHERE clause excludes it. The loser must return False so the
    button handler can render an "already resolved" reply instead of acting.
    """
    store = ElevationStore(tmp_db)
    _create_pending(store)

    first = store.mark_approved("u-1", approved_by="click-1")
    second = store.mark_approved("u-1", approved_by="click-2")
    third = store.mark_denied("u-1", approved_by="click-3")  # too late

    assert (first, second, third) == (True, False, False)
    row = store.get("u-1")
    assert row["status"] == "approved"
    assert row["approved_by"] == "click-1"


def test_mark_approved_after_denied_does_nothing(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_pending(store)
    assert store.mark_denied("u-1", approved_by="x") is True
    # Subsequent approve must be a CAS no-op; row stays 'denied'.
    assert store.mark_approved("u-1", approved_by="y") is False
    assert store.get("u-1")["status"] == "denied"


def test_mark_executed_succeeds_only_from_approved(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_pending(store)

    # From 'pending', mark_executed must fail (the broker only sees 'approved' rows).
    assert (
        store.mark_executed("u-1", exit_code=0, stdout="x", stderr="", error=None)
        is False
    )
    assert store.get("u-1")["status"] == "pending"

    # Approve, then execute — should succeed and stamp the result columns.
    store.mark_approved("u-1", approved_by="me")
    assert (
        store.mark_executed("u-1", exit_code=0, stdout="hi", stderr="", error=None)
        is True
    )
    row = store.get("u-1")
    assert row["status"] == "executed"
    assert row["exit_code"] == 0
    assert row["stdout"] == "hi"
    assert row["executed_at"] is not None


def test_mark_executed_with_error_lands_in_broker_error(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_pending(store)
    store.mark_approved("u-1", approved_by="me")
    assert (
        store.mark_executed(
            "u-1", exit_code=-1, stdout="", stderr="boom", error="spawn_error:nope"
        )
        is True
    )
    row = store.get("u-1")
    assert row["status"] == "broker_error"
    assert row["error"] == "spawn_error:nope"
    assert row["stderr"] == "boom"


def test_expire_pending_on_boot_flips_only_pending(tmp_db: Path):
    """Mass-update sweep: only `pending` rows get flipped to `expired`. Already-
    `approved` / `denied` / `executed` rows are untouched (an `approved` row
    that survived a restart belongs to the operator to clean up manually)."""
    store = ElevationStore(tmp_db)

    # Three rows: pending, approved, denied.
    store.create(
        uuid="p", reason="r", command="c", command_sha256="s", cwd=None,
        requested_by="main", approval_timeout_s=60, command_timeout_s=60,
    )
    store.create(
        uuid="a", reason="r", command="c", command_sha256="s", cwd=None,
        requested_by="main", approval_timeout_s=60, command_timeout_s=60,
    )
    store.create(
        uuid="d", reason="r", command="c", command_sha256="s", cwd=None,
        requested_by="main", approval_timeout_s=60, command_timeout_s=60,
    )
    store.mark_approved("a", approved_by="me")
    store.mark_denied("d", approved_by="me")

    n = store.expire_pending_on_boot()
    assert n == 1
    assert store.get("p")["status"] == "expired"
    assert store.get("p")["timed_out_at"] is not None
    assert store.get("a")["status"] == "approved"
    assert store.get("d")["status"] == "denied"


def test_expire_pending_on_boot_no_pending_returns_zero(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_pending(store)
    store.mark_approved("u-1", approved_by="me")
    assert store.expire_pending_on_boot() == 0


def test_list_for_view_rebind_returns_only_rows_with_message_ids(tmp_db: Path):
    store = ElevationStore(tmp_db)
    store.create(
        uuid="bound", reason="r", command="c", command_sha256="s", cwd=None,
        requested_by="main", approval_timeout_s=60, command_timeout_s=60,
    )
    store.attach_message("bound", channel_id=1, message_id=2)
    store.create(
        uuid="unbound", reason="r", command="c", command_sha256="s", cwd=None,
        requested_by="main", approval_timeout_s=60, command_timeout_s=60,
    )
    rows = store.list_for_view_rebind()
    assert {r["uuid"] for r in rows} == {"bound"}


def test_event_for_returns_same_instance_per_uuid(tmp_db: Path):
    store = ElevationStore(tmp_db)
    a = store.event_for("u-1")
    b = store.event_for("u-1")
    c = store.event_for("u-2")
    assert a is b
    assert a is not c


def test_forget_event_drops_uuid(tmp_db: Path):
    store = ElevationStore(tmp_db)
    a = store.event_for("u-1")
    store.forget_event("u-1")
    b = store.event_for("u-1")
    assert a is not b


@pytest.mark.asyncio
async def test_event_set_by_one_coro_wakes_another(tmp_db: Path):
    """Sanity: the awaiting tool call rendezvous works when the event is set
    from another task — same shape as button-handler ↔ tool-call rendezvous."""
    store = ElevationStore(tmp_db)
    event = store.event_for("u-1")

    async def setter():
        await asyncio.sleep(0.01)
        store.event_for("u-1").set()

    asyncio.create_task(setter())
    await asyncio.wait_for(event.wait(), timeout=1.0)
