"""Cron scheduler loop. Wakes every CRON_TICK_SECONDS, fires due jobs.

Cron-fired jobs go through the normal AgentRunner pipeline so they create
ledger rows tagged `triggered_by="cron:{id}"`. The history tab in the dashboard
queries the ledger with `triggered_by_prefix="cron:"`, picking up both
auto-fired and manually-triggered runs.

`AgentRunner.send()` is an async generator (NOT a coroutine), so cron paths
must wrap it with `_run_to_completion` before scheduling as a task.

Concurrency cap: count of active cron-tagged ledger rows (queued + running).
`runner.send` enqueues BEFORE taking its per-runner lock — so counting only
running rows would undercount and let scheduler over-fire.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from src.config import CRON_AUTO_PAUSE, CRON_MAX_CONCURRENT, CRON_TICK_SECONDS, CRON_TZ
from src.cron_store import CronStore, _compute_next_run
from src.store import count_active_background_runs

log = logging.getLogger(__name__)


async def _run_to_completion(
    runner: Any, prompt: str, triggered_by: str
) -> tuple[bool, bool]:
    """Drain runner.send. Returns (saw_final, saw_error)."""
    saw_final = False
    saw_error = False
    async for ev in runner.send(prompt, triggered_by=triggered_by):
        if ev.kind == "final":
            saw_final = True
        elif ev.kind == "error":
            saw_error = True
    return saw_final, saw_error


async def run_background_agent_task(
    *,
    registry: Any,
    runner_key: str,
    agent_name: str,
    prompt: str,
    triggered_by: str,
) -> tuple[bool, bool]:
    """Dispatch a one-shot agent task outside the cron path.

    Used by dashboard-triggered scans (and any future "kick off an agent
    without scheduling a cron" surface). Acquires the registry's background
    slot for the agent, gets-or-creates the runner under the given key, and
    drains ``runner.send`` per ``feedback_runner_send_async_gen.md``. Does
    NOT touch CronStore — that bookkeeping is intentionally scoped to
    ``_run_job`` so dashboard runs don't appear in the cron history view.

    Returns ``(saw_final, saw_error)`` so the caller can decide what to
    record on its own audit table (e.g. ``scan_runs``).
    """
    runner = registry.get_or_create(runner_key, agent_name=agent_name)
    async with registry.background_slot(agent_name):
        try:
            return await _run_to_completion(runner, prompt, triggered_by)
        except Exception:
            log.exception(
                "background agent task crashed (runner_key=%s, agent=%s)",
                runner_key, agent_name,
            )
            return False, True


def _wrap_prompt_for_destination(job: dict[str, Any]) -> str:
    """Build a fire-time prompt that posts the reminder text to Discord.

    Two posting styles:

    - 'verbatim' (default) — literal post, no editorial. Used for structured
      payloads (standups, status posts, code blocks) that must reach Discord
      exactly as written. The directive is deliberately strict because earlier
      versions invited the agent to compose a "reminder response" which
      produced multi-paragraph essays instead of relaying the literal text.

    - 'reminder' — short personal reminders ("take out the trash"). The
      scheduler asks Maine to lightly flavor the text (one fitting emoji + a
      touch of warmth) before posting. Short and clear, not a paragraph.

    Hardcoded `agent="main"` because Maine owns #inbox; Intel/Hermes may not be
    in the channel even if their cron job is the one running. Override-to-other
    channels still works because `dest` is parameterized off the row.

    NULL destination = legacy/unmanaged (e.g. morning-brief which embeds its
    own post_to_channel call). Run the prompt as-is regardless of style.
    """
    dest = job.get("destination_channel_id")
    if not dest:
        return job["prompt"]
    if job.get("style") == "reminder":
        return (
            f"This is a scheduled reminder. The user wrote the short reminder "
            f"text between the --- markers below. Post a lightly-flavored "
            f"version to Discord channel {dest}: keep it short (one line is "
            f"usually right), pick one fitting emoji, add a touch of warmth "
            f"or personality. Preserve the user's meaning — do NOT rewrite, "
            f"expand into a paragraph, add framing like \"Here's your "
            f"reminder:\", or add a confirmation message after posting.\n\n"
            f"---\n{job['prompt']}\n---\n\n"
            f"Call: post_to_channel(channel_id=\"{dest}\", "
            f"content=<your flavored version of the reminder>, agent=\"main\")"
        )
    return (
        f"This is a scheduled reminder. Post the text between the --- markers "
        f"below to Discord channel {dest}, VERBATIM. Do not paraphrase, "
        f"summarize, frame, editorialize, or add commentary. Post only the "
        f"reminder text itself — no prefix, no suffix, no confirmation, no "
        f"meta-explanation of how the routing worked.\n\n"
        f"---\n{job['prompt']}\n---\n\n"
        f"Call: post_to_channel(channel_id=\"{dest}\", "
        f"content=<the text between --- markers above, exactly as written>, "
        f"agent=\"main\")"
    )


async def _run_job(
    runner: Any,
    job: dict[str, Any],
    store: CronStore,
    registry: Any | None = None,
    runner_key: str | None = None,
) -> None:
    """Drive one cron job to completion, then record_run with the outcome."""
    saw_final = False
    saw_error = False
    prompt = _wrap_prompt_for_destination(job)

    async def drive() -> None:
        nonlocal saw_final, saw_error
        try:
            saw_final, saw_error = await _run_to_completion(
                runner, prompt, f"cron:{job['id']}"
            )
        except Exception:
            log.exception("cron job %s crashed", job["id"])

    if registry is not None:
        async with registry.background_slot(job["target_agent"]):
            await drive()
    else:
        await drive()

    try:
        store.record_run(job["id"], success=(saw_final and not saw_error))
        if job.get("oneshot"):
            store.set_enabled(job["id"], False)
    except Exception:
        log.exception("cron job %s: failed to record result", job["id"])


async def _tick_once(
    store: CronStore,
    registry: Any,
    ledger: Any,
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[asyncio.Task[None]]:
    """One scheduler tick. Returns the list of background tasks fired."""
    now = now_fn()
    due = store.due_jobs(now.isoformat())
    if not due:
        return []

    active = count_active_background_runs(ledger)

    if CRON_AUTO_PAUSE and active >= CRON_MAX_CONCURRENT:
        log.info("cron auto-paused: active=%d cap=%d", active, CRON_MAX_CONCURRENT)
        return []

    slots = max(0, CRON_MAX_CONCURRENT - active)
    if slots <= 0:
        return []

    tasks: list[asyncio.Task[None]] = []
    for job in due[:slots]:
        # Default: stable runner key per cron job, so the Claude session
        # resumes across firings (cheap context reuse).
        # fresh_session=1: suffix with firing timestamp so each firing gets a
        # new runner with no prior session_id. Used for jobs whose judgment
        # must not carry context across firings.
        if job.get("fresh_session"):
            ts_suffix = now.strftime("%Y%m%dT%H%M%S")
            runner_key = f"cron:{job['id']}:{ts_suffix}"
        else:
            runner_key = f"cron:{job['id']}"
        try:
            runner = registry.get_or_create(
                runner_key, agent_name=job["target_agent"]
            )
        except Exception:
            log.exception("cron job %s: failed to acquire runner", job["id"])
            continue
        tasks.append(
            asyncio.create_task(
                _run_job(runner, job, store, registry, runner_key=runner_key)
            )
        )
        try:
            next_iso = _compute_next_run(job["cron_expr"], now_utc=now)
            store.update_next_run(job["id"], next_iso)
        except Exception:
            log.exception(
                "cron job %s: failed to compute next_run", job["id"]
            )
    return tasks


async def scheduler_loop(
    stop: asyncio.Event,
    store: CronStore,
    registry: Any,
    ledger: Any,
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    tick_seconds: int = CRON_TICK_SECONDS,
) -> None:
    """Run cron ticks on this event loop until `stop` is set."""
    log.info(
        "cron scheduler started (tick=%ds, tz=%s, cap=%d, auto_pause=%s)",
        tick_seconds, CRON_TZ, CRON_MAX_CONCURRENT, CRON_AUTO_PAUSE,
    )
    background: set[asyncio.Task[None]] = set()
    while not stop.is_set():
        try:
            new_tasks = await _tick_once(store, registry, ledger, now_fn=now_fn)
            if new_tasks:
                log.info("cron scheduler tick fired %d job(s)", len(new_tasks))
                for t in new_tasks:
                    background.add(t)
                    t.add_done_callback(background.discard)
        except Exception:
            log.exception("cron scheduler tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass
    log.info("cron scheduler stopped (drain %d in-flight task(s))", len(background))
    if background:
        await asyncio.gather(*background, return_exceptions=True)
