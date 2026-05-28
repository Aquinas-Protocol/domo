"""Tests for src/cron_tools.py — CronContext tool methods."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.config import CRON_TZ, INBOX_CHANNEL_ID
from src.cron_store import CronStore
from src.cron_tools import CronContext, resolve_create_kwargs


class StubRunner:
    async def send(self, prompt, *, triggered_by="user", **_):
        if False:  # pragma: no cover
            yield


class StubRegistry:
    def __init__(self):
        self.keys: list[tuple[str, str]] = []

    def get_or_create(self, key: str, agent_name: str) -> StubRunner:
        self.keys.append((key, agent_name))
        return StubRunner()


def _ctx(tmp_db: Path) -> tuple[CronContext, StubRegistry]:
    registry = StubRegistry()
    ctx = CronContext(
        cron_store=CronStore(tmp_db),
        ledger=None,
        registry=registry,
    )
    return ctx, registry


def _job(**overrides):
    base = dict(
        name="daily-check",
        cron_expr="0 9 * * *",
        target_agent="main",
        prompt="run morning check",
        description="optional",
    )
    base.update(overrides)
    return base


def _is_error(result: dict) -> bool:
    return bool(result.get("isError"))


def _text(result: dict) -> str:
    return result["content"][0]["text"]


# ---------------- create_job ----------------

@pytest.mark.asyncio
async def test_create_job_succeeds_and_persists(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    result = await ctx.create_job(**_job())
    assert not _is_error(result)
    assert "Created cron job" in _text(result)
    assert len(ctx.cron_store.list_all()) == 1


@pytest.mark.asyncio
async def test_create_job_marks_created_by_agent(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    await ctx.create_job(**_job())
    row = ctx.cron_store.list_all()[0]
    assert row["created_by"] == "agent"


@pytest.mark.asyncio
async def test_create_job_rejects_unknown_target_agent(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    result = await ctx.create_job(**_job(target_agent="ghost"))
    assert _is_error(result)
    assert "unknown target_agent" in _text(result)


@pytest.mark.asyncio
async def test_create_job_rejects_invalid_cron(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    result = await ctx.create_job(**_job(cron_expr="bogus"))
    assert _is_error(result)
    assert "invalid cron_expr" in _text(result)


@pytest.mark.asyncio
async def test_create_job_rejects_sub_minimum_interval(tmp_db: Path):
    """Every-minute cron is below the 5-minute floor."""
    ctx, _ = _ctx(tmp_db)
    result = await ctx.create_job(**_job(cron_expr="* * * * *"))
    assert _is_error(result)
    assert "below the minimum" in _text(result)


@pytest.mark.asyncio
async def test_create_job_rejects_missing_required(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    result = await ctx.create_job(
        name="", cron_expr="0 9 * * *", target_agent="main", prompt="x",
    )
    assert _is_error(result)


@pytest.mark.asyncio
async def test_create_job_rejects_duplicate_name(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    assert not _is_error(await ctx.create_job(**_job(name="dup")))
    result = await ctx.create_job(**_job(name="dup"))
    assert _is_error(result)
    assert "already exists" in _text(result)


# ---------------- agent_task mode ----------------

@pytest.mark.asyncio
async def test_create_job_default_mode_persists_inbox_destination(tmp_db: Path):
    """Regression: default (reminder) mode still writes #inbox dest, not NULL."""
    ctx, _ = _ctx(tmp_db)
    await ctx.create_job(**_job())
    row = ctx.cron_store.list_all()[0]
    assert row["destination_channel_id"] == str(INBOX_CHANNEL_ID)


@pytest.mark.asyncio
async def test_create_job_agent_task_allows_null_destination(tmp_db: Path):
    """agent_task=True with no destination stores NULL — scheduler reads NULL
    as 'pass prompt verbatim, let the agent decide where to post'."""
    ctx, _ = _ctx(tmp_db)
    result = await ctx.create_job(**_job(name="digest"), agent_task=True)
    assert not _is_error(result), _text(result)
    row = ctx.cron_store.list_all()[0]
    assert row["destination_channel_id"] is None


@pytest.mark.asyncio
async def test_create_job_agent_task_rejects_explicit_destination(tmp_db: Path):
    """agent_task picks its own posting target — explicit dest is a contradiction."""
    ctx, _ = _ctx(tmp_db)
    result = await ctx.create_job(
        **_job(name="oops"),
        destination_channel_id="1234567890",
        agent_task=True,
    )
    assert _is_error(result)
    assert "agent_task=True is incompatible" in _text(result)


# ---------------- list_jobs ----------------

@pytest.mark.asyncio
async def test_list_jobs_empty(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    result = await ctx.list_jobs()
    assert not _is_error(result)
    assert "No cron jobs" in _text(result)


@pytest.mark.asyncio
async def test_list_jobs_returns_formatted_text(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    await ctx.create_job(**_job(name="alpha"))
    await ctx.create_job(**_job(name="beta"))
    result = await ctx.list_jobs()
    text = _text(result)
    assert "2 cron job" in text
    assert "alpha" in text
    assert "beta" in text


@pytest.mark.asyncio
async def test_list_jobs_enabled_only(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    await ctx.create_job(**_job(name="alpha"))
    await ctx.create_job(**_job(name="beta"))
    # Disable beta
    job_b = next(j for j in ctx.cron_store.list_all() if j["name"] == "beta")
    ctx.cron_store.set_enabled(job_b["id"], False)

    text = _text(await ctx.list_jobs(enabled_only=True))
    assert "alpha" in text
    assert "beta" not in text


# ---------------- toggle_job ----------------

@pytest.mark.asyncio
async def test_toggle_job_disables(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    await ctx.create_job(**_job())
    jid = ctx.cron_store.list_all()[0]["id"]
    result = await ctx.toggle_job(id=jid, enabled=False)
    assert not _is_error(result)
    assert ctx.cron_store.get(jid)["enabled"] == 0


@pytest.mark.asyncio
async def test_toggle_job_unknown_id(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    result = await ctx.toggle_job(id=9999, enabled=True)
    assert _is_error(result)


# ---------------- delete_job ----------------

@pytest.mark.asyncio
async def test_delete_job(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    await ctx.create_job(**_job())
    jid = ctx.cron_store.list_all()[0]["id"]
    result = await ctx.delete_job(id=jid)
    assert not _is_error(result)
    assert ctx.cron_store.get(jid) is None


@pytest.mark.asyncio
async def test_delete_job_unknown_id(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    result = await ctx.delete_job(id=9999)
    assert _is_error(result)


# ---------------- run_now ----------------

@pytest.mark.asyncio
async def test_run_now_acquires_runner_and_returns_immediately(tmp_db: Path):
    ctx, registry = _ctx(tmp_db)
    await ctx.create_job(**_job())
    jid = ctx.cron_store.list_all()[0]["id"]
    result = await ctx.run_now(id=jid)
    # Allow the fire-and-forget task a tick to hit the registry.
    await asyncio.sleep(0)
    assert not _is_error(result)
    assert "Triggered cron" in _text(result)
    assert registry.keys == [(f"cron:{jid}", "main")]


@pytest.mark.asyncio
async def test_run_now_unknown_id(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    result = await ctx.run_now(id=9999)
    assert _is_error(result)


# ---------------- as_mcp_server smoke ----------------

def test_as_mcp_server_constructs_without_error(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    server = ctx.as_mcp_server()
    # Just verify it returns a server object — structure is SDK-defined.
    assert server is not None


# ---------------- resolve_create_kwargs ----------------

def test_resolve_defaults_destination_to_inbox():
    kwargs = resolve_create_kwargs(
        name="x", cron_expr="0 9 * * *", target_agent="main", prompt="go",
    )
    assert kwargs["destination_channel_id"] == str(INBOX_CHANNEL_ID)


def test_resolve_explicit_destination_honored():
    kwargs = resolve_create_kwargs(
        name="x", cron_expr="0 9 * * *", target_agent="main", prompt="go",
        destination_channel_id="123456789012345679",
    )
    assert kwargs["destination_channel_id"] == "123456789012345679"


def test_resolve_rejects_non_digit_destination():
    with pytest.raises(ValueError, match="digits-only"):
        resolve_create_kwargs(
            name="x", cron_expr="0 9 * * *", target_agent="main", prompt="go",
            destination_channel_id="not-a-snowflake",
        )


def test_resolve_with_at_synthesizes_cron_expr_and_oneshot():
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    kwargs = resolve_create_kwargs(
        name="x", cron_expr="", target_agent="main", prompt="go",
        at=future.isoformat(),
    )
    # Synthesized cron_expr should be valid 5-field cron from the local fields.
    assert kwargs["oneshot"] is True
    assert kwargs["next_run_at"] == future.astimezone(timezone.utc).isoformat()
    parts = kwargs["cron_expr"].split()
    assert len(parts) == 5


def test_resolve_at_naive_treated_as_local_tz():
    """A naive ISO timestamp should be interpreted as CRON_TZ local time."""
    future_local = datetime.now(ZoneInfo(CRON_TZ)) + timedelta(hours=2)
    naive_iso = future_local.replace(tzinfo=None).isoformat()
    kwargs = resolve_create_kwargs(
        name="x", cron_expr="", target_agent="main", prompt="go", at=naive_iso,
    )
    expected_utc_iso = future_local.astimezone(timezone.utc).isoformat()
    assert kwargs["next_run_at"] == expected_utc_iso


def test_resolve_at_in_past_rejected():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError, match="must be in the future"):
        resolve_create_kwargs(
            name="x", cron_expr="", target_agent="main", prompt="go", at=past,
        )


def test_resolve_at_unparseable_rejected():
    with pytest.raises(ValueError, match="parse at"):
        resolve_create_kwargs(
            name="x", cron_expr="", target_agent="main", prompt="go",
            at="tomorrow at noon",
        )


def test_resolve_oneshot_skips_min_interval():
    """oneshot=True from `at` lets the synthesized cron_expr bypass the
    5-minute floor that would otherwise reject the placeholder."""
    soon = datetime.now(timezone.utc) + timedelta(minutes=2)
    kwargs = resolve_create_kwargs(
        name="x", cron_expr="", target_agent="main", prompt="go",
        at=soon.isoformat(),
    )
    assert kwargs["oneshot"] is True


def test_resolve_recurring_below_min_interval_rejected():
    with pytest.raises(ValueError, match="below the minimum"):
        resolve_create_kwargs(
            name="x", cron_expr="* * * * *", target_agent="main", prompt="go",
        )


def test_resolve_rejects_unknown_target_agent():
    with pytest.raises(ValueError, match="unknown target_agent"):
        resolve_create_kwargs(
            name="x", cron_expr="0 9 * * *", target_agent="ghost", prompt="go",
        )


def test_resolve_rejects_invalid_cron_expr():
    with pytest.raises(ValueError, match="invalid cron_expr"):
        resolve_create_kwargs(
            name="x", cron_expr="bogus", target_agent="main", prompt="go",
        )


def test_resolve_rejects_missing_required_when_no_at():
    with pytest.raises(ValueError, match="required"):
        resolve_create_kwargs(
            name="", cron_expr="0 9 * * *", target_agent="main", prompt="go",
        )


# ---------------- create_job: integration with new params ----------------

@pytest.mark.asyncio
async def test_create_job_default_destination_is_inbox(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    await ctx.create_job(**_job())
    row = ctx.cron_store.list_all()[0]
    assert row["destination_channel_id"] == str(INBOX_CHANNEL_ID)


@pytest.mark.asyncio
async def test_create_job_explicit_destination(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    await ctx.create_job(
        **_job(), destination_channel_id="123456789012345679",
    )
    row = ctx.cron_store.list_all()[0]
    assert row["destination_channel_id"] == "123456789012345679"


@pytest.mark.asyncio
async def test_create_job_with_at_makes_oneshot(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    result = await ctx.create_job(
        name="oven-check", cron_expr="", target_agent="main",
        prompt="check the oven", at=future.isoformat(),
    )
    assert not _is_error(result)
    row = ctx.cron_store.list_all()[0]
    assert row["oneshot"] == 1
    assert row["next_run_at"] == future.astimezone(timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_create_job_at_in_past_returns_error(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    result = await ctx.create_job(
        name="too-late", cron_expr="", target_agent="main",
        prompt="x", at=past,
    )
    assert _is_error(result)
    assert "future" in _text(result)


@pytest.mark.asyncio
async def test_create_job_default_fresh_session_is_false(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    await ctx.create_job(**_job())
    row = ctx.cron_store.list_all()[0]
    assert row["fresh_session"] == 0


@pytest.mark.asyncio
async def test_create_job_fresh_session_true_persists(tmp_db: Path):
    """MCP-path parity with the dashboard route: fresh_session=True must
    reach the cron_jobs row so the scheduler suffixes the runner key per
    firing instead of resuming the prior session."""
    ctx, _ = _ctx(tmp_db)
    await ctx.create_job(**_job(name="scanner"), fresh_session=True)
    row = ctx.cron_store.list_all()[0]
    assert row["fresh_session"] == 1


@pytest.mark.asyncio
async def test_create_job_default_style_is_verbatim(tmp_db: Path):
    """Backwards compat: jobs that don't specify style stay on the strict
    verbatim path so existing scheduled posts keep their literal-post
    contract."""
    ctx, _ = _ctx(tmp_db)
    await ctx.create_job(**_job())
    row = ctx.cron_store.list_all()[0]
    assert row["style"] == "verbatim"


@pytest.mark.asyncio
async def test_create_job_reminder_style_persists(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    await ctx.create_job(**_job(name="trash"), style="reminder")
    row = ctx.cron_store.list_all()[0]
    assert row["style"] == "reminder"


@pytest.mark.asyncio
async def test_create_job_rejects_unknown_style(tmp_db: Path):
    ctx, _ = _ctx(tmp_db)
    result = await ctx.create_job(**_job(name="bad"), style="sassy")
    assert _is_error(result)
    assert "style" in _text(result)


@pytest.mark.asyncio
async def test_create_job_rejects_reminder_style_with_agent_task(tmp_db: Path):
    """agent_task jobs pick their own posting target — reminder flavoring
    doesn't apply, so the combination is rejected rather than silently
    ignored."""
    ctx, _ = _ctx(tmp_db)
    result = await ctx.create_job(
        **_job(name="combo"), agent_task=True, style="reminder",
    )
    assert _is_error(result)
    assert "reminder" in _text(result).lower()


# ---------------- run_now goes through _run_job ----------------

@pytest.mark.asyncio
async def test_run_now_oneshot_auto_disables(tmp_db: Path):
    """run_now must route through _run_job so manual triggers honor the
    oneshot lifecycle. Otherwise a one-time reminder fired manually would
    re-fire on the next scheduled tick."""
    ctx, registry = _ctx(tmp_db)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    await ctx.create_job(
        name="manual-oneshot", cron_expr="", target_agent="main",
        prompt="x", at=future.isoformat(),
    )
    jid = ctx.cron_store.list_all()[0]["id"]
    assert ctx.cron_store.get(jid)["enabled"] == 1

    result = await ctx.run_now(id=jid)
    assert not _is_error(result)
    # Drain the fire-and-forget task to completion.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    assert ctx.cron_store.get(jid)["enabled"] == 0
