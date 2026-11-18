"""GitHub connection, stats, push and safe cloning."""
from __future__ import annotations

import asyncio
import shutil

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db as dbm
from .. import limits
from ..db import get_db
from ..models import CloneRequest, GitHubStats, Task, User, utcnow
from ..security import ratelimit
from ..security.auth import require_user
from ..services import clone, gitops, tasks
from ..web import owned_project, parse_uuid

router = APIRouter(prefix="/api")


@router.get("/github/status")
async def gh_status(user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    acct = await gitops.account(db, user.id)
    return {"tools": await gitops.tools(),
            "account": {"login": acct.login, "scopes": acct.scopes, "connected_at": acct.connected_at.isoformat()} if acct else None}


class TokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=255)


@router.post("/github/token")
async def gh_token(body: TokenIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    await ratelimit.enforce(f"ghtoken:{user.id}", 10, 3600)
    try:
        acct = await gitops.connect_token(db, user, body.token)
    except gitops.GitError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "login": acct.login, "scopes": acct.scopes}


async def _store_device_token(user_id, token: str) -> str:
    async with dbm.session() as db:
        user = await db.get(User, user_id)
        acct = await gitops.connect_token(db, user, token)
        return acct.login


@router.post("/github/device")
async def gh_device(user: User = Depends(require_user)):
    await ratelimit.enforce(f"ghdevice:{user.id}", 10, 3600)
    tools = await gitops.tools()
    if not tools.get("gh"):
        raise HTTPException(503, "The GitHub CLI isn't installed in Elvin's container.")
    flow = await gitops.device_start(user, _store_device_token)
    return flow.public()


@router.get("/github/device/{flow_id}")
async def gh_device_status(flow_id: str, user: User = Depends(require_user)):
    flow = gitops.device_status(flow_id, user.id)
    if flow is None:
        raise HTTPException(404, "Sign-in session not found.")
    return flow.public()


@router.post("/github/disconnect")
async def gh_disconnect(user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    await gitops.disconnect(db, user)
    return {"ok": True}


def stats_json(row: GitHubStats | None) -> dict:
    if row is None:
        return {"data": {}, "suggestions": [], "fetched_at": None, "error": None}
    data = {k: v for k, v in (row.data or {}).items() if not k.startswith("_")}
    return {"data": data, "suggestions": row.suggestions or [], "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
            "error": row.error}


@router.get("/github/stats")
async def gh_stats(user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    row = await db.get(GitHubStats, user.id)
    stale = row is None or row.fetched_at is None or (utcnow() - row.fetched_at).total_seconds() > limits.GITHUB_STATS_MAX_AGE_S
    if stale:  # first visit / old data: fetch now (this is the "collected at the beginning" view)
        row = await gitops.refresh_stats(db, user, force=True)
    return stats_json(row)


@router.post("/github/stats/refresh")
async def gh_stats_refresh(user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    ok, retry = await ratelimit.hit(f"ghstats:{user.id}", 1, limits.GITHUB_REFRESH_MIN_INTERVAL_S)
    if not ok:
        raise HTTPException(429, f"Stats were refreshed a moment ago - try again in {retry}s.")
    row = await gitops.refresh_stats(db, user, force=True)
    return stats_json(row)


async def _token_or_400(db: AsyncSession, user: User) -> str:
    token = await gitops.token_for(db, user.id)
    if not token:
        raise HTTPException(400, "Connect GitHub first.")
    return token


@router.get("/github/repos/{owner}/{repo}")
async def gh_repo_view(owner: str, repo: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """Read-only repository overview (README, files, recent commits)."""
    await ratelimit.enforce(f"ghview:{user.id}", 120, 3600)
    token = await _token_or_400(db, user)
    try:
        return await gitops.repo_view(token, owner, repo)
    except gitops.GitError as exc:
        raise HTTPException(404 if "Not Found" in str(exc) else 400, "GitHub couldn't show that repository." if str(exc) == "auth" else str(exc)) from exc


@router.get("/github/repos/{owner}/{repo}/contents")
async def gh_repo_contents(owner: str, repo: str, path: str = "", user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    await ratelimit.enforce(f"ghview:{user.id}", 120, 3600)
    token = await _token_or_400(db, user)
    try:
        return await gitops.repo_contents(token, owner, repo, path[:1000])
    except gitops.GitError as exc:
        raise HTTPException(404 if "Not Found" in str(exc) else 400, str(exc)) from exc


@router.get("/projects/{project_id}/git")
async def project_git(project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    acct = await gitops.account(db, user.id)
    return {"status": await gitops.project_status(project), "github_repo": project.github_repo, "repo_url": project.repo_url,
            "account": {"login": acct.login} if acct else None, "tools": await gitops.tools(), "default_name": project.slug}


class PushIn(BaseModel):
    repo_name: str = Field(min_length=1, max_length=100)
    private: bool = True
    description: str = Field(default="", max_length=300)
    message: str = Field(default="Update from Elvin", max_length=500)
    secrets_action: str = Field(default="", pattern="^(|ignore_files|push_anyway)$")


@router.post("/projects/{project_id}/git/push")
async def project_push(project_id: str, body: PushIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    await ratelimit.enforce(f"push:{user.id}", 20, 3600)
    try:
        return await gitops.push(db, user, project, repo_name=body.repo_name.strip(), private=body.private,
                                 description=body.description, message=body.message, secrets_action=body.secrets_action)
    except gitops.GitError as exc:
        raise HTTPException(400, str(exc)) from exc


# --------------------------------------------------------------------------- clone
class CloneIn(BaseModel):
    url: str = Field(min_length=3, max_length=500)


def clone_json(c: CloneRequest) -> dict:
    return {"id": str(c.id), "url": c.url, "status": c.status, "owner": c.owner, "repo": c.repo, "commit": c.commit_sha[:12],
            "license_spdx": c.license_spdx, "license_name": c.license_name, "terms": c.terms, "simplified": c.simplified,
            "summary": c.summary, "readme": c.readme[:20_000], "file_count": c.file_count, "size_bytes": c.size_bytes,
            "project_id": str(c.project_id) if c.project_id else None, "error": c.error, "scan": c.scan,
            "agreed_at": c.agreed_at.isoformat() if c.agreed_at else None, "created_at": c.created_at.isoformat()}


@router.post("/clone", status_code=202)
async def start_clone(body: CloneIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    await ratelimit.enforce(f"clone:{user.id}", 20, 3600)
    try:
        await asyncio.to_thread(clone.parse_repo_url, body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    pending = (await db.execute(select(func.count()).select_from(CloneRequest).where(
        CloneRequest.user_id == user.id, CloneRequest.status.in_(("inspecting", "awaiting_agreement", "scanning"))))).scalar_one()
    if pending >= limits.PENDING_CLONES_PER_USER:
        raise HTTPException(409, f"Finish or cancel your other {pending} clone(s) first.")
    req = CloneRequest(user_id=user.id, url=body.url.strip()[:500])
    db.add(req)
    await db.flush()
    task = Task(user_id=user.id, kind="clone", status="queued", title=f"Clone {body.url.strip()[:150]}", params={"clone_id": str(req.id)})
    db.add(task)
    await db.commit()
    tasks.manager.launch(task, clone.inspect(req.id, user, task.id))
    return {"clone_id": str(req.id), "task_id": str(task.id)}


async def _owned_clone(db: AsyncSession, user: User, clone_id: str) -> CloneRequest:
    req = await db.get(CloneRequest, parse_uuid(clone_id))
    if req is None or req.user_id != user.id:
        raise HTTPException(404, "Clone request not found.")
    return req


@router.get("/clone")
async def list_clones(user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(CloneRequest).where(CloneRequest.user_id == user.id).order_by(CloneRequest.created_at.desc()).limit(15))).scalars().all()
    return {"clones": [{k: v for k, v in clone_json(c).items() if k not in ("readme", "terms", "simplified")} for c in rows]}


@router.get("/clone/{clone_id}")
async def get_clone(clone_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    return clone_json(await _owned_clone(db, user, clone_id))


@router.post("/clone/{clone_id}/simplify")
async def simplify_terms(clone_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    req = await _owned_clone(db, user, clone_id)
    if req.status != "awaiting_agreement":
        raise HTTPException(409, "Terms can only be simplified while waiting for your agreement.")
    await ratelimit.enforce(f"simplify:{user.id}", 20, 3600)
    try:
        items = await clone.simplify(db, req, user)
    except Exception as exc:
        raise HTTPException(503, f"Elvin couldn't simplify the terms right now ({exc}).") from exc
    return {"simplified": items}


class AgreeIn(BaseModel):
    agreed: list[str] = Field(default_factory=list, max_length=50)


@router.post("/clone/{clone_id}/agree", status_code=202)
async def agree(clone_id: str, body: AgreeIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    req = await _owned_clone(db, user, clone_id)
    if req.status != "awaiting_agreement":
        raise HTTPException(409, "This clone is not waiting for your agreement.")
    needed = {s["id"] for s in req.terms}
    if set(body.agreed) != needed:
        raise HTTPException(400, "Tick every statement to agree - or cancel the clone.")
    tools = await gitops.tools()
    if not tools.get("git"):
        raise HTTPException(503, "Git is not installed.")
    req.status = "scanning"
    req.agreed_at = utcnow()
    req.error = None
    task = Task(user_id=user.id, kind="clone", status="queued", title=f"Import {req.owner}/{req.repo}", params={"clone_id": str(req.id)})
    db.add(task)
    await db.commit()
    tasks.manager.launch(task, clone.finalize(req.id, user, task.id))
    return {"ok": True, "task_id": str(task.id)}


@router.post("/clone/{clone_id}/cancel")
async def cancel_clone(clone_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    req = await _owned_clone(db, user, clone_id)
    if req.status in ("completed",):
        raise HTTPException(409, "This clone was already imported.")
    req.status = "cancelled"
    req.updated_at = utcnow()
    await db.commit()
    await asyncio.to_thread(shutil.rmtree, clone.quarantine_dir(req.id), True)
    return {"ok": True}
