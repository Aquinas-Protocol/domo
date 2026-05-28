"""FastAPI routes for the dashboard.

Endpoints:
  GET  /                       index.html (or pin form via JS)
  POST /api/auth/pin           {pin: str} → sets session cookie
  POST /api/auth/logout        clears cookie
  GET  /api/me                 returns auth status + agent list
  GET  /api/agents             list with channel ids and queue depth
  POST /api/agents/{name}/chat {text: str} → streams Server-Sent Events
  GET  /api/ledger             query params: agent, status, since_minutes, limit
  GET  /api/usage              daily cost + lifetime token summary
  GET  /api/credit             Agent SDK credit governor state (tier + remaining)
  GET  /api/vault/tree         query param: path (default "")
  GET  /api/vault/file         query param: path (file rel path)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from src.config import AGENTS, OPERATOR_USER_ID
from src.cron_scheduler import _run_job
from src.cron_tools import resolve_create_kwargs
from src.web.auth import (
    COOKIE_NAME,
    cookie_kwargs,
    issue_session_token,
    require_session,
    verify_pin,
)
from src.web.vault_fs import list_dir, read_text, safe_resolve

log = logging.getLogger(__name__)


def build_router(app_state: Any) -> APIRouter:
    """`app_state` is a BotApp; we read .registry, .ledger off it."""
    r = APIRouter()

    # --------------------------- Auth ---------------------------

    @r.post("/api/auth/pin")
    async def login(payload: dict, request: Request, response: Response):
        pin = (payload.get("pin") or "").strip()
        if not verify_pin(pin):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad pin")
        token = issue_session_token()
        is_local = (request.client.host in ("127.0.0.1", "::1") if request.client else True)
        response.set_cookie(value=token, **cookie_kwargs(is_local))
        return {"ok": True}

    @r.post("/api/auth/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @r.get("/api/me")
    async def me(request: Request):
        from src.web.auth import is_authenticated
        token = request.cookies.get(COOKIE_NAME)
        return {
            "authenticated": is_authenticated(token),
            "operator_user_id": OPERATOR_USER_ID,
        }

    # --------------------------- Agents ---------------------------

    @r.get("/api/agents", dependencies=[Depends(require_session)])
    async def agents_list():
        out = []
        for name, spec in AGENTS.items():
            out.append({
                "name": name,
                "display_name": spec.display_name,
                "channel_id": spec.channel_id,
                "can_delegate": spec.can_delegate,
                "enabled_tools": spec.enabled_tools,
                "queue_depth": app_state.registry._inflight.get(name, 0),
                "online": name in app_state.bots,
                # False = hide from agent tiles + war-room voices on the
                # dashboard. The render layer filters; the API still returns
                # all agents so ledger filter, online counts, etc. stay accurate.
                "dashboard_visible": spec.dashboard_visible,
            })
        return out

    @r.post("/api/agents/{name}/chat", dependencies=[Depends(require_session)])
    async def agent_chat(name: str, payload: dict):
        if name not in AGENTS:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown agent {name!r}")
        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty message")

        runner_key = name  # dashboard chats land on each agent's main runner
        runner = app_state.registry.get_or_create(runner_key, agent_name=name)

        async def event_stream():
            acquired = await app_state.registry.acquire(name)
            if not acquired:
                yield _sse({"kind": "error", "text": f"{name} hit concurrency cap"})
                return
            try:
                async for ev in runner.send(text, triggered_by="dashboard"):
                    yield _sse({
                        "kind": ev.kind,
                        "text": ev.text,
                        "tool_name": ev.tool_name,
                        "ledger_id": ev.ledger_id,
                        "session_id": ev.session_id,
                        "cost_usd": ev.cost_usd,
                    })
                    if ev.kind == "final":
                        return
            except Exception as e:
                log.exception("dashboard chat failed")
                yield _sse({"kind": "error", "text": f"{type(e).__name__}: {e}"})
            finally:
                app_state.registry.release(name)

        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # --------------------------- Ledger ---------------------------

    @r.get("/api/ledger", dependencies=[Depends(require_session)])
    async def ledger_query(
        agent: str | None = None,
        status_: str | None = Query(None, alias="status"),
        since_minutes: int = 240,
        limit: int = 50,
    ):
        rows = app_state.ledger.query(
            agent=agent or None,
            status=status_ or None,
            since_minutes=since_minutes,
            limit=limit,
        )
        return rows

    @r.get("/api/usage", dependencies=[Depends(require_session)])
    async def usage_summary():
        return app_state.ledger.usage_summary()

    @r.get("/api/credit", dependencies=[Depends(require_session)])
    async def credit_state():
        from src.credit_governor import CreditGovernor
        return CreditGovernor(app_state.ledger).as_dict()

    # --------------------------- Vault ---------------------------

    @r.get("/api/vault/tree", dependencies=[Depends(require_session)])
    async def vault_tree(path: str = ""):
        try:
            vp = safe_resolve(path)
        except ValueError as e:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
        except FileNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"not found: {path!r}")
        if not vp.is_dir:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "tree path must be a directory")
        return {"path": vp.relative, "entries": list_dir(vp)}

    @r.get("/api/vault/file", dependencies=[Depends(require_session)])
    async def vault_file(path: str):
        try:
            vp = safe_resolve(path)
        except ValueError as e:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
        except FileNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"not found: {path!r}")
        if vp.is_dir:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "file path is a directory")
        return {"path": vp.relative, "content": read_text(vp)}

    # --------------------------- Cron / scheduler ---------------------------

    @r.get("/api/cron/jobs", dependencies=[Depends(require_session)])
    async def cron_list():
        return app_state.cron_store.list_all()

    @r.post("/api/cron/jobs", dependencies=[Depends(require_session)])
    async def cron_create(payload: dict):
        try:
            kwargs = resolve_create_kwargs(
                name=payload.get("name") or "",
                cron_expr=payload.get("cron_expr") or "",
                target_agent=payload.get("target_agent") or "",
                prompt=payload.get("prompt") or "",
                description=payload.get("description") or None,
                destination_channel_id=payload.get("destination_channel_id"),
                oneshot=bool(payload.get("oneshot", False)),
                at=payload.get("at"),
                agent_task=bool(payload.get("agent_task", False)),
                fresh_session=bool(payload.get("fresh_session", False)),
                style=payload.get("style") or "verbatim",
            )
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
        try:
            jid = app_state.cron_store.create(**kwargs, created_by="user")
        except sqlite3.IntegrityError:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"duplicate name: {kwargs['name']!r}"
            )
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
        return app_state.cron_store.get(jid)

    @r.patch("/api/cron/jobs/{job_id}", dependencies=[Depends(require_session)])
    async def cron_patch(job_id: int, payload: dict):
        if not app_state.cron_store.get(job_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no job #{job_id}")
        fields: dict[str, Any] = {}
        for key in ("name", "description", "cron_expr", "target_agent", "prompt",
                    "enabled", "destination_channel_id", "oneshot", "fresh_session",
                    "style"):
            if key in payload:
                fields[key] = payload[key]
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        if "oneshot" in fields:
            fields["oneshot"] = 1 if fields["oneshot"] else 0
        if "fresh_session" in fields:
            fields["fresh_session"] = 1 if fields["fresh_session"] else 0
        try:
            return app_state.cron_store.update(job_id, **fields)
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
        except sqlite3.IntegrityError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e))

    @r.delete("/api/cron/jobs/{job_id}", dependencies=[Depends(require_session)])
    async def cron_delete(job_id: int):
        if not app_state.cron_store.get(job_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no job #{job_id}")
        app_state.cron_store.delete(job_id)
        return {"ok": True}

    @r.post("/api/cron/jobs/{job_id}/run", dependencies=[Depends(require_session)])
    async def cron_run_now(job_id: int):
        job = app_state.cron_store.get(job_id)
        if not job:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no job #{job_id}")
        # Match the cron-fire path's runner-key logic: fresh_session=1 jobs
        # get a timestamped key per invocation so each run starts a new Claude
        # session. Without this, repeated dashboard "Run Now" clicks accumulate
        # context and tokens climb on every click.
        if job.get("fresh_session"):
            from datetime import datetime, timezone
            ts_suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            runner_key = f"cron:{job['id']}:{ts_suffix}"
        else:
            runner_key = f"cron:{job['id']}"
        try:
            runner = app_state.registry.get_or_create(
                runner_key, agent_name=job["target_agent"]
            )
        except Exception as e:
            log.exception("cron run_now: failed to acquire runner")
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e))
        # Route through _run_job (not _run_to_completion) so manual triggers
        # share the same lifecycle as scheduled fires: prompt wrapping,
        # record_run, and oneshot auto-disable.
        asyncio.create_task(_run_job(runner, job, app_state.cron_store, app_state.registry))
        return {"ok": True}

    @r.get("/api/cron/history", dependencies=[Depends(require_session)])
    async def cron_history(
        since_minutes: int = 1440,
        limit: int = 100,
    ):
        return app_state.ledger.query(
            triggered_by_prefix="cron:",
            since_minutes=since_minutes,
            limit=limit,
        )

    return r


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
