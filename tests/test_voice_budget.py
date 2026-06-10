"""Budget gate: per-day character ceiling for ElevenLabs TTS."""

from __future__ import annotations

from src import config
from src.voice import budget


def test_consume_within_budget_and_exact_fit(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_DAILY_CHAR_BUDGET", 100)
    assert budget.check_and_consume(60, db_path=tmp_db) is True
    assert budget.check_and_consume(40, db_path=tmp_db) is True  # exactly at cap
    assert budget.check_and_consume(1, db_path=tmp_db) is False  # over


def test_over_budget_attempt_consumes_nothing(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_DAILY_CHAR_BUDGET", 100)
    assert budget.check_and_consume(101, db_path=tmp_db) is False
    # the rejected attempt must not have burned any of the day's budget
    assert budget.check_and_consume(100, db_path=tmp_db) is True


def test_nonpositive_chars_rejected(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_DAILY_CHAR_BUDGET", 100)
    assert budget.check_and_consume(0, db_path=tmp_db) is False
    assert budget.check_and_consume(-5, db_path=tmp_db) is False
