"""Thin httpx client for the ElevenLabs streaming text-to-speech endpoint.

Deliberately no ``elevenlabs`` SDK: one POST to
``/v1/text-to-speech/{voice_id}/stream``, body streamed to a temp ``.mp3``
whose path is returned. Model and output format are pinned for cost/latency
(``eleven_turbo_v2_5`` @ ``mp3_22050_32``). Sync on purpose — callers on the
event loop wrap it in ``asyncio.to_thread``, the same idiom as the store.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import httpx

from src import config

log = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io"
MODEL_ID = "eleven_turbo_v2_5"
DEFAULT_OUTPUT_FORMAT = "mp3_22050_32"
TIMEOUT_S = 60.0


class TTSError(RuntimeError):
    """Synthesis failed: missing config, transport error, non-200, or empty audio."""


def synthesize(
    text: str,
    *,
    voice_id: str | None = None,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
) -> Path:
    """Synthesize ``text`` and return the path of a temp ``.mp3``.

    The caller owns budget enforcement (src/voice/budget.py) and deletion of
    the returned file.
    """
    if not text or not text.strip():
        raise TTSError("empty text")
    voice = voice_id or config.ELEVENLABS_VOICE_ID
    if not config.ELEVENLABS_API_KEY or not voice:
        raise TTSError("ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID not configured")

    url = f"{API_BASE}/v1/text-to-speech/{voice}/stream"
    out_path: Path | None = None
    total = 0
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            with client.stream(
                "POST",
                url,
                params={"output_format": output_format},
                headers={"xi-api-key": config.ELEVENLABS_API_KEY},
                json={"text": text, "model_id": MODEL_ID},
            ) as resp:
                if resp.status_code != 200:
                    body = resp.read()[:300]
                    raise TTSError(
                        f"TTS HTTP {resp.status_code}: "
                        f"{body.decode('utf-8', 'replace')}"
                    )
                fd, name = tempfile.mkstemp(suffix=".mp3", prefix="nike-tts-")
                out_path = Path(name)
                with os.fdopen(fd, "wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
                        total += len(chunk)
                if total == 0:
                    raise TTSError("TTS returned empty audio body")
    except httpx.HTTPError as exc:
        if out_path is not None:
            out_path.unlink(missing_ok=True)
        raise TTSError(f"TTS transport error: {exc!r}") from exc
    except TTSError:
        if out_path is not None:
            out_path.unlink(missing_ok=True)
        raise

    log.info("synthesized %d chars -> %s (%d bytes)", len(text), out_path, total)
    return out_path
