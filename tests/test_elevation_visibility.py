"""Tool-surface tests for `request_admin_elevation`.

The elevation tool is dangerous by design — it routes elevated commands
through Maine's Discord channel after user approval. It must be visible
ONLY to the operator agent (`main`). Curator, research, and comms must not
even know it exists in their MCP namespace.

Two layers, mirroring `test_curator_tool_surface.py`:

1. Registry filter math: build a RunnerRegistry, register the elevation
   server with the bot.py-shaped `agents=frozenset({"main"})`, and assert
   the per-agent name lookup excludes everyone else.
2. Source-pattern check: read `src/bot.py` and assert the elevation
   registration block uses `agents=frozenset({"main"})` literally.
"""

from __future__ import annotations

from pathlib import Path

from src.agent_runner import RunnerRegistry
from src.store import Ledger, SessionStore


ELEVATION_TOOL = "mcp__elevation__request_admin_elevation"


def _build_registry(tmp_db: Path) -> RunnerRegistry:
    registry = RunnerRegistry(SessionStore(tmp_db), Ledger(tmp_db))
    registry.register(
        name="elevation",
        server=object(),  # placeholder — registry only invokes at runner build
        tool_names=(ELEVATION_TOOL,),
        agents=frozenset({"main"}),
    )
    return registry


def test_main_sees_elevation_tool(tmp_db: Path):
    registry = _build_registry(tmp_db)
    assert ELEVATION_TOOL in registry._tool_names_for("main")


def test_research_does_not_see_elevation_tool(tmp_db: Path):
    registry = _build_registry(tmp_db)
    assert ELEVATION_TOOL not in registry._tool_names_for("research")


def test_comms_does_not_see_elevation_tool(tmp_db: Path):
    registry = _build_registry(tmp_db)
    assert ELEVATION_TOOL not in registry._tool_names_for("comms")


def test_curator_does_not_see_elevation_tool(tmp_db: Path):
    registry = _build_registry(tmp_db)
    assert ELEVATION_TOOL not in registry._tool_names_for("curator")


def test_unknown_agent_does_not_see_elevation_tool(tmp_db: Path):
    """Defense in depth: if a future agent is added with a name not in any
    registration's `agents` set, it must not inherit the elevation tool."""
    registry = _build_registry(tmp_db)
    assert ELEVATION_TOOL not in registry._tool_names_for("brand-new-agent")


# --------------------------- source-pattern check ---------------------------


_BOT_PY = Path(__file__).resolve().parent.parent / "src" / "bot.py"


def test_bot_py_elevation_registration_is_main_only():
    """Read bot.py and confirm the elevation registration is scoped to
    main. Single-source-of-truth check that the wire helper hasn't drifted
    to a broader `agents=` set under our nose."""
    src = _BOT_PY.read_text(encoding="utf-8")
    needle = 'name="elevation"'
    idx = src.find(needle)
    assert idx != -1, "registry.register(name=\"elevation\", ...) not found in bot.py"
    open_pos = src.rfind("self.registry.register(", 0, idx)
    assert open_pos != -1
    cursor = src.index("(", open_pos)
    depth = 0
    end = -1
    while cursor < len(src):
        ch = src[cursor]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = cursor
                break
        cursor += 1
    assert end != -1
    block = src[open_pos:end + 1]
    assert (
        'agents=frozenset({"main"})' in block
        or "agents=frozenset({'main'})" in block
    ), (
        "elevation MCP registration must be scoped to main only. "
        f"Block:\n{block}"
    )
