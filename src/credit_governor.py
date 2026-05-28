"""Anthropic Agent SDK credit governor.

Starting 2026-06-15 the Max 5x plan splits programmatic usage onto a separate
$100/month credit that resets with the billing cycle and does NOT roll over.
Extra Usage is intentionally disabled on this account, so when the credit is
exhausted the SDK simply stops responding — every cron, every Discord-driven
agent run, and every ``claude -p`` call dies until the cycle rolls.

This module reads the ledger and decides what behavior we should be in right
now. It does NOT wire itself into dispatch — that's a deliberate Phase 0c
choice: ship the math, surface it on the dashboard, smoke-test against real
data before any caller takes orders from it.

Tier semantics:

- ``NORMAL``: remaining > $30. No-op; full speed.
- ``WARN``: $15–30 remaining. Surface a banner, but no behavior change.
- ``SOFT_ROUTE``: $10–15 remaining. Heavy work (research, comms) should
  prefer ``mcp__specialist__query_gpt5``; non-essential crons should pause.
- ``REJECT``: $5–10 remaining. New Claude SDK starts are denied when their
  estimated worst-case cost would breach the floor. Anything in flight gets
  bounded via ``max_budget_usd``.
- ``HARD_PAUSE``: < $5 remaining. No new Claude SDK starts at all.
  ``claude -p`` paths also skip via preflight. Codex paths still allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.config import CREDIT_AGENT_SDK_USD
from src.store import Ledger


# Worst-case per-run cost estimate by model. Used by the REJECT tier to decide
# whether a fresh dispatch can fit inside the remaining headroom above the
# HARD_PAUSE floor. These are intentionally CONSERVATIVE upper bounds, not
# averages — a typical Opus deep-research run is ~$3, but the worst observed
# is closer to $7 (see ledger: research = $36 across few runs). If a model
# isn't in the map, default to the most expensive estimate so we fail safe.
_MODEL_WORST_CASE_USD = {
    "claude-opus-4-7": 5.00,
    "claude-opus-4-6": 5.00,
    "claude-sonnet-4-6": 1.00,
    "claude-sonnet-4-5": 1.00,
    "claude-haiku-4-5": 0.10,
}
_FALLBACK_WORST_CASE_USD = 5.00


def estimate_run_cost(model: str | None) -> float:
    """Conservative worst-case dollar estimate for one agent turn.

    Used by the governor's REJECT tier to bound dispatch decisions. The
    intent is "would the very worst case for this model breach our floor?",
    not "what will this run probably cost". Unknown models fail safe to the
    highest-tier estimate.
    """
    if not model:
        return _FALLBACK_WORST_CASE_USD
    return _MODEL_WORST_CASE_USD.get(model, _FALLBACK_WORST_CASE_USD)


class Tier(str, Enum):
    NORMAL = "normal"
    WARN = "warn"
    SOFT_ROUTE = "soft_route"
    REJECT = "reject"
    HARD_PAUSE = "hard_pause"


# Threshold boundaries are remaining-credit dollars, NOT consumed.
# Picked per the plan's 4-tier reserve model so a single Opus run cannot
# overshoot the next floor. Adjust here, not in callers, to retune.
_THRESHOLDS = (
    (30.0, Tier.NORMAL),
    (15.0, Tier.WARN),
    (10.0, Tier.SOFT_ROUTE),
    (5.0, Tier.REJECT),
)


@dataclass(frozen=True)
class CreditDecision:
    """One snapshot of the governor's state.

    Callers consume two things: ``allow_start`` (the boolean dispatch gate)
    and ``suggested_max_budget_usd`` (a per-run cap that keeps any in-flight
    work from breaching the floor). The other fields are for dashboards and
    logs — ``tier``/``remaining_credit``/``message`` together explain *why*
    the dispatch decision was made, which matters when a cron silently
    skips.
    """

    tier: Tier
    remaining_credit_usd: float
    consumed_credit_usd: float
    total_credit_usd: float
    cycle_start: str
    message: str
    allow_start: bool
    suggested_max_budget_usd: float | None


def _classify(remaining: float) -> Tier:
    for floor, tier in _THRESHOLDS:
        if remaining > floor:
            return tier
    return Tier.HARD_PAUSE


class CreditGovernor:
    """Read-only view of credit state. Does NOT mutate the ledger.

    Construct with a Ledger and optional overrides for testing. ``decide``
    is the only hot path; callers should not cache its result longer than
    a single dispatch decision because in-flight runs can move the needle.
    """

    def __init__(
        self,
        ledger: Ledger,
        *,
        total_credit_usd: float = CREDIT_AGENT_SDK_USD,
        bucket: str = "claude_agent_sdk",
    ):
        self.ledger = ledger
        self.total_credit_usd = float(total_credit_usd)
        self.bucket = bucket

    def as_dict(self, estimated_run_cost_usd: float = 0.0) -> dict[str, object]:
        """JSON-serializable snapshot. Mirrors ``decide`` for cross-process consumers."""
        d = self.decide(estimated_run_cost_usd)
        return {
            "tier": d.tier.value,
            "remaining_credit_usd": d.remaining_credit_usd,
            "consumed_credit_usd": d.consumed_credit_usd,
            "total_credit_usd": d.total_credit_usd,
            "cycle_start": d.cycle_start,
            "message": d.message,
            "allow_start": d.allow_start,
            "suggested_max_budget_usd": d.suggested_max_budget_usd,
            "bucket": self.bucket,
        }

    def decide(self, estimated_run_cost_usd: float = 0.0) -> CreditDecision:
        """Compute the current tier and the recommended dispatch action.

        ``estimated_run_cost_usd`` is the caller's worst-case projection for
        the run it's about to start. When the governor is in ``REJECT``, we
        block any start whose estimate would breach the $5 floor — otherwise
        a single Opus run easily overshoots. ``HARD_PAUSE`` blocks all
        starts regardless of estimate.
        """
        summary = self.ledger.billing_cycle_summary(bucket=self.bucket)
        consumed = float(summary.get("cost_usd") or 0.0)
        remaining = max(0.0, self.total_credit_usd - consumed)
        tier = _classify(remaining)
        cycle_start = str(summary.get("cycle_start") or "")

        allow_start = True
        suggested_max: float | None = None
        message = f"{tier.value}: ${remaining:.2f} remaining of ${self.total_credit_usd:.2f} (cycle {cycle_start})"

        if tier is Tier.SOFT_ROUTE:
            # Run still allowed, but cap it so a single run can't crash us
            # through the REJECT floor. $5 keeps two more Opus turns of room.
            suggested_max = max(0.5, remaining - 5.0)
            message = (
                f"soft-route: ${remaining:.2f} remaining; prefer query_gpt5 for heavy work "
                f"and pause non-essential crons (cycle {cycle_start})"
            )
        elif tier is Tier.REJECT:
            # Allow only runs whose worst-case fits inside what's left above
            # the hard-pause floor. Anything bigger gets denied.
            headroom = max(0.0, remaining - 5.0)
            allow_start = estimated_run_cost_usd <= headroom
            suggested_max = headroom if allow_start else None
            if not allow_start:
                message = (
                    f"reject: estimated cost ${estimated_run_cost_usd:.2f} exceeds "
                    f"headroom ${headroom:.2f} (${remaining:.2f} remaining; cycle {cycle_start})"
                )
            else:
                message = (
                    f"reject-bounded: ${remaining:.2f} remaining; capped at ${headroom:.2f} "
                    f"(cycle {cycle_start})"
                )
        elif tier is Tier.HARD_PAUSE:
            allow_start = False
            message = f"hard-pause: ${remaining:.2f} remaining of ${self.total_credit_usd:.2f}; no new Claude SDK starts (cycle {cycle_start})"

        return CreditDecision(
            tier=tier,
            remaining_credit_usd=remaining,
            consumed_credit_usd=consumed,
            total_credit_usd=self.total_credit_usd,
            cycle_start=cycle_start,
            message=message,
            allow_start=allow_start,
            suggested_max_budget_usd=suggested_max,
        )
