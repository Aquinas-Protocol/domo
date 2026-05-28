"""MCP tools the main agent can call: delegation, hive-mind query, cancel,
and the existing spawn_thread_agent. Plus the multimodal attachment intake.

Delegation calls RunnerRegistry directly (no synthetic Discord messages),
optionally mirroring the brief+reply into the target agent's channel for
audit trail. The audit mirror posts via the agent's webhook so the display
name is correct (Maine→Intel handoff appears as Intel saying it).
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

from src.config import AGENTS, GROQ_API_KEY, VAULT_ROOT, get_agent

log = logging.getLogger(__name__)

GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"

# Whether delegate_to_* mirrors the brief + reply into the target agent's
# channel as an audit trail. Off by default to keep channels uncluttered.
MIRROR_DELEGATIONS = os.getenv("MIRROR_DELEGATIONS", "false").lower() in ("true", "1", "yes")


# ============================ Multimodal intake ============================

def _dest_for_mime(mime: str | None, filename: str) -> Path:
    if mime is None:
        mime, _ = mimetypes.guess_type(filename)
    mime = (mime or "").lower()
    if mime.startswith("audio/"):
        return VAULT_ROOT / "raw" / "voice-memos"
    if mime.startswith("image/"):
        return VAULT_ROOT / "raw" / "notes"
    if mime == "application/pdf":
        return VAULT_ROOT / "raw" / "articles"
    return VAULT_ROOT / "raw" / "notes"


async def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)


async def _transcribe_with_groq(audio_path: Path) -> str | None:
    if not GROQ_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            with audio_path.open("rb") as f:
                files = {"file": (audio_path.name, f, "audio/ogg")}
                data = {"model": GROQ_MODEL, "response_format": "text"}
                headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
                resp = await client.post(
                    GROQ_WHISPER_URL, headers=headers, files=files, data=data
                )
                resp.raise_for_status()
                return resp.text.strip()
    except Exception as e:
        log.warning("Groq transcription failed for %s: %s", audio_path, e)
        return None


async def intake_attachments(message: discord.Message) -> str:
    if not message.attachments:
        return ""
    lines: list[str] = []
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for att in message.attachments:
        dest_dir = _dest_for_mime(att.content_type, att.filename)
        dest = dest_dir / f"{date}-{message.id}-{att.filename}"
        try:
            await _download(att.url, dest)
        except Exception as e:
            log.warning("Failed to download %s: %s", att.url, e)
            lines.append(f"[Attachment download failed: {att.filename}]")
            continue

        mime = (att.content_type or mimetypes.guess_type(att.filename)[0] or "").lower()
        if mime.startswith("audio/"):
            transcript = await _transcribe_with_groq(dest)
            if transcript:
                sidecar = dest.with_suffix(dest.suffix + ".txt")
                sidecar.write_text(transcript, encoding="utf-8")
                lines.append(f"[Voice transcript] {transcript}")
                lines.append(f"[Audio saved: {dest}]")
            else:
                lines.append(f"[Audio saved (no transcript): {dest}]")
        elif mime.startswith("image/"):
            lines.append(f"[Attached image] {dest}")
        elif mime == "application/pdf":
            lines.append(f"[Attached PDF] {dest}")
        else:
            lines.append(f"[Attached file] {dest}")

    return "\n".join(lines) + "\n\n" if lines else ""


# ============================ MCP tools (Maine only) ============================

class ThreadSpawnContext:
    """Holds references the MCP tools need to act on Discord and the registry.

    With multi-bot, threads are created in the destination agent's channel
    using that agent's own bot client. So spawning a research thread uses
    Intel's bot to create the thread in #research, and Intel handles all
    follow-up messages there.
    """

    def __init__(
        self,
        bots: dict[str, discord.Client],
        registry: Any,         # RunnerRegistry
        store: Any,            # SessionStore
        ledger: Any,           # Ledger
    ):
        self.bots = bots
        self.registry = registry
        self.store = store
        self.ledger = ledger

    def _client_for(self, agent_name: str) -> discord.Client | None:
        return self.bots.get(agent_name)

    # --- helper used by delegate + mirror ---

    async def _run_to_completion(
        self,
        target_agent: str,
        runner_key: str,
        brief: str,
        triggered_by: str,
        parent_ledger_id: int | None,
    ) -> tuple[str, int | None, str | None]:
        """Drive a runner to its final ResultMessage. Returns (text, ledger_id, error)."""
        runner = self.registry.get_or_create(runner_key, agent_name=target_agent)
        buf: list[str] = []
        last_ledger_id: int | None = None
        error: str | None = None
        try:
            async for ev in runner.send(
                brief, triggered_by=triggered_by, parent_ledger_id=parent_ledger_id
            ):
                if ev.ledger_id is not None:
                    last_ledger_id = ev.ledger_id
                if ev.kind == "text":
                    buf.append(ev.text)
                elif ev.kind == "error":
                    error = ev.text
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        return ("".join(buf).strip(), last_ledger_id, error)

    async def _mirror_to_channel(self, agent_name: str, text: str) -> None:
        """Best-effort post into the agent's own channel using their bot.
        Silently no-ops if mirroring is disabled or the agent isn't wired."""
        if not MIRROR_DELEGATIONS:
            return
        spec = get_agent(agent_name)
        client = self._client_for(agent_name)
        if spec is None or spec.channel_id is None or client is None:
            return
        try:
            ch = client.get_channel(spec.channel_id) or await client.fetch_channel(spec.channel_id)
            await ch.send(text[:1900])
        except Exception:
            log.exception("delegation mirror to %s failed", agent_name)

    def as_mcp_server(self):
        ctx = self

        # --- spawn_thread_agent (existing, extended with agent_type) ---

        @tool(
            "spawn_thread_agent",
            "Create a new Discord thread inside the destination agent's channel "
            "with its own persistent sub-agent. Use this when a task warrants an "
            "isolated long-running context the user can return to (research projects, "
            "ongoing multi-turn work). Returns the thread URL immediately; the "
            "sub-agent's first response streams into the thread in the background. "
            "Set agent_type to 'main' (default), 'research', or 'comms' to choose "
            "which persona and channel drive the sub-agent.",
            {"title": str, "brief": str, "agent_type": str},
        )
        async def spawn_thread_agent(args: dict[str, Any]) -> dict[str, Any]:
            title = args["title"][:100]
            brief = args["brief"]
            agent_type = (args.get("agent_type") or "main").strip()
            if agent_type not in AGENTS:
                return {
                    "content": [{"type": "text",
                                 "text": f"Error: unknown agent_type '{agent_type}'. Valid: {list(AGENTS)}"}],
                    "isError": True,
                }

            target_spec = AGENTS[agent_type]
            target_client = ctx._client_for(agent_type)
            if target_client is None or target_spec.channel_id is None:
                return {
                    "content": [{"type": "text",
                                 "text": f"Error: agent '{agent_type}' has no bot or channel configured."}],
                    "isError": True,
                }

            channel = target_client.get_channel(target_spec.channel_id) or \
                await target_client.fetch_channel(target_spec.channel_id)
            if not isinstance(channel, discord.TextChannel):
                return {
                    "content": [{"type": "text",
                                 "text": f"Error: #{agent_type} is not a guild text channel; cannot create threads."}],
                    "isError": True,
                }

            thread = await channel.create_thread(
                name=title,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=10080,
            )
            thread_key = f"thread:{thread.id}"
            ctx.store.upsert(thread_key, cwd=None, parent_key="main", source="discord")

            snippet = brief[:200] + ("…" if len(brief) > 200 else "")
            await thread.send(f"**Brief ({agent_type}):** {snippet}\n\n_Working…_")

            async def _run_first_turn():
                final, lid, err = await ctx._run_to_completion(
                    target_agent=agent_type,
                    runner_key=thread_key,
                    brief=brief,
                    triggered_by="spawn_thread_agent",
                    parent_ledger_id=None,
                )
                content = (f"❌ {err}" if err else (final or "_(sub-agent returned no text)_"))[:1900]
                try:
                    await thread.send(content)
                except discord.HTTPException:
                    log.exception("thread first-turn delivery failed")

            asyncio.create_task(_run_first_turn())

            return {
                "content": [{
                    "type": "text",
                    "text": (
                        f"Thread created: {thread.jump_url}\n"
                        f"thread_agent_key: {thread_key}\n"
                        f"agent_type: {agent_type}\n"
                        "Sub-agent is processing the brief in the background; first "
                        "response will appear in the thread shortly."
                    ),
                }]
            }

        # --- delegate_to_research ---

        @tool(
            "delegate_to_research",
            "Hand a research brief to Intel (the deep-research agent) and "
            "return her reply inline so you can summarize it for the user. Use "
            "for multi-source web research and deep dives.",
            {"brief": str},
        )
        async def delegate_to_research(args: dict[str, Any]) -> dict[str, Any]:
            return await _delegate_inline(ctx, "research", args["brief"])

        # --- delegate_to_comms ---

        @tool(
            "delegate_to_comms",
            "Hand a correspondence brief to Hermes (the comms agent) and "
            "return his reply inline so you can summarize it for the user. Use "
            "for drafting messages, recalling relationship context, or summarising "
            "communications history.",
            {"brief": str},
        )
        async def delegate_to_comms(args: dict[str, Any]) -> dict[str, Any]:
            return await _delegate_inline(ctx, "comms", args["brief"])

        # --- query_hive_mind ---

        @tool(
            "query_hive_mind",
            "Read recent task ledger entries to find what other agents have "
            "been doing. Filter by agent name ('research', 'comms', etc.) or "
            "status ('queued', 'running', 'completed', 'failed', 'cancelled'). "
            "Useful for answering 'what is Intel working on right now' or "
            "'what did Hermes draft this morning'.",
            {"agent": str, "status": str, "since_minutes": int, "limit": int},
        )
        async def query_hive_mind(args: dict[str, Any]) -> dict[str, Any]:
            agent = (args.get("agent") or "").strip() or None
            status = (args.get("status") or "").strip() or None
            since = int(args.get("since_minutes") or 240)
            limit = int(args.get("limit") or 20)
            rows = ctx.ledger.query(agent=agent, status=status, since_minutes=since, limit=limit)
            if not rows:
                return {"content": [{"type": "text",
                                     "text": f"No ledger entries match (agent={agent}, status={status}, since={since}min)."}]}
            lines = [f"{len(rows)} ledger entries:"]
            for r in rows:
                lines.append(
                    f"  #{r['id']} [{r['agent_name']}] {r['status']} — "
                    f"{(r['summary'] or r['error_summary'] or '(pending)')[:200]} "
                    f"(by {r['triggered_by']} at {r['created_at']})"
                )
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        # --- cancel_run ---

        @tool(
            "cancel_run",
            "Mark a ledger task as cancelled. Note: this updates the ledger but "
            "does not currently kill the underlying subprocess if it's mid-tool-call; "
            "the running task will complete its current step before observing.",
            {"ledger_id": int},
        )
        async def cancel_run(args: dict[str, Any]) -> dict[str, Any]:
            lid = int(args["ledger_id"])
            ctx.ledger.mark_cancelled(lid, reason="cancelled by main")
            return {"content": [{"type": "text", "text": f"Ledger #{lid} marked cancelled."}]}

        return create_sdk_mcp_server(
            name="domo",
            version="0.2.0",
            tools=[
                spawn_thread_agent,
                delegate_to_research,
                delegate_to_comms,
                query_hive_mind,
                cancel_run,
            ],
        )


# ----------------------------- Delegation helper -----------------------------

async def _delegate_inline(
    ctx: ThreadSpawnContext, target_agent: str, brief: str
) -> dict[str, Any]:
    if target_agent not in AGENTS:
        return {
            "content": [{"type": "text",
                         "text": f"Error: agent '{target_agent}' not in registry."}],
            "isError": True,
        }
    spec = AGENTS[target_agent]
    if not spec.can_delegate and target_agent == "main":
        return {
            "content": [{"type": "text", "text": "Cannot delegate to self."}],
            "isError": True,
        }

    final, ledger_id, err = await ctx._run_to_completion(
        target_agent=target_agent,
        runner_key=target_agent,        # delegations reuse the agent's main runner
        brief=brief,
        triggered_by="delegation:main",
        parent_ledger_id=None,
    )
    if err:
        return {
            "content": [{"type": "text",
                         "text": f"{spec.display_name} returned error: {err}\nledger_id: {ledger_id}"}],
            "isError": True,
        }

    # Audit mirror (if enabled) — fire and forget.
    if MIRROR_DELEGATIONS and final:
        asyncio.create_task(ctx._mirror_to_channel(target_agent, final))

    return {
        "content": [{
            "type": "text",
            "text": (
                f"{spec.display_name} reply (ledger #{ledger_id}):\n\n"
                f"{final or '(empty)'}"
            ),
        }]
    }
