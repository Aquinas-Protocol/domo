"""PIN auth + session cookie behavior."""

from __future__ import annotations

import pytest

from src.web.auth import (
    hash_pin,
    is_authenticated,
    issue_session_token,
    verify_pin,
)


@pytest.fixture(autouse=True)
def _dashboard_env(monkeypatch):
    """Set fresh env vars for each test. Auth module reads env at call time
    (no module reload needed) so this is sufficient and doesn't pollute state
    across tests."""
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-secret-do-not-use")
    import bcrypt
    pin_hash = bcrypt.hashpw(b"1234", bcrypt.gensalt()).decode("utf-8")
    monkeypatch.setenv("DASHBOARD_PIN_HASH", pin_hash)
    yield


def test_verify_pin_correct():
    assert verify_pin("1234")


def test_verify_pin_wrong():
    assert not verify_pin("0000")
    assert not verify_pin("")
    assert not verify_pin("12345")


def test_verify_pin_no_hash_configured(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PIN_HASH", raising=False)
    assert not verify_pin("1234")


def test_issue_and_validate_token():
    token = issue_session_token()
    assert is_authenticated(token)


def test_invalid_token_rejected():
    assert not is_authenticated(None)
    assert not is_authenticated("")
    assert not is_authenticated("not-a-real-token")
    assert not is_authenticated("aaa.bbb.ccc")


def test_tampered_token_rejected():
    token = issue_session_token()
    bad = token[:-5] + "ZZZZZ"
    assert not is_authenticated(bad)


def test_hash_pin_roundtrip(monkeypatch):
    new_hash = hash_pin("alpha-bravo")
    monkeypatch.setenv("DASHBOARD_PIN_HASH", new_hash)
    assert verify_pin("alpha-bravo")
    assert not verify_pin("wrong")
