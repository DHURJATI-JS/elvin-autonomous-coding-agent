"""Template rendering (Jinja2, autoescaping ON for every template) and shared
request helpers."""
from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import jinja2
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import limits
from .config import get_settings
from .models import Project, Task, User
from .security.auth import session_fingerprint
from .security.csrf import get_csrf_token

BASE = Path(__file__).parent
_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE / "templates")),
    autoescape=True,  # escape everything, always - never mark user content |safe
    trim_blocks=True,
    lstrip_blocks=True,
)
templates = Jinja2Templates(env=_env)


def _timeago(value: dt.datetime | None) -> str:
    if not value:
        return "never"
    delta = dt.datetime.now(dt.timezone.utc) - value
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    if s < 86400 * 30:
        return f"{s // 86400} d ago"
    return value.strftime("%d %b %Y")


def _filesize(n: int | None) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


_env.filters["timeago"] = _timeago
_env.filters["filesize"] = _filesize
_env.globals["limits"] = limits


async def nav_projects(db: AsyncSession, user: User) -> list[Project]:
    return list((await db.execute(select(Project).where(Project.owner_id == user.id, Project.archived.is_(False))
                                  .order_by(Project.last_opened_at.desc()).limit(limits.NAV_RECENT_PROJECTS))).scalars().all())


async def render(request: Request, db: AsyncSession, name: str, ctx: dict[str, Any] | None = None, *,
                 user: User | None = None, status_code: int = 200) -> HTMLResponse:
    s = get_settings()
    ctx = dict(ctx or {})
    nav: list[Project] = []
    current: Project | None = None
    running = 0
    if user is not None:
        nav = await nav_projects(db, user)
        current = next((p for p in nav if p.id == user.current_project_id), nav[0] if nav else None)
        if ctx.get("project") is not None:
            current = ctx["project"]
        running = (await db.execute(select(func.count()).select_from(Task).where(
            Task.user_id == user.id, Task.status.in_(("queued", "running", "awaiting_approval"))))).scalar_one()
    flash = request.session.pop("flash", None)
    ctx.update({
        "request": request, "nonce": request.state.csp_nonce, "csrf_token": get_csrf_token(request), "user": user,
        "nav_projects": nav, "current_project": current, "running_tasks": running, "flash": flash,
        "session_fp": session_fingerprint(request) if user else "", "app_name": s.app_name,
        "theme": (user.theme if user else "system"), "host_os": s.host_os, "path": request.url.path,
        "heartbeat_s": limits.HEARTBEAT_INTERVAL_S,
    })
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


async def owned_project(db: AsyncSession, user: User, project_id: Any, *, lock: bool = False) -> Project:
    try:
        pid = uuid.UUID(str(project_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    q = select(Project).where(Project.id == pid, Project.owner_id == user.id)
    if lock:
        q = q.with_for_update()
    project = (await db.execute(q)).scalar_one_or_none()
    if project is None:  # same answer whether it doesn't exist or belongs to someone else
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


def parse_uuid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Not found.") from exc
