"""Cron job storage. Mirrors the Ledger pattern in src/store.py.

`next_run_at` is always set on `create()` — there's no NULL bootstrap.
`due_jobs()` filters strictly on `next_run_at <= now`. Cron expressions are
interpreted in `CRON_TZ` local time; `next_run_at` is stored as UTC ISO.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from src.config import AGENTS, CRON_TZ, SESSIONS_DB
from src.store import _now, connect


_ALLOWED_UPDATE_FIELDS = {
    "name", "description", "cron_expr", "target_agent", "prompt",
    "enabled", "next_run_at", "destination_channel_id", "oneshot",
    "fresh_session", "style",
}

_ALLOWED_STYLES = {"verbatim", "reminder"}


def _compute_next_run(cron_expr: str, now_utc: datetime | None = None) -> str:
    """Next firing time after now (UTC ISO). cron_expr interpreted in CRON_TZ."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    tz = ZoneInfo(CRON_TZ)
    now_local = now_utc.astimezone(tz)
    next_local = croniter(cron_expr, now_local).get_next(datetime)
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)
    return next_local.astimezone(timezone.utc).isoformat()


class CronStore:
    def __init__(self, db_path: Path = SESSIONS_DB):
        self.db_path = db_path

    def create(
        self,
        *,
        name: str,
        cron_expr: str,
        target_agent: str,
        prompt: str,
        description: str | None = None,
        created_by: str = "user",
        now_utc: datetime | None = None,
        destination_channel_id: str | None = None,
        oneshot: bool = False,
        next_run_at: str | None = None,
        fresh_session: bool = False,
        style: str = "verbatim",
    ) -> int:
        if target_agent not in AGENTS:
            raise ValueError(f"unknown target_agent: {target_agent!r}")
        if not croniter.is_valid(cron_expr):
            raise ValueError(f"invalid cron_expr: {cron_expr!r}")
        if destination_channel_id is not None and not destination_channel_id.isdigit():
            raise ValueError(
                f"destination_channel_id must be digits-only string, "
                f"got {destination_channel_id!r}"
            )
        if style not in _ALLOWED_STYLES:
            raise ValueError(
                f"style must be one of {sorted(_ALLOWED_STYLES)}, got {style!r}"
            )
        next_iso = next_run_at if next_run_at else _compute_next_run(
            cron_expr, now_utc=now_utc
        )
        with connect(self.db_path) as c:
            cur = c.execute(
                """
                INSERT INTO cron_jobs(
                    name, description, cron_expr, target_agent, prompt,
                    enabled, created_by, created_at, next_run_at,
                    run_count, fail_count,
                    destination_channel_id, oneshot, fresh_session, style
                )
                VALUES(?, ?, ?, ?, ?, 1, ?, ?, ?, 0, 0, ?, ?, ?, ?)
                """,
                (name, description, cron_expr, target_agent, prompt,
                 created_by, _now(), next_iso,
                 destination_channel_id, 1 if oneshot else 0,
                 1 if fresh_session else 0, style),
            )
            return cur.lastrowid

    def list_all(self) -> list[dict[str, Any]]:
        with connect(self.db_path) as c:
            rows = c.execute(
                "SELECT * FROM cron_jobs ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, job_id: int) -> dict[str, Any] | None:
        with connect(self.db_path) as c:
            row = c.execute(
                "SELECT * FROM cron_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
        return dict(row) if row else None

    def update(self, job_id: int, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get(job_id)
        bad = set(fields) - _ALLOWED_UPDATE_FIELDS
        if bad:
            raise ValueError(f"unknown fields: {sorted(bad)}")
        if "target_agent" in fields and fields["target_agent"] not in AGENTS:
            raise ValueError(f"unknown target_agent: {fields['target_agent']!r}")
        if "cron_expr" in fields:
            if not croniter.is_valid(fields["cron_expr"]):
                raise ValueError(f"invalid cron_expr: {fields['cron_expr']!r}")
            fields.setdefault("next_run_at", _compute_next_run(fields["cron_expr"]))
        if "style" in fields and fields["style"] not in _ALLOWED_STYLES:
            raise ValueError(
                f"style must be one of {sorted(_ALLOWED_STYLES)}, "
                f"got {fields['style']!r}"
            )
        sets = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values()) + [int(job_id)]
        with connect(self.db_path) as c:
            c.execute(f"UPDATE cron_jobs SET {sets} WHERE id=?", params)
        return self.get(job_id)

    def delete(self, job_id: int) -> None:
        with connect(self.db_path) as c:
            c.execute("DELETE FROM cron_jobs WHERE id=?", (int(job_id),))

    def set_enabled(self, job_id: int, enabled: bool) -> dict[str, Any] | None:
        return self.update(job_id, enabled=1 if enabled else 0)

    def record_run(self, job_id: int, success: bool) -> None:
        col = "run_count" if success else "fail_count"
        with connect(self.db_path) as c:
            c.execute(
                f"UPDATE cron_jobs SET {col} = {col} + 1, last_run_at=? "
                "WHERE id=?",
                (_now(), int(job_id)),
            )

    def update_next_run(self, job_id: int, next_iso: str) -> None:
        with connect(self.db_path) as c:
            c.execute(
                "UPDATE cron_jobs SET next_run_at=? WHERE id=?",
                (next_iso, int(job_id)),
            )

    def due_jobs(self, now_iso: str) -> list[dict[str, Any]]:
        with connect(self.db_path) as c:
            rows = c.execute(
                "SELECT * FROM cron_jobs WHERE enabled=1 AND next_run_at <= ? "
                "ORDER BY next_run_at ASC",
                (now_iso,),
            ).fetchall()
        return [dict(r) for r in rows]
