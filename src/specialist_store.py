"""Persistence for Codex specialist thread labels."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.config import SESSIONS_DB
from src.store import _now, connect


class SpecialistStore:
    """Per-call connection wrapper around specialist_threads."""

    def __init__(self, db_path: Path = SESSIONS_DB):
        self.db_path = db_path

    def get(self, label: str) -> dict[str, Any] | None:
        with connect(self.db_path) as c:
            row = c.execute(
                """
                SELECT label, provider, session_id, created_at, last_used_at, turn_count
                FROM specialist_threads
                WHERE label=?
                """,
                (label,),
            ).fetchone()
        return dict(row) if row else None

    def upsert(self, label: str, provider: str, session_id: str) -> None:
        now = _now()
        with connect(self.db_path) as c:
            c.execute(
                """
                INSERT INTO specialist_threads(
                    label, provider, session_id, created_at, last_used_at, turn_count
                )
                VALUES(?, ?, ?, ?, ?, 0)
                ON CONFLICT(label) DO UPDATE SET
                    provider = excluded.provider,
                    session_id = excluded.session_id,
                    last_used_at = excluded.last_used_at
                """,
                (label, provider, session_id, now, now),
            )

    def record_turn(self, label: str) -> None:
        with connect(self.db_path) as c:
            c.execute(
                """
                UPDATE specialist_threads
                SET turn_count=turn_count + 1, last_used_at=?
                WHERE label=?
                """,
                (_now(), label),
            )
