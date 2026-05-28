"""FastAPI factory + uvicorn server that runs on the bot's asyncio event loop.

Single process: discord clients + FastAPI in the same loop, sharing the
in-memory RunnerRegistry, SessionStore, and Ledger.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.config import DASHBOARD_BIND_HOST, DASHBOARD_PORT
from src.web.routes import build_router

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def build_app(app_state: Any) -> FastAPI:
    app = FastAPI(title="domo dashboard", version="0.2.0")
    app.include_router(build_router(app_state))

    # SPA-style: / serves index.html, JS handles routing client-side.
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


class _NoSignalServer(uvicorn.Server):
    """uvicorn installs SIGINT/SIGTERM handlers by default, which conflicts
    with the parent process (discord.py + our BotApp shutdown). Suppress
    them; BotApp.shutdown() calls server.should_exit = True instead."""

    def install_signal_handlers(self) -> None:  # noqa: D401
        return None


def make_server(app_state: Any) -> _NoSignalServer:
    config = uvicorn.Config(
        app=build_app(app_state),
        host=DASHBOARD_BIND_HOST,
        port=DASHBOARD_PORT,
        loop="asyncio",
        log_level="warning",   # bot's own logger handles our messages
        access_log=False,
    )
    return _NoSignalServer(config)
