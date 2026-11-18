"""Core JSON APIs: presence heartbeat, live event stream (SSE), system status,
settings and AI usage."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db as dbm
from .. import limits, redisx
from ..config import get_settings
from ..db import get_db
from ..models import TaskEvent, User, utcnow
from ..security import ratelimit
from ..security.auth import audit, require_user, session_fingerprint, session_still_valid, token_hash
from ..security.passwords import hash_password, password_problems, verify_password
from ..services import clamav, runner_client, status_monitor, tasks, usage
from ..services.ai import router as ai

router = APIRouter(prefix="/api")


@router.post("/presence/heartbeat")
async def heartbeat(request: Request, user: User = Depends(require_user)):
    await tasks.presence.heartbeat(user.id)
    running = [{"id": str(r.task_id), "kind": r.kind, "project_id": str(r.project_id) if r.project_id else None}
               for r in tasks.manager.user_tasks(user.id)]
    return {"ok": True, "session_fp": session_fingerprint(request), "running": running}


@router.post("/presence/leave")
async def leave(user: User = Depends(require_user)):
    await tasks.presence.leave(user.id)
    return {"ok": True}


def _sse(event: dict[str, Any]) -> str:
    out = ""
    if event.get("id"):
        out += f"id: {event['id']}\n"
    out += f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
    return out


@router.get("/stream")
async def stream(request: Request, after: int = 0, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    user_id = user.id
    sid_hash = token_hash(str(request.session.get("sid", "")))
    try:
        last = int(request.headers.get("last-event-id") or after or 0)
    except ValueError:
        last = 0
    await db.close()  # never hold a pooled connection for the lifetime of a stream
    queue = tasks.bus.subscribe(user_id)
    await tasks.presence.heartbeat(user_id)

    async def gen():
        try:
            yield "retry: 4000\n\n"
            if last:
                async with dbm.session() as s:
                    rows = (await s.execute(select(TaskEvent).where(TaskEvent.user_id == user_id, TaskEvent.id > last)
                                            .order_by(TaskEvent.id).limit(500))).scalars().all()
                for ev in rows:
                    yield _sse({"id": ev.id, "task_id": str(ev.task_id), "project_id": str(ev.project_id) if ev.project_id else None,
                                "type": ev.type, "data": ev.data, "ts": ev.ts.isoformat(), "replay": True})
            checked = time.monotonic()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10)
                    yield _sse(event)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                if time.monotonic() - checked > limits.HEARTBEAT_INTERVAL_S:
                    checked = time.monotonic()
                    if not await session_still_valid(user_id, sid_hash):
                        yield _sse({"type": "session_ended", "data": {"message": "Your account signed in somewhere else."}})
                        break
                    await tasks.presence.heartbeat(user_id)
        finally:
            tasks.bus.unsubscribe(user_id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@router.get("/system/summary")
async def system_summary(user: User = Depends(require_user)):
    """Cheap snapshot for the reduced-effort banner (no new checks)."""
    return status_monitor.monitor.summary()


@router.get("/system/monitor")
async def system_monitor(user: User = Depends(require_user)):
    mon = status_monitor.monitor
    if mon.checked_at is None:
        await mon.check_once()
    return {**mon.snapshot(), "is_admin": user.is_admin, "gateway_url": ai.gateway_url()}


@router.post("/system/monitor/refresh")
async def system_monitor_refresh(user: User = Depends(require_user)):
    await ratelimit.enforce(f"status-refresh:{user.id}", 4, 60, "Status was refreshed a moment ago.")
    snap = await status_monitor.monitor.check_once()
    return {**snap, "is_admin": user.is_admin, "gateway_url": ai.gateway_url()}


class GatewayIn(BaseModel):
    url: str = Field(min_length=8, max_length=300)
    api_key: str | None = Field(default=None, max_length=300)


def _admin_only(user: User, what: str) -> None:
    if not user.is_admin:
        raise HTTPException(403, f"Only the administrator (the first account) can change {what}.")


async def _save_settings(db: AsyncSession, request: Request, user: User, values: dict[str, str]) -> None:
    from ..services import app_settings

    await app_settings.save(db, values, user_id=user.id, ip=request.client.host if request.client else None)
    await db.commit()
    await app_settings.refresh()


@router.post("/system/gateway")
async def set_gateway(body: GatewayIn, request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """Admin only: point Elvin at the gateway's new address (e.g. after a DHCP change)."""
    _admin_only(user, "the gateway address")
    try:
        url = ai.normalize_gateway_url(body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    values = {"gateway_url": url}
    if body.api_key:
        values["gateway_api_key"] = body.api_key.strip()
    await _save_settings(db, request, user, values)
    snap = await status_monitor.monitor.check_once()
    return {**snap, "is_admin": True, "gateway_url": ai.gateway_url()}


class GemmaSourceIn(BaseModel):
    source: str = Field(pattern="^(auto|local)$")


@router.post("/system/gemma-source")
async def set_gemma_source(body: GemmaSourceIn, request: Request, user: User = Depends(require_user),
                           db: AsyncSession = Depends(get_db)):
    """Admin only: "local" = use the local Docker Gemma for now (e.g. while the gateway has trouble);
    "auto" = gateway first, local Docker Gemma whenever the gateway is down."""
    _admin_only(user, "where Gemma runs")
    if body.source == "local" and ai.LOCAL_FALLBACK not in ai.registry().providers:
        raise HTTPException(409, "The local Docker Gemma isn't set up on this installation - re-run the installer to add it.")
    await ratelimit.enforce(f"gemma-source:{user.id}", 20, 3600)
    await _save_settings(db, request, user, {"gemma_source": body.source})
    return {**status_monitor.monitor.snapshot(), "is_admin": True, "gateway_url": ai.gateway_url()}


@router.get("/system/status")
async def system_status(user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    s = get_settings()
    reg = ai.registry()
    providers, clam, runner = await asyncio.gather(reg.status(), clamav.status(), runner_client.health())
    try:
        await db.execute(text("select 1"))
        db_ok = True
    except Exception:  # pragma: no cover
        db_ok = False
    return {
        "platform": {"os": s.host_os, "arch": s.host_arch, "gpu": s.host_gpu, "gpu_name": s.host_gpu_name, "ram_gb": s.host_ram_gb,
                     "vram_gb": s.host_vram_gb, "compatible": s.host_compatible, "local_backend": s.local_llm_backend,
                     "qwen_model": s.qwen_model if s.has_local_qwen else None},
        "providers": providers, "teams": reg.teams(), "default_team": reg.default_team(),
        "clamav": clam, "runner": {"online": runner[0], "detail": runner[1]},
        "redis": await redisx.ping(), "database": db_ok, "running_tasks": len(tasks.manager.user_tasks(user.id)),
        "workspace_hint": s.host_home_hint, "summary": status_monitor.monitor.summary(),
        "latency": {k: {"api_ms": v["api_ms"], "gen_ms": v["gen_ms"]} for k, v in status_monitor.monitor.latest.items()},
        "gateway": {k: v for k, v in status_monitor.monitor.gateway.items() if k != "history"},
    }


class SettingsIn(BaseModel):
    theme: str | None = None
    versioning_enabled: bool | None = None
    ai_team: str | None = None
    ai_effort: str | None = None
    model_prefs: dict[str, str] | None = None


@router.post("/settings")
async def save_settings(body: SettingsIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    reg = ai.registry()
    if body.theme is not None:
        if body.theme not in ("system", "dark", "light"):
            raise HTTPException(400, "Unknown theme.")
        user.theme = body.theme
    if body.versioning_enabled is not None:
        user.versioning_enabled = body.versioning_enabled
    if body.ai_team is not None:
        if body.ai_team not in ai.TEAMS:
            raise HTTPException(400, "Unknown AI team.")
        if not reg.team_available(body.ai_team):
            raise HTTPException(400, "That combination needs a model that isn't set up on this computer.")
        user.ai_team = body.ai_team
    if body.ai_effort is not None:
        if body.ai_effort not in usage.EFFORTS:
            raise HTTPException(400, "Unknown effort level.")
        user.ai_effort = body.ai_effort
    if body.model_prefs is not None:
        prefs = dict(user.model_prefs or {})
        allowed = {"claude_coder": usage.CLAUDE_CODER_CHOICES, "claude_fast": usage.CLAUDE_FAST_CHOICES,
                   "openai_coder": usage.openai_choices(get_settings()), "openai_fast": usage.openai_choices(get_settings())}
        for key, value in body.model_prefs.items():
            if key not in allowed or value not in allowed[key]:
                raise HTTPException(400, f"Unknown model choice for {key}.")
            prefs[key] = value
        user.model_prefs = prefs
    await db.commit()
    return {"ok": True, "theme": user.theme, "versioning_enabled": user.versioning_enabled, "ai_team": user.ai_team,
            "ai_effort": user.ai_effort, "model_prefs": user.model_prefs}


class PasswordIn(BaseModel):
    current: str = Field(max_length=300)
    new: str = Field(max_length=300)
    confirm: str = Field(max_length=300)


@router.post("/settings/password")
async def change_password(body: PasswordIn, request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    await ratelimit.enforce(f"pwchange:{user.id}", 10, 3600)
    if not await verify_password(user.password_hash, body.current):
        raise HTTPException(400, "Your current password is wrong.")
    if body.new != body.confirm:
        raise HTTPException(400, "The two new passwords don't match.")
    problems = password_problems(body.new, username=user.username, email=user.email)
    if problems:
        raise HTTPException(400, " ".join(problems))
    user.password_hash = await hash_password(body.new)
    user.password_changed_at = utcnow()
    await audit(db, "password_changed", request, user.id)
    await db.commit()
    return {"ok": True}


@router.get("/settings/options")
async def settings_options(user: User = Depends(require_user)):
    s = get_settings()
    reg = ai.registry()
    return {
        "teams": reg.teams(), "current_team": user.ai_team, "effective_team": user.ai_team if reg.team_available(user.ai_team) else reg.default_team(),
        "efforts": [{"id": e, "blurb": usage.EFFORT_BLURB[e]} for e in usage.EFFORTS], "current_effort": user.ai_effort,
        "models": {"claude_coder": usage.CLAUDE_CODER_CHOICES if s.has_claude else [],
                   "claude_fast": usage.CLAUDE_FAST_CHOICES if s.has_claude else [],
                   "openai_coder": usage.openai_choices(s) if s.has_openai else [],
                   "openai_fast": usage.openai_choices(s) if s.has_openai else []},
        "defaults": {"claude_coder": s.claude_coder_model, "claude_fast": s.claude_fast_model,
                     "openai_coder": s.openai_model, "openai_fast": s.openai_fast_model},
        "model_prefs": user.model_prefs or {}, "versioning_enabled": user.versioning_enabled, "theme": user.theme,
    }


@router.get("/usage")
async def usage_summary(days: int = 30, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    days = max(1, min(days, 365))
    await usage.flush()
    data = await usage.summary(db, user.id, days)
    reg = ai.registry()
    data["recommendations"] = usage.recommendations(data, team=user.ai_team, effort=user.ai_effort, teams=reg.teams())
    data["prices_note"] = "Costs are estimates from public list prices; local models (Gemma, Qwen) are free."
    return data
