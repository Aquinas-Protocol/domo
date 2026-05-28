"""Unit tests for the CreditGovernor tier ladder.

These tests construct a Ledger over the standard ``tmp_db`` fixture, drop
``mark_completed`` rows tagged ``claude_agent_sdk`` at controlled cost
amounts, then assert the governor lands on the right tier. The bucket
boundaries here MUST stay in sync with _THRESHOLDS in src/credit_governor.py
— if a threshold moves, the test should fail until both sides agree.
"""

from __future__ import annotations

from pathlib import Path

from src.credit_governor import CreditDecision, CreditGovernor, Tier
from src.store import Ledger, SessionStore


def _setup(db: Path) -> tuple[SessionStore, Ledger]:
    store = SessionStore(db)
    ledger = Ledger(db)
    store.upsert("main", session_id=None, cwd=None, parent_key=None)
    return store, ledger


def _drop_cost(ledger: Ledger, amount: float, bucket: str = "claude_agent_sdk") -> int:
    lid = ledger.enqueue(
        discord_key="main", agent_name="main",
        persona_version="t", triggered_by="test",
    )
    ledger.mark_completed(
        lid, summary="x", claude_session_id="s",
        cost_usd=amount,
        provider="claude" if bucket == "claude_agent_sdk" else "codex",
        runtime="claude_sdk" if bucket == "claude_agent_sdk" else "codex_cli",
        billing_bucket=bucket,
    )
    return lid


def test_empty_ledger_is_normal(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    gov = CreditGovernor(ledger)
    d = gov.decide()
    assert d.tier is Tier.NORMAL
    assert d.allow_start is True
    assert d.remaining_credit_usd == 100.0


def test_warn_tier_below_30(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    _drop_cost(ledger, 75.0)  # $25 remaining
    d = CreditGovernor(ledger).decide()
    assert d.tier is Tier.WARN
    assert d.allow_start is True
    assert 24.9 <= d.remaining_credit_usd <= 25.1


def test_soft_route_tier(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    _drop_cost(ledger, 88.0)  # $12 remaining
    d = CreditGovernor(ledger).decide()
    assert d.tier is Tier.SOFT_ROUTE
    assert d.allow_start is True
    # SOFT_ROUTE caps the per-run budget so a single Opus run can't punch
    # through the $5 hard-pause floor.
    assert d.suggested_max_budget_usd is not None
    assert d.suggested_max_budget_usd <= d.remaining_credit_usd - 5.0 + 0.01


def test_reject_blocks_oversized_run(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    _drop_cost(ledger, 92.5)  # $7.50 remaining; headroom = $2.50
    d = CreditGovernor(ledger).decide(estimated_run_cost_usd=5.00)
    assert d.tier is Tier.REJECT
    assert d.allow_start is False


def test_reject_allows_small_run_bounded(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    _drop_cost(ledger, 92.5)  # $7.50 remaining; headroom = $2.50
    d = CreditGovernor(ledger).decide(estimated_run_cost_usd=1.00)
    assert d.tier is Tier.REJECT
    assert d.allow_start is True
    assert d.suggested_max_budget_usd is not None
    assert d.suggested_max_budget_usd <= 2.51


def test_hard_pause_below_5(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    _drop_cost(ledger, 96.0)  # $4 remaining
    d = CreditGovernor(ledger).decide()
    assert d.tier is Tier.HARD_PAUSE
    assert d.allow_start is False
    assert d.suggested_max_budget_usd is None


def test_codex_bucket_does_not_consume_claude_credit(tmp_db: Path):
    """Codex specialist costs must NOT pull Claude credit toward HARD_PAUSE."""
    _, ledger = _setup(tmp_db)
    _drop_cost(ledger, 50.0, bucket="codex_oauth")  # all on Codex
    d = CreditGovernor(ledger).decide()
    assert d.tier is Tier.NORMAL
    assert d.remaining_credit_usd == 100.0


def test_custom_total_credit(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    _drop_cost(ledger, 15.0)
    # Pro plan would be a $20 credit.
    d = CreditGovernor(ledger, total_credit_usd=20.0).decide()
    assert 4.9 <= d.remaining_credit_usd <= 5.1
    assert d.tier is Tier.HARD_PAUSE


def test_message_includes_cycle_start(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    d = CreditGovernor(ledger).decide()
    # cycle_start is YYYY-MM-DD and always present in the message.
    assert d.cycle_start
    assert d.cycle_start in d.message


def test_decision_is_immutable(tmp_db: Path):
    _, ledger = _setup(tmp_db)
    d = CreditGovernor(ledger).decide()
    # Frozen dataclass: any mutation should raise.
    try:
        d.tier = Tier.HARD_PAUSE  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("CreditDecision should be frozen")
