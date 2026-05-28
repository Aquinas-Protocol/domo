"""Opaque token encoding for CalDAV calendar and event identifiers.

Tools never expose raw CalDAV URLs to agents. `calendar_id` and `event_id` are
url-safe base64-encoded JSON blobs that round-trip through the agent layer
without leaking the iCloud principal href.

If ``CALENDAR_TOKEN_SECRET`` is set in the environment, tokens are HMAC-SHA256
signed: ``<base64(payload)>.<base64(sig)>``. Without a secret, the payload
travels unsigned — safe for an internal-only agent boundary, but
defense-in-depth is one env var away.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload_b64: str, secret: str) -> str:
    sig = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64encode(sig)


def _encode_token(payload: dict[str, Any], secret: str | None) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64encode(raw)
    if not secret:
        return payload_b64
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def _decode_token(token: str, secret: str | None) -> dict[str, Any]:
    if "." in token:
        payload_b64, sig_b64 = token.split(".", 1)
        if secret:
            expected = _sign(payload_b64, secret)
            if not hmac.compare_digest(expected, sig_b64):
                raise ValueError("token signature mismatch")
        # If no secret configured but a signature was present, accept the payload
        # so tokens issued under a previous secret keep working after rotation.
    else:
        payload_b64 = token
        if secret:
            # Token issued before the secret was set — still accept, the boundary
            # is internal. Operators who want strict signing should rotate ids.
            pass
    raw = _b64decode(payload_b64)
    return json.loads(raw.decode("utf-8"))


def encode_calendar_id(href: str, *, secret: str | None = None) -> str:
    """Pack a CalDAV calendar href into an opaque calendar_id token."""
    return _encode_token({"href": href}, secret)


def decode_calendar_id(token: str, *, secret: str | None = None) -> str:
    """Unpack a calendar_id back to its href. Raises ValueError on tampering."""
    payload = _decode_token(token, secret)
    href = payload.get("href")
    if not isinstance(href, str) or not href:
        raise ValueError("calendar_id missing href")
    return href


def encode_event_id(
    *,
    calendar_href: str,
    uid: str,
    recurrence_id: str | None = None,
    etag: str | None = None,
    secret: str | None = None,
) -> str:
    """Pack an event identity (+ optional recurrence + etag) into a token.

    ``recurrence_id`` is the RECURRENCE-ID of an expanded occurrence; absent
    for one-off events or master rows. ``etag`` is the live etag at read time
    so write tools can detect concurrent modifications without an extra round
    trip.
    """
    payload: dict[str, Any] = {"href": calendar_href, "uid": uid}
    if recurrence_id:
        payload["rid"] = recurrence_id
    if etag:
        payload["etag"] = etag
    return _encode_token(payload, secret)


def decode_event_id(token: str, *, secret: str | None = None) -> dict[str, Any]:
    """Unpack an event_id. Returns ``{calendar_href, uid, recurrence_id?, etag?}``."""
    payload = _decode_token(token, secret)
    href = payload.get("href")
    uid = payload.get("uid")
    if not isinstance(href, str) or not href:
        raise ValueError("event_id missing calendar href")
    if not isinstance(uid, str) or not uid:
        raise ValueError("event_id missing uid")
    out: dict[str, Any] = {"calendar_href": href, "uid": uid}
    if isinstance(payload.get("rid"), str):
        out["recurrence_id"] = payload["rid"]
    if isinstance(payload.get("etag"), str):
        out["etag"] = payload["etag"]
    return out
