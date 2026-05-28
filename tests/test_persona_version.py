"""Persona file hashing — edits change the persona_version on the next load."""

from __future__ import annotations

from pathlib import Path

from src.agent_runner import _load_persona
from src.config import AgentSpec


def _make_spec(persona_file: Path) -> AgentSpec:
    return AgentSpec(
        name="test",
        display_name="Test",
        persona_file=persona_file,
        token=None,
        channel_id=None,
        webhook_url=None,
        can_delegate=False,
        enabled_tools=None,
        model=None,
    )


def test_persona_version_changes_on_edit(tmp_path: Path):
    p = tmp_path / "persona.md"
    p.write_text("Original content\n", encoding="utf-8")
    spec = _make_spec(p)

    text1, hash1 = _load_persona(spec)
    assert text1 == "Original content\n"
    assert len(hash1) == 12

    p.write_text("Edited content\n", encoding="utf-8")
    text2, hash2 = _load_persona(spec)
    assert text2 == "Edited content\n"
    assert hash2 != hash1, "hash should change after persona edit"


def test_persona_version_stable_for_same_content(tmp_path: Path):
    p = tmp_path / "persona.md"
    p.write_text("Same content\n", encoding="utf-8")
    spec = _make_spec(p)

    _, h1 = _load_persona(spec)
    _, h2 = _load_persona(spec)
    assert h1 == h2


def test_missing_persona_returns_sentinel(tmp_path: Path):
    p = tmp_path / "nonexistent.md"
    spec = _make_spec(p)
    text, h = _load_persona(spec)
    assert text == ""
    assert h == "missing"


# ---------------- model pinning (per-agent in agents.yaml) ----------------

def test_agents_yaml_pins_model_for_each_agent():
    """All three production agents have an explicit `model` set so the
    runtime model choice is durable across service restarts (instead of
    silently inheriting whatever DEFAULT_LLM_MODEL the env happened to have)."""
    from src.config import AGENTS
    for name in ("main", "research", "comms"):
        spec = AGENTS.get(name)
        assert spec is not None, f"missing agent {name!r}"
        assert spec.model, (
            f"agent {name!r} has no model pinned in agents.yaml — "
            f"runtime would silently inherit DEFAULT_LLM_MODEL"
        )


def test_agentspec_accepts_none_model():
    """Test fixtures and migration paths can construct AgentSpec without a
    model pin (None means 'inherit SDK default')."""
    spec = AgentSpec(
        name="test", display_name="Test", persona_file=Path("x.md"),
        token=None, channel_id=None, webhook_url=None, can_delegate=False,
        enabled_tools=None, model=None,
    )
    assert spec.model is None
