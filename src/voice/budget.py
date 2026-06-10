"""Per-day character budget for ElevenLabs TTS.

A runaway ceiling, not a usage target: ~1 morning summary is ~900 chars
against a 2,000/day default. Consumes BEFORE the API call — a failed call
burns budget, because over-counting is the safe direction for a spend
ceiling. Deliberately not wired into task_ledger/credit_governor (Anthropic
spend); ElevenLabs is billed in its own subscription credits.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from src import config
from src.config import SESSIONS_DB
from src.store import connect

log = logging.getLogger(__name__)


def check_and_consume(chars: int, db_path: Path = SESSIONS_DB) -> bool:
    """Atomically consume ``chars`` from today's budget; False = over budget.

    The conditional UPDATE makes check+consume one statement, so the ceiling
    holds even if a second caller ever appears.
    """
    if chars <= 0:
        return False
    budget = config.ELEVENLABS_DAILY_CHAR_BUDGET
    today = datetime.now(timezone.utc).date().isoformat()
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO voice_tts_usage (date, chars_used) VALUES (?, 0)",
            (today,),
        )
        cur = conn.execute(
            "UPDATE voice_tts_usage SET chars_used = chars_used + ? "
            "WHERE date = ? AND chars_used + ? <= ?",
            (chars, today, chars, budget),
        )
        if cur.rowcount == 1:
            return True
    log.warning(
        "voice TTS daily budget exceeded: %d chars requested, budget %d",
        chars,
        budget,
    )
    return False
