"""AgentRunner: one ClaudeSDKClient per Discord key, with persona, lifecycle, and backpressure.

Each runner is bound to an agent_name (main/research/comms/...) at construction.
The agent's persona is loaded from second-brain/agents/<name>.md, hashed for
persona_version tracking, and appended to the system prompt. RunnerRegistry
enforces a global concurrency cap and per-agent queue depth.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from hooks.pretooluse_guard import guard as pretooluse_guard
from src.config import (
    AgentSpec,
    BACKGROUND_QUEUE_DEPTH_PER_AGENT,
    BOT_CWD,
    CREDIT_GOVERNOR_ENFORCE,
    CRON_TZ,
    IDLE_TEARDOWN_MINUTES,
    MAX_CONCURRENT_RUNS,
    MAX_QUEUE_DEPTH_PER_AGENT,
    MEMORY_FILES,
    PERMISSION_MODE,
    RUN_TIMEOUT_SECONDS,
    SETTING_SOURCES,
    SYSTEM_PROMPT_PRESET,
    build_system_prompt_append,
    get_agent,
)
from src.credit_governor import CreditGovernor, estimate_run_cost
from src.store import Ledger, SessionStore

log = logging.getLogger(__name__)


@dataclass
class AgentEvent:
    kind: str  # "text" | "tool_use" | "tool_result" | "final" | "error" | "busy"
    text: str = ""
    tool_name: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    ledger_id: int | None = None


def _coerce_token_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _usage_token_count(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None

    for key in ("total_tokens", "totalTokens"):
        total = _coerce_token_count(usage.get(key))
        if total is not None:
            return total

    token_keys = [key for key in usage if key.endswith("_tokens") or key.endswith("Tokens")]
    if token_keys:
        return sum(_coerce_token_count(usage.get(key)) or 0 for key in token_keys)

    nested_total = 0
    found_nested = False
    for value in usage.values():
        count = _usage_token_count(value)
        if count is not None:
            nested_total += count
            found_nested = True
    return nested_total if found_nested else None


_SDK_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _usage_breakdown(usage: Any) -> dict[str, int | None]:
    """Split-token counts from the SDK ``usage`` (or ``model_usage``) shape.

    The Anthropic SDK reports four classes per turn: input, output,
    cache_creation_input, cache_read_input. They can land at the top of
    ``usage`` (flat) or nested under per-model keys in ``model_usage``.
    This walks both shapes, sums across nested dicts, and maps SDK field
    names to the ledger's column names (drops the ``_input`` segment so
    ``cache_*`` columns stay short).

    Returns all-``None`` if no token fields are present (legacy/unknown
    shape) so the caller can detect "not measured" vs "measured as zero".
    """
    counts = {f: 0 for f in _SDK_USAGE_FIELDS}
    seen_any = False

    def _walk(node: Any) -> None:
        nonlocal seen_any
        if not isinstance(node, dict):
            return
        for k, v in node.items():
            if k in counts:
                coerced = _coerce_token_count(v)
                if coerced is not None:
                    counts[k] += coerced
                    seen_any = True
            elif isinstance(v, dict):
                _walk(v)

    _walk(usage)
    if not seen_any:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cache_creation_tokens": None,
            "cache_read_tokens": None,
        }
    return {
        "input_tokens": counts["input_tokens"],
        "output_tokens": counts["output_tokens"],
        "cache_creation_tokens": counts["cache_creation_input_tokens"],
        "cache_read_tokens": counts["cache_read_input_tokens"],
    }


@dataclass(frozen=True)
class McpRegistration:
    name: str
    server: Any
    tool_names: tuple[str, ...]
    agents: frozenset[str] | None = None


# ----------------------------- Persona helpers -----------------------------

def _load_persona(spec: AgentSpec) -> tuple[str, str]:
    """Read the persona file; return (text, persona_version_hash)."""
    if not spec.persona_file.exists():
        log.warning("persona file missing for %s: %s", spec.name, spec.persona_file)
        return "", "missing"
    text = spec.persona_file.read_text(encoding="utf-8")
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return text, h


def _load_memory_for_main() -> str:
    chunks: list[str] = []
    for path in MEMORY_FILES:
        if path.exists():
            try:
                chunks.append(f"### {path.name}\n{path.read_text(encoding='utf-8')}")
            except OSError as e:
                log.warning("failed to read memory file %s: %s", path, e)
    return "\n\n".join(chunks)


def _current_time_prefix(now: datetime | None = None, tz_name: str = CRON_TZ) -> str:
    """Render a one-line clock stamp for prepending to every prompt.

    The system prompt is built once at runner construction and cached; without
    a per-turn time injection, agents reasoning about "tomorrow" / "in an
    hour" / "next Friday" rely on training-time priors and frequently land on
    the wrong date. Stamping every ``runner.send`` keeps date math grounded.

    The output is always rendered in ``tz_name``: naive inputs are assumed to
    already be in that zone; aware inputs are converted.

    Format: ``[Current time: Thursday 2026-04-30 09:11 CDT]``
    """
    tz = ZoneInfo(tz_name)
    moment = now or datetime.now(tz)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    moment = moment.astimezone(tz)
    return f"[Current time: {moment:%A %Y-%m-%d %H:%M %Z}]"


# ----------------------------- AgentRunner -----------------------------

class AgentRunner:
    """One ClaudeSDKClient bound to one agent persona, lazy + idle-torn-down."""

    def __init__(
        self,
        discord_key: str,
        agent_name: str,
        store: SessionStore,
        ledger: Ledger,
        mcp_servers: dict[str, Any] | None = None,
        mcp_tool_names: tuple[str, ...] = (),
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
    ):
        self.key = discord_key
        self.agent_name = agent_name
        self.store = store
        self.ledger = ledger
        self.mcp_servers = mcp_servers or {}
        self._mcp_tool_names = mcp_tool_names
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self._client: ClaudeSDKClient | None = None
        self._lock = asyncio.Lock()
        self._last_used = 0.0
        self._persona_version: str = "unloaded"
        self._spec: AgentSpec | None = get_agent(agent_name)
        if self._spec is None:
            log.warning("AgentRunner %s: no spec found for agent_name=%s", discord_key, agent_name)

    @property
    def persona_version(self) -> str:
        return self._persona_version

    def _build_options(self) -> ClaudeAgentOptions:
        if self._spec is None:
            persona_text = ""
            self._persona_version = "no-spec"
            tool_allowlist = None
        else:
            persona_text, self._persona_version = _load_persona(self._spec)
            tool_allowlist = self._spec.enabled_tools

        memory_text = _load_memory_for_main() if self.agent_name == "main" else ""
        append_text = build_system_prompt_append(self.agent_name, persona_text, memory_text)

        opts: dict[str, Any] = dict(
            cwd=str(BOT_CWD),
            setting_sources=SETTING_SOURCES,
            permission_mode=PERMISSION_MODE,
            system_prompt={**SYSTEM_PROMPT_PRESET, "append": append_text},
            mcp_servers=self.mcp_servers,
            hooks={
                "PreToolUse": [HookMatcher(matcher=None, hooks=[pretooluse_guard])],
            },
            resume=self.store.get_session_id(self.key),
        )
        if self._spec is not None and self._spec.model:
            opts["model"] = self._spec.model
        if self.max_turns is not None:
            opts["max_turns"] = self.max_turns
        if self.max_budget_usd is not None:
            opts["max_budget_usd"] = self.max_budget_usd
        if tool_allowlist:
            allowed_tools = list(tool_allowlist)
            for tool_name in self._mcp_tool_names:
                if tool_name not in allowed_tools:
                    allowed_tools.append(tool_name)
            opts["allowed_tools"] = allowed_tools
        return ClaudeAgentOptions(**opts)

    async def _ensure_client(self) -> ClaudeSDKClient:
        if self._client is None:
            options = self._build_options()
            self._client = ClaudeSDKClient(options=options)
            await self._client.connect()
            log.info(
                "runner %s (agent=%s persona=%s): client connected (resume=%s)",
                self.key, self.agent_name, self._persona_version, options.resume,
            )
        return self._client

    async def _aclose_unlocked(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:
                log.warning("runner %s: disconnect error: %s", self.key, e)
            self._client = None
            log.info("runner %s: client torn down", self.key)

    async def aclose(self) -> None:
        async with self._lock:
            await self._aclose_unlocked()

    async def idle_for(self, seconds: int) -> bool:
        return self._client is not None and (time.monotonic() - self._last_used) >= seconds

    async def send(
        self,
        text: str,
        triggered_by: str = "user",
        parent_ledger_id: int | None = None,
        discord_msg_url: str | None = None,
        mission_run_id: int | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if self._spec is not None and self._persona_version == "unloaded":
            _, self._persona_version = _load_persona(self._spec)

        self.store.upsert(self.key, cwd=str(BOT_CWD), source=triggered_by)

        ledger_id = self.ledger.enqueue(
            discord_key=self.key,
            agent_name=self.agent_name,
            persona_version=self._persona_version,
            triggered_by=triggered_by,
            parent_ledger_id=parent_ledger_id,
            discord_msg_url=discord_msg_url,
            mission_run_id=mission_run_id,
        )

        async with self._lock:
            self._last_used = time.monotonic()
            try:
                # Credit governor: refuse dispatch when the Agent SDK credit
                # is exhausted (only enforced when CREDIT_GOVERNOR_ENFORCE is
                # true; otherwise we just log so the math can be validated
                # against real traffic before June 15).
                gov_model = self._spec.model if self._spec else None
                gov_estimate = estimate_run_cost(gov_model)
                gov_decision = CreditGovernor(self.ledger).decide(
                    estimated_run_cost_usd=gov_estimate
                )
                log.info(
                    "governor %s (agent=%s model=%s est=$%.2f): %s",
                    self.key, self.agent_name, gov_model, gov_estimate,
                    gov_decision.message,
                )
                if not gov_decision.allow_start:
                    if CREDIT_GOVERNOR_ENFORCE:
                        self.ledger.mark_failed(
                            ledger_id,
                            f"credit_governor_block: {gov_decision.message}",
                        )
                        yield AgentEvent(
                            kind="error",
                            text=f"credit governor: {gov_decision.message}",
                            ledger_id=ledger_id,
                        )
                        return
                    log.warning(
                        "governor %s would-have-blocked (observe-only): %s",
                        self.key, gov_decision.message,
                    )

                client = await self._ensure_client()
                self.ledger.mark_running(ledger_id)
                # Prepend a fresh wall-clock stamp every turn so the agent's
                # date math doesn't drift from real time.
                stamped_text = f"{_current_time_prefix()}\n\n{text}"
                await client.query(stamped_text)
                self.store.touch(self.key)

                buf: list[str] = []
                final_session_id: str | None = None
                final_cost: float | None = None
                final_tokens: int | None = None

                async with asyncio.timeout(RUN_TIMEOUT_SECONDS):
                    async for msg in client.receive_response():
                        if isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    buf.append(block.text)
                                    yield AgentEvent(
                                        kind="text", text=block.text, ledger_id=ledger_id
                                    )
                                elif isinstance(block, ToolUseBlock):
                                    yield AgentEvent(
                                        kind="tool_use", tool_name=block.name, ledger_id=ledger_id
                                    )
                                elif isinstance(block, ToolResultBlock):
                                    yield AgentEvent(kind="tool_result", ledger_id=ledger_id)
                        elif isinstance(msg, ResultMessage):
                            final_session_id = getattr(msg, "session_id", None)
                            final_cost = getattr(msg, "total_cost_usd", None)
                            usage_raw = getattr(msg, "usage", None)
                            breakdown = _usage_breakdown(usage_raw)
                            # Newer SDK builds put per-class counts in usage;
                            # older builds only populate model_usage.
                            if breakdown["input_tokens"] is None:
                                usage_raw = getattr(msg, "model_usage", None)
                                breakdown = _usage_breakdown(usage_raw)
                            final_tokens = _usage_token_count(usage_raw)
                            if final_session_id:
                                self.store.upsert(
                                    self.key,
                                    session_id=final_session_id,
                                    cwd=str(BOT_CWD),
                                    source=triggered_by,
                                )
                            summary = ("".join(buf).strip() or "")[:300] or "(no text)"
                            self.ledger.mark_completed(
                                ledger_id,
                                summary,
                                final_session_id,
                                final_cost,
                                final_tokens,
                                provider="claude",
                                runtime="claude_sdk",
                                billing_bucket="claude_agent_sdk",
                                input_tokens=breakdown["input_tokens"],
                                output_tokens=breakdown["output_tokens"],
                                cache_creation_tokens=breakdown["cache_creation_tokens"],
                                cache_read_tokens=breakdown["cache_read_tokens"],
                            )
                            yield AgentEvent(
                                kind="final",
                                session_id=final_session_id,
                                cost_usd=final_cost,
                                ledger_id=ledger_id,
                            )
                            return
            except asyncio.TimeoutError:
                self.ledger.mark_failed(ledger_id, f"timeout after {RUN_TIMEOUT_SECONDS}s")
                log.warning("runner %s: timeout", self.key)
                yield AgentEvent(
                    kind="error",
                    text=f"timeout after {RUN_TIMEOUT_SECONDS}s",
                    ledger_id=ledger_id,
                )
                await self._aclose_unlocked()
            except asyncio.CancelledError:
                self.ledger.mark_cancelled(ledger_id)
                raise
            except Exception as e:
                self.ledger.mark_failed(ledger_id, f"{type(e).__name__}: {e}")
                log.exception("runner %s: error during receive_response", self.key)
                yield AgentEvent(
                    kind="error", text=f"{type(e).__name__}: {e}", ledger_id=ledger_id
                )
                await self._aclose_unlocked()


# ----------------------------- RunnerRegistry -----------------------------

class RunnerRegistry:
    """Map discord_key -> AgentRunner. Holds the global concurrency cap."""

    def __init__(self, store: SessionStore, ledger: Ledger):
        self.store = store
        self.ledger = ledger
        self.runners: dict[str, AgentRunner] = {}
        self.mcp_registrations: list[McpRegistration] = []
        self._global_sem = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
        self._inflight: dict[str, int] = {}
        self._background_inflight: dict[str, int] = {}

    def register(
        self,
        *,
        name: str,
        server: Any,
        tool_names: tuple[str, ...],
        agents: frozenset[str] | None = None,
    ) -> None:
        if any(reg.name == name for reg in self.mcp_registrations):
            raise ValueError(f"MCP server {name!r} is already registered")
        self.mcp_registrations.append(
            McpRegistration(
                name=name,
                server=server,
                tool_names=tool_names,
                agents=agents,
            )
        )

    def _is_registered_for(self, reg: McpRegistration, agent_name: str) -> bool:
        return reg.agents is None or agent_name in reg.agents

    def _mcp_servers_for(self, agent_name: str) -> dict[str, Any]:
        return {
            reg.name: reg.server
            for reg in self.mcp_registrations
            if self._is_registered_for(reg, agent_name)
        }

    def _tool_names_for(self, agent_name: str) -> tuple[str, ...]:
        names: list[str] = []
        for reg in self.mcp_registrations:
            if not self._is_registered_for(reg, agent_name):
                continue
            for tool_name in reg.tool_names:
                if tool_name not in names:
                    names.append(tool_name)
        return tuple(names)

    def get_or_create(
        self,
        key: str,
        agent_name: str,
        *,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
    ) -> AgentRunner:
        if key not in self.runners:
            self.runners[key] = AgentRunner(
                discord_key=key,
                agent_name=agent_name,
                store=self.store,
                ledger=self.ledger,
                mcp_servers=self._mcp_servers_for(agent_name),
                mcp_tool_names=self._tool_names_for(agent_name),
                max_turns=max_turns,
                max_budget_usd=max_budget_usd,
            )
        return self.runners[key]

    def is_busy(self, agent_name: str) -> bool:
        return self._inflight.get(agent_name, 0) >= MAX_QUEUE_DEPTH_PER_AGENT

    async def acquire(self, agent_name: str) -> bool:
        if self.is_busy(agent_name):
            return False
        self._inflight[agent_name] = self._inflight.get(agent_name, 0) + 1
        await self._global_sem.acquire()
        return True

    async def acquire_background(
        self,
        agent_name: str,
        *,
        wait: bool = True,
        poll_seconds: float = 5.0,
    ) -> bool:
        cap = max(
            0,
            min(
                BACKGROUND_QUEUE_DEPTH_PER_AGENT,
                MAX_QUEUE_DEPTH_PER_AGENT - 1,
                MAX_CONCURRENT_RUNS - 1,
            ),
        )
        while True:
            total = self._inflight.get(agent_name, 0)
            background = self._background_inflight.get(agent_name, 0)
            if (
                total < MAX_QUEUE_DEPTH_PER_AGENT
                and background < cap
                and not self._global_sem.locked()
            ):
                self._inflight[agent_name] = total + 1
                self._background_inflight[agent_name] = background + 1
                try:
                    await self._global_sem.acquire()
                    return True
                except BaseException:
                    self._inflight[agent_name] = max(0, self._inflight.get(agent_name, 0) - 1)
                    self._background_inflight[agent_name] = max(
                        0, self._background_inflight.get(agent_name, 0) - 1
                    )
                    raise
            if not wait:
                return False
            await asyncio.sleep(poll_seconds)

    @asynccontextmanager
    async def background_slot(self, agent_name: str):
        await self.acquire_background(agent_name)
        try:
            yield
        finally:
            self.release(agent_name, background=True)

    def release(self, agent_name: str, *, background: bool = False) -> None:
        self._global_sem.release()
        self._inflight[agent_name] = max(0, self._inflight.get(agent_name, 0) - 1)
        if background:
            self._background_inflight[agent_name] = max(
                0, self._background_inflight.get(agent_name, 0) - 1
            )

    async def reap_idle(self) -> None:
        threshold = IDLE_TEARDOWN_MINUTES * 60
        for runner in list(self.runners.values()):
            if await runner.idle_for(threshold):
                await runner.aclose()

    async def aclose_all(self) -> None:
        for runner in list(self.runners.values()):
            await runner.aclose()
