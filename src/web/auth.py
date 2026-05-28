"""PIN-based session auth for the dashboard.

Local-only (binds to 127.0.0.1). PIN is bcrypt-hashed and stored in .env via
the wizard. On successful verify, sets a signed HttpOnly cookie via
itsdangerous; subsequent requests are authenticated by cookie verification.

Cloudflare Access JWT verification is intentionally NOT included in this
phase — when remote access is set up later, that becomes a layer on top.
"""

from __future__ import annotations

import time
from typing import Annotated

import os

import bcrypt
from fastapi import Cookie, Depends, HTTPException, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src.config import DASHBOARD_SESSION_TTL_HOURS

COOKIE_NAME = "domo_session"


def _pin_hash() -> str | None:
    """Read fresh from env so tests can monkeypatch without a config reload."""
    return os.getenv("DASHBOARD_PIN_HASH")


def _session_secret() -> str | None:
    return os.getenv("DASHBOARD_SESSION_SECRET")


def _signer() -> URLSafeTimedSerializer:
    secret = _session_secret()
    if not secret:
        raise RuntimeError("DASHBOARD_SESSION_SECRET is not configured")
    return URLSafeTimedSerializer(secret, salt="dashboard-session")


def verify_pin(pin: str) -> bool:
    """Constant-time PIN check via bcrypt."""
    pin_hash = _pin_hash()
    if not pin_hash or not pin:
        return False
    try:
        return bcrypt.checkpw(pin.encode("utf-8"), pin_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def hash_pin(pin: str) -> str:
    """Used by the wizard to produce the bcrypt hash to store in .env."""
    return bcrypt.hashpw(pin.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def issue_session_token() -> str:
    """Signed token that goes into the cookie."""
    payload = {"iat": int(time.time())}
    return _signer().dumps(payload)


def cookie_kwargs(request_is_local: bool) -> dict:
    """Cookie attributes. Secure=False on plain http://localhost; flip to True
    when behind a Cloudflare tunnel (terminating TLS at the edge)."""
    return dict(
        key=COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=not request_is_local,
        max_age=DASHBOARD_SESSION_TTL_HOURS * 3600,
        path="/",
    )


# ----------------------------- Dependencies -----------------------------

def _check_token(token: str | None) -> None:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    try:
        _signer().loads(token, max_age=DASHBOARD_SESSION_TTL_HOURS * 3600)
    except SignatureExpired:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired")
    except BadSignature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad session")


def require_session(
    session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> None:
    """FastAPI dependency that 401s any request without a valid session cookie."""
    _check_token(session)


def is_authenticated(token: str | None) -> bool:
    """Non-raising version for the index page to know which UI to render."""
    try:
        _check_token(token)
        return True
    except HTTPException:
        return False
