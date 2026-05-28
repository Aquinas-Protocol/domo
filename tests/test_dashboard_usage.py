"""Dashboard usage summary route."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.store import Ledger, SessionStore
from src.web.auth import COOKIE_NAME, issue_session_token
from src.web.routes import build_router


@pytest.fixture(autouse=True)
def _dashboard_env(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-secret-do-not-use")
    pin_hash = bcrypt.hashpw(b"1234", bcrypt.gensalt()).decode("utf-8")
    monkeypatch.setenv("DASHBOARD_PIN_HASH", pin_hash)
    yield


def _make_client(tmp_db: Path) -> TestClient:
    SessionStore(tmp_db).upsert("main")
    app_state = SimpleNamespace(
        ledger=Ledger(tmp_db),
    )
    app = FastAPI()
    app.include_router(build_router(app_state))
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    return client


def test_usage_endpoint_returns_daily_cost_and_lifetime_tokens(tmp_db: Path):
    SessionStore(tmp_db).upsert("main")
    ledger = Ledger(tmp_db)
    tz = ZoneInfo("America/Chicago")
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = (today_start - timedelta(minutes=15)).astimezone(timezone.utc).isoformat()
    today = (today_start + timedelta(minutes=15)).astimezone(timezone.utc).isoformat()

    old_id = ledger.enqueue("main", "main", "v1", triggered_by="user")
    ledger.mark_completed(old_id, "old", "sess-old", 2.25, 1_250)
    new_id = ledger.enqueue("main", "main", "v1", triggered_by="user")
    ledger.mark_completed(new_id, "new", "sess-new", 0.75, 2_750)
    with sqlite3.connect(tmp_db) as c:
        c.execute("UPDATE task_ledger SET created_at=? WHERE id=?", (yesterday, old_id))
        c.execute("UPDATE task_ledger SET created_at=? WHERE id=?", (today, new_id))

    body = _make_client(tmp_db).get("/api/usage").json()

    assert body["daily_cost_usd"] == 0.75
    assert body["daily_runs"] == 1
    assert body["lifetime_cost_usd"] == 3.0
    assert body["lifetime_tokens"] == 4_000
