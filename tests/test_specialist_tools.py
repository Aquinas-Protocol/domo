"""Tests for the Codex specialist subprocess wrapper."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from src import specialist_tools
from src.specialist_store import SpecialistStore
from src.specialist_tools import CodexProcessResult, SpecialistContext, parse_codex_jsonl
from src.store import Ledger

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _tool_text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


def _install_fake_process(monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, Any]]):
    calls: list[list[str]] = []

    async def fake(argv: list[str], timeout_s: int, *, stdin_text: str | None = None) -> CodexProcessResult:
        calls.append(list(argv))
        response = responses[min(len(calls) - 1, len(responses) - 1)]
        if response.get("timeout"):
            raise asyncio.TimeoutError
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        if response.get("final") is not None:
            output_path.write_text(str(response["final"]), encoding="utf-8")
        return CodexProcessResult(
            returncode=int(response.get("returncode", 0)),
            stdout=str(response.get("stdout", "")),
            stderr=str(response.get("stderr", "")),
        )

    monkeypatch.setattr(specialist_tools, "_run_codex_process", fake)
    return calls


def _context(tmp_db: Path) -> SpecialistContext:
    return SpecialistContext(Path("codex"), SpecialistStore(tmp_db), Ledger(tmp_db))


async def test_first_turn_without_thread_is_ephemeral(monkeypatch, tmp_db: Path):
    calls = _install_fake_process(
        monkeypatch,
        [{"stdout": _fixture("codex_exec_jsonl.txt"), "final": "ok"}],
    )
    result = await _context(tmp_db).query_gpt5(task="say ok")

    assert _tool_text(result) == "ok"
    assert "--ephemeral" in calls[0]
    assert "resume" not in calls[0]


async def test_first_turn_with_thread_persists_session(monkeypatch, tmp_db: Path):
    calls = _install_fake_process(
        monkeypatch,
        [{"stdout": _fixture("codex_exec_jsonl.txt"), "final": "first"}],
    )
    ctx = _context(tmp_db)

    result = await ctx.query_gpt5(task="remember this", thread="tax-liens")

    assert _tool_text(result) == "first"
    assert "--ephemeral" not in calls[0]
    assert "resume" not in calls[0]
    row = SpecialistStore(tmp_db).get("tax-liens")
    assert row is not None
    assert row["session_id"] == "019dd584-01c1-7ea3-be02-632a9a5b9400"
    assert row["turn_count"] == 1


async def test_second_turn_uses_resume_with_config_sandbox(monkeypatch, tmp_db: Path):
    calls = _install_fake_process(
        monkeypatch,
        [
            {"stdout": _fixture("codex_exec_jsonl.txt"), "final": "first"},
            {"stdout": _fixture("codex_exec_resume_jsonl.txt"), "final": "beta"},
        ],
    )
    ctx = _context(tmp_db)

    await ctx.query_gpt5(task="first", thread="tax-liens")
    result = await ctx.query_gpt5(task="second", thread="tax-liens")

    assert _tool_text(result) == "beta"
    assert calls[1][0:3] == ["codex", "exec", "resume"]
    assert "--sandbox" not in calls[1]
    assert 'sandbox_mode="read-only"' in calls[1]
    assert "019dd584-01c1-7ea3-be02-632a9a5b9400" in calls[1]
    assert SpecialistStore(tmp_db).get("tax-liens")["turn_count"] == 2


async def test_subprocess_timeout_marks_ledger_failed(monkeypatch, tmp_db: Path):
    _install_fake_process(monkeypatch, [{"timeout": True}])

    result = await _context(tmp_db).query_gpt5(
        task="slow",
        thread="timeout-thread",
        timeout_s=1,
    )

    assert result.get("isError") is True
    assert "timed out" in _tool_text(result)
    rows = Ledger(tmp_db).query(agent="specialist", status="failed")
    assert len(rows) == 1
    assert "timed out" in rows[0]["error_summary"]


def test_parser_reads_real_fixture_and_allows_missing_cost():
    events = parse_codex_jsonl(_fixture("codex_exec_jsonl.txt"))

    assert events.session_id == "019dd584-01c1-7ea3-be02-632a9a5b9400"
    assert events.final_text == "ok"
    assert events.cost_usd is None
    assert events.usage is not None


def test_parser_accepts_slashed_event_names():
    events = parse_codex_jsonl(
        '{"type":"thread/started","threadId":"sid-1"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
    )

    assert events.session_id == "sid-1"
    assert events.final_text == "done"


async def test_large_prompt_routed_via_stdin(monkeypatch, tmp_db: Path):
    captured: dict[str, Any] = {}

    async def fake(argv: list[str], timeout_s: int, *, stdin_text: str | None = None) -> CodexProcessResult:
        captured["argv"] = list(argv)
        captured["stdin_text"] = stdin_text
        Path(argv[argv.index("--output-last-message") + 1]).write_text("graded", encoding="utf-8")
        return CodexProcessResult(returncode=0, stdout=_fixture("codex_exec_jsonl.txt"), stderr="")

    monkeypatch.setattr(specialist_tools, "_run_codex_process", fake)
    big = "x" * (specialist_tools.PROMPT_STDIN_THRESHOLD + 100)

    result = await _context(tmp_db).query_gpt5(task=big)

    assert _tool_text(result) == "graded"
    assert "-" in captured["argv"]          # prompt routed via the stdin sentinel
    assert big not in captured["argv"]      # the large prompt is NOT on the command line
    assert captured["stdin_text"] == big    # it is delivered over stdin instead


async def test_small_prompt_stays_on_argv(monkeypatch, tmp_db: Path):
    captured: dict[str, Any] = {}

    async def fake(argv: list[str], timeout_s: int, *, stdin_text: str | None = None) -> CodexProcessResult:
        captured["argv"] = list(argv)
        captured["stdin_text"] = stdin_text
        Path(argv[argv.index("--output-last-message") + 1]).write_text("ok", encoding="utf-8")
        return CodexProcessResult(returncode=0, stdout=_fixture("codex_exec_jsonl.txt"), stderr="")

    monkeypatch.setattr(specialist_tools, "_run_codex_process", fake)

    result = await _context(tmp_db).query_gpt5(task="a short question")

    assert _tool_text(result) == "ok"
    assert "a short question" in captured["argv"]  # short prompts unchanged: still on argv
    assert captured["stdin_text"] is None
    assert "-" not in captured["argv"]


async def test_concurrent_first_turns_share_one_session_label(monkeypatch, tmp_db: Path):
    calls: list[list[str]] = []

    async def fake(argv: list[str], timeout_s: int, *, stdin_text: str | None = None) -> CodexProcessResult:
        calls.append(list(argv))
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        await asyncio.sleep(0.01)
        if "resume" in argv:
            output_path.write_text("second", encoding="utf-8")
            return CodexProcessResult(
                returncode=0,
                stdout=_fixture("codex_exec_resume_jsonl.txt"),
                stderr="",
            )
        output_path.write_text("first", encoding="utf-8")
        return CodexProcessResult(
            returncode=0,
            stdout=_fixture("codex_exec_jsonl.txt"),
            stderr="",
        )

    monkeypatch.setattr(specialist_tools, "_run_codex_process", fake)
    ctx = _context(tmp_db)

    results = await asyncio.gather(
        ctx.query_gpt5(task="first", thread="race-test"),
        ctx.query_gpt5(task="second", thread="race-test"),
    )

    assert [_tool_text(result) for result in results] == ["first", "second"]
    assert len(calls) == 2
    assert "resume" not in calls[0]
    assert calls[1][0:3] == ["codex", "exec", "resume"]
    row = SpecialistStore(tmp_db).get("race-test")
    assert row is not None
    assert row["turn_count"] == 2
