"""MCP tools agents call to create/manage cron jobs.

Registered on ALL agent runners via `RunnerRegistry.register(..., agents=None)`.
So Hermes / Intel / Maine can each create or trigger jobs from their Discord
channel, DMs, or future War Room conversations.

Cron-fired jobs reuse the existing `RunnerRegistry.get_or_create` + drained
`runner.send` path so they create normal ledger rows tagged
`triggered_by="cron:{id}"`. Manual `run_now` invocations use the same prefix
so they show up in the same /api/cron/history view.

Tool methods are exposed as plain async methods on `CronContext` for testing,
with the `@tool`-decorated functions in `as_mcp_server()` being thin wrappers.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import create_sdk_mcp_server, tool
from croniter import croniter

from src.config import AGENTS, CRON_MIN_INTERVAL_MINUTES, CRON_TZ, INBOX_CHANNEL_ID
from src.cron_scheduler import _run_job
from src.cron_store import CronStore

log = logging.getLogger(__name__)


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


_ALLOWED_STYLES = {"verbatim", "reminder"}


def resolve_create_kwargs(
    *,
    name: str,
    cron_expr: str,
    target_agent: str,
    prompt: str,
    description: str | None = None,
    destination_channel_id: str | None = None,
    oneshot: bool = False,
    at: str | None = None,
    agent_task: bool = False,
    fresh_session: bool = False,
    style: str = "verbatim",
) -> dict[str, Any]:
    """Validate + canonicalize create-job params for both MCP and HTTP entrypoints.

    If `at` is provided, the input is treated as a one-time reminder:
    cron_expr is synthesized from `at` (display-only — oneshot disables the
    job after it fires, so the placeholder never re-fires), `next_run_at` is
    overridden to the exact UTC ISO of `at`, and `oneshot` is forced True.

    Naive `at` timestamps are interpreted as `CRON_TZ` local time. Past `at`
    timestamps are rejected.

    `destination_channel_id` defaults to `INBOX_CHANNEL_ID` when omitted —
    this is the "all reminders default to #inbox" rule.

    `agent_task=True` opts out of the verbatim-post wrapper: the prompt runs
    as a real agent task and `destination_channel_id` must be NULL (the agent
    posts to whatever channel its prompt instructs). Combining
    `agent_task=True` with an explicit destination is rejected.

    Raises ValueError on invalid input. Returns kwargs to splat into
    `CronStore.create()` (excluding `created_by`, which the caller picks).
    """
    name = (name or "").strip()
    cron_expr = (cron_expr or "").strip()
    target_agent = (target_agent or "").strip()
    prompt = (prompt or "").strip()
    next_run_at_override: str | None = None

    at_str = (at or "").strip()
    if at_str:
        try:
            at_dt = datetime.fromisoformat(at_str)
        except ValueError as e:
            raise ValueError(f"could not parse at as ISO timestamp: {e}")
        if at_dt.tzinfo is None:
            at_dt = at_dt.replace(tzinfo=ZoneInfo(CRON_TZ))
        if at_dt.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ValueError("at must be in the future")
        at_local = at_dt.astimezone(ZoneInfo(CRON_TZ))
        cron_expr = (
            f"{at_local.minute} {at_local.hour} "
            f"{at_local.day} {at_local.month} *"
        )
        next_run_at_override = at_dt.astimezone(timezone.utc).isoformat()
        oneshot = True

    if not (name and cron_expr and target_agent and prompt):
        raise ValueError(
            "name, cron_expr (or at), target_agent, and prompt are required"
        )
    if target_agent not in AGENTS:
        raise ValueError(
            f"unknown target_agent {target_agent!r}; valid: {sorted(AGENTS)}"
        )
    if not croniter.is_valid(cron_expr):
        raise ValueError(f"invalid cron_expr {cron_expr!r}")

    if not oneshot:
        c = croniter(cron_expr, datetime.now(timezone.utc))
        first = c.get_next(datetime)
        second = c.get_next(datetime)
        delta_min = (second - first).total_seconds() / 60
        if delta_min < CRON_MIN_INTERVAL_MINUTES:
            raise ValueError(
                f"interval {int(delta_min)}m is below the minimum "
                f"{CRON_MIN_INTERVAL_MINUTES}m"
            )

    dest_in = (destination_channel_id or "").strip()
    if agent_task:
        if dest_in:
            raise ValueError(
                "agent_task=True is incompatible with an explicit "
                "destination_channel_id; the agent picks its own posting target"
            )
        dest = None
    else:
        dest = dest_in or str(INBOX_CHANNEL_ID)
        if not dest.isdigit():
            raise ValueError(
                f"destination_channel_id must be digits-only string, "
                f"got {destination_channel_id!r}"
            )

    style_in = (style or "verbatim").strip() or "verbatim"
    if style_in not in _ALLOWED_STYLES:
        raise ValueError(
            f"style must be one of {sorted(_ALLOWED_STYLES)}, got {style_in!r}"
        )
    if style_in == "reminder" and agent_task:
        raise ValueError(
            "style='reminder' is incompatible with agent_task=True; reminder "
            "flavoring only applies to verbatim-post jobs"
        )

    return dict(
        name=name,
        cron_expr=cron_expr,
        target_agent=target_agent,
        prompt=prompt,
        description=description,
        destination_channel_id=dest,
        oneshot=oneshot,
        next_run_at=next_run_at_override,
        fresh_session=bool(fresh_session),
        style=style_in,
    )


class CronContext:
    """Holds references the cron MCP tools need to act on store + registry."""

    def __init__(
        self,
        cron_store: CronStore,
        ledger: Any,
        registry: Any,
    ):
        self.cron_store = cron_store
        self.ledger = ledger
        self.registry = registry

    # --------------------------- tool logic ---------------------------

    async def create_job(
        self,
        *,
        name: str,
        cron_expr: str,
        target_agent: str,
        prompt: str,
        description: str | None = None,
        destination_channel_id: str | None = None,
        oneshot: bool = False,
        at: str | None = None,
        agent_task: bool = False,
        fresh_session: bool = False,
        style: str = "verbatim",
    ) -> dict[str, Any]:
        try:
            kwargs = resolve_create_kwargs(
                name=name, cron_expr=cron_expr, target_agent=target_agent,
                prompt=prompt, description=description,
                destination_channel_id=destination_channel_id,
                oneshot=oneshot, at=at, agent_task=agent_task,
                fresh_session=fresh_session, style=style,
            )
        except ValueError as e:
            return _err(str(e))
        try:
            jid = self.cron_store.create(**kwargs, created_by="agent")
        except sqlite3.IntegrityError:
            return _err(f"a cron job named {kwargs['name']!r} already exists")
        except ValueError as e:
            return _err(str(e))
        return _ok(
            f"Created cron job #{jid}: {kwargs['name']!r} on "
            f"'{kwargs['cron_expr']}' -> {kwargs['target_agent']}"
        )

    async def list_jobs(self, *, enabled_only: bool = False) -> dict[str, Any]:
        jobs = self.cron_store.list_all()
        if enabled_only:
            jobs = [j for j in jobs if j["enabled"]]
        if not jobs:
            return _ok("No cron jobs.")
        lines = [f"{len(jobs)} cron job(s):"]
        for j in jobs:
            state = "ENABLED" if j["enabled"] else "DISABLED"
            lines.append(
                f"  #{j['id']} [{j['target_agent']}] {j['name']!r} "
                f"'{j['cron_expr']}' ({state}, runs={j['run_count']}, "
                f"fails={j['fail_count']})"
            )
        return _ok("\n".join(lines))

    async def toggle_job(self, *, id: int, enabled: bool) -> dict[str, Any]:
        jid = int(id)
        if not self.cron_store.get(jid):
            return _err(f"no cron job #{jid}")
        self.cron_store.set_enabled(jid, bool(enabled))
        verb = "enabled" if enabled else "disabled"
        return _ok(f"Cron job #{jid} {verb}.")

    async def delete_job(self, *, id: int) -> dict[str, Any]:
        jid = int(id)
        if not self.cron_store.get(jid):
            return _err(f"no cron job #{jid}")
        self.cron_store.delete(jid)
        return _ok(f"Cron job #{jid} deleted.")

    async def run_now(self, *, id: int) -> dict[str, Any]:
        jid = int(id)
        job = self.cron_store.get(jid)
        if not job:
            return _err(f"no cron job #{jid}")
        try:
            runner = self.registry.get_or_create(
                f"cron:{job['id']}", agent_name=job["target_agent"]
            )
        except Exception as e:
            log.exception("run_now: failed to acquire runner for cron #%s", jid)
            return _err(f"failed to acquire runner: {e}")
        # Route through _run_job (not _run_to_completion) so manual triggers
        # share the same lifecycle as scheduled fires: prompt wrapping,
        # record_run, and oneshot auto-disable.
        asyncio.create_task(_run_job(runner, job, self.cron_store))
        return _ok(f"Triggered cron:{jid} ({job['name']!r}).")

    # --------------------------- MCP server ---------------------------

    def as_mcp_server(self):
        ctx = self

        @tool(
            "create_cron_job",
            "Create a scheduled reminder OR agent task. Supply EITHER cron_expr "
            "(5-field cron, e.g. '0 9 * * *' for 09:00 daily, interpreted in "
            "CRON_TZ) for recurring jobs, OR `at` (ISO timestamp, e.g. "
            "'2026-04-29T15:00:00') for a one-time job. Naive `at` timestamps "
            "are read as CRON_TZ local; past times are rejected. When `at` is "
            "set, the job becomes a oneshot — it auto-disables after firing "
            "once. target_agent must be a configured agent key (main / research "
            "/ comms). DEFAULT MODE — REMINDER: result is posted verbatim to "
            "destination_channel_id (defaults to #inbox). destination_channel_id "
            "is a Discord snowflake passed as a STRING (e.g. "
            "\"123456789012345678\") — JSON numbers lose precision on 64-bit "
            "IDs. AGENT-TASK MODE — set agent_task=true to run the prompt as a "
            "real agent task (no verbatim wrapper). The agent invokes its own "
            "tools and posts wherever its prompt instructs. agent_task=true is "
            "incompatible with destination_channel_id. Use this for jobs that "
            "must call MCP tools (e.g. pull email, summarize, then post). "
            "Minimum firing interval is enforced for recurring jobs only. "
            "Set fresh_session=true for jobs whose judgment must not carry "
            "context across firings (e.g. daily scanners) — each firing "
            "spawns a clean Claude session instead of resuming the prior one. "
            "style controls how the reminder text is posted: 'verbatim' "
            "(DEFAULT) posts the prompt exactly as-written and is right for "
            "anything structured (standups, status posts, code, anything the "
            "user wrote precisely). 'reminder' is for short personal "
            "reminders (\"take out the trash\", \"call Mark\") — the "
            "scheduler tells you at fire time to lightly flavor the text "
            "with a fitting emoji and a touch of warmth before posting. "
            "Pick 'reminder' when the user wrote a casual one-liner; pick "
            "'verbatim' for everything else. style='reminder' is "
            "incompatible with agent_task=True.",
            {"name": str, "cron_expr": str, "target_agent": str,
             "prompt": str, "description": str,
             "destination_channel_id": str, "oneshot": bool, "at": str,
             "agent_task": bool, "fresh_session": bool, "style": str},
        )
        async def create_cron_job(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.create_job(
                name=args.get("name", ""),
                cron_expr=args.get("cron_expr", ""),
                target_agent=args.get("target_agent", ""),
                prompt=args.get("prompt", ""),
                description=args.get("description"),
                destination_channel_id=args.get("destination_channel_id"),
                oneshot=bool(args.get("oneshot", False)),
                at=args.get("at"),
                agent_task=bool(args.get("agent_task", False)),
                fresh_session=bool(args.get("fresh_session", False)),
                style=args.get("style", "verbatim"),
            )

        @tool(
            "list_cron_jobs",
            "List all cron jobs with their schedules, target agents, and "
            "enabled state. Set enabled_only=true to only show active ones.",
            {"enabled_only": bool},
        )
        async def list_cron_jobs(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.list_jobs(
                enabled_only=bool(args.get("enabled_only", False))
            )

        @tool(
            "toggle_cron_job",
            "Enable or disable an existing cron job. Pass enabled=true to "
            "enable, enabled=false to disable. Disabled jobs don't fire on "
            "the schedule but can still be triggered manually.",
            {"id": int, "enabled": bool},
        )
        async def toggle_cron_job(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.toggle_job(
                id=int(args["id"]),
                enabled=bool(args.get("enabled", True)),
            )

        @tool(
            "delete_cron_job",
            "Permanently delete a cron job. The job's run history in the "
            "ledger is preserved (rows are tagged 'cron:{id}').",
            {"id": int},
        )
        async def delete_cron_job(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.delete_job(id=int(args["id"]))

        @tool(
            "run_cron_job_now",
            "Trigger an existing cron job to run immediately, in addition to "
            "its normal schedule. Returns immediately; the run completes in "
            "the background and shows up in the ledger as 'cron:{id}'.",
            {"id": int},
        )
        async def run_cron_job_now(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.run_now(id=int(args["id"]))

        return create_sdk_mcp_server(
            name="cron",
            version="0.1.0",
            tools=[
                create_cron_job,
                list_cron_jobs,
                toggle_cron_job,
                delete_cron_job,
                run_cron_job_now,
            ],
        )
