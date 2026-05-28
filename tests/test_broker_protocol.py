"""Branch tests for broker.elevation_broker.handle_request.

The broker has one job: take a `request_id`, look up the canonical row,
re-validate it (status='approved', sha256 matches, not in deny list), spawn
the elevated subprocess, and report back. Each rejection branch and the
happy path is covered here. The pipe layer (`serve`, `_serve_one_connection`)
is Windows-specific and not exercised by these tests; it's small and lives
above this pure handler.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from broker import elevation_broker as broker
from src.elevation_store import ElevationStore


def _create_approved_row(
    store: ElevationStore,
    *,
    uuid: str = "u-1",
    command: str = "whoami",
    cwd: str | None = None,
    command_timeout_s: int = 30,
) -> tuple[str, str]:
    sha = hashlib.sha256(command.encode("utf-8")).hexdigest()
    store.create(
        uuid=uuid,
        reason="test",
        command=command,
        command_sha256=sha,
        cwd=cwd,
        requested_by="main",
        approval_timeout_s=120,
        command_timeout_s=command_timeout_s,
    )
    store.mark_approved(uuid, approved_by="user-77")
    return uuid, sha


# --------------------------- empty / unknown ---------------------------


async def test_missing_request_id_returns_broker_error(tmp_db: Path):
    store = ElevationStore(tmp_db)
    resp = await broker.handle_request({}, store)
    assert resp["status"] == "broker_error"
    assert "missing request_id" in resp["error"]


async def test_unknown_request_id_returns_broker_error(tmp_db: Path):
    store = ElevationStore(tmp_db)
    resp = await broker.handle_request({"request_id": "ghost"}, store)
    assert resp["status"] == "broker_error"
    assert "unknown request_id" in resp["error"]


# --------------------------- status check ---------------------------


async def test_pending_row_refused(tmp_db: Path):
    """The bot is supposed to flip pending → approved before calling the
    broker. If a pending row reaches the broker, refuse — the user hasn't
    approved this request yet."""
    store = ElevationStore(tmp_db)
    store.create(
        uuid="u-1", reason="r", command="whoami",
        command_sha256=hashlib.sha256(b"whoami").hexdigest(),
        cwd=None, requested_by="main",
        approval_timeout_s=60, command_timeout_s=60,
    )
    resp = await broker.handle_request({"request_id": "u-1"}, store)
    assert resp["status"] == "broker_error"
    assert "pending" in resp["error"]
    # Row remains pending; broker did not modify state.
    assert store.get("u-1")["status"] == "pending"


async def test_executed_row_refused(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_approved_row(store)
    # Pre-execute it to land at status='executed'.
    store.mark_executed("u-1", exit_code=0, stdout="", stderr="", error=None)
    resp = await broker.handle_request({"request_id": "u-1"}, store)
    assert resp["status"] == "broker_error"
    assert "executed" in resp["error"]


# --------------------------- sha256 check ---------------------------


async def test_sha_mismatch_refused_and_marked_broker_error(tmp_db: Path):
    """Direct SQLite tampering: someone (not the bot) edited `command`
    without updating `command_sha256`. Broker recomputes and refuses."""
    store = ElevationStore(tmp_db)
    _create_approved_row(store, command="whoami")
    # Tamper with the command directly via SQL.
    from src.store import connect
    with connect(tmp_db) as conn:
        conn.execute(
            "UPDATE elevation_requests SET command='format C:' WHERE uuid='u-1'"
        )
    resp = await broker.handle_request({"request_id": "u-1"}, store)
    assert resp["status"] == "broker_error"
    assert "sha256 mismatch" in resp["error"]
    row = store.get("u-1")
    assert row["status"] == "broker_error"
    assert row["error"] == "sha256_mismatch"


# --------------------------- deny list ---------------------------


async def test_deny_list_blocks_disk_format(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_approved_row(store, command="format D:")
    resp = await broker.handle_request({"request_id": "u-1"}, store)
    assert resp["status"] == "broker_error"
    assert "disk_format" in resp["error"]
    row = store.get("u-1")
    assert row["status"] == "broker_error"
    assert row["error"] == "deny_list:disk_format"


async def test_deny_list_blocks_windows_dir_removal(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_approved_row(store, command=r"Remove-Item -Recurse -Force C:\Windows")
    resp = await broker.handle_request({"request_id": "u-1"}, store)
    assert resp["status"] == "broker_error"
    assert "system_dir_removal" in resp["error"]


async def test_deny_list_blocks_system_hive_delete(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_approved_row(store, command=r"reg delete HKLM\SYSTEM\CurrentControlSet /f")
    resp = await broker.handle_request({"request_id": "u-1"}, store)
    assert resp["status"] == "broker_error"
    assert "system_hive_delete" in resp["error"]


# --------------------------- happy path with fake runner ---------------------------


async def test_success_runs_command_runner_and_marks_executed(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_approved_row(store, command="Write-Output hi", command_timeout_s=30)

    captured: dict[str, object] = {}

    async def fake_runner(command: str, cwd: str | None, timeout_s: int):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["timeout_s"] = timeout_s
        return 0, "hi\n", ""

    resp = await broker.handle_request(
        {"request_id": "u-1"}, store, command_runner=fake_runner
    )
    assert resp["status"] == "executed"
    assert resp["exit_code"] == 0
    assert resp["stdout"] == "hi\n"
    assert captured["command"] == "Write-Output hi"
    assert captured["timeout_s"] == 30
    row = store.get("u-1")
    assert row["status"] == "executed"
    assert row["exit_code"] == 0
    assert row["stdout"] == "hi\n"


async def test_runner_raises_marks_broker_error(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_approved_row(store)

    async def boom_runner(command, cwd, timeout_s):  # noqa: ARG001
        raise RuntimeError("spawn failed: file not found")

    resp = await broker.handle_request(
        {"request_id": "u-1"}, store, command_runner=boom_runner
    )
    assert resp["status"] == "broker_error"
    assert "spawn_error" in resp["error"]
    row = store.get("u-1")
    assert row["status"] == "broker_error"


async def test_runner_nonzero_exit_still_marks_executed(tmp_db: Path):
    """Nonzero exit is a *command* failure, not a broker failure. The row
    lands at 'executed' with the captured exit_code; the bot decides how to
    surface that to Maine."""
    store = ElevationStore(tmp_db)
    _create_approved_row(store)

    async def fail_runner(command, cwd, timeout_s):  # noqa: ARG001
        return 5, "", "ACCESS DENIED"

    resp = await broker.handle_request(
        {"request_id": "u-1"}, store, command_runner=fail_runner
    )
    assert resp["status"] == "executed"
    assert resp["exit_code"] == 5
    assert resp["stderr"] == "ACCESS DENIED"
    row = store.get("u-1")
    assert row["status"] == "executed"
    assert row["error"] is None


# --------------------------- output truncation ---------------------------


async def test_large_output_is_truncated(tmp_db: Path):
    store = ElevationStore(tmp_db)
    _create_approved_row(store)
    big = "x" * 50000

    async def big_runner(command, cwd, timeout_s):  # noqa: ARG001
        return 0, big, ""

    resp = await broker.handle_request(
        {"request_id": "u-1"}, store, command_runner=big_runner
    )
    assert resp["status"] == "executed"
    assert len(resp["stdout"].encode("utf-8")) <= broker.MAX_OUTPUT_BYTES + 200
    assert "truncated" in resp["stdout"]


# --------------------------- deny pattern unit ---------------------------


def test_check_deny_list_returns_label_or_none():
    assert broker._check_deny_list("whoami") is None
    assert broker._check_deny_list("format c:") == "disk_format"
    assert broker._check_deny_list("format    Z:") == "disk_format"
    assert broker._check_deny_list("Remove-Item -Force C:\\Windows\\System32") == "system_dir_removal"
    assert broker._check_deny_list(r"reg delete HKLM\SYSTEM\foo") == "system_hive_delete"
