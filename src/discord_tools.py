"""MCP tool: post_to_channel(channel_id, content, agent='main').

Lets cron-fired prompts (and any agent in general) post into a Discord channel
that isn't the calling agent's own channel. Used by the morning-brief cron job
to drop the brief into #daily-brief from the Daedalus identity.

Registered on every runner via RunnerRegistry.register(..., agents=None)
alongside the cron tools.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

import discord
from claude_agent_sdk import create_sdk_mcp_server, tool

if TYPE_CHECKING:
    from src.bot import AgentBot

log = logging.getLogger(__name__)

# Discord's hard per-message limit. Mirrors bot.py:MAX_MSG; importing it would
# cycle, so the constant is duplicated.
MAX_MSG = 2000


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _chunk_for_discord(content: str, limit: int = MAX_MSG) -> list[str]:
    """Split content at <= limit chars, preferring paragraph then line breaks.

    Coarser than bot.py:_chunk (which is fence-aware and streaming-debounced).
    Adequate for short paragraph-based posts like the morning brief.
    """
    if len(content) <= limit:
        return [content]
    chunks: list[str] = []
    remaining = content
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class DiscordContext:
    """Holds bot-client references for the post_to_channel tool."""

    def __init__(self, bots: dict[str, "AgentBot"]):
        self.bots = bots

    async def post_to_channel(
        self,
        *,
        channel_id: str,
        content: str,
        agent: str = "main",
    ) -> dict[str, Any]:
        agent = (agent or "main").strip() or "main"
        if agent not in self.bots:
            return _err(
                f"unknown agent {agent!r}; available: {sorted(self.bots)}"
            )
        if not content or not content.strip():
            return _err("content is required")
        # bool is a subclass of int in Python; exclude it explicitly.
        if isinstance(channel_id, int) and not isinstance(channel_id, bool):
            return _err(
                "channel_id arrived as a JSON integer — Discord snowflakes are "
                "64-bit and lose precision when serialized as JSON numbers "
                "(53-bit doubles). Pass channel_id as a quoted JSON string."
            )
        if not isinstance(channel_id, str) or not channel_id.strip():
            return _err("channel_id is required and must be a non-empty string")
        cs = channel_id.strip()
        if not cs.isdigit():
            return _err(f"channel_id must be digits only, got {cs!r}")
        cid = int(cs)

        bot = self.bots[agent]
        try:
            ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
        except discord.NotFound:
            return _err(f"channel {cid} not found by {agent!r} bot")
        except discord.Forbidden:
            return _err(f"{agent!r} bot lacks access to channel {cid}")
        except Exception as e:
            log.exception("post_to_channel: channel resolve failed")
            return _err(f"could not resolve channel {cid}: {e}")

        chunks = _chunk_for_discord(content)
        first_id: int | None = None
        try:
            for piece in chunks:
                msg = await ch.send(piece)
                if first_id is None:
                    first_id = msg.id
        except discord.Forbidden:
            return _err(
                f"{agent!r} bot cannot send messages in channel {cid} "
                "(missing Send Messages permission)"
            )
        except discord.HTTPException as e:
            return _err(f"discord rejected post: {e}")
        except Exception as e:
            log.exception("post_to_channel: send failed")
            return _err(f"post failed: {e}")

        return _ok(
            f"Posted to channel {cid} as {agent!r} "
            f"({len(chunks)} message(s); first id={first_id})"
        )

    def as_mcp_server(self):
        ctx = self

        @tool(
            "post_to_channel",
            "Post a message to a specific Discord channel by ID. "
            "channel_id MUST be passed as a STRING (e.g. \"123456789012345679\"), "
            "not a JSON number — Discord snowflake IDs are 64-bit and lose "
            "precision when serialized as JSON numbers (which are 53-bit "
            "doubles). The `agent` param picks which bot identity posts "
            "(default 'main' = Maine / Daedalus); use this from cron-driven "
            "prompts to post into channels outside the calling agent's own "
            "channel. Content over 2000 chars is auto-chunked into multiple "
            "messages.",
            {
                "channel_id": Annotated[
                    str,
                    "Discord channel snowflake ID as a JSON string "
                    "(e.g. \"123456789012345679\"). NOT a JSON number — "
                    "snowflakes are 64-bit and lose precision when serialized "
                    "as JSON numbers (53-bit doubles).",
                ],
                "content": str,
                "agent": str,
            },
        )
        async def post_to_channel(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.post_to_channel(
                channel_id=args.get("channel_id", ""),
                content=str(args.get("content", "")),
                agent=str(args.get("agent", "main")),
            )

        return create_sdk_mcp_server(
            name="discord",
            version="0.1.0",
            tools=[post_to_channel],
        )
