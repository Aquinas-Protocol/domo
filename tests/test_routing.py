"""Per-bot routing: each AgentBot's _runner_key_for() only handles messages
addressed to its own agent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src import config
from src.bot import AgentBot, BotApp


@pytest.fixture
def patched_agents(monkeypatch):
    """Inject minimal AgentSpec records with channel_ids for the test."""

    def make_spec(name, channel_id):
        return config.AgentSpec(
            name=name,
            display_name=name.title(),
            persona_file=config.VAULT_ROOT / "agents" / f"{name}.md",
            token=f"fake-token-{name}",
            channel_id=channel_id,
            webhook_url=None,
            can_delegate=(name == "main"),
            enabled_tools=None,
            model=None,
        )

    monkeypatch.setitem(config.AGENTS, "main", make_spec("main", 11111))
    monkeypatch.setitem(config.AGENTS, "research", make_spec("research", 22222))
    monkeypatch.setitem(config.AGENTS, "comms", make_spec("comms", 33333))
    yield


def _make_bot(agent_name: str) -> AgentBot:
    """AgentBot without invoking discord.Client.__init__ (which needs intents).

    We bypass init by creating a bare instance and setting the attributes
    _runner_key_for needs.
    """
    bot = AgentBot.__new__(AgentBot)
    bot.agent_name = agent_name
    bot.app = MagicMock(spec=BotApp)
    return bot


def test_main_dm_routes_to_main(patched_agents):
    bot = _make_bot("main")
    msg = MagicMock()
    msg.channel = MagicMock(spec=discord.DMChannel)
    assert bot._runner_key_for(msg) == "main"


def test_research_dm_routes_to_research(patched_agents):
    """Each bot has its own DMs; an Intel DM lands on Intel, not Maine."""
    bot = _make_bot("research")
    msg = MagicMock()
    msg.channel = MagicMock(spec=discord.DMChannel)
    assert bot._runner_key_for(msg) == "research"


def test_main_handles_main_channel(patched_agents):
    bot = _make_bot("main")
    msg = MagicMock()
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = 11111
    msg.channel = ch
    assert bot._runner_key_for(msg) == "main"


def test_main_ignores_research_channel(patched_agents):
    bot = _make_bot("main")
    msg = MagicMock()
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = 22222  # research channel
    msg.channel = ch
    assert bot._runner_key_for(msg) is None


def test_research_handles_research_channel(patched_agents):
    bot = _make_bot("research")
    msg = MagicMock()
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = 22222
    msg.channel = ch
    assert bot._runner_key_for(msg) == "research"


def test_research_ignores_main_channel(patched_agents):
    bot = _make_bot("research")
    msg = MagicMock()
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = 11111
    msg.channel = ch
    assert bot._runner_key_for(msg) is None


def test_research_thread_under_research_channel(patched_agents):
    bot = _make_bot("research")
    parent = MagicMock(spec=discord.TextChannel)
    parent.id = 22222
    thread = MagicMock(spec=discord.Thread)
    thread.id = 444
    thread.parent = parent
    msg = MagicMock()
    msg.channel = thread
    assert bot._runner_key_for(msg) == "thread:444"


def test_research_ignores_thread_under_main_channel(patched_agents):
    bot = _make_bot("research")
    parent = MagicMock(spec=discord.TextChannel)
    parent.id = 11111  # main, not research
    thread = MagicMock(spec=discord.Thread)
    thread.id = 444
    thread.parent = parent
    msg = MagicMock()
    msg.channel = thread
    assert bot._runner_key_for(msg) is None


def test_thread_with_no_parent_returns_none(patched_agents):
    bot = _make_bot("research")
    thread = MagicMock(spec=discord.Thread)
    thread.parent = None
    msg = MagicMock()
    msg.channel = thread
    assert bot._runner_key_for(msg) is None


# ---- _maybe_open_thread tests ----

def _spec_with_auto_thread(
    name: str, channel_id: int, auto_thread: bool
) -> config.AgentSpec:
    return config.AgentSpec(
        name=name,
        display_name=name.title(),
        persona_file=config.VAULT_ROOT / "agents" / f"{name}.md",
        token=f"fake-token-{name}",
        channel_id=channel_id,
        webhook_url=None,
        can_delegate=(name == "main"),
        enabled_tools=None,
        model=None,
        auto_thread=auto_thread,
    )


def _make_message(content: str = "", attachments=None, channel=None):
    msg = MagicMock()
    msg.content = content
    msg.attachments = attachments or []
    msg.channel = channel
    msg.create_thread = AsyncMock()
    return msg


async def test_auto_thread_created_for_top_level_ask(monkeypatch):
    monkeypatch.setitem(
        config.AGENTS, "research",
        _spec_with_auto_thread("research", 22222, auto_thread=True),
    )
    bot = _make_bot("research")
    bot.app = MagicMock()

    ch = MagicMock(spec=discord.TextChannel)
    ch.id = 22222
    thread = MagicMock(spec=discord.Thread)
    thread.id = 999

    msg = _make_message(content="What's the latest on SpaceX?", channel=ch)
    msg.create_thread.return_value = thread

    new_key, send_target = await bot._maybe_open_thread(msg, "research")

    msg.create_thread.assert_awaited_once()
    _, kwargs = msg.create_thread.call_args
    assert kwargs["name"] == "What's the latest on SpaceX?"
    assert kwargs["auto_archive_duration"] == 10080
    assert new_key == "thread:999"
    assert send_target is thread
    bot.app.store.upsert.assert_called_once_with(
        "thread:999", cwd=None, parent_key="research", source="discord",
    )


async def test_auto_thread_skipped_when_disabled(monkeypatch):
    monkeypatch.setitem(
        config.AGENTS, "research",
        _spec_with_auto_thread("research", 22222, auto_thread=False),
    )
    bot = _make_bot("research")

    ch = MagicMock(spec=discord.TextChannel)
    ch.id = 22222
    msg = _make_message(content="What's the latest?", channel=ch)

    new_key, send_target = await bot._maybe_open_thread(msg, "research")

    msg.create_thread.assert_not_awaited()
    assert new_key == "research"
    assert send_target is ch


async def test_auto_thread_skipped_in_existing_thread(monkeypatch):
    monkeypatch.setitem(
        config.AGENTS, "research",
        _spec_with_auto_thread("research", 22222, auto_thread=True),
    )
    bot = _make_bot("research")

    parent = MagicMock(spec=discord.TextChannel)
    parent.id = 22222
    thread = MagicMock(spec=discord.Thread)
    thread.id = 444
    thread.parent = parent
    msg = _make_message(content="follow-up question", channel=thread)

    new_key, send_target = await bot._maybe_open_thread(msg, "thread:444")

    msg.create_thread.assert_not_awaited()
    assert new_key == "thread:444"
    assert send_target is thread


async def test_auto_thread_attachment_only_message(monkeypatch):
    monkeypatch.setitem(
        config.AGENTS, "research",
        _spec_with_auto_thread("research", 22222, auto_thread=True),
    )
    bot = _make_bot("research")
    bot.app = MagicMock()

    ch = MagicMock(spec=discord.TextChannel)
    ch.id = 22222
    new_thread = MagicMock(spec=discord.Thread)
    new_thread.id = 777

    attachment = MagicMock()
    attachment.filename = "screenshot.png"
    msg = _make_message(content="", attachments=[attachment], channel=ch)
    msg.create_thread.return_value = new_thread

    new_key, send_target = await bot._maybe_open_thread(msg, "research")

    msg.create_thread.assert_awaited_once()
    _, kwargs = msg.create_thread.call_args
    assert kwargs["name"] == "Attachment: screenshot.png"
    assert new_key == "thread:777"
    assert send_target is new_thread
