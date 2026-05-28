"""Branch tests for the request_admin_elevation MCP tool.

The tool fans out into a few distinct paths and we want each isolated:

- Pre-Discord rejections (vault-write precheck, timeout caps, empty fields):
  must short-circuit with `isError=True` BEFORE the embed is posted.
- Approval timeout: event never fires, row flips to `expired`, error returned.
- Deny path: button handler equivalent (mark_denied + set event) wakes the
  awaiter, which sees `denied` and returns the blocked-task error.
- Broker-unavailable path: approval lands, broker IPC raises FileNotFoundError,
  row flips to `broker_error`, sensible error returned.

We mock the bot/channel/message so no real Discord call fires. We monkeypatch
_talk_to_broker so no real named-pipe call fires either.
"""

from __future__ import annotations

import asyncio
import uuid as uuid_mod
from pathlib import Path
from typing import Any

import pytest

from src.config import AgentSpec
from src.elevation_store import ElevationStore
from src import elevation_tools


# --------------------------- fakes ---------------------------


class FakeMessage:
    def __init__(self, id_: int = 99):
        self.id = id_
        self.edits: list[dict[str, Any]] = []
        # Match the real discord.Message shape so _render_timeout_state can
        # read msg.embeds without blowing up. The tool stashes msg.jump_url too.
        self.embeds: list[Any] = []
        self.jump_url = f"https://discord.com/channels/0/0/{id_}"

    async def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


class FakeChannel:
    def __init__(self):
        self.sent: list[dict[str, Any]] = []
        self.messages: list[FakeMessage] = []

    async def send(self, **kwargs: Any) -> FakeMessage:
        self.sent.append(kwargs)
        msg = FakeMessage()
        self.messages.append(msg)
        return msg


class FakeBot:
    def __init__(self, channel: FakeChannel):
        self.channel = channel

    def get_channel(self, cid: int):  # noqa: ARG002
        return self.channel

    async def fetch_channel(self, cid: int):  # noqa: ARG002
        return self.channel


def _fake_main_spec() -> AgentSpec:
    return AgentSpec(
        name="main",
        display_name="Maine",
        persona_file=Path("/dev/null"),
        token="t",
        channel_id=12345,
        webhook_url=None,
        can_delegate=True,
        enabled_tools=None,
        model="claude-sonnet-4-6",
        dashboard_visible=True,
    )


def _make_ctx(monkeypatch: pytest.MonkeyPatch, tmp_db: Path):
    monkeypatch.setattr(
        elevation_tools, "AGENTS", {"main": _fake_main_spec()}
    )
    channel = FakeChannel()
    bot = FakeBot(channel)
    store = ElevationStore(tmp_db)
    ctx = elevation_tools.ElevationContext(bots={"main": bot}, store=store)
    return ctx, store, channel


def _patch_uuid(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Force a deterministic uuid so tests can drive the row directly."""
    fixed = uuid_mod.UUID(value)
    monkeypatch.setattr(elevation_tools._uuid_mod, "uuid4", lambda: fixed)


# --------------------------- pre-Discord rejections ---------------------------


async def test_empty_reason_returns_error(tmp_db: Path, monkeypatch):
    ctx, store, channel = _make_ctx(monkeypatch, tmp_db)
    result = await ctx._handle({
        "reason": "",
        "command": "whoami",
        "cwd": "",
        "approval_timeout_s": 60,
        "command_timeout_s": 60,
    })
    assert result.get("isError") is True
    assert "reason is required" in result["content"][0]["text"]
    assert channel.sent == []


async def test_empty_command_returns_error(tmp_db: Path, monkeypatch):
    ctx, store, channel = _make_ctx(monkeypatch, tmp_db)
    result = await ctx._handle({
        "reason": "x",
        "command": "   ",
        "cwd": "",
        "approval_timeout_s": 60,
        "command_timeout_s": 60,
    })
    assert result.get("isError") is True
    assert "command is required" in result["content"][0]["text"]
    assert channel.sent == []


async def test_timeout_cap_violation_rejected(tmp_db: Path, monkeypatch):
    """approval_timeout_s + command_timeout_s > MAX_TIMEOUT_TOTAL_S blocks
    before any embed is posted."""
    ctx, store, channel = _make_ctx(monkeypatch, tmp_db)
    # Each individual cap is 240; sum 240+240=480 fits under 540 cap.
    # Push approval to 241 to trip the per-field cap.
    result = await ctx._handle({
        "reason": "x", "command": "whoami", "cwd": "",
        "approval_timeout_s": 241, "command_timeout_s": 60,
    })
    assert result.get("isError") is True
    assert "exceeds cap" in result["content"][0]["text"]
    assert channel.sent == []


async def test_per_field_cap_violation_rejected(tmp_db: Path, monkeypatch):
    ctx, store, channel = _make_ctx(monkeypatch, tmp_db)
    # 240 + 240 = 480 — both fit under per-field cap (240). But with safety margin
    # cap of MAX_TIMEOUT_TOTAL_S (= RUN_TIMEOUT_SECONDS - 60 = 540), 480 is fine.
    # To trip the SUM cap specifically we need values within per-field but summing
    # over total. Per-field is 240 so the max possible sum is 480 — under 540.
    # Therefore the sum cap is unreachable today via legal per-field values; the
    # per-field cap is the binding constraint. This test documents that.
    result = await ctx._handle({
        "reason": "x", "command": "whoami", "cwd": "",
        "approval_timeout_s": 0, "command_timeout_s": 60,
    })
    assert result.get("isError") is True
    assert "must be positive" in result["content"][0]["text"]
    assert channel.sent == []


async def test_vault_write_precheck_blocks_remove_item(tmp_db: Path, monkeypatch):
    """The classic refusal: Remove-Item against raw/ never reaches Discord."""
    ctx, store, channel = _make_ctx(monkeypatch, tmp_db)
    result = await ctx._handle({
        "reason": "cleanup",
        "command": r"Remove-Item C:\Users\testuser\Documents\second-brain\raw\old.md",
        "cwd": "",
        "approval_timeout_s": 60,
        "command_timeout_s": 60,
    })
    assert result.get("isError") is True
    assert "vault" in result["content"][0]["text"].lower()
    assert "raw/" in result["content"][0]["text"] or "agents/" in result["content"][0]["text"]
    assert channel.sent == [], "precheck must short-circuit before posting"


async def test_vault_write_precheck_blocks_redirection_to_agents(tmp_db: Path, monkeypatch):
    ctx, store, channel = _make_ctx(monkeypatch, tmp_db)
    result = await ctx._handle({
        "reason": "test",
        "command": r"echo hello > C:\Users\testuser\Documents\second-brain\agents\rogue.md",
        "cwd": "",
        "approval_timeout_s": 60,
        "command_timeout_s": 60,
    })
    assert result.get("isError") is True
    assert channel.sent == []


async def test_no_main_channel_returns_error(tmp_db: Path, monkeypatch):
    """If the operator's channel isn't configured the tool refuses up front."""
    spec = _fake_main_spec()
    spec_no_channel = AgentSpec(
        name=spec.name, display_name=spec.display_name, persona_file=spec.persona_file,
        token=spec.token, channel_id=None, webhook_url=None,
        can_delegate=spec.can_delegate, enabled_tools=spec.enabled_tools,
        model=spec.model, dashboard_visible=spec.dashboard_visible,
    )
    monkeypatch.setattr(elevation_tools, "AGENTS", {"main": spec_no_channel})
    store = ElevationStore(tmp_db)
    ctx = elevation_tools.ElevationContext(bots={"main": FakeBot(FakeChannel())}, store=store)
    result = await ctx._handle({
        "reason": "x", "command": "whoami", "cwd": "",
        "approval_timeout_s": 60, "command_timeout_s": 60,
    })
    assert result.get("isError") is True
    assert "channel" in result["content"][0]["text"].lower()


# --------------------------- approval timeout ---------------------------


async def test_approval_timeout_marks_expired_and_returns_error(tmp_db: Path, monkeypatch):
    """No button click within approval_timeout_s → row flipped to expired."""
    ctx, store, channel = _make_ctx(monkeypatch, tmp_db)
    _patch_uuid(monkeypatch, "00000000-0000-0000-0000-00000000aaaa")
    result = await ctx._handle({
        "reason": "x", "command": "whoami", "cwd": "",
        "approval_timeout_s": 1, "command_timeout_s": 60,
    })
    assert result.get("isError") is True
    assert "timed out" in result["content"][0]["text"].lower()
    row = store.get("00000000-0000-0000-0000-00000000aaaa")
    assert row is not None
    assert row["status"] == "expired"
    # Embed was posted (otherwise we'd never reach the await).
    assert len(channel.sent) == 1


# --------------------------- denial path ---------------------------


async def test_denial_returns_blocked_error(tmp_db: Path, monkeypatch):
    ctx, store, channel = _make_ctx(monkeypatch, tmp_db)
    target = "00000000-0000-0000-0000-00000000bbbb"
    _patch_uuid(monkeypatch, target)

    async def deny_after_post():
        # Wait for the row to exist + the embed to be posted.
        for _ in range(50):
            if store.get(target) is not None:
                break
            await asyncio.sleep(0.01)
        # Simulate the button handler.
        store.mark_denied(target, approved_by="user-77")
        store.event_for(target).set()

    asyncio.create_task(deny_after_post())
    result = await ctx._handle({
        "reason": "x", "command": "whoami", "cwd": "",
        "approval_timeout_s": 5, "command_timeout_s": 60,
    })
    assert result.get("isError") is True
    assert "denied" in result["content"][0]["text"].lower()
    row = store.get(target)
    assert row["status"] == "denied"


# --------------------------- broker-unavailable path ---------------------------


async def test_approved_then_broker_unavailable(tmp_db: Path, monkeypatch):
    """Approve fires; tool tries to talk to the broker; pipe missing →
    row flipped to broker_error and clear error returned."""
    ctx, store, channel = _make_ctx(monkeypatch, tmp_db)
    target = "00000000-0000-0000-0000-00000000cccc"
    _patch_uuid(monkeypatch, target)

    async def approve_after_post():
        for _ in range(50):
            if store.get(target) is not None:
                break
            await asyncio.sleep(0.01)
        store.mark_approved(target, approved_by="user-77")
        store.event_for(target).set()

    async def fake_broker(uuid: str, command_timeout_s: int):  # noqa: ARG001
        raise FileNotFoundError("broker pipe not found (\\\\.\\pipe\\test)")

    monkeypatch.setattr(elevation_tools, "_talk_to_broker", fake_broker)

    asyncio.create_task(approve_after_post())
    result = await ctx._handle({
        "reason": "x", "command": "whoami", "cwd": "",
        "approval_timeout_s": 5, "command_timeout_s": 60,
    })
    assert result.get("isError") is True
    text = result["content"][0]["text"]
    assert "broker" in text.lower() and "not reachable" in text.lower()
    row = store.get(target)
    assert row["status"] == "broker_error"
    assert row["error"] == "broker_unavailable"


# --------------------------- approved + broker success ---------------------------


async def test_approved_then_broker_success_returns_output(tmp_db: Path, monkeypatch):
    ctx, store, channel = _make_ctx(monkeypatch, tmp_db)
    target = "00000000-0000-0000-0000-00000000dddd"
    _patch_uuid(monkeypatch, target)

    async def approve_after_post():
        for _ in range(50):
            if store.get(target) is not None:
                break
            await asyncio.sleep(0.01)
        store.mark_approved(target, approved_by="user-77")
        store.event_for(target).set()

    async def fake_broker(uuid: str, command_timeout_s: int):  # noqa: ARG001
        # In real life the broker would also write the row to executed; do that here
        # so the test reflects end-to-end state.
        store.mark_executed(uuid, exit_code=0, stdout="ok\n", stderr="", error=None)
        return {"status": "executed", "exit_code": 0, "stdout": "ok\n", "stderr": ""}

    monkeypatch.setattr(elevation_tools, "_talk_to_broker", fake_broker)

    asyncio.create_task(approve_after_post())
    result = await ctx._handle({
        "reason": "x", "command": "whoami", "cwd": "",
        "approval_timeout_s": 5, "command_timeout_s": 60,
    })
    assert result.get("isError") is None or result.get("isError") is False
    text = result["content"][0]["text"]
    assert "Exit 0" in text
    assert "ok" in text
    row = store.get(target)
    assert row["status"] == "executed"
    assert row["exit_code"] == 0
