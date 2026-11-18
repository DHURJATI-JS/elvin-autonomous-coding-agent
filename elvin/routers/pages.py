"""HTML pages. Logged-out visitors see the homepage; signed-in users get the dashboard."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Project, User, utcnow
from ..security.auth import require_user, resolve_user
from ..services.ai import router as ai
from ..web import nav_projects, owned_project, render

router = APIRouter()
FEATURES = {
    "elvin": ("elvin.html", "Elvin AI"),
    "editor": ("editor.html", "Editor"),
    "bughunt": ("bughunt.html", "Bug Hunt"),
    "vulns": ("vulns.html", "Vulnerability Scan"),
    "loadtest": ("loadtest.html", "Load Test"),
    "git": ("git.html", "Git & GitHub"),
    "history": ("history.html", "Version history"),
}


@router.get("/")
async def home(request: Request, db: AsyncSession = Depends(get_db)):
    if await resolve_user(request, db) is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return await render(request, db, "landing.html", {"teams": ai.TEAMS})


@router.get("/about")
async def about(request: Request, db: AsyncSession = Depends(get_db)):
    return await render(request, db, "about.html", {}, user=await resolve_user(request, db))


@router.get("/dashboard")
async def dashboard(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    return await render(request, db, "dashboard.html", {"title": "Dashboard"}, user=user)


@router.get("/projects")
async def projects_page(request: Request, pick: str = "", user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    pick = pick if pick in FEATURES else ""
    return await render(request, db, "projects.html", {"title": "Projects", "pick": pick,
                                                       "pick_label": FEATURES[pick][1] if pick else ""}, user=user)


@router.get("/go/{feature}")
async def go(feature: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    if feature not in FEATURES:
        raise HTTPException(status_code=404, detail="Page not found.")
    recent = await nav_projects(db, user)
    target = next((p for p in recent if p.id == user.current_project_id), recent[0] if recent else None)
    if target is None:
        return RedirectResponse(f"/projects?pick={feature}", status_code=303)
    return RedirectResponse(f"/p/{target.id}/{feature}", status_code=303)


@router.get("/p/{project_id}")
async def project_home(project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    return RedirectResponse(f"/p/{project.id}/elvin", status_code=303)


@router.get("/p/{project_id}/{feature}")
async def project_feature(request: Request, project_id: str, feature: str, user: User = Depends(require_user),
                          db: AsyncSession = Depends(get_db)):
    if feature not in FEATURES:
        raise HTTPException(status_code=404, detail="Page not found.")
    project = await owned_project(db, user, project_id)
    if project.archived:
        project.archived = False  # opening an archived project brings it back
    project.last_opened_at = utcnow()
    user.current_project_id = project.id
    await db.commit()
    template, label = FEATURES[feature]
    return await render(request, db, template, {"project": project, "feature": feature, "title": f"{label} - {project.name}"},
                        user=user)


@router.get("/clone")
async def clone_page(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    return await render(request, db, "clone.html", {"title": "Clone a repository"}, user=user)


@router.get("/status")
async def status_page(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    return await render(request, db, "status.html", {"title": "System status"}, user=user)


@router.get("/github")
async def github_page(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    return await render(request, db, "github.html", {"title": "Your GitHub"}, user=user)


@router.get("/settings")
async def settings_page(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    return await render(request, db, "settings.html", {"title": "Settings"}, user=user)


@router.get("/usage")
async def usage_page(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    return await render(request, db, "usage.html", {"title": "AI usage"}, user=user)


@router.get("/healthz")
async def healthz(db: AsyncSession = Depends(get_db)):
    await db.execute(select(1))
    return {"ok": True}
