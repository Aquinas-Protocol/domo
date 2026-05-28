"""Tests for src/discord_tools.py — DiscordContext.post_to_channel."""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest

from src.discord_tools import DiscordContext, _chunk_for_discord


# ---------------- fakes ----------------


class FakeMessage:
    def __init__(self, id_: int):
        self.id = id_


class FakeChannel:
    def __init__(self, send_should_raise: BaseException | None = None):
        self.sent: list[str] = []
        self._raise = send_should_raise
        self._next_id = 1000

    async def send(self, content: str) -> FakeMessage:
        if self._raise is not None:
            raise self._raise
        self.sent.append(content)
        msg = FakeMessage(self._next_id)
        self._next_id += 1
        return msg


class FakeBot:
    """Mimics the bits of discord.Client we use: get_channel + fetch_channel."""

    def __init__(
        self,
        channel: FakeChannel | None = None,
        fetch_raises: BaseException | None = None,
        cached: bool = True,
    ):
        # `cached` toggles whether get_channel returns the channel (cache hit)
        # or None (forcing a fetch_channel call).
        self._channel = channel
        self._fetch_raises = fetch_raises
        self._cached = cached

    def get_channel(self, cid: int):
        return self._channel if self._cached else None

    async def fetch_channel(self, cid: int):
        if self._fetch_raises is not None:
            raise self._fetch_raises
        if self._channel is None:
            response = MagicMock(status=404, reason="Not Found")
            raise discord.NotFound(response, "channel not found")
        return self._channel


def _is_error(result: dict) -> bool:
    return bool(result.get("isError"))


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _forbidden() -> discord.Forbidden:
    response = MagicMock(status=403, reason="Forbidden")
    return discord.Forbidden(response, "missing perms")


def _http_error(status: int = 500, msg: str = "boom") -> discord.HTTPException:
    response = MagicMock(status=status, reason="Server Error")
    return discord.HTTPException(response, msg)


# ---------------- success paths ----------------


@pytest.mark.asyncio
async def test_post_to_channel_success_default_agent():
    ch = FakeChannel()
    ctx = DiscordContext(bots={"main": FakeBot(channel=ch)})
    result = await ctx.post_to_channel(channel_id="42", content="hello")
    assert not _is_error(result)
    assert "Posted to channel 42" in _text(result)
    assert "main" in _text(result)
    assert ch.sent == ["hello"]


@pytest.mark.asyncio
async def test_post_to_channel_success_explicit_agent():
    ch = FakeChannel()
    ctx = DiscordContext(
        bots={"main": FakeBot(channel=None), "comms": FakeBot(channel=ch)}
    )
    result = await ctx.post_to_channel(
        channel_id="99", content="hi from hermes", agent="comms"
    )
    assert not _is_error(result)
    assert ch.sent == ["hi from hermes"]
    assert "'comms'" in _text(result)


@pytest.mark.asyncio
async def test_post_to_channel_uses_get_channel_first():
    """Cache hit: get_channel returns; fetch_channel must NOT be called."""
    ch = FakeChannel()
    bot = FakeBot(
        channel=ch,
        fetch_raises=RuntimeError("fetch_channel should not be called"),
        cached=True,
    )
    ctx = DiscordContext(bots={"main": bot})
    result = await ctx.post_to_channel(channel_id="1", content="x")
    assert not _is_error(result)


@pytest.mark.asyncio
async def test_post_to_channel_falls_back_to_fetch_on_cache_miss():
    """Cache miss: get_channel returns None, fetch_channel resolves it."""
    ch = FakeChannel()
    bot = FakeBot(channel=ch, cached=False)
    ctx = DiscordContext(bots={"main": bot})
    result = await ctx.post_to_channel(channel_id="1", content="x")
    assert not _is_error(result)
    assert ch.sent == ["x"]


@pytest.mark.asyncio
async def test_post_to_channel_chunks_long_content():
    ch = FakeChannel()
    ctx = DiscordContext(bots={"main": FakeBot(channel=ch)})
    long = "a" * 2050
    result = await ctx.post_to_channel(channel_id="1", content=long)
    assert not _is_error(result)
    assert len(ch.sent) >= 2
    assert "2 message" in _text(result)


# ---------------- error paths ----------------


@pytest.mark.asyncio
async def test_post_to_channel_unknown_agent():
    ctx = DiscordContext(bots={"main": FakeBot(channel=FakeChannel())})
    result = await ctx.post_to_channel(channel_id="1", content="x", agent="ghost")
    assert _is_error(result)
    assert "unknown agent" in _text(result)


@pytest.mark.asyncio
async def test_post_to_channel_empty_agent_falls_back_to_main():
    ch = FakeChannel()
    ctx = DiscordContext(bots={"main": FakeBot(channel=ch)})
    result = await ctx.post_to_channel(channel_id="1", content="x", agent="   ")
    assert not _is_error(result)
    assert ch.sent == ["x"]


@pytest.mark.asyncio
async def test_post_to_channel_empty_content_rejected():
    ctx = DiscordContext(bots={"main": FakeBot(channel=FakeChannel())})
    result = await ctx.post_to_channel(channel_id="1", content="   ")
    assert _is_error(result)
    assert "content is required" in _text(result)


@pytest.mark.asyncio
async def test_post_to_channel_invalid_channel_id():
    ctx = DiscordContext(bots={"main": FakeBot(channel=FakeChannel())})
    result = await ctx.post_to_channel(channel_id="abc", content="x")
    assert _is_error(result)
    assert "digits only" in _text(result)


@pytest.mark.asyncio
async def test_post_to_channel_accepts_snowflake_string_without_precision_loss():
    """Discord snowflakes are 64-bit; passed as string they survive a round trip.

    Regression: schema was originally `int`, which forced the LLM to emit a JSON
    number — quantized to a 53-bit double, dropping the trailing digits. Schema
    is now `str`; the int cast happens server-side, preserving precision.
    """
    snowflake = "123456789012345679"  # > 2**53, would round if JSON-numerized
    ch = FakeChannel()
    ctx = DiscordContext(bots={"main": FakeBot(channel=ch)})
    result = await ctx.post_to_channel(channel_id=snowflake, content="x")
    assert not _is_error(result)
    assert snowflake in _text(result)  # full ID printed back, no truncation


@pytest.mark.asyncio
async def test_post_to_channel_rejects_int_input_loudly():
    """Snowflake guard: int input means JSON-number serialization happened
    upstream; reject so the LLM gets isError back and the cron prompt's
    STOP CONDITIONS abort the firing rather than posting to a wrong channel."""
    ctx = DiscordContext(bots={"main": FakeBot(channel=FakeChannel())})
    result = await ctx.post_to_channel(channel_id=123456789012345679, content="x")  # type: ignore[arg-type]
    assert _is_error(result)
    assert "JSON integer" in _text(result)


def test_post_to_channel_schema_carries_precision_warning():
    """Schema regression: the channel_id parameter schema must keep the
    Annotated precision warning so the LLM sees it inside the tool spec.
    Without this, the inline JSON-Schema description disappears and the
    LLM has only the prose docstring to lean on — historically insufficient."""
    import inspect
    src = inspect.getsource(DiscordContext.as_mcp_server)
    assert "Annotated[" in src
    assert "JSON number" in src
    assert "64-bit" in src


@pytest.mark.asyncio
async def test_post_to_channel_not_found():
    bot = FakeBot(channel=None, cached=False)
    ctx = DiscordContext(bots={"main": bot})
    result = await ctx.post_to_channel(channel_id="1", content="x")
    assert _is_error(result)
    assert "not found" in _text(result)


@pytest.mark.asyncio
async def test_post_to_channel_fetch_forbidden():
    bot = FakeBot(channel=None, fetch_raises=_forbidden(), cached=False)
    ctx = DiscordContext(bots={"main": bot})
    result = await ctx.post_to_channel(channel_id="1", content="x")
    assert _is_error(result)
    assert "lacks access" in _text(result)


@pytest.mark.asyncio
async def test_post_to_channel_send_forbidden():
    ch = FakeChannel(send_should_raise=_forbidden())
    ctx = DiscordContext(bots={"main": FakeBot(channel=ch)})
    result = await ctx.post_to_channel(channel_id="1", content="x")
    assert _is_error(result)
    assert "Send Messages" in _text(result)


@pytest.mark.asyncio
async def test_post_to_channel_http_exception_on_send():
    ch = FakeChannel(send_should_raise=_http_error())
    ctx = DiscordContext(bots={"main": FakeBot(channel=ch)})
    result = await ctx.post_to_channel(channel_id="1", content="x")
    assert _is_error(result)
    assert "discord rejected" in _text(result)


# ---------------- chunker ----------------


def test_chunk_short_content_one_chunk():
    assert _chunk_for_discord("short") == ["short"]


def test_chunk_paragraph_boundary():
    text = ("a" * 1500) + "\n\n" + ("b" * 600)
    chunks = _chunk_for_discord(text)
    assert len(chunks) == 2
    assert chunks[0].startswith("a") and chunks[0].endswith("a")
    assert chunks[1].startswith("b")
    assert all(len(c) <= 2000 for c in chunks)


def test_chunk_falls_back_to_line_break():
    text = ("a" * 1500) + "\n" + ("b" * 600)
    chunks = _chunk_for_discord(text)
    assert len(chunks) == 2
    assert all(len(c) <= 2000 for c in chunks)


def test_chunk_no_breakpoint_falls_back_to_hard_cut():
    text = "a" * 2050
    chunks = _chunk_for_discord(text)
    assert len(chunks) == 2
    assert len(chunks[0]) == 2000
    assert len(chunks[1]) == 50


# ---------------- as_mcp_server smoke ----------------


def test_as_mcp_server_constructs():
    ctx = DiscordContext(bots={"main": FakeBot()})
    server = ctx.as_mcp_server()
    assert server is not None
