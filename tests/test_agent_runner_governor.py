"""Governor wiring in AgentRunner.send.

The "blocked" path returns before any SDK work, so we can exercise it
without mocking ClaudeSDKClient — load enough cost into the ledger to push
the governor into HARD_PAUSE, flip CREDIT_GOVERNOR_ENFORCE on, then consume
the async generator and assert we hit the error event with no SDK calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import agent_runner
from src.agent_runner import AgentEvent, AgentRunner
from src.store import Ledger, SessionStore


def _setup(db: Path) -> tuple[SessionStore, Ledger]:
    store = SessionStore(db)
    ledger = Ledger(db)
    store.upsert("research", session_id=None, cwd=None, parent_key=None)
    return store, ledger


def _drop_cost(ledger: Ledger, amount: float) -> None:
    lid = ledger.enqueue(
        discord_key="research", agent_name="research",
        persona_version="t", triggered_by="test",
    )
    ledger.mark_completed(
        lid, summary="x", claude_session_id="s",
        cost_usd=amount,
        provider="claude", runtime="claude_sdk",
        billing_bucket="claude_agent_sdk",
    )


@pytest.mark.asyncio
async def test_governor_blocks_dispatch_when_enforced(tmp_db: Path, monkeypatch):
    store, ledger = _setup(tmp_db)
    # Burn through the entire credit so we're in HARD_PAUSE.
    _drop_cost(ledger, 99.0)
    monkeypatch.setattr(agent_runner, "CREDIT_GOVERNOR_ENFORCE", True)

    runner = AgentRunner(
        discord_key="research", agent_name="research",
        store=store, ledger=ledger,
    )
    events = [ev async for ev in runner.send("hello", triggered_by="test")]

    # First event should be the governor's error; no AssistantMessage events.
    assert events, "expected at least one event from a blocked dispatch"
    first = events[0]
    assert first.kind == "error"
    assert "credit governor" in first.text.lower()
    assert "hard-pause" in first.text.lower()

    # Ledger row exists and was marked failed (audit trail).
    rows = ledger.query(agent="research", status="failed")
    assert len(rows) == 1
    assert "credit_governor_block" in (rows[0]["error_summary"] or "")


@pytest.mark.asyncio
async def test_governor_observe_only_does_not_block(tmp_db: Path, monkeypatch):
    """With ENFORCE=false (default) a blocked decision should NOT short-circuit.

    We can't fully run the SDK in a test, so we assert that the runner moves
    past the governor check and into _ensure_client — i.e. the early-return
    branch is NOT taken. Easiest tell: no 'failed' row tagged with
    credit_governor_block, and the runner attempts to construct a client
    (which we let fail naturally without a real Claude environment).
    """
    store, ledger = _setup(tmp_db)
    _drop_cost(ledger, 99.0)
    monkeypatch.setattr(agent_runner, "CREDIT_GOVERNOR_ENFORCE", False)

    runner = AgentRunner(
        discord_key="research", agent_name="research",
        store=store, ledger=ledger,
    )
    # Consume the generator; whatever happens after the governor check, we
    # only care that we did NOT short-circuit on credit_governor_block.
    try:
        async for _ in runner.send("hello", triggered_by="test"):
            pass
    except Exception:
        pass  # any post-governor failure is fine

    blocked_rows = [
        r for r in ledger.query(agent="research", since_minutes=60, limit=10)
        if "credit_governor_block" in (r.get("error_summary") or "")
    ]
    assert blocked_rows == [], "observe-only mode should not write block rows"
