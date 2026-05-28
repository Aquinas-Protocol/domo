"""Multi-bot Discord entry: one Discord client per agent, shared registry.

Architecture:
- BotApp owns the shared RunnerRegistry, SessionStore, Ledger, http_session,
  background tasks (idle reaper, OAuth sanity check), and the dict of clients.
- AgentBot is a discord.Client subclass, one instance per configured agent.
  Each bot only handles messages for its own agent (DMs to itself, posts in
  its own channel, threads under its own channel).
- All clients share the same asyncio event loop via asyncio.gather.

Run: py -m src.bot
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
import discord
from claude_agent_sdk import ClaudeAgentOptions, query

from src.agent_runner import AgentEvent, AgentRunner, RunnerRegistry
from src.config import (
    AGENTS,
    BOT_CWD,
    CALENDAR_TOKEN_SECRET,
    CODEX_EXE_PATH,
    CRON_TZ,
    DASHBOARD_BIND_HOST,
    DASHBOARD_ENABLED,
    DASHBOARD_PORT,
    DATA_DIR,
    ICLOUD_ALLOWED_CALENDARS,
    ICLOUD_APP_PASSWORD,
    ICLOUD_APPLE_ID,
    ICLOUD_READONLY_CALENDARS,
    OAUTH_SANITY_CHECK_HOURS,
    OPERATOR_USER_ID,
    PERMISSION_MODE,
    RUNTIME_LOG,
    SETTING_SOURCES,
    AgentSpec,
)
from src.calendar_client import CalendarClient
from src.calendar_tools import (
    CalendarReadContext,
    CalendarWriteContext,
    _ListCalendarsCache,
)
from src.elevation_store import ElevationStore
from src.elevation_tools import ElevationContext
from src.elevation_view import ElevationView
from src.cron_scheduler import scheduler_loop as cron_scheduler_loop
from src.cron_store import CronStore
from src.cron_tools import CronContext
from src.discord_tools import DiscordContext
from src.specialist_store import SpecialistStore
from src.specialist_tools import SpecialistContext, codex_health_probe
from src.spawner import ThreadSpawnContext, intake_attachments
from src.store import Ledger, SessionStore, migrate
from src.video_tools import VideoContext

MAX_MSG = 2000
CHUNK_LIMIT = 1900
EDIT_DEBOUNCE_SECS = 1.0
SLASH_TRANSLATION_PREFIX = "!"

log = logging.getLogger("domo")


def _setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        RUNTIME_LOG, maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    # NSSM redirects stdout to a cp1252 pipe; force UTF-8 so emit() survives non-ASCII records.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[handler, stream])


def _chunk(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split text at <limit> chars without breaking fenced code blocks."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    fence_open = False
    while len(remaining) > limit:
        slice_end = limit
        cut = remaining.rfind("\n\n", 0, slice_end)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, slice_end)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, slice_end)
        if cut <= 0:
            cut = limit
        piece = remaining[:cut]

        fences_in_piece = piece.count("```")
        if (fences_in_piece + (1 if fence_open else 0)) % 2 == 1:
            piece = piece + "\n```"
            fence_open = not fence_open
        else:
            fence_open = (piece.count("```") % 2 == 1) ^ fence_open

        chunks.append(piece)
        next_start_prefix = "```\n" if fence_open else ""
        remaining = next_start_prefix + remaining[cut:].lstrip("\n ")

    if remaining:
        chunks.append(remaining)
    return chunks


def _translate_bang_to_slash(text: str) -> str:
    if text.startswith(SLASH_TRANSLATION_PREFIX) and len(text) > 1 and text[1].isalpha():
        return "/" + text[1:]
    return text


def _thread_name_from(content: str, attachments=None, limit: int = 80) -> str:
    """Derive a Discord thread name from a message's content/attachments.

    Discord caps thread names at 100 chars; 80 leaves slack and avoids
    awkward mid-word cuts. Attachment-only messages use the first few
    filenames so the channel-level thread list stays scannable.
    """
    cleaned = " ".join((content or "").split())
    if cleaned:
        return cleaned[:limit].rstrip()
    if attachments:
        names = ", ".join(a.filename for a in list(attachments)[:3])
        derived = f"Attachment: {names}"[:limit].rstrip()
        return derived or "Ask"
    return "Ask"


# ============================ AgentBot ============================

class AgentBot(discord.Client):
    """One Discord client tied to one agent. Sees only messages directed at
    its agent: DMs to itself, posts in its channel, threads under its channel."""

    def __init__(self, agent_name: str, app: "BotApp"):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        intents.dm_messages = True
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.agent_name = agent_name
        self.app = app

    @property
    def spec(self) -> AgentSpec:
        return AGENTS[self.agent_name]

    async def on_ready(self) -> None:
        log.info(
            "[%s] logged in as %s (id=%s) — channel=%s",
            self.agent_name, self.user, self.user.id if self.user else "?", self.spec.channel_id,
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id is not None:
            return
        if OPERATOR_USER_ID is None or message.author.id != OPERATOR_USER_ID:
            return

        runner_key = self._runner_key_for(message)
        if runner_key is None:
            return

        # Backpressure check before constructing/queueing the runner. Busy
        # notices stay in the channel so a rejected ask doesn't leave behind
        # an empty thread.
        if self.app.registry.is_busy(self.agent_name):
            try:
                await message.channel.send(
                    f"⏳ {self.spec.display_name} is busy (queue depth at limit). Try again in a minute."
                )
            except discord.HTTPException:
                pass
            return

        runner_key, send_target = await self._maybe_open_thread(message, runner_key)

        runner = self.app.registry.get_or_create(runner_key, agent_name=self.agent_name)

        async with send_target.typing():
            prefix = await intake_attachments(message)
            content = _translate_bang_to_slash(message.content or "")
            full_prompt = (prefix + content).strip()
            if not full_prompt:
                return
            await self._stream_response(
                channel=send_target,
                runner=runner,
                prompt=full_prompt,
                msg_url=message.jump_url,
            )

    def _runner_key_for(self, message: discord.Message) -> str | None:
        """Decide the runner key for a message addressed to THIS bot.

        Each bot only sees messages relevant to its agent because Discord only
        delivers messages to bots that have access to the channel. Even so, we
        gate explicitly: only this agent's channel and threads under it count.
        DMs always count as the agent's main runner key (bot accounts have
        separate DM threads with each user).
        """
        ch = message.channel
        if isinstance(ch, discord.DMChannel):
            return self.agent_name
        if isinstance(ch, discord.Thread):
            parent = ch.parent
            if parent is None or parent.id != self.spec.channel_id:
                return None
            return f"thread:{ch.id}"
        if isinstance(ch, discord.TextChannel):
            if ch.id != self.spec.channel_id:
                return None
            return self.agent_name
        return None

    async def _maybe_open_thread(
        self,
        message: discord.Message,
        runner_key: str,
    ) -> tuple[str, discord.abc.Messageable]:
        """If this is a top-level ask in the agent's own channel and the
        agent has auto_thread enabled, create a public thread anchored to
        the message and return the new (runner_key, send_target).

        Otherwise (DM, existing thread, auto_thread off, empty payload,
        or thread creation failure) return the original runner_key and
        the message's channel — so the existing in-channel reply path
        keeps working as a fallback.
        """
        if not (
            runner_key == self.agent_name
            and isinstance(message.channel, discord.TextChannel)
            and self.spec.auto_thread
        ):
            return runner_key, message.channel
        if not ((message.content or "").strip() or message.attachments):
            return runner_key, message.channel
        try:
            thread = await message.create_thread(
                name=_thread_name_from(message.content, message.attachments),
                auto_archive_duration=10080,
            )
        except (discord.HTTPException, discord.Forbidden) as e:
            log.warning(
                "[%s] auto-thread create failed (%s); replying in channel",
                self.agent_name, e,
            )
            return runner_key, message.channel
        new_key = f"thread:{thread.id}"
        self.app.store.upsert(
            new_key, cwd=None, parent_key=self.agent_name, source="discord",
        )
        return new_key, thread

    async def _stream_response(
        self,
        channel: discord.abc.Messageable,
        runner: AgentRunner,
        prompt: str,
        msg_url: str | None = None,
    ) -> None:
        placeholder = await channel.send("_Working…_")
        buf: list[str] = []
        last_edit = 0.0
        error_text: str | None = None

        acquired = await self.app.registry.acquire(self.agent_name)
        if not acquired:
            await placeholder.edit(
                content=f"⏳ {self.spec.display_name} hit her concurrency cap. Try again shortly."
            )
            return

        try:
            async for ev in runner.send(prompt, triggered_by="user", discord_msg_url=msg_url):
                if ev.kind == "text":
                    buf.append(ev.text)
                    now = time.monotonic()
                    if now - last_edit >= EDIT_DEBOUNCE_SECS:
                        preview = ("".join(buf))[:CHUNK_LIMIT]
                        try:
                            await placeholder.edit(content=preview + "\n\n_…_")
                        except discord.HTTPException:
                            pass
                        last_edit = now
                elif ev.kind == "tool_use":
                    now = time.monotonic()
                    if now - last_edit >= EDIT_DEBOUNCE_SECS:
                        preview = ("".join(buf))[:CHUNK_LIMIT]
                        try:
                            label = (preview + f"\n\n_🔧 using {ev.tool_name}…_") if preview \
                                else f"_🔧 using {ev.tool_name}…_"
                            await placeholder.edit(content=label)
                        except discord.HTTPException:
                            pass
                        last_edit = now
                elif ev.kind == "error":
                    error_text = ev.text
        except Exception as e:
            log.exception("[%s] stream_response unexpected error", self.agent_name)
            error_text = f"{type(e).__name__}: {e}"
        finally:
            self.app.registry.release(self.agent_name)

        if error_text:
            try:
                await placeholder.edit(content=f"❌ {error_text}"[:MAX_MSG])
            except discord.HTTPException:
                pass
            return

        final = "".join(buf).strip() or "_(no response text)_"
        chunks = _chunk(final, limit=CHUNK_LIMIT)
        try:
            await placeholder.edit(content=chunks[0][:MAX_MSG])
        except discord.HTTPException:
            pass
        for extra in chunks[1:]:
            try:
                await channel.send(extra[:MAX_MSG])
            except discord.HTTPException as e:
                log.warning("[%s] follow-up chunk failed: %s", self.agent_name, e)


# ============================ BotApp ============================

class BotApp:
    """Owns the shared infrastructure and orchestrates N AgentBot clients."""

    def __init__(self):
        applied = migrate()
        log.info("sqlite schema at user_version=%d", applied)
        self.store = SessionStore()
        self.ledger = Ledger()
        self.cron_store = CronStore()
        self.specialist_store = SpecialistStore()
        self.specialist_ctx: SpecialistContext | None = None
        self.registry = RunnerRegistry(self.store, self.ledger)
        self.bots: dict[str, AgentBot] = {}
        self.http_session: aiohttp.ClientSession | None = None
        self._reaper_task: asyncio.Task[Any] | None = None
        self._oauth_task: asyncio.Task[Any] | None = None
        self._scheduler_task: asyncio.Task[Any] | None = None
        self._scheduler_stop = asyncio.Event()
        self._dashboard_server: Any | None = None

    def _build_bots(self) -> None:
        for name, spec in AGENTS.items():
            if not spec.token:
                log.warning("agent %s has no token configured; skipping", name)
                continue
            self.bots[name] = AgentBot(agent_name=name, app=self)
        if "main" not in self.bots:
            log.error("no token for 'main' agent; cannot run")
            sys.exit(2)
        log.info("agents enabled: %s", list(self.bots))

    def _wire_main_mcp_tools(self) -> None:
        spec = AGENTS.get("main")
        if spec is None or spec.channel_id is None:
            log.error("main agent's channel_id not set; spawn_thread_agent will fail")
            return
        spawn_ctx = ThreadSpawnContext(
            bots=self.bots,
            registry=self.registry,
            store=self.store,
            ledger=self.ledger,
        )
        self.registry.register(
            name="domo",
            server=spawn_ctx.as_mcp_server(),
            tool_names=(
                "mcp__domo__spawn_thread_agent",
                "mcp__domo__delegate_to_research",
                "mcp__domo__delegate_to_comms",
                "mcp__domo__query_hive_mind",
                "mcp__domo__cancel_run",
            ),
            agents=frozenset({"main"}),
        )

    def _wire_elevation_tools(self) -> None:
        """Register `request_admin_elevation` for the operator agent only.

        Mass-expires any rows still `pending` from a prior process so persistent
        views resolve to "expired" instead of trying to honor a dead await. Then
        re-binds persistent views for recent rows so button clicks on yesterday's
        embeds still route to a real handler instead of Discord's generic
        "interaction failed".
        """
        spec = AGENTS.get("main")
        if spec is None or spec.channel_id is None:
            log.warning("main agent's channel_id not set; elevation tool not wired")
            return
        elev_store = ElevationStore()
        expired = elev_store.expire_pending_on_boot()
        if expired:
            log.info("elevation: expired %d stale pending rows on boot", expired)
        elev_ctx = ElevationContext(bots=self.bots, store=elev_store)
        self.registry.register(
            name="elevation",
            server=elev_ctx.as_mcp_server(),
            tool_names=("mcp__elevation__request_admin_elevation",),
            agents=frozenset({"main"}),
        )
        # Re-bind persistent views so old embeds keep responding to clicks.
        # Safe to call before bot.start() — discord.py routes by custom_id once
        # the websocket comes up.
        main_bot = self.bots.get("main")
        if main_bot is not None:
            for row in elev_store.list_for_view_rebind():
                try:
                    main_bot.add_view(
                        ElevationView(row["uuid"], elev_store),
                        message_id=int(row["message_id"]),
                    )
                except Exception:
                    log.exception(
                        "elevation: failed to re-bind view for uuid=%s", row["uuid"],
                    )

    def _wire_cron_tools(self) -> None:
        """Register cron MCP server for the council (main + research + comms).

        New agents are excluded by default — opt them in explicitly.
        """
        cron_ctx = CronContext(
            cron_store=self.cron_store,
            ledger=self.ledger,
            registry=self.registry,
        )
        self.registry.register(
            name="cron",
            server=cron_ctx.as_mcp_server(),
            tool_names=(
                "mcp__cron__create_cron_job",
                "mcp__cron__list_cron_jobs",
                "mcp__cron__toggle_cron_job",
                "mcp__cron__delete_cron_job",
                "mcp__cron__run_cron_job_now",
            ),
            agents=frozenset({"main", "research", "comms"}),
        )

    def _wire_discord_tools(self) -> None:
        """Register post_to_channel for the council (main + research + comms).

        New agents are excluded by default — opt them in explicitly.
        """
        ctx = DiscordContext(bots=self.bots)
        self.registry.register(
            name="discord",
            server=ctx.as_mcp_server(),
            tool_names=("mcp__discord__post_to_channel",),
            agents=frozenset({"main", "research", "comms"}),
        )

    def _wire_video_tools(self) -> None:
        """Register watch_video for Intel only.

        Maine reaches video work via delegate_to_research; Hermes doesn't
        consume video. Wrapping the bradautomates/claude-video /watch skill;
        if the skill isn't installed, the tool returns a helpful error at
        call-time rather than refusing to register.
        """
        ctx = VideoContext()
        self.registry.register(
            name="video",
            server=ctx.as_mcp_server(),
            tool_names=("mcp__video__watch_video",),
            agents=frozenset({"research"}),
        )

    def _wire_calendar_tools(self) -> None:
        """Register two CalDAV MCP servers: read for all, write for main+comms.

        Skipped entirely if either ICLOUD_APPLE_ID or ICLOUD_APP_PASSWORD is
        unset — agents shouldn't see calendar tools that will only fail at
        runtime. A half-configured pair is logged as a warning so the operator
        notices the typo.
        """
        if not (ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD):
            if ICLOUD_APPLE_ID or ICLOUD_APP_PASSWORD:
                log.warning(
                    "calendar tools disabled: only one of "
                    "ICLOUD_APPLE_ID / ICLOUD_APP_PASSWORD is set"
                )
            else:
                log.info(
                    "calendar tools disabled: ICLOUD_APPLE_ID and "
                    "ICLOUD_APP_PASSWORD not set"
                )
            return

        client = CalendarClient(
            username=ICLOUD_APPLE_ID,
            password=ICLOUD_APP_PASSWORD,
            allowed_calendars=ICLOUD_ALLOWED_CALENDARS,
            readonly_calendars=ICLOUD_READONLY_CALENDARS,
            token_secret=CALENDAR_TOKEN_SECRET,
            user_email=ICLOUD_APPLE_ID,
            tz_name=CRON_TZ,
        )
        cache = _ListCalendarsCache()
        read_ctx = CalendarReadContext(client, cache=cache)
        write_ctx = CalendarWriteContext(client, cache=cache)
        self.registry.register(
            name="calendar_read",
            server=read_ctx.as_mcp_server(),
            tool_names=(
                "mcp__calendar_read__list_calendars",
                "mcp__calendar_read__list_events",
                "mcp__calendar_read__get_event",
                "mcp__calendar_read__find_free_time",
            ),
            agents=frozenset({"main", "research", "comms"}),
        )
        self.registry.register(
            name="calendar_write",
            server=write_ctx.as_mcp_server(),
            tool_names=(
                "mcp__calendar_write__create_event",
                "mcp__calendar_write__update_event",
                "mcp__calendar_write__delete_event",
            ),
            agents=frozenset({"main", "comms"}),
        )
        log.info("calendar_read registered (all agents); calendar_write registered (main, comms)")

    async def _wire_specialist_tools(self) -> None:
        if not CODEX_EXE_PATH:
            log.warning("specialist disabled: codex CLI not found")
            return

        ok, err = await codex_health_probe(CODEX_EXE_PATH)
        if not ok:
            log.error("specialist disabled: codex health probe failed: %s", err)
            return

        ctx = SpecialistContext(
            Path(CODEX_EXE_PATH),
            self.specialist_store,
            self.ledger,
        )
        self.specialist_ctx = ctx
        self.registry.register(
            name="specialist",
            server=ctx.as_mcp_server(),
            tool_names=("mcp__specialist__query_gpt5",),
            agents=frozenset({"main", "research"}),
        )
        log.info("specialist registered for {main, research}")

    async def _idle_reaper(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.registry.reap_idle()
            except Exception:
                log.exception("idle reaper error")

    async def _oauth_sanity_loop(self) -> None:
        interval = OAUTH_SANITY_CHECK_HOURS * 3600
        await asyncio.sleep(interval)
        while True:
            ok, err = await _oauth_sanity_check()
            if ok:
                log.info("OAuth sanity check passed")
            else:
                log.error("OAuth sanity check FAILED: %s", err)
                main_bot = self.bots.get("main")
                main_channel = AGENTS["main"].channel_id if "main" in AGENTS else None
                if main_bot and main_channel:
                    try:
                        ch = main_bot.get_channel(main_channel) or await main_bot.fetch_channel(main_channel)
                        await ch.send(
                            "⚠️ **Claude subscription auth failed** — run `claude login` "
                            "on the host and the bots will recover on the next message.\n"
                            f"Error: `{err}`"
                        )
                    except Exception:
                        log.exception("failed to post OAuth failure notice")
            await asyncio.sleep(interval)

    async def run(self) -> None:
        self._build_bots()
        self._wire_main_mcp_tools()
        self._wire_elevation_tools()
        self._wire_cron_tools()
        self._wire_discord_tools()
        self._wire_video_tools()
        self._wire_calendar_tools()
        await self._wire_specialist_tools()
        self.http_session = aiohttp.ClientSession()
        self._reaper_task = asyncio.create_task(self._idle_reaper())
        self._oauth_task = asyncio.create_task(self._oauth_sanity_loop())
        self._scheduler_task = asyncio.create_task(
            cron_scheduler_loop(
                self._scheduler_stop, self.cron_store, self.registry, self.ledger
            )
        )

        # Build the optional dashboard server. Only enabled when the wizard
        # has set DASHBOARD_PIN_HASH and DASHBOARD_SESSION_SECRET.
        coros = [
            bot.start(AGENTS[name].token)  # type: ignore[arg-type]
            for name, bot in self.bots.items()
        ]
        if DASHBOARD_ENABLED:
            from src.web.app import make_server
            self._dashboard_server = make_server(self)
            log.info("dashboard enabled at http://%s:%d", DASHBOARD_BIND_HOST, DASHBOARD_PORT)
            coros.append(self._dashboard_server.serve())
        else:
            log.info("dashboard disabled (set DASHBOARD_PIN_HASH and DASHBOARD_SESSION_SECRET to enable)")

        try:
            await asyncio.gather(*coros)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        log.info("BotApp shutdown — closing dashboard, runners, http session, bots")
        if self._dashboard_server is not None:
            self._dashboard_server.should_exit = True
        if self._reaper_task:
            self._reaper_task.cancel()
        if self._oauth_task:
            self._oauth_task.cancel()
        self._scheduler_stop.set()
        if self._scheduler_task:
            self._scheduler_task.cancel()
        try:
            await asyncio.wait_for(self.registry.aclose_all(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            log.warning("runner shutdown timed out; proceeding anyway")
        if self.http_session:
            try:
                await asyncio.wait_for(self.http_session.close(), timeout=2.0)
            except Exception:
                pass
        for name, bot in list(self.bots.items()):
            try:
                await asyncio.wait_for(bot.close(), timeout=2.0)
            except Exception:
                log.warning("bot %s close timed out", name)


async def _oauth_sanity_check() -> tuple[bool, str]:
    """Make a trivial one-token query through the SDK to verify auth works."""
    try:
        options = ClaudeAgentOptions(
            cwd=str(BOT_CWD),
            setting_sources=SETTING_SOURCES,
            permission_mode=PERMISSION_MODE,
        )
        got_any = False
        async for _ in query(prompt="ping", options=options):
            got_any = True
            break
        return (True, "") if got_any else (False, "no response from SDK query")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> None:
    _setup_logging()
    if OPERATOR_USER_ID is None:
        log.error("OPERATOR_USER_ID not set. Run `py wizard.py` first.")
        sys.exit(2)
    if not any(spec.token for spec in AGENTS.values()):
        log.error("No agent tokens configured. Run `py wizard.py`.")
        sys.exit(2)

    app = BotApp()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
