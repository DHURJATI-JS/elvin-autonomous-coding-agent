"""Vulnerability detector, bug hunting and load testing APIs."""
from __future__ import annotations

import asyncio
import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits
from ..config import get_settings
from ..db import get_db
from ..models import (
    BugFinding, BugHuntRun, LoadTestConfig, LoadTestEndpointStat, LoadTestRun, LoadTestSample, Task, User, VulnFinding, VulnScan,
    utcnow,
)
from ..security import ratelimit
from ..security.auth import require_user
from ..services import bughunt, journal, loadtest, snapshots, tasks, usage, vulns
from ..services.fs import normalize_rel, project_root, resolve_in, sha256_bytes
from ..web import owned_project, parse_uuid

router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- vulnerabilities
def finding_json(f: VulnFinding) -> dict[str, Any]:
    return {"id": f.id, "ecosystem": f.ecosystem, "package": f.package, "version": f.version, "manifest": f.manifest,
            "vuln_ids": f.vuln_ids, "severity": f.severity, "score": f.score, "summary": f.summary, "fixed_version": f.fixed_version,
            "recommendation": f.recommendation, "details": f.details,
            "fixable": bool(f.fixed_version)}


async def _scan_json(db: AsyncSession, scan: VulnScan | None) -> dict[str, Any]:
    if scan is None:
        return {"scan": None, "findings": []}
    rows = (await db.execute(select(VulnFinding).where(VulnFinding.scan_id == scan.id))).scalars().all()
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}
    rows = sorted(rows, key=lambda f: (order.get(f.severity, 5), f.package))
    return {"scan": {"id": str(scan.id), "status": scan.status, "created_at": scan.created_at.isoformat(), "summary": scan.summary,
                     "error": scan.error}, "findings": [finding_json(f) for f in rows]}


@router.get("/projects/{project_id}/vulns")
async def latest_vulns(project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    scan = (await db.execute(select(VulnScan).where(VulnScan.project_id == project.id).order_by(VulnScan.created_at.desc()).limit(1))).scalar_one_or_none()
    return await _scan_json(db, scan)


@router.post("/projects/{project_id}/vulns/scan")
async def run_vuln_scan(project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    await ratelimit.enforce(f"vulnscan:{user.id}", 20, 3600)
    scan = await vulns.scan_project(db, project)
    return await _scan_json(db, scan)


async def _owned_finding(db: AsyncSession, user: User, finding_id: int) -> tuple[VulnFinding, Any]:
    f = await db.get(VulnFinding, finding_id)
    if f is None:
        raise HTTPException(404, "Finding not found.")
    scan = await db.get(VulnScan, f.scan_id)
    project = await owned_project(db, user, scan.project_id)
    return f, project


@router.post("/vulns/findings/{finding_id}/fix-preview")
async def fix_preview(finding_id: int, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    f, project = await _owned_finding(db, user, finding_id)
    if not f.fixed_version:
        raise HTTPException(400, "There is no fixed version to upgrade to - replace the library instead.")
    root = project_root(project.owner_id, project.slug)
    target = resolve_in(root, f.manifest)
    if not target.is_file():
        raise HTTPException(404, "The manifest file no longer exists.")
    old = (await asyncio.to_thread(target.read_bytes)).decode("utf-8", errors="replace")
    new = vulns.apply_fix_to_text(f.manifest, old, f.package, f.version, f.fixed_version)
    if new is None or new == old:
        cmd = {"npm": f"npm install {f.package}@{f.fixed_version}", "PyPI": f"pip install '{f.package}>={f.fixed_version}'",
               "Go": f"go get {f.package}@v{f.fixed_version}", "crates.io": f"cargo update -p {f.package} --precise {f.fixed_version}",
               "RubyGems": f"bundle update {f.package}", "Packagist": f"composer require {f.package}:^{f.fixed_version}"}.get(f.ecosystem, "")
        return {"automatic": False, "command": cmd, "message": f"Elvin can't edit {f.manifest} automatically. Run this instead."}
    import difflib
    diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{f.manifest}", f"b/{f.manifest}"))
    return {"automatic": True, "manifest": f.manifest, "diff": diff, "sha256": sha256_bytes(new.encode())}


class FixApply(BaseModel):
    sha256: str = Field(min_length=64, max_length=64)


@router.post("/vulns/findings/{finding_id}/fix-apply")
async def fix_apply(finding_id: int, body: FixApply, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    f, project = await _owned_finding(db, user, finding_id)
    root = project_root(project.owner_id, project.slug)
    rel = normalize_rel(f.manifest)
    old = (await asyncio.to_thread(resolve_in(root, rel).read_bytes)).decode("utf-8", errors="replace")
    new = vulns.apply_fix_to_text(rel, old, f.package, f.version, f.fixed_version or "")
    if new is None or sha256_bytes(new.encode()) != body.sha256:
        raise HTTPException(409, "The manifest changed since the preview. Preview the fix again.")
    await asyncio.to_thread(journal.apply_ops, root, journal.journal_dir_for(get_settings().data_dir, project.id),
                            [journal.Op("write", rel, new.encode())])
    if user.versioning_enabled:
        await snapshots.create(db, project, kind="manual", label=f"Upgraded {f.package} to {f.fixed_version} (vulnerability fix)", created_by="user")
    else:
        await snapshots.refresh_index(db, project)
        await db.commit()
    return {"ok": True}


@router.post("/vulns/findings/{finding_id}/alternatives")
async def alternatives(finding_id: int, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    f, project = await _owned_finding(db, user, finding_id)
    await ratelimit.enforce(f"alts:{user.id}", 30, 3600)
    token = usage.set_context(user_id=user.id, project_id=project.id, feature="vulns", effort="low", model_prefs=user.model_prefs or {})
    try:
        alts = await vulns.suggest_alternatives(f.ecosystem, f.package, user.ai_team)
    finally:
        usage.reset_context(token)
    return {"alternatives": alts}


class LibCheck(BaseModel):
    ecosystem: str = Field(max_length=20)
    name: str = Field(max_length=214)
    version: str = Field(default="", max_length=64)


@router.post("/projects/{project_id}/vulns/check")
async def check_lib(project_id: str, body: LibCheck, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    await ratelimit.enforce(f"libcheck:{user.id}", 60, 3600)
    token = usage.set_context(user_id=user.id, project_id=project.id, feature="vulns", effort="low", model_prefs=user.model_prefs or {})
    try:
        return await vulns.check_library(db, ecosystem=body.ecosystem, name=body.name, version=body.version, project_id=project.id,
                                         team=user.ai_team)
    finally:
        usage.reset_context(token)


# --------------------------------------------------------------------------- bug hunting
@router.get("/projects/{project_id}/bughunt/options")
async def bughunt_options(project_id: str, folder: str = "", user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    root = project_root(project.owner_id, project.slug)
    folder = normalize_rel(folder, allow_empty=True)
    files, total, skipped = await asyncio.to_thread(bughunt.collect, root, folder)
    return {"folder": folder, "file_count": len(files), "byte_count": total, "skipped": skipped,
            "large": bughunt.is_large(len(files), total), "providers": bughunt.available_providers(len(files), total)}


class HuntIn(BaseModel):
    folder: str = Field(default="", max_length=1000)
    provider: str = Field(pattern="^(claude_haiku|chatgpt|qwen)$")


@router.post("/projects/{project_id}/bughunt", status_code=202)
async def start_hunt(project_id: str, body: HuntIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    await ratelimit.enforce(f"bughunt:{user.id}", 20, 3600)
    folder = normalize_rel(body.folder, allow_empty=True)
    resolve_in(project_root(project.owner_id, project.slug), folder, allow_root=True)
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    if (await db.execute(select(BugHuntRun.id).where(BugHuntRun.project_id == project.id, BugHuntRun.status == "running"))).first():
        raise HTTPException(409, "A bug hunt is already running for this project.")
    running = (await db.execute(select(func.count()).select_from(Task).where(Task.user_id == user.id, Task.status.in_(tasks.ACTIVE)))).scalar_one()
    if running >= limits.MAX_CONCURRENT_TASKS_PER_USER:
        raise HTTPException(429, "You already have the maximum number of tasks running.")
    task = Task(user_id=user.id, project_id=project.id, kind="bughunt", status="queued", title=f"Bug hunt: /{folder}"[:200],
                params={"folder": folder, "provider": body.provider})
    db.add(task)
    await db.flush()
    run = BugHuntRun(project_id=project.id, user_id=user.id, task_id=task.id, provider=body.provider, folder=folder)
    db.add(run)
    await db.commit()
    await tasks.presence.heartbeat(user.id)
    tasks.manager.launch(task, bughunt.run_hunt(run.id, task.id, user, project, folder, body.provider))
    return {"run_id": str(run.id), "task_id": str(task.id)}


def run_json(r: BugHuntRun) -> dict[str, Any]:
    return {"id": str(r.id), "task_id": str(r.task_id) if r.task_id else None, "created_at": r.created_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None, "status": r.status, "provider": r.provider,
            "provider_label": bughunt.PROVIDER_LABELS.get(r.provider, r.provider), "folder": r.folder, "file_count": r.file_count,
            "finding_count": r.finding_count, "progress": r.progress, "error": r.error}


@router.get("/projects/{project_id}/bughunt/runs")
async def hunt_runs(project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    rows = (await db.execute(select(BugHuntRun).where(BugHuntRun.project_id == project.id).order_by(BugHuntRun.created_at.desc()).limit(30))).scalars().all()
    return {"runs": [run_json(r) for r in rows]}


async def _owned_run(db: AsyncSession, user: User, run_id: str) -> BugHuntRun:
    run = await db.get(BugHuntRun, parse_uuid(run_id))
    if run is None:
        raise HTTPException(404, "Run not found.")
    await owned_project(db, user, run.project_id)
    return run


@router.get("/bughunt/{run_id}")
async def hunt_detail(run_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    run = await _owned_run(db, user, run_id)
    rows = (await db.execute(select(BugFinding).where(BugFinding.run_id == run.id).order_by(BugFinding.id))).scalars().all()
    return {"run": run_json(run), "findings": [{"id": f.id, "severity": f.severity, "category": f.category, "cwe": f.cwe, "title": f.title,
                                               "file": f.file, "line": f.line, "description": f.description,
                                               "recommendation": f.recommendation, "source": f.source, "provider": f.provider} for f in rows]}


@router.post("/bughunt/{run_id}/cancel")
async def hunt_cancel(run_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    run = await _owned_run(db, user, run_id)
    if run.task_id and tasks.manager.is_running(run.task_id):
        await tasks.manager.cancel(run.task_id, "Stopped by you.")
    return {"ok": True}


def _csv_cell(v: Any) -> Any:
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


@router.get("/bughunt/{run_id}/export.csv")
async def hunt_csv(run_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    run = await _owned_run(db, user, run_id)
    rows = (await db.execute(select(BugFinding).where(BugFinding.run_id == run.id).order_by(BugFinding.id))).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["severity", "title", "file", "line", "category", "cwe", "description", "recommendation", "source", "model"])
    for f in rows:
        w.writerow([_csv_cell(x) for x in (f.severity, f.title, f.file, f.line, f.category, f.cwe, f.description, f.recommendation, f.source, f.provider)])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="elvin-bughunt-{str(run.id)[:8]}.csv"'})


# --------------------------------------------------------------------------- load testing
class LTConfigIn(BaseModel):
    name: str = Field(default="Default", max_length=80)
    host: str = Field(max_length=300)
    users: int = Field(default=10, ge=1, le=limits.LOADTEST_MAX_USERS)
    spawn_rate: int = Field(default=2, ge=1, le=limits.LOADTEST_MAX_SPAWN_RATE)
    duration_s: int = Field(default=60, ge=limits.LOADTEST_MIN_DURATION_S, le=limits.LOADTEST_MAX_DURATION_S)
    endpoints: list[dict[str, Any]] = Field(default_factory=list, max_length=limits.LOADTEST_MAX_ENDPOINTS)
    save: bool = True


def cfg_json(c: LoadTestConfig) -> dict[str, Any]:
    return {"id": str(c.id), "name": c.name, "host": c.host, "users": c.users, "spawn_rate": c.spawn_rate,
            "duration_s": c.duration_s, "endpoints": c.endpoints, "updated_at": c.updated_at.isoformat()}


@router.get("/projects/{project_id}/loadtest/configs")
async def lt_configs(project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    rows = (await db.execute(select(LoadTestConfig).where(LoadTestConfig.project_id == project.id).order_by(LoadTestConfig.updated_at.desc()))).scalars().all()
    return {"configs": [cfg_json(c) for c in rows], "limits": {"users": limits.LOADTEST_MAX_USERS, "duration": limits.LOADTEST_MAX_DURATION_S,
                                                                "endpoints": limits.LOADTEST_MAX_ENDPOINTS, "weight": limits.LOADTEST_MAX_WEIGHT}}


@router.delete("/loadtest/configs/{config_id}")
async def lt_config_delete(config_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    cfg = await db.get(LoadTestConfig, parse_uuid(config_id))
    if cfg is None:
        raise HTTPException(404, "Not found.")
    await owned_project(db, user, cfg.project_id)
    await db.delete(cfg)
    await db.commit()
    return {"ok": True}


@router.post("/projects/{project_id}/loadtest/run", status_code=202)
async def lt_run(project_id: str, body: LTConfigIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    await ratelimit.enforce(f"loadtest:{user.id}", 30, 3600)
    try:
        endpoints = loadtest.clean_endpoints(body.endpoints)
        await loadtest.validate_target(body.host)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if body.save:
        existing = (await db.execute(select(LoadTestConfig).where(LoadTestConfig.project_id == project.id,
                                                                  LoadTestConfig.name == body.name.strip()[:80]))).scalar_one_or_none()
        if existing is None:
            count = (await db.execute(select(func.count()).select_from(LoadTestConfig).where(LoadTestConfig.project_id == project.id))).scalar_one()
            if count >= limits.LOADTEST_CONFIGS_PER_PROJECT:
                raise HTTPException(409, f"Up to {limits.LOADTEST_CONFIGS_PER_PROJECT} saved configurations per project - delete one.")
            existing = LoadTestConfig(project_id=project.id, name=body.name.strip()[:80] or "Default", host=body.host)
            db.add(existing)
        existing.host, existing.users, existing.spawn_rate, existing.duration_s = body.host, body.users, body.spawn_rate, body.duration_s
        existing.endpoints, existing.updated_at = endpoints, utcnow()
        await db.commit()
    try:
        run, task = await loadtest.launch(db, user, project, {"host": body.host, "endpoints": endpoints, "users": body.users,
                                                               "spawn_rate": body.spawn_rate, "duration_s": body.duration_s})
    except ValueError as exc:
        raise HTTPException(409 if isinstance(exc, loadtest.LoadTestRejected) else 400, str(exc)) from exc
    return {"run_id": str(run.id), "task_id": str(task.id), "note": run.config.get("note", "")}


def lt_run_json(r: LoadTestRun) -> dict[str, Any]:
    return {"id": str(r.id), "status": r.status, "config": r.config, "started_at": r.started_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None, "total_requests": r.total_requests,
            "total_failures": r.total_failures, "avg_ms": r.avg_ms, "p50_ms": r.p50_ms, "p95_ms": r.p95_ms, "p99_ms": r.p99_ms,
            "max_ms": r.max_ms, "rps": r.rps, "error": r.error, "summary": r.summary, "task_id": str(r.task_id) if r.task_id else None}


@router.get("/projects/{project_id}/loadtest/runs")
async def lt_runs(project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    rows = (await db.execute(select(LoadTestRun).where(LoadTestRun.project_id == project.id).order_by(LoadTestRun.started_at.desc()).limit(50))).scalars().all()
    return {"runs": [lt_run_json(r) for r in rows]}


async def _owned_lt(db: AsyncSession, user: User, run_id: str) -> LoadTestRun:
    run = await db.get(LoadTestRun, parse_uuid(run_id))
    if run is None:
        raise HTTPException(404, "Run not found.")
    await owned_project(db, user, run.project_id)
    return run


@router.get("/loadtest/{run_id}")
async def lt_detail(run_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    run = await _owned_lt(db, user, run_id)
    stats = (await db.execute(select(LoadTestEndpointStat).where(LoadTestEndpointStat.run_id == run.id))).scalars().all()
    samples = (await db.execute(select(LoadTestSample).where(LoadTestSample.run_id == run.id).order_by(LoadTestSample.t))).scalars().all()
    return {"run": lt_run_json(run),
            "endpoints": [{"method": s.method, "name": s.name, "requests": s.requests, "failures": s.failures, "median": s.median,
                           "avg": s.avg, "min": s.min, "max": s.max, "p90": s.p90, "p95": s.p95, "p99": s.p99, "rps": s.rps,
                           "fail_rps": s.fail_rps} for s in stats],
            "samples": [{"t": s.t, "users": s.users, "rps": s.rps, "fail_rps": s.fail_rps, "p50": s.p50, "p95": s.p95, "avg": s.avg}
                        for s in samples]}


@router.post("/loadtest/{run_id}/stop")
async def lt_stop(run_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    run = await _owned_lt(db, user, run_id)
    if run.task_id and tasks.manager.is_running(run.task_id):
        info = tasks.manager.running.get(run.task_id)
        if info:
            info.cancel_reason = "Stopped by you."
        await loadtest.stop_process(run.id)  # graceful: Locust writes its final results
    return {"ok": True}


@router.get("/loadtest/{run_id}/export.csv")
async def lt_csv(run_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    run = await _owned_lt(db, user, run_id)
    stats = (await db.execute(select(LoadTestEndpointStat).where(LoadTestEndpointStat.run_id == run.id))).scalars().all()
    samples = (await db.execute(select(LoadTestSample).where(LoadTestSample.run_id == run.id).order_by(LoadTestSample.t))).scalars().all()
    return Response(loadtest.to_csv(run, list(stats), list(samples)), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="elvin-loadtest-{str(run.id)[:8]}.csv"'})
