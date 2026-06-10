"""discord.py voice playback wrapper: join -> play -> wait -> disconnect.

``VoiceClient.play()`` is non-blocking; completion is signaled from the
player thread via the ``after`` callback, bridged onto the loop with
``call_soon_threadsafe``. The client always disconnects on exit so no
orphan voice connection outlives a failed playback.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 15.0
PLAYBACK_MAX_S = 180.0  # hard cap; a ~60s summary should never hit this


class VoicePlayError(RuntimeError):
    """Playback failed: bad channel, connect failure, or ffmpeg/player error."""


async def play_audio_in_channel(
    bot: discord.Client,
    channel_id: int,
    audio_path: Path,
) -> float:
    """Play ``audio_path`` in voice channel ``channel_id``; return seconds played."""
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException as exc:
            raise VoicePlayError(
                f"voice channel {channel_id} not found: {exc}"
            ) from exc
    if not isinstance(channel, discord.VoiceChannel):
        raise VoicePlayError(f"channel {channel_id} is not a voice channel")

    # Reuse/move an existing voice connection in this guild (idempotent join).
    vc = discord.utils.get(bot.voice_clients, guild=channel.guild)
    if vc is not None and vc.channel and vc.channel.id != channel_id:
        await vc.move_to(channel)
    elif vc is None:
        try:
            vc = await channel.connect(timeout=CONNECT_TIMEOUT_S)
        except (asyncio.TimeoutError, discord.DiscordException) as exc:
            raise VoicePlayError(
                f"could not connect to voice channel {channel.name!r}: {exc!r}"
            ) from exc

    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    play_error: Exception | None = None

    def _after(exc: Exception | None) -> None:
        nonlocal play_error
        play_error = exc
        loop.call_soon_threadsafe(done.set)

    try:
        try:
            vc.play(discord.FFmpegPCMAudio(str(audio_path)), after=_after)
        except discord.DiscordException as exc:
            raise VoicePlayError(f"could not start playback: {exc!r}") from exc
        start = loop.time()
        try:
            await asyncio.wait_for(done.wait(), timeout=PLAYBACK_MAX_S)
        except asyncio.TimeoutError as exc:
            vc.stop()
            raise VoicePlayError(
                f"playback exceeded the {PLAYBACK_MAX_S:.0f}s cap"
            ) from exc
        if play_error is not None:
            raise VoicePlayError(f"player error: {play_error!r}")
        return loop.time() - start
    finally:
        try:
            await vc.disconnect(force=True)
        except Exception:
            log.exception("voice disconnect failed for channel %s", channel_id)
