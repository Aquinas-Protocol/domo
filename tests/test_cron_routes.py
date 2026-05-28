"""Integration tests for /api/cron/* routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import INBOX_CHANNEL_ID
from src.cron_store import CronStore
from src.store import Ledger, SessionStore
from src.web.auth import COOKIE_NAME, issue_session_token
from src.web.routes import build_router


@pytest.fixture(autouse=True)
def _dashboard_env(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-secret-do-not-use")
    pin_hash = bcrypt.hashpw(b"1234", bcrypt.gensalt()).decode("utf-8")
    monkeypatch.setenv("DASHBOARD_PIN_HASH", pin_hash)
    yield


class StubRunner:
    """Empty async-generator stub. cron_run_now drains it as a task."""
    async def send(self, prompt, *, triggered_by="user", **_):
        if False:  # pragma: no cover
            yield


class StubRegistry:
    def get_or_create(self, key, agent_name):
        return StubRunner()


def _make_client(tmp_db: Path) -> TestClient:
    SessionStore(tmp_db).upsert("main")  # FK seed for ledger
    app_state = SimpleNamespace(
        cron_store=CronStore(tmp_db),
        ledger=Ledger(tmp_db),
        registry=StubRegistry(),
        bots={},
    )
    app = FastAPI()
    app.include_router(build_router(app_state))
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    return client


def _payload(**overrides):
    base = dict(
        name="test-job",
        cron_expr="0 9 * * *",
        target_agent="main",
        prompt="run morning check",
        description="optional",
    )
    base.update(overrides)
    return base


# ---------------- auth ----------------

def test_unauthenticated_returns_401(tmp_db: Path):
    client = _make_client(tmp_db)
    client.cookies.clear()
    assert client.get("/api/cron/jobs").status_code == 401
    assert client.post("/api/cron/jobs", json=_payload()).status_code == 401
    assert client.get("/api/cron/history").status_code == 401


# ---------------- list ----------------

def test_list_empty(tmp_db: Path):
    client = _make_client(tmp_db)
    r = client.get("/api/cron/jobs")
    assert r.status_code == 200
    assert r.json() == []


# ---------------- create ----------------

def test_create_round_trip(tmp_db: Path):
    client = _make_client(tmp_db)
    r = client.post("/api/cron/jobs", json=_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "test-job"
    assert body["enabled"] == 1
    assert body["next_run_at"] is not None

    listed = client.get("/api/cron/jobs").json()
    assert len(listed) == 1
    assert listed[0]["id"] == body["id"]


def test_create_rejects_invalid_cron(tmp_db: Path):
    client = _make_client(tmp_db)
    r = client.post("/api/cron/jobs", json=_payload(cron_expr="not-cron"))
    assert r.status_code == 422


def test_create_rejects_unknown_target_agent(tmp_db: Path):
    client = _make_client(tmp_db)
    r = client.post("/api/cron/jobs", json=_payload(target_agent="ghost"))
    assert r.status_code == 422


def test_create_rejects_sub_minimum_interval(tmp_db: Path):
    """Every-minute cron is below the 5-minute floor."""
    client = _make_client(tmp_db)
    r = client.post("/api/cron/jobs", json=_payload(cron_expr="* * * * *"))
    assert r.status_code == 422


def test_create_rejects_missing_required_fields(tmp_db: Path):
    client = _make_client(tmp_db)
    r = client.post("/api/cron/jobs", json={"name": "only-name"})
    assert r.status_code == 422


def test_create_rejects_duplicate_name(tmp_db: Path):
    client = _make_client(tmp_db)
    assert client.post("/api/cron/jobs", json=_payload(name="dup")).status_code == 200
    r = client.post("/api/cron/jobs", json=_payload(name="dup"))
    assert r.status_code == 409


# ---------------- patch ----------------

def test_patch_toggles_enabled(tmp_db: Path):
    client = _make_client(tmp_db)
    jid = client.post("/api/cron/jobs", json=_payload()).json()["id"]
    r = client.patch(f"/api/cron/jobs/{jid}", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] == 0


def test_patch_validates_target_agent(tmp_db: Path):
    client = _make_client(tmp_db)
    jid = client.post("/api/cron/jobs", json=_payload()).json()["id"]
    r = client.patch(f"/api/cron/jobs/{jid}", json={"target_agent": "ghost"})
    assert r.status_code == 422


def test_patch_validates_cron_expr(tmp_db: Path):
    client = _make_client(tmp_db)
    jid = client.post("/api/cron/jobs", json=_payload()).json()["id"]
    r = client.patch(f"/api/cron/jobs/{jid}", json={"cron_expr": "bogus"})
    assert r.status_code == 422


def test_patch_404_unknown_id(tmp_db: Path):
    client = _make_client(tmp_db)
    r = client.patch("/api/cron/jobs/9999", json={"enabled": False})
    assert r.status_code == 404


# ---------------- delete ----------------

def test_delete(tmp_db: Path):
    client = _make_client(tmp_db)
    jid = client.post("/api/cron/jobs", json=_payload()).json()["id"]
    assert client.delete(f"/api/cron/jobs/{jid}").status_code == 200
    assert client.get("/api/cron/jobs").json() == []


def test_delete_404_unknown(tmp_db: Path):
    client = _make_client(tmp_db)
    r = client.delete("/api/cron/jobs/9999")
    assert r.status_code == 404


# ---------------- run-now ----------------

def test_run_now_returns_200_immediately(tmp_db: Path):
    client = _make_client(tmp_db)
    jid = client.post("/api/cron/jobs", json=_payload()).json()["id"]
    r = client.post(f"/api/cron/jobs/{jid}/run")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_run_now_404_unknown(tmp_db: Path):
    client = _make_client(tmp_db)
    r = client.post("/api/cron/jobs/9999/run")
    assert r.status_code == 404


# ---------------- destination_channel_id + oneshot + at (v4) ----------------

def test_create_default_destination_is_inbox(tmp_db: Path):
    """POST /api/cron/jobs without destination_channel_id resolves to #inbox."""
    client = _make_client(tmp_db)
    body = client.post("/api/cron/jobs", json=_payload()).json()
    assert body["destination_channel_id"] == str(INBOX_CHANNEL_ID)
    assert body["oneshot"] == 0


def test_create_explicit_destination_honored(tmp_db: Path):
    client = _make_client(tmp_db)
    body = client.post(
        "/api/cron/jobs",
        json=_payload(destination_channel_id="123456789012345679"),
    ).json()
    assert body["destination_channel_id"] == "123456789012345679"


def test_create_rejects_non_digit_destination(tmp_db: Path):
    client = _make_client(tmp_db)
    r = client.post(
        "/api/cron/jobs",
        json=_payload(destination_channel_id="not-a-snowflake"),
    )
    assert r.status_code == 422


def test_create_with_at_makes_oneshot(tmp_db: Path):
    """POST with `at` produces a one-time reminder that auto-disables."""
    client = _make_client(tmp_db)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    body = client.post(
        "/api/cron/jobs",
        json=_payload(name="oven", cron_expr="", at=future.isoformat()),
    ).json()
    assert body["oneshot"] == 1
    assert body["next_run_at"] == future.astimezone(timezone.utc).isoformat()


def test_create_at_in_past_rejected(tmp_db: Path):
    client = _make_client(tmp_db)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    r = client.post(
        "/api/cron/jobs",
        json=_payload(name="too-late", cron_expr="", at=past),
    )
    assert r.status_code == 422


def test_patch_destination_channel_id(tmp_db: Path):
    client = _make_client(tmp_db)
    jid = client.post("/api/cron/jobs", json=_payload()).json()["id"]
    r = client.patch(
        f"/api/cron/jobs/{jid}",
        json={"destination_channel_id": "123456789012345679"},
    )
    assert r.status_code == 200
    assert r.json()["destination_channel_id"] == "123456789012345679"


def test_patch_oneshot(tmp_db: Path):
    client = _make_client(tmp_db)
    jid = client.post("/api/cron/jobs", json=_payload()).json()["id"]
    r = client.patch(f"/api/cron/jobs/{jid}", json={"oneshot": True})
    assert r.status_code == 200
    assert r.json()["oneshot"] == 1


def test_create_round_trip_persists_raw_prompt(tmp_db: Path):
    """The wrapping happens at fire time in _run_job; the stored prompt stays
    raw so PATCH and list views see what the user wrote."""
    client = _make_client(tmp_db)
    body = client.post(
        "/api/cron/jobs",
        json=_payload(prompt="remind me to drink water"),
    ).json()
    assert body["prompt"] == "remind me to drink water"
    assert "post_to_channel" not in body["prompt"]


# ---------------- history ----------------

def test_history_filters_to_cron_prefix(tmp_db: Path):
    client = _make_client(tmp_db)
    ledger = Ledger(tmp_db)
    ledger.enqueue("main", "main", "v1", triggered_by="user")
    ledger.enqueue("main", "main", "v1", triggered_by="cron:1")
    ledger.enqueue("main", "main", "v1", triggered_by="cron:2")

    rows = client.get("/api/cron/history").json()
    assert len(rows) == 2
    assert all(row["triggered_by"].startswith("cron:") for row in rows)
