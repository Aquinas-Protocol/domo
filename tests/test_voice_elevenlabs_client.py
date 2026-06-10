"""ElevenLabs client: success, HTTP error, transport error, empty body, config."""

from __future__ import annotations

import httpx
import pytest

from src import config
from src.voice.elevenlabs_client import TTSError, synthesize


class _FakeResponse:
    def __init__(self, status_code=200, chunks=(b"ID3", b"fake-audio"), body=b""):
        self.status_code = status_code
        self._chunks = chunks
        self._body = body

    def read(self):
        return self._body

    def iter_bytes(self):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeClient:
    response: _FakeResponse = _FakeResponse()
    raises: Exception | None = None
    last_request: dict | None = None

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        if type(self).raises is not None:
            raise type(self).raises
        type(self).last_request = {"method": method, "url": url, **kwargs}
        return type(self).response


@pytest.fixture
def tts_env(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setattr(config, "ELEVENLABS_VOICE_ID", "test-voice")
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    _FakeClient.response = _FakeResponse()
    _FakeClient.raises = None
    _FakeClient.last_request = None


def test_success_writes_mp3(tts_env):
    path = synthesize("hello world")
    try:
        assert path.suffix == ".mp3"
        assert path.read_bytes() == b"ID3fake-audio"
        req = _FakeClient.last_request
        assert "test-voice" in req["url"]
        assert req["headers"]["xi-api-key"] == "test-key"
        assert req["json"]["text"] == "hello world"
    finally:
        path.unlink(missing_ok=True)


def test_http_error_raises(tts_env):
    _FakeClient.response = _FakeResponse(status_code=401, body=b"unauthorized")
    with pytest.raises(TTSError, match="401"):
        synthesize("hello")


def test_transport_error_raises(tts_env):
    _FakeClient.raises = httpx.ConnectError("boom")
    with pytest.raises(TTSError, match="transport"):
        synthesize("hello")


def test_empty_audio_body_raises(tts_env):
    _FakeClient.response = _FakeResponse(chunks=())
    with pytest.raises(TTSError, match="empty audio"):
        synthesize("hello")


def test_missing_config_raises(tts_env, monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", None)
    with pytest.raises(TTSError, match="not configured"):
        synthesize("hello")


def test_empty_text_raises(tts_env):
    with pytest.raises(TTSError, match="empty text"):
        synthesize("   ")
