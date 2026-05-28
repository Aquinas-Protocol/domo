"""Persistent + in-memory state for `request_admin_elevation`.

SQLite-backed status machine for elevation requests, plus an in-memory
`asyncio.Event` registry keyed by request uuid so the awaiting tool call and
the button-press handler can rendezvous.

Status machine:

    pending  ──CAS──>  approved   (user clicked Approve)
    pending  ──CAS──>  denied     (user clicked Deny)
    pending  ──CAS──>  expired    (approval_timeout_s elapsed; or boot sweep)
    approved ──CAS──>  executed   (broker ran the command; row carries result)
    approved ──CAS──>  broker_error (broker rejected — sha mismatch, deny-list,
                                     spawn failure, or command timeout)

Every CAS update returns True only if exactly one row was updated (the SQL
predicate includes the expected source status). Losers in a double-click race
return False and the caller should render an "already resolved" reply instead
of acting.

The in-memory `_events` dict lives on this class instance. The bot process
holds one ElevationStore that owns the events; the broker process holds its
own ElevationStore and never touches `_events` (it doesn't await on the bot
side of the CAS — it just reads SQLite).
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import SESSIONS_DB
from src.store import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


VALID_STATUSES = {
    "pending",
    "approved",
    "denied",
    "expired",
    "executed",
    "broker_error",
}


class ElevationStore:
    def __init__(self, db_path: Path = SESSIONS_DB):
        self.db_path = db_path
        self._events: dict[str, asyncio.Event] = {}

    # --------------------------- writes ---------------------------

    def create(
        self,
        *,
        uuid: str,
        reason: str,
        command: str,
        command_sha256: str,
        cwd: str | None,
        requested_by: str,
        approval_timeout_s: int,
        command_timeout_s: int,
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO elevation_requests (
                    uuid, reason, command, command_sha256, cwd,
                    requested_by, requested_at,
                    approval_timeout_s, command_timeout_s, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    uuid, reason, command, command_sha256, cwd,
                    requested_by, _now(),
                    int(approval_timeout_s), int(command_timeout_s),
                ),
            )

    def attach_message(self, uuid: str, *, channel_id: int, message_id: int) -> None:
        """Save the embed coordinates so persistent views can re-bind on restart."""
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE elevation_requests SET channel_id=?, message_id=? WHERE uuid=?",
                (int(channel_id), int(message_id), uuid),
            )

    def mark_approved(self, uuid: str, *, approved_by: str) -> bool:
        return self._cas_transition(
            uuid,
            from_status="pending",
            to_status="approved",
            extra_sets={"approved_by": approved_by, "approved_at": _now()},
        )

    def mark_denied(self, uuid: str, *, approved_by: str) -> bool:
        return self._cas_transition(
            uuid,
            from_status="pending",
            to_status="denied",
            extra_sets={"approved_by": approved_by, "approved_at": _now()},
        )

    def mark_timed_out(self, uuid: str) -> bool:
        return self._cas_transition(
            uuid,
            from_status="pending",
            to_status="expired",
            extra_sets={"timed_out_at": _now()},
        )

    def mark_executed(
        self,
        uuid: str,
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
        error: str | None,
    ) -> bool:
        to_status = "broker_error" if error else "executed"
        return self._cas_transition(
            uuid,
            from_status="approved",
            to_status=to_status,
            extra_sets={
                "exit_code": int(exit_code),
                "stdout": stdout,
                "stderr": stderr,
                "error": error,
                "executed_at": _now(),
            },
        )

    def expire_pending_on_boot(self) -> int:
        """Bulk-flip every still-`pending` row to `expired`. Run once at startup
        before opening Discord connections so any persistent-view click on a
        stale row resolves to "expired" rather than an attempted approval.
        """
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE elevation_requests "
                "SET status='expired', timed_out_at=? "
                "WHERE status='pending'",
                (_now(),),
            )
            return cur.rowcount or 0

    def _cas_transition(
        self,
        uuid: str,
        *,
        from_status: str,
        to_status: str,
        extra_sets: dict[str, Any],
    ) -> bool:
        if to_status not in VALID_STATUSES:
            raise ValueError(f"invalid target status: {to_status!r}")
        cols = ["status=?"] + [f"{k}=?" for k in extra_sets]
        params: list[Any] = [to_status] + list(extra_sets.values())
        params.extend([uuid, from_status])
        sql = (
            f"UPDATE elevation_requests SET {', '.join(cols)} "
            "WHERE uuid=? AND status=?"
        )
        with connect(self.db_path) as conn:
            cur = conn.execute(sql, params)
            return (cur.rowcount or 0) == 1

    # --------------------------- reads ---------------------------

    def get(self, uuid: str) -> sqlite3.Row | None:
        with connect(self.db_path) as conn:
            return conn.execute(
                "SELECT * FROM elevation_requests WHERE uuid=?",
                (uuid,),
            ).fetchone()

    def list_for_view_rebind(self, *, since_seconds: int = 86400) -> list[sqlite3.Row]:
        """Recent rows (any status) whose persistent view should still resolve.

        After a restart, button clicks on old embeds need to land on a handler
        that responds gracefully — even for `expired` / `denied` / `executed`
        rows — so the user sees "expired" instead of Discord's generic
        "interaction failed". 24h is generous; older embeds are unlikely to
        get clicked.
        """
        cutoff = (
            datetime.now(timezone.utc).timestamp() - max(0, int(since_seconds))
        )
        with connect(self.db_path) as conn:
            return [
                row
                for row in conn.execute(
                    "SELECT * FROM elevation_requests "
                    "WHERE channel_id IS NOT NULL AND message_id IS NOT NULL "
                    "ORDER BY requested_at DESC LIMIT 200"
                )
                if datetime.fromisoformat(row["requested_at"]).timestamp() >= cutoff
            ]

    # --------------------------- in-memory ---------------------------

    def event_for(self, uuid: str) -> asyncio.Event:
        """Idempotent: same `Event` returned for the same uuid until forgotten."""
        ev = self._events.get(uuid)
        if ev is None:
            ev = asyncio.Event()
            self._events[uuid] = ev
        return ev

    def forget_event(self, uuid: str) -> None:
        """Drop the in-memory event after the awaiter is done so the dict
        doesn't grow without bound. Safe no-op if absent."""
        self._events.pop(uuid, None)
