"""Print Anthropic Agent SDK credit governor state as JSON; exit code is the tier.

Designed for cross-process consumers — Windows Task Scheduler PowerShell
scripts like ``nightly-ingest.ps1`` shell out here to decide whether a
``claude -p`` call should even start.

Exit codes encode the tier so callers can branch on ``$LASTEXITCODE`` without
parsing JSON:

  0 = NORMAL or WARN (proceed; WARN is informational)
  2 = SOFT_ROUTE     (prefer Codex; non-essential crons should pause)
  3 = REJECT         (deny new starts whose estimated cost won't fit)
  4 = HARD_PAUSE     (no Claude SDK starts at all this cycle)

Stdout is always a single-line JSON object matching
``CreditGovernor.as_dict()``. Stderr is silent on success.
"""

from __future__ import annotations

import argparse
import json
import sys

from src.config import CREDIT_AGENT_SDK_USD, SESSIONS_DB
from src.credit_governor import CreditGovernor, Tier
from src.store import Ledger, migrate

_TIER_EXIT = {
    Tier.NORMAL: 0,
    Tier.WARN: 0,
    Tier.SOFT_ROUTE: 2,
    Tier.REJECT: 3,
    Tier.HARD_PAUSE: 4,
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--estimate-usd", type=float, default=0.0,
        help="Estimated worst-case cost of the run that's about to start. "
             "Used by the REJECT tier to decide if there's enough headroom.",
    )
    p.add_argument(
        "--total-credit-usd", type=float, default=CREDIT_AGENT_SDK_USD,
        help=f"Override the monthly credit ceiling (default ${CREDIT_AGENT_SDK_USD:.2f}).",
    )
    args = p.parse_args()

    # Self-migrate the ledger before reading. PowerShell/cron consumers can
    # run before the bot has started for the day, so we cannot assume the
    # schema is current.
    migrate(SESSIONS_DB)

    gov = CreditGovernor(Ledger(), total_credit_usd=args.total_credit_usd)
    payload = gov.as_dict(estimated_run_cost_usd=args.estimate_usd)
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")

    return _TIER_EXIT.get(Tier(payload["tier"]), 0)


if __name__ == "__main__":
    sys.exit(main())
