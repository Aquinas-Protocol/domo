"""Tests for src/video_tools.py — VideoContext, parser, and persistence."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from src.video_tools import (
    HARD_FRAME_CAP,
    MIN_TRANSCRIPT_CHARS,
    VideoContext,
    WatchResult,
    _parse_watch_report,
    _slugify,
)


# -------------------- Fixture stdouts --------------------
#
# These mirror the actual print() statements in upstream
# bradautomates/claude-video scripts/watch.py. If upstream changes its report
# layout, update these and _parse_watch_report's regex catalog together.

FIXTURE_WITH_WHISPER = """
# watch: video report

- **Source:** https://www.youtube.com/watch?v=dQw4w9WgXcQ
- **Title:** Rick Astley - Never Gonna Give You Up
- **Uploader:** Rick Astley
- **Duration:** 03:32 (212.0s)
- **Resolution:** 1280x720 (avc1.4d401f)
- **Frames:** 60 @ 0.283 fps, full mode (budget 60, max 80)
- **Frame size:** 512px wide
- **Transcript:** 47 segments (via whisper (groq))

## Frames

Frames live at: `/tmp/watch-abc/frames`

**Read each frame path below with the Read tool to view the image.** Frames are in chronological order; `t=MM:SS` is the absolute timestamp in the source video.

- `/tmp/watch-abc/frames/frame_0001.jpg` (t=00:00)
- `/tmp/watch-abc/frames/frame_0002.jpg` (t=00:03)
- `/tmp/watch-abc/frames/frame_0003.jpg` (t=00:07)

## Transcript

_Source: whisper (groq)._

```
[00:00] We're no strangers to love
[00:04] You know the rules and so do I
[00:08] A full commitment's what I'm thinking of
```

---
_Work dir: `/tmp/watch-abc` — delete when done._
"""

FIXTURE_NO_TRANSCRIPT = """
# watch: video report

- **Source:** https://example.com/silent.mp4
- **Title:** Silent Demo
- **Duration:** 00:30 (30.0s)
- **Frames:** 30 @ 1.000 fps, full mode (budget 30, max 80)
- **Frame size:** 512px wide
- **Transcript:** none available

## Frames

Frames live at: `/tmp/watch-xyz/frames`

**Read each frame path below with the Read tool to view the image.**

- `/tmp/watch-xyz/frames/frame_0001.jpg` (t=00:00)
- `/tmp/watch-xyz/frames/frame_0002.jpg` (t=00:01)

## Transcript

_No transcript available — proceed with frames only. Captions were missing and the Whisper fallback was unavailable._

---
_Work dir: `/tmp/watch-xyz` — delete when done._
"""

FIXTURE_FOCUSED_CAPTIONS = """
# watch: video report

- **Source:** https://www.youtube.com/watch?v=focus
- **Title:** Long Lecture
- **Duration:** 45:00 (2700.0s)
- **Focus range:** 05:00 → 10:00 (300.0s)
- **Frames:** 80 @ 0.267 fps, focused mode (budget 80, max 80)
- **Frame size:** 512px wide
- **Transcript:** 22 segments in range (via captions)

## Frames

Frames live at: `/tmp/watch-foc/frames`

**Read each frame path below with the Read tool to view the image.**

- `/tmp/watch-foc/frames/frame_0001.jpg` (t=05:00)
- `/tmp/watch-foc/frames/frame_0002.jpg` (t=05:15)

## Transcript

_Source: captions. Filtered to 05:00 → 10:00:_

```
[05:00] Welcome back. In this section we will cover gradient descent.
[05:15] First, recall the chain rule from calculus.
```

---
_Work dir: `/tmp/watch-foc` — delete when done._
"""


# -------------------- Parser tests --------------------


def test_parse_with_whisper_extracts_all_fields():
    r = _parse_watch_report(FIXTURE_WITH_WHISPER)
    assert r.title == "Rick Astley - Never Gonna Give You Up"
    assert r.uploader == "Rick Astley"
    assert r.duration == "03:32"
    assert r.transcript_source == "whisper (groq)"
    assert r.frame_paths == [
        "/tmp/watch-abc/frames/frame_0001.jpg",
        "/tmp/watch-abc/frames/frame_0002.jpg",
        "/tmp/watch-abc/frames/frame_0003.jpg",
    ]
    assert r.transcript_text is not None
    assert "We're no strangers to love" in r.transcript_text
    assert r.work_dir == "/tmp/watch-abc"


def test_parse_no_transcript_returns_none_transcript_text():
    r = _parse_watch_report(FIXTURE_NO_TRANSCRIPT)
    assert r.title == "Silent Demo"
    assert r.transcript_source is None
    assert r.transcript_text is None
    assert len(r.frame_paths) == 2
    assert r.work_dir == "/tmp/watch-xyz"


def test_parse_focused_captions_extracts_source():
    r = _parse_watch_report(FIXTURE_FOCUSED_CAPTIONS)
    assert r.transcript_source == "captions"
    assert r.transcript_text is not None
    assert "gradient descent" in r.transcript_text
    assert r.duration == "45:00"
    assert len(r.frame_paths) == 2


def test_parse_short_transcript_below_threshold_treated_as_none():
    """A pathological case: transcript fence present but content too short."""
    short = (
        "## Transcript\n\n_Source: captions._\n\n```\nhi\n```\n\n---\n"
        "_Work dir: `/tmp/x` — delete when done._\n"
    )
    r = _parse_watch_report(short)
    assert r.transcript_text is None  # below MIN_TRANSCRIPT_CHARS


def test_parse_handles_crlf_line_endings():
    """Windows: subprocess'd Python emits \\r\\n. Parser must normalize."""
    crlf = FIXTURE_WITH_WHISPER.replace("\n", "\r\n")
    r = _parse_watch_report(crlf)
    assert r.title == "Rick Astley - Never Gonna Give You Up"
    assert r.transcript_source == "whisper (groq)"
    assert r.transcript_text is not None
    assert "We're no strangers to love" in r.transcript_text
    assert len(r.frame_paths) == 3
    assert r.duration == "03:32"


# -------------------- Slug helper tests --------------------


def test_slugify_basic():
    assert _slugify("Rick Astley - Never Gonna Give You Up") == \
        "rick-astley-never-gonna-give-you-up"


def test_slugify_handles_punctuation_and_unicode():
    assert _slugify("café? wow!") == "caf-wow"


def test_slugify_empty_falls_back_to_untitled():
    assert _slugify("") == "untitled"
    assert _slugify("   ") == "untitled"


def test_slugify_truncates_at_max_len():
    long = "a" * 200
    assert len(_slugify(long, max_len=60)) == 60


# -------------------- Fake subprocess plumbing --------------------


class _FakeProcess:
    def __init__(self, *, stdout: bytes, stderr: bytes = b"", returncode: int = 0,
                 hang: bool = False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            # Wait long enough to exceed any reasonable test timeout — the
            # test triggers asyncio.wait_for with timeout=0.05 and expects
            # the wrapper to call .kill() on us.
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.killed = True
                raise
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def _patch_subprocess(monkeypatch, captured: list[list[str]], proc: _FakeProcess):
    async def fake_exec(*cmd: str, **_kwargs: Any) -> _FakeProcess:
        captured.append(list(cmd))
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def _make_ctx(tmp_path: Path) -> VideoContext:
    """Build a VideoContext rooted in tmp_path with a fake watch.py present."""
    skill_dir = tmp_path / "skill"
    (skill_dir / "scripts").mkdir(parents=True)
    fake_watch = skill_dir / "scripts" / "watch.py"
    fake_watch.write_text("# stub\n", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    transcripts_dir = tmp_path / "vault" / "raw" / "youtube-transcripts"
    return VideoContext(
        watch_py=fake_watch,
        runs_dir=runs_dir,
        transcripts_dir=transcripts_dir,
        vault_root=tmp_path / "vault",
    )


# -------------------- watch() integration tests --------------------


@pytest.mark.asyncio
async def test_watch_rejects_empty_url(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    result = await ctx.watch(url="")
    assert result.get("isError")
    assert "url is required" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_watch_errors_when_skill_missing(tmp_path: Path):
    ctx = VideoContext(
        watch_py=tmp_path / "does-not-exist" / "watch.py",
        runs_dir=tmp_path / "runs",
        transcripts_dir=tmp_path / "vault" / "raw" / "youtube-transcripts",
        vault_root=tmp_path / "vault",
    )
    result = await ctx.watch(url="https://example.com/v.mp4")
    assert result.get("isError")
    assert "watch.py not found" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_watch_persists_transcript_when_present(tmp_path: Path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    captured: list[list[str]] = []
    proc = _FakeProcess(stdout=FIXTURE_WITH_WHISPER.encode("utf-8"))
    _patch_subprocess(monkeypatch, captured, proc)

    result = await ctx.watch(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        question="what's the song about",
        max_frames=60,
    )

    import json as _json
    assert not result.get("isError")
    payload = _json.loads(result["content"][0]["text"])
    assert payload["title"] == "Rick Astley - Never Gonna Give You Up"
    assert payload["transcript_source"] == "whisper (groq)"
    assert payload["transcript_path"] is not None
    assert payload["transcript_path"].startswith("raw/youtube-transcripts/")
    assert "frame_paths" in payload and len(payload["frame_paths"]) == 3
    assert payload["question"] == "what's the song about"
    assert payload["watch_report_markdown"].startswith("\n# watch: video report")

    # Verify the file was actually written with proper frontmatter.
    written = ctx.transcripts_dir / Path(payload["transcript_path"]).name
    body = written.read_text(encoding="utf-8")
    assert "type: source-summary" in body
    assert "transcribed-by: whisper (groq)" in body
    assert "We're no strangers to love" in body
    assert 'title: "Rick Astley - Never Gonna Give You Up"' in body

    # Verify subprocess args (no shell, list form, frame cap honored).
    assert len(captured) == 1
    cmd = captured[0]
    assert str(ctx.watch_py) in cmd
    assert "--out-dir" in cmd
    assert "--max-frames" in cmd
    assert cmd[cmd.index("--max-frames") + 1] == "60"


@pytest.mark.asyncio
async def test_watch_persists_transcript_on_crlf_stdout(tmp_path: Path, monkeypatch):
    """End-to-end: Windows-style CRLF stdout must still produce a transcript file."""
    ctx = _make_ctx(tmp_path)
    crlf_bytes = FIXTURE_WITH_WHISPER.replace("\n", "\r\n").encode("utf-8")
    proc = _FakeProcess(stdout=crlf_bytes)
    _patch_subprocess(monkeypatch, [], proc)

    result = await ctx.watch(url="https://www.youtube.com/watch?v=x")

    import json as _json
    payload = _json.loads(result["content"][0]["text"])
    assert payload["transcript_path"] is not None
    assert payload["transcript_path"].startswith("raw/youtube-transcripts/")
    assert payload["title"] == "Rick Astley - Never Gonna Give You Up"
    # The returned report should have been normalized too — no stray \r.
    assert "\r\n" not in payload["watch_report_markdown"]
    written = ctx.transcripts_dir / Path(payload["transcript_path"]).name
    assert "We're no strangers to love" in written.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_watch_recovers_title_from_info_json_when_upstream_dropped_it(
    tmp_path: Path, monkeypatch
):
    """Upstream download.py loses title on Windows when read_text() trips
    cp1252 on UTF-8 info.json. Wrapper re-reads with explicit UTF-8."""
    ctx = _make_ctx(tmp_path)

    # Fixture stdout WITHOUT a Title or Uploader line — simulates upstream's
    # silent-Exception fallback that sets info = {"url": url}.
    stdout_no_title = """
# watch: video report

- **Source:** https://youtu.be/QZMljuD10sU
- **Duration:** 08:36 (516.0s)
- **Frames:** 30 @ 0.058 fps, full mode (budget 30, max 80)
- **Frame size:** 512px wide
- **Transcript:** 120 segments (via captions)

## Frames

Frames live at: `/tmp/x/frames`

- `/tmp/x/frames/frame_0001.jpg` (t=00:00)

## Transcript

_Source: captions._

```
[00:00] Welcome to the demo of the watch skill.
[00:05] In this video we cover frames and audio.
```

---
_Work dir: `/tmp/x` — delete when done._
"""
    proc = _FakeProcess(stdout=stdout_no_title.encode("utf-8"))

    # Inject the run id so we can pre-seed the info.json at the right path.
    captured: list[list[str]] = []
    async def fake_exec(*cmd: str, **_kwargs: Any) -> _FakeProcess:
        captured.append(list(cmd))
        # Find --out-dir arg, pre-write info.json into it before the wrapper
        # parses the result.
        out_idx = list(cmd).index("--out-dir")
        out_dir = Path(cmd[out_idx + 1])
        download_dir = out_dir / "download"
        download_dir.mkdir(parents=True, exist_ok=True)
        info = {
            "title": "My Claude Code Can INSTANTLY Watch Any Video (Here's How)",
            "uploader": "Brad | AI & Automation",
        }
        (download_dir / "video.info.json").write_text(
            __import__("json").dumps(info), encoding="utf-8",
        )
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await ctx.watch(url="https://youtu.be/QZMljuD10sU")

    import json as _json
    payload = _json.loads(result["content"][0]["text"])
    assert payload["title"] == \
        "My Claude Code Can INSTANTLY Watch Any Video (Here's How)"
    assert payload["uploader"] == "Brad | AI & Automation"
    # Filename should now use the recovered title, not run_id-doubled.
    assert payload["transcript_path"] is not None
    assert "untitled" not in payload["transcript_path"].lower()
    # Should contain a slug derived from the title, not the run-id-twice pattern.
    name = Path(payload["transcript_path"]).name
    assert "claude-code-can-instantly" in name


@pytest.mark.asyncio
async def test_watch_skips_persistence_when_no_transcript(tmp_path: Path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    captured: list[list[str]] = []
    proc = _FakeProcess(stdout=FIXTURE_NO_TRANSCRIPT.encode("utf-8"))
    _patch_subprocess(monkeypatch, captured, proc)

    result = await ctx.watch(url="https://example.com/silent.mp4")

    import json as _json
    payload = _json.loads(result["content"][0]["text"])
    assert payload["transcript_path"] is None
    assert payload["transcript_source"] is None
    # The transcripts dir should not contain a stub file.
    if ctx.transcripts_dir.exists():
        assert list(ctx.transcripts_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_watch_save_transcript_false_skips_persistence(tmp_path: Path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    proc = _FakeProcess(stdout=FIXTURE_WITH_WHISPER.encode("utf-8"))
    _patch_subprocess(monkeypatch, [], proc)

    result = await ctx.watch(
        url="https://www.youtube.com/watch?v=x",
        save_transcript=False,
    )

    import json as _json
    payload = _json.loads(result["content"][0]["text"])
    assert payload["transcript_path"] is None
    if ctx.transcripts_dir.exists():
        assert list(ctx.transcripts_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_watch_caps_max_frames_to_hard_limit(tmp_path: Path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    captured: list[list[str]] = []
    proc = _FakeProcess(stdout=FIXTURE_WITH_WHISPER.encode("utf-8"))
    _patch_subprocess(monkeypatch, captured, proc)

    await ctx.watch(url="https://x", max_frames=999)

    cmd = captured[0]
    idx = cmd.index("--max-frames")
    assert int(cmd[idx + 1]) == HARD_FRAME_CAP


@pytest.mark.asyncio
async def test_watch_passes_focus_range_args(tmp_path: Path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    captured: list[list[str]] = []
    proc = _FakeProcess(stdout=FIXTURE_FOCUSED_CAPTIONS.encode("utf-8"))
    _patch_subprocess(monkeypatch, captured, proc)

    await ctx.watch(url="https://x", start="05:00", end="10:00", no_whisper=True)

    cmd = captured[0]
    assert "--start" in cmd and cmd[cmd.index("--start") + 1] == "05:00"
    assert "--end" in cmd and cmd[cmd.index("--end") + 1] == "10:00"
    assert "--no-whisper" in cmd


@pytest.mark.asyncio
async def test_watch_returns_error_on_nonzero_exit(tmp_path: Path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    proc = _FakeProcess(
        stdout=b"",
        stderr=b"yt-dlp: ERROR: video unavailable",
        returncode=1,
    )
    _patch_subprocess(monkeypatch, [], proc)

    result = await ctx.watch(url="https://x")
    assert result.get("isError")
    text = result["content"][0]["text"]
    assert "exited 1" in text
    assert "video unavailable" in text


@pytest.mark.asyncio
async def test_watch_kills_subprocess_on_timeout(tmp_path: Path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    proc = _FakeProcess(stdout=b"", hang=True)
    _patch_subprocess(monkeypatch, [], proc)

    # Tiny timeout to make this fast and deterministic.
    result = await ctx.watch(url="https://x", timeout_s=1)

    assert result.get("isError")
    assert "timed out" in result["content"][0]["text"]
    assert proc.killed


# -------------------- as_mcp_server smoke --------------------


def test_as_mcp_server_constructs(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    server = ctx.as_mcp_server()
    assert server is not None


# -------------------- Sanity on threshold constant --------------------


def test_min_transcript_chars_is_sane():
    """Guard against future tweaks accidentally letting empty fences through."""
    assert MIN_TRANSCRIPT_CHARS >= 10
