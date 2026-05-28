"""MCP registration routing for per-agent tools."""

from __future__ import annotations

from src.agent_runner import RunnerRegistry
from src.store import Ledger, SessionStore


def test_registry_routes_all_and_allowlisted_mcp_servers(tmp_db):
    registry = RunnerRegistry(SessionStore(tmp_db), Ledger(tmp_db))
    cron_server = object()
    discord_server = object()
    specialist_server = object()
    delegate_server = object()
    mission_server = object()

    registry.register(
        name="cron",
        server=cron_server,
        tool_names=("mcp__cron__create_cron_job",),
        agents=None,
    )
    registry.register(
        name="discord",
        server=discord_server,
        tool_names=("mcp__discord__post_to_channel",),
        agents=None,
    )
    registry.register(
        name="specialist",
        server=specialist_server,
        tool_names=("mcp__specialist__query_gpt5",),
        agents=frozenset({"main", "research"}),
    )
    registry.register(
        name="domo",
        server=delegate_server,
        tool_names=("mcp__domo__delegate_to_research",),
        agents=frozenset({"main"}),
    )
    registry.register(
        name="mission",
        server=mission_server,
        tool_names=("mcp__mission__create_mission_task",),
        agents=frozenset({"main", "research"}),
    )

    assert registry._mcp_servers_for("main") == {
        "cron": cron_server,
        "discord": discord_server,
        "specialist": specialist_server,
        "domo": delegate_server,
        "mission": mission_server,
    }
    assert registry._mcp_servers_for("research") == {
        "cron": cron_server,
        "discord": discord_server,
        "specialist": specialist_server,
        "mission": mission_server,
    }
    assert registry._mcp_servers_for("comms") == {
        "cron": cron_server,
        "discord": discord_server,
    }

    assert "mcp__specialist__query_gpt5" in registry._tool_names_for("main")
    assert "mcp__specialist__query_gpt5" in registry._tool_names_for("research")
    assert "mcp__specialist__query_gpt5" not in registry._tool_names_for("comms")
    assert "mcp__mission__create_mission_task" in registry._tool_names_for("main")
    assert "mcp__mission__create_mission_task" in registry._tool_names_for("research")
    assert "mcp__mission__create_mission_task" not in registry._tool_names_for("comms")
    assert "specialist" not in registry._mcp_servers_for("comms")
    assert "mission" not in registry._mcp_servers_for("comms")


def test_get_or_create_freezes_agent_specific_mcp_set(tmp_db):
    registry = RunnerRegistry(SessionStore(tmp_db), Ledger(tmp_db))
    registry.register(
        name="specialist",
        server=object(),
        tool_names=("mcp__specialist__query_gpt5",),
        agents=frozenset({"main", "research"}),
    )

    main_runner = registry.get_or_create("main", "main")
    comms_runner = registry.get_or_create("comms", "comms")

    assert "specialist" in main_runner.mcp_servers
    assert "mcp__specialist__query_gpt5" in main_runner._mcp_tool_names
    assert "specialist" not in comms_runner.mcp_servers
    assert "mcp__specialist__query_gpt5" not in comms_runner._mcp_tool_names


def test_video_mcp_is_research_only(tmp_db):
    """watch_video belongs to Intel; Maine delegates, Hermes doesn't use video."""
    registry = RunnerRegistry(SessionStore(tmp_db), Ledger(tmp_db))
    video_server = object()
    registry.register(
        name="video",
        server=video_server,
        tool_names=("mcp__video__watch_video",),
        agents=frozenset({"research"}),
    )

    assert registry._mcp_servers_for("research") == {"video": video_server}
    assert registry._mcp_servers_for("main") == {}
    assert registry._mcp_servers_for("comms") == {}

    assert "mcp__video__watch_video" in registry._tool_names_for("research")
    assert "mcp__video__watch_video" not in registry._tool_names_for("main")
    assert "mcp__video__watch_video" not in registry._tool_names_for("comms")
