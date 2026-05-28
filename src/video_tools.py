"""MCP tool: watch_video — wraps the bradautomates/claude-video /watch skill.

Subprocesses scripts/watch.py, parses the stdout markdown report, and (when a
real transcript is produced) auto-persists it under
<vault>/raw/youtube-transcripts/<date>-<slug>-<run_id>.md so the existing
/ingest pipeline picks it up.

Registered ONLY for the research agent (Intel). Maine reaches video work via
delegate_to_research; Hermes does not consume video.

watch.py prints a markdown report to stdout with frame paths and a fenced
transcript block; it does not write a separate transcript file. We parse the
stdout and only persist a vault file when transcript text is actually present.
Empty / silent videos return transcript_path=None so /ingest isn't fed stubs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from src.config import DATA_DIR, VAULT_ROOT

log = logging.getLogger(__name__)

# Where /plugin install puts the skill on disk. Resolved at call-time (not
# import-time) so plugin updates are picked up on the next watch_video call
# without a bot restart. Override with WATCH_SKILL_DIR pointing at a directory
# whose `scripts/watch.py` is the canonical entry point.
_PLUGIN_CACHE_ROOT = Path.home() / ".claude" / "plugins" / "cache" / "claude-video" / "watch"
_MARKETPLACE_MIRROR = (
    Path.home() / ".claude" / "plugins" / "marketplaces" / "claude-video" / "scripts" / "watch.py"
)
_LEGACY_SKILLS_DIR = Path.home() / ".claude" / "skills" / "watch"


def _version_key(p: Path) -> tuple[int, ...]:
    """Sort key for a version-named directory like '0.1.2'. Non-numeric → 0."""
    parts: list[int] = []
    for chunk in p.name.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _resolve_watch_py() -> Path:
    """Find the active scripts/watch.py. Existence is checked by the caller."""
    override = os.getenv("WATCH_SKILL_DIR")
    if override:
        return Path(override) / "scripts" / "watch.py"
    if _PLUGIN_CACHE_ROOT.exists():
        versions = [p for p in _PLUGIN_CACHE_ROOT.iterdir() if p.is_dir()]
        if versions:
            latest = max(versions, key=_version_key)
            candidate = latest / "scripts" / "watch.py"
            if candidate.exists():
                return candidate
    if _MARKETPLACE_MIRROR.exists():
        return _MARKETPLACE_MIRROR
    return _LEGACY_SKILLS_DIR / "scripts" / "watch.py"

WATCH_RUNS_DIR = DATA_DIR / "watch-runs"
TRANSCRIPTS_DIR = VAULT_ROOT / "raw" / "youtube-transcripts"

DEFAULT_TIMEOUT_S = 600
HARD_FRAME_CAP = 100
MIN_TRANSCRIPT_CHARS = 20  # below this, treat as no usable transcript


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _slugify(value: str, max_len: int = 60) -> str:
    """Lowercase ASCII slug. Collapses non-alnum runs to single hyphens."""
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:max_len].rstrip("-") or "untitled"


@dataclass
class WatchResult:
    title: str | None = None
    uploader: str | None = None
    duration: str | None = None           # human-readable, e.g. "12:34"
    transcript_source: str | None = None  # "captions" / "whisper (groq)" / etc.
    frame_paths: list[str] = field(default_factory=list)
    transcript_text: str | None = None    # None when not present or too short
    work_dir: str | None = None           # parsed from watch.py's footer
    raw_report: str = ""


# Regex catalog — anchored to the actual print() statements in upstream watch.py.
# If upstream changes its report format these will need to follow.
_RE_TITLE = re.compile(r"^- \*\*Title:\*\* (.+)$", re.MULTILINE)
_RE_UPLOADER = re.compile(r"^- \*\*Uploader:\*\* (.+)$", re.MULTILINE)
_RE_DURATION = re.compile(r"^- \*\*Duration:\*\* (.+?) \(", re.MULTILINE)
# "via X" only appears when a transcript exists; "none available" appears alone.
_RE_TRANSCRIPT_VIA = re.compile(
    r"^- \*\*Transcript:\*\* \d+ segments(?:\s+in range)?\s+\(via (.+?)\)$",
    re.MULTILINE,
)
_RE_FRAME_LINE = re.compile(r"^- `([^`]+)` \(t=([0-9:]+)\)$", re.MULTILINE)
# Transcript fenced block sits under "## Transcript" header and ends with the
# trailing "---" divider. Capture the first ```...``` block after the header.
_RE_TRANSCRIPT_BLOCK = re.compile(
    r"## Transcript\b.*?\n```\n(.*?)\n```",
    re.DOTALL,
)
_RE_WORKDIR = re.compile(r"_Work dir: `([^`]+)`")


def _read_info_json(work_dir: Path) -> dict[str, Any]:
    """Read video.info.json from the work dir with explicit UTF-8.

    Upstream download.py calls Path.read_text() without `encoding=`, which
    trips Windows cp1252 on yt-dlp's UTF-8 info.json (especially on large
    files with non-ASCII description / comment text). The bare `except` there
    silently drops title + uploader from the stdout report. We re-read with
    UTF-8 explicitly so the persisted vault file gets a real title.
    """
    info_path = work_dir / "download" / "video.info.json"
    if not info_path.exists():
        return {}
    try:
        return json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_watch_report(stdout: str) -> WatchResult:
    """Extract structured fields from watch.py's stdout markdown report.

    Defensively normalizes CRLF → LF so the line-anchored regexes match on
    Windows, where Python's child-process print() emits \\r\\n even when the
    parent pipes stdout. Callers that already normalize are unaffected.
    """
    stdout = stdout.replace("\r\n", "\n").replace("\r", "\n")
    out = WatchResult(raw_report=stdout)
    if m := _RE_TITLE.search(stdout):
        out.title = m.group(1).strip()
    if m := _RE_UPLOADER.search(stdout):
        out.uploader = m.group(1).strip()
    if m := _RE_DURATION.search(stdout):
        out.duration = m.group(1).strip()
    if m := _RE_TRANSCRIPT_VIA.search(stdout):
        source = (m.group(1) or "").strip()
        out.transcript_source = source or None
    out.frame_paths = [m.group(1) for m in _RE_FRAME_LINE.finditer(stdout)]
    if m := _RE_TRANSCRIPT_BLOCK.search(stdout):
        text = m.group(1).strip()
        if len(text) >= MIN_TRANSCRIPT_CHARS:
            out.transcript_text = text
    if m := _RE_WORKDIR.search(stdout):
        out.work_dir = m.group(1).strip()
    return out


def _build_transcript_frontmatter(
    *,
    title: str | None,
    url: str,
    duration: str | None,
    transcript_source: str | None,
    frames_count: int,
    today_iso: str,
) -> str:
    """Render YAML frontmatter matching the vault's CLAUDE.md §2 schema."""
    safe_title = (title or "Untitled video").replace('"', "'")
    if transcript_source == "captions":
        transcribed_by = "native-captions"
    elif transcript_source:
        transcribed_by = transcript_source
    else:
        transcribed_by = "unknown"
    return (
        "---\n"
        f'title: "{safe_title}"\n'
        "type: source-summary\n"
        "sources:\n"
        f"  - {url}\n"
        "related: []\n"
        f"created: {today_iso}\n"
        f"last-updated: {today_iso}\n"
        f'duration: "{duration or "unknown"}"\n'
        f"transcribed-by: {transcribed_by}\n"
        f"frames-analyzed: {frames_count}\n"
        "---\n"
    )


@dataclass(frozen=True)
class _RunPaths:
    run_id: str
    short_id: str
    work_dir: Path

    @classmethod
    def new(cls, root: Path) -> "_RunPaths":
        rid = uuid.uuid4().hex
        return cls(run_id=rid, short_id=rid[:8], work_dir=root / rid)


class VideoContext:
    """Holds references the video MCP tool needs.

    Constructor takes paths so tests can swap them out with tmp_path / fakes.
    Defaults match production layout.
    """

    def __init__(
        self,
        *,
        watch_py: Path | None = None,
        runs_dir: Path = WATCH_RUNS_DIR,
        transcripts_dir: Path = TRANSCRIPTS_DIR,
        vault_root: Path = VAULT_ROOT,
        python_exe: str | None = None,
    ):
        # Resolve watch_py lazily so plugin updates are picked up without
        # a bot restart. Tests pass an explicit path to bypass discovery.
        self._explicit_watch_py = Path(watch_py) if watch_py is not None else None
        self.runs_dir = Path(runs_dir)
        self.transcripts_dir = Path(transcripts_dir)
        self.vault_root = Path(vault_root)
        self.python_exe = python_exe or sys.executable

    @property
    def watch_py(self) -> Path:
        return self._explicit_watch_py or _resolve_watch_py()

    async def watch(
        self,
        *,
        url: str,
        question: str | None = None,
        max_frames: int = 80,
        save_transcript: bool = True,
        start: str | None = None,
        end: str | None = None,
        no_whisper: bool = False,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        url = (url or "").strip()
        if not url:
            return _err("url is required")

        if not self.watch_py.exists():
            return _err(
                f"watch.py not found at {self.watch_py}. Install with "
                "`/plugin marketplace add bradautomates/claude-video` then "
                "`/plugin install watch@claude-video`. Override the location "
                "by setting WATCH_SKILL_DIR before starting the bot."
            )

        run = _RunPaths.new(self.runs_dir)
        run.work_dir.mkdir(parents=True, exist_ok=True)
        capped_frames = max(1, min(int(max_frames), HARD_FRAME_CAP))

        cmd: list[str] = [
            self.python_exe,
            str(self.watch_py),
            url,
            "--out-dir", str(run.work_dir),
            "--max-frames", str(capped_frames),
        ]
        if start:
            cmd += ["--start", str(start)]
        if end:
            cmd += ["--end", str(end)]
        if no_whisper:
            cmd += ["--no-whisper"]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return _err(f"failed to launch watch.py: {type(e).__name__}: {e}")

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=int(timeout_s)
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return _err(f"watch_video timed out after {timeout_s}s")

        if proc.returncode != 0:
            stderr_tail = (stderr_b.decode("utf-8", errors="replace") or "").strip()[-1500:]
            return _err(
                f"watch.py exited {proc.returncode}. stderr tail:\n{stderr_tail}"
            )

        # Normalize CRLF → LF so line-anchored regexes work. Python's print()
        # on Windows emits \r\n even when stdout is piped, which would break
        # _parse_watch_report's ^...$ matchers and leave transcript_text=None.
        stdout = (
            stdout_b.decode("utf-8", errors="replace")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
        result = _parse_watch_report(stdout)

        # Recover title/uploader from info.json if upstream's Windows
        # cp1252 read_text() ate them silently. Belt-and-suspenders.
        if result.title is None or result.uploader is None:
            info = _read_info_json(run.work_dir)
            if info:
                result.title = result.title or info.get("title")
                result.uploader = (
                    result.uploader
                    or info.get("uploader")
                    or info.get("channel")
                )

        transcript_path: str | None = None
        if save_transcript and result.transcript_text:
            try:
                transcript_path = self._persist_transcript(
                    url=url, result=result, run=run,
                )
            except OSError as e:
                log.warning("transcript persist failed: %s", e)
                # Don't fail the whole call — agent still has the report.

        payload: dict[str, Any] = {
            "title": result.title,
            "uploader": result.uploader,
            "duration": result.duration,
            "transcript_path": transcript_path,
            "transcript_source": result.transcript_source,
            "frame_paths": result.frame_paths,
            "frames_count": len(result.frame_paths),
            "work_dir": result.work_dir or str(run.work_dir),
            "watch_report_markdown": stdout,
        }
        if question:
            payload["question"] = question
        return _ok(json.dumps(payload, indent=2))

    def _persist_transcript(
        self,
        *,
        url: str,
        result: WatchResult,
        run: _RunPaths,
    ) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        slug = _slugify(result.title or run.short_id)
        filename = f"{today}-{slug}-{run.short_id}.md"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        target = self.transcripts_dir / filename
        frontmatter = _build_transcript_frontmatter(
            title=result.title,
            url=url,
            duration=result.duration,
            transcript_source=result.transcript_source,
            frames_count=len(result.frame_paths),
            today_iso=today,
        )
        body = (
            f"# {result.title or 'Untitled video'}\n\n"
            "## Overview\n"
            "(left blank — populated by /ingest when it builds the wiki "
            "source-summary page)\n\n"
            "## Transcript\n"
            f"```\n{result.transcript_text}\n```\n"
        )
        target.write_text(frontmatter + "\n" + body, encoding="utf-8")
        try:
            rel = target.relative_to(self.vault_root)
            return str(rel).replace("\\", "/")
        except ValueError:
            return str(target).replace("\\", "/")

    def as_mcp_server(self):
        ctx = self

        @tool(
            "watch_video",
            (
                "Download a video, extract frames, and transcribe via the "
                "bradautomates/claude-video /watch skill. Accepts video URLs "
                "(YouTube, Vimeo, TikTok, X, Twitch, Instagram, etc.) or local "
                "file paths. Returns a JSON payload with frame file paths, the "
                "transcript source, the saved transcript path (when a real "
                "transcript was produced), and the full report. "
                "**You MUST call the Read tool on each path in `frame_paths` "
                "to view the frames before answering** — this tool does not "
                "analyze images itself; you analyze them via Claude's "
                "multimodal Read. Frames remain readable for the rest of this "
                "conversation under data/watch-runs/<run_id>/frames/. "
                "When `save_transcript` is true (default), the transcript is "
                "also written to the vault's raw/youtube-transcripts/ so the "
                "nightly /ingest pipeline picks it up. Use `start`/`end` "
                "(MM:SS) to focus on a section of long videos. Use "
                "`no_whisper=true` to skip Whisper and rely on native captions "
                "only. `max_frames` is capped at 100 by upstream."
            ),
            {
                "url": str,
                "question": str,
                "max_frames": int,
                "save_transcript": bool,
                "start": str,
                "end": str,
                "no_whisper": bool,
                "timeout_s": int,
            },
        )
        async def watch_video(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.watch(
                url=args.get("url", ""),
                question=args.get("question") or None,
                max_frames=int(args.get("max_frames") or 80),
                save_transcript=bool(args.get("save_transcript", True)),
                start=args.get("start") or None,
                end=args.get("end") or None,
                no_whisper=bool(args.get("no_whisper", False)),
                timeout_s=int(args.get("timeout_s") or DEFAULT_TIMEOUT_S),
            )

        return create_sdk_mcp_server(
            name="video",
            version="0.1.0",
            tools=[watch_video],
        )
