from __future__ import annotations

import pytest

from src.agent_runner import RunnerRegistry
from src.config import (
    BACKGROUND_QUEUE_DEPTH_PER_AGENT,
    MAX_CONCURRENT_RUNS,
    MAX_QUEUE_DEPTH_PER_AGENT,
)
from src.store import Ledger, SessionStore


@pytest.mark.asyncio
async def test_background_slots_leave_room_for_user_chat(tmp_db):
    registry = RunnerRegistry(SessionStore(tmp_db), Ledger(tmp_db))
    background_cap = max(
        0,
        min(
            BACKGROUND_QUEUE_DEPTH_PER_AGENT,
            MAX_QUEUE_DEPTH_PER_AGENT - 1,
            MAX_CONCURRENT_RUNS - 1,
        ),
    )

    acquired = []
    for _ in range(background_cap):
        acquired.append(await registry.acquire_background("research", wait=False))
    assert acquired == [True] * background_cap

    assert await registry.acquire_background("research", wait=False) is False
    assert await registry.acquire("research") is True

    registry.release("research")
    for _ in range(background_cap):
        registry.release("research", background=True)


def test_get_or_create_applies_mission_runner_overrides(tmp_db):
    registry = RunnerRegistry(SessionStore(tmp_db), Ledger(tmp_db))
    runner = registry.get_or_create(
        "mission:1", "research", max_turns=40, max_budget_usd=2.0
    )
    assert runner.max_turns == 40
    assert runner.max_budget_usd == 2.0
