"""Codex CLI backed GPT-5 specialist MCP tool."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from src.config import SPECIALIST_DEFAULT_TIMEOUT_SECONDS
from src.specialist_store import SpecialistStore
from src.store import Ledger, SessionStore

log = logging.getLogger(__name__)

LEDGER_KEY = "specialist:gpt5"
REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
# Prompts longer than this are delivered to codex via stdin (the `-` prompt
# sentinel) instead of as a command-line argument. codex.cmd is invoked through
# cmd.exe, whose command line is capped near 8 KB; a document-sized prompt (e.g.
# the mission evaluator inlining a brief) overflows it with "The command line is
# too long." stdin has no such limit.
PROMPT_STDIN_THRESHOLD = 4000


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


@dataclass(frozen=True)
class CodexExecEvents:
    session_id: str | None
    final_text: str | None
    cost_usd: float | None
    usage: dict[str, Any] | None


@dataclass(frozen=True)
class CodexProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _find_first_key(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return value[key]
        for child in value.values():
            found = _find_first_key(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first_key(child, keys)
            if found is not None:
                return found
    return None


def _extract_cost_usd(event: dict[str, Any]) -> float | None:
    for key in ("cost_usd", "total_cost_usd", "cost"):
        value = event.get(key)
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, dict):
            nested = value.get("usd") or value.get("cost_usd") or value.get("total_cost_usd")
            if isinstance(nested, int | float):
                return float(nested)

    usage = event.get("usage")
    if isinstance(usage, dict):
        for key in ("cost_usd", "total_cost_usd", "cost"):
            value = usage.get(key)
            if isinstance(value, int | float):
                return float(value)
    return None


def _extract_agent_text(event: dict[str, Any]) -> str | None:
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") == "agent_message":
        text = item.get("text")
        if isinstance(text, str):
            return text

    message = event.get("message")
    if isinstance(message, dict):
        text = message.get("text")
        if isinstance(text, str):
            return text
        content = message.get("content")
        if isinstance(content, list):
            parts = [
                block.get("text")
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            if parts:
                return "".join(parts)
    return None


def parse_codex_jsonl(text: str) -> CodexExecEvents:
    session_id: str | None = None
    final_text: str | None = None
    cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    session_keys = {"thread_id", "threadId", "session_id", "sessionId"}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        event_type = str(event.get("type", "")).replace("/", ".")
        if event_type.endswith("started"):
            found = _find_first_key(event, session_keys)
            if isinstance(found, str) and found:
                session_id = session_id or found

        found = _find_first_key(event, session_keys)
        if isinstance(found, str) and found:
            session_id = session_id or found

        text_part = _extract_agent_text(event)
        if text_part is not None:
            final_text = text_part

        if isinstance(event.get("usage"), dict):
            usage = event["usage"]

        cost = _extract_cost_usd(event)
        if cost is not None:
            cost_usd = cost

    return CodexExecEvents(
        session_id=session_id,
        final_text=final_text,
        cost_usd=cost_usd,
        usage=usage,
    )


def _make_temp_output_path() -> Path:
    fd, raw_path = tempfile.mkstemp(prefix="codex-last-message-", suffix=".txt")
    os.close(fd)
    return Path(raw_path)


def _read_text_if_present(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _cleanup_temp(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("failed to remove temp Codex output %s", path, exc_info=True)


def _summarize_failure(stderr: str, stdout: str = "") -> str:
    combined = (stderr or stdout or "codex exec failed").strip()
    if len(combined) > 1200:
        return combined[:1200] + "...[truncated]"
    return combined


async def _run_codex_process(
    argv: list[str], timeout_s: int, *, stdin_text: str | None = None
) -> CodexProcessResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=stdin_bytes),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise
    return CodexProcessResult(
        returncode=proc.returncode or 0,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
    )


async def codex_health_probe(
    codex_exe: str | Path,
    *,
    timeout_s: int = 30,
) -> tuple[bool, str]:
    output_path = _make_temp_output_path()
    argv = [
        str(codex_exe),
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--output-last-message",
        str(output_path),
        "Reply 'pong' and nothing else.",
    ]
    try:
        result = await _run_codex_process(argv, timeout_s)
    except asyncio.TimeoutError:
        return False, f"codex health probe timed out after {timeout_s}s"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        final_text = _read_text_if_present(output_path)
        _cleanup_temp(output_path)

    if result.returncode != 0:
        return False, _summarize_failure(result.stderr, result.stdout)

    events = parse_codex_jsonl(result.stdout)
    if not events.session_id:
        return False, "codex health probe produced no thread/session id"
    if not final_text:
        return False, "codex health probe produced no last-message output"
    return True, ""


class SpecialistContext:
    """MCP-facing wrapper around `codex exec` one-shot and resume calls."""

    def __init__(self, codex_exe: Path, store: SpecialistStore, ledger: Ledger):
        self.codex_exe = codex_exe
        self.store = store
        self.ledger = ledger
        self._thread_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, label: str | None) -> asyncio.Lock:
        if not label:
            return asyncio.Lock()
        lock = self._thread_locks.get(label)
        if lock is None:
            lock = asyncio.Lock()
            self._thread_locks[label] = lock
        return lock

    def _ensure_ledger_session(self) -> None:
        SessionStore(self.ledger.db_path).upsert(LEDGER_KEY, source="specialist")

    def _argv_for(
        self,
        *,
        task: str,
        output_path: Path,
        session_id: str | None,
        thread: str | None,
        reasoning_effort: str | None,
        via_stdin: bool = False,
    ) -> list[str]:
        prompt_arg = "-" if via_stdin else task
        if session_id:
            argv = [
                str(self.codex_exe),
                "exec",
                "resume",
                "--json",
                "--skip-git-repo-check",
                "-c",
                'sandbox_mode="read-only"',
                "--output-last-message",
                str(output_path),
            ]
            if reasoning_effort:
                argv.extend(["-c", f"model_reasoning_effort={reasoning_effort}"])
            argv.extend([session_id, prompt_arg])
            return argv

        argv = [
            str(self.codex_exe),
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--output-last-message",
            str(output_path),
        ]
        if not thread:
            argv.append("--ephemeral")
        if reasoning_effort:
            argv.extend(["-c", f"model_reasoning_effort={reasoning_effort}"])
        argv.append(prompt_arg)
        return argv

    async def query_gpt5(
        self,
        *,
        task: str,
        thread: str | None = None,
        reasoning_effort: str | None = None,
        timeout_s: int = SPECIALIST_DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        task = (task or "").strip()
        if not task:
            return _err("task is required")

        label = (thread or "").strip() or None
        effort = (reasoning_effort or "").strip() or None
        if effort and effort not in REASONING_EFFORTS:
            return _err(
                "reasoning_effort must be one of: "
                + ", ".join(sorted(REASONING_EFFORTS))
            )

        # Observability: Codex runs as a blind, sandboxed process — it only
        # sees what's in `task`. Log the input size + head/tail so we can tell
        # whether a caller actually inlined the material it's asking about, vs
        # gesturing at content Codex can't see (a recurring failure mode). The
        # response length is logged on completion below.
        log.info(
            "query_gpt5 request: thread=%s effort=%s task_chars=%d head=%r tail=%r",
            label, effort, len(task), task[:200], task[-200:],
        )

        timeout_s = max(1, int(timeout_s))
        triggered_by = f"specialist:gpt5:{label or 'ephemeral'}"

        async with self._lock_for(label):
            existing = self.store.get(label) if label else None
            session_id = existing["session_id"] if existing else None
            # Route oversized prompts through stdin to dodge the Windows
            # command-line length limit. New sessions only; the resume path
            # keeps its short-prompt argv form (no caller resumes with a large
            # prompt today).
            via_stdin = session_id is None and len(task) > PROMPT_STDIN_THRESHOLD
            output_path = _make_temp_output_path()
            argv = self._argv_for(
                task=task,
                output_path=output_path,
                session_id=session_id,
                thread=label,
                reasoning_effort=effort,
                via_stdin=via_stdin,
            )

            ledger_id: int | None = None
            try:
                self._ensure_ledger_session()
                ledger_id = self.ledger.enqueue(
                    discord_key=LEDGER_KEY,
                    agent_name="specialist",
                    persona_version="gpt5",
                    triggered_by=triggered_by,
                )
                self.ledger.mark_running(ledger_id)

                try:
                    result = await _run_codex_process(
                        argv, timeout_s, stdin_text=task if via_stdin else None
                    )
                except asyncio.TimeoutError:
                    self.ledger.mark_failed(
                        ledger_id,
                        f"codex exec timed out after {timeout_s}s",
                    )
                    return _err(f"codex exec timed out after {timeout_s}s")

                events = parse_codex_jsonl(result.stdout)
                final_text = _read_text_if_present(output_path) or events.final_text or ""
                returned_session_id = events.session_id or session_id

                if result.returncode != 0:
                    failure = _summarize_failure(result.stderr, result.stdout)
                    self.ledger.mark_failed(ledger_id, failure)
                    return _err(failure)

                if not final_text:
                    self.ledger.mark_failed(
                        ledger_id,
                        "codex exec completed without final text",
                    )
                    return _err("codex exec completed without final text")

                log.info(
                    "query_gpt5 response: thread=%s resp_chars=%d",
                    label, len(final_text),
                )

                if label and returned_session_id:
                    self.store.upsert(label, "gpt5", returned_session_id)
                    self.store.record_turn(label)

                self.ledger.mark_completed(
                    ledger_id,
                    final_text[:300],
                    returned_session_id,
                    events.cost_usd,
                    provider="codex",
                    runtime="codex_cli",
                    billing_bucket="codex_oauth",
                )
                return _ok(final_text)
            except Exception as e:
                log.exception("query_gpt5 failed")
                if ledger_id is not None:
                    self.ledger.mark_failed(ledger_id, f"{type(e).__name__}: {e}")
                return _err(f"{type(e).__name__}: {e}")
            finally:
                _cleanup_temp(output_path)

    def as_mcp_server(self):
        ctx = self

        @tool(
            "query_gpt5",
            "Ask the GPT-5.5 Codex specialist a focused question. `task` is "
            "required. Optional `thread` is a caller-chosen label for "
            "multi-turn continuity; reuse the same label to resume that Codex "
            "session. Optional `reasoning_effort` may be low, medium, high, "
            "or xhigh.",
            {"task": str, "thread": str, "reasoning_effort": str},
        )
        async def query_gpt5(args: dict[str, Any]) -> dict[str, Any]:
            return await ctx.query_gpt5(
                task=str(args.get("task", "")),
                thread=args.get("thread"),
                reasoning_effort=args.get("reasoning_effort"),
            )

        return create_sdk_mcp_server(
            name="specialist",
            version="0.1.0",
            tools=[query_gpt5],
        )
