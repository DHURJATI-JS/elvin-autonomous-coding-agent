"""Projects, files (editor), uploads, integrity and version history."""
from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits
from ..config import get_settings
from ..db import get_db
from ..db import session as dbm_session
from ..models import CommandRun, Message, Project, ScanResult, Snapshot, Task, User, utcnow
from ..security import ratelimit
from ..security.auth import confirm_password, require_user
from ..services import archives, clamav, integrity, journal, runner_client, snapshots, tasks, usage, vulns
from ..services.ai import router as ai
from ..services.fs import (
    PathError, atomic_write_bytes, build_tree, is_text, language_for, list_dirs, normalize_rel, project_root, resolve_in,
    sha256_bytes, slugify,
)
from ..web import owned_project, parse_uuid

router = APIRouter(prefix="/api")
_last_autosave: dict[uuid.UUID, float] = {}


def project_json(p: Project) -> dict:
    return {"id": str(p.id), "name": p.name, "slug": p.slug, "description": p.description, "source": p.source,
            "archived": p.archived, "status": p.status, "repo_url": p.repo_url, "github_repo": p.github_repo,
            "file_count": p.file_count, "total_bytes": p.total_bytes, "created_at": p.created_at.isoformat(),
            "last_opened_at": p.last_opened_at.isoformat(), "integrity_ok": (p.integrity or {}).get("ok", True)}


async def _new_project(db: AsyncSession, user: User, name: str, description: str, source: str) -> Project:
    name = name.strip()[:80]
    if len(name) < 2:
        raise HTTPException(400, "Give the project a name (at least 2 characters).")
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())  # race-safe cap
    count = (await db.execute(select(func.count()).select_from(Project).where(Project.owner_id == user.id))).scalar_one()
    if count >= limits.MAX_PROJECTS_PER_USER:
        raise HTTPException(409, f"You can have up to {limits.MAX_PROJECTS_PER_USER} projects. Delete one to make room.")
    base = slugify(name)
    slug, n = base, 1
    while (await db.execute(select(Project.id).where(Project.owner_id == user.id, Project.slug == slug))).first():
        n += 1
        slug = f"{base}-{n}"
    project = Project(owner_id=user.id, name=name, slug=slug, description=description.strip()[:500], source=source)
    db.add(project)
    await db.flush()
    return project


class ProjectIn(BaseModel):
    name: str = Field(max_length=80)
    description: str = Field(default="", max_length=500)


@router.get("/projects")
async def list_projects(include_archived: bool = True, user: User = Depends(require_user),
                        db: AsyncSession = Depends(get_db)):
    q = select(Project).where(Project.owner_id == user.id)
    if not include_archived:
        q = q.where(Project.archived.is_(False))
    rows = (await db.execute(q.order_by(Project.archived, Project.last_opened_at.desc()))).scalars().all()
    return {"projects": [project_json(p) for p in rows], "limit": limits.MAX_PROJECTS_PER_USER}


@router.post("/projects")
async def create_project(body: ProjectIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await _new_project(db, user, body.name, body.description, "new")
    root = project_root(user.id, project.slug)
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    readme = f"# {project.name}\n\n{project.description or 'Created with Elvin.'}\n"
    await asyncio.to_thread(atomic_write_bytes, root / "README.md", readme.encode())
    await db.commit()
    await snapshots.create(db, project, kind="initial", label="Project created", created_by="user")
    return project_json(project)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    archived: bool | None = None


@router.patch("/projects/{project_id}")
async def patch_project(project_id: str, body: ProjectPatch, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    if body.name is not None and len(body.name.strip()) >= 2:
        project.name = body.name.strip()[:80]
    if body.description is not None:
        project.description = body.description.strip()[:500]
    if body.archived is not None:
        project.archived = body.archived
        if body.archived and user.current_project_id == project.id:
            user.current_project_id = None
    await db.commit()
    return project_json(project)


class DeleteIn(BaseModel):
    confirm_name: str = Field(max_length=80)
    delete_files: bool = False
    password: str | None = Field(default=None, max_length=200)


@router.post("/projects/{project_id}/delete")
async def delete_project(project_id: str, body: DeleteIn, request: Request, user: User = Depends(require_user),
                         db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    if body.confirm_name.strip() != project.name:
        raise HTTPException(400, "Type the project name exactly to confirm.")
    await confirm_password(db, request, user, body.password, action=f'delete the project "{project.name}"')
    for r in tasks.manager.user_tasks(user.id):
        if r.project_id == project.id:
            await tasks.manager.cancel(r.task_id, "The project was deleted.")
    root = project_root(user.id, project.slug)
    pid = project.id
    if user.current_project_id == pid:
        user.current_project_id = None
    await db.delete(project)
    await db.commit()
    await asyncio.to_thread(snapshots.remove_store, pid)
    await asyncio.to_thread(shutil.rmtree, get_settings().data_dir / "journals" / str(pid), True)
    if body.delete_files:
        await asyncio.to_thread(shutil.rmtree, root, True)
    else:  # keep the files, out of Elvin's way
        trash = get_settings().home / "removed-projects"
        if root.exists():
            trash.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(root), str(trash / f"{project.slug}-{int(time.time())}"))
    return {"ok": True, "files_kept_in": None if body.delete_files else f"{get_settings().host_home_hint}/removed-projects"}


async def _receive_files(request: Request, staging: Path) -> list[str]:
    form = await request.form(max_files=limits.MAX_UPLOAD_FILES, max_fields=limits.MAX_UPLOAD_FILES + 20)
    written: list[str] = []
    total = 0
    for key, item in form.multi_items():
        if key != "files" or isinstance(item, str):
            continue
        rel = normalize_rel(item.filename or "")
        target = resolve_in(staging, rel, for_write=True)
        data = await item.read()
        if len(data) > limits.MAX_SINGLE_UPLOAD_BYTES:
            raise HTTPException(413, f"{rel} is larger than {limits.MAX_SINGLE_UPLOAD_BYTES // (1024 * 1024)} MB.")
        total += len(data)
        if total > limits.MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"The upload is larger than {limits.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        await asyncio.to_thread(atomic_write_bytes, target, data)
        written.append(rel)
    if not written:
        raise HTTPException(400, "No files were received.")
    return written


async def _scan_or_block(db: AsyncSession, user: User, staging: Path, project_id: uuid.UUID | None, context: str) -> dict:
    scan = await clamav.scan_dir(staging)
    db.add(ScanResult(user_id=user.id, project_id=project_id, context=context, status=scan["status"], scanned=scan["scanned"],
                      infected=scan["infected"][:50]))
    await db.commit()
    reason = clamav.must_block(scan)
    if reason:
        shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(422 if scan["status"] == "infected" else 503, reason)
    return scan


@router.post("/projects/upload")
async def upload_new_project(request: Request, name: str = "", user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    await ratelimit.enforce(f"upload:{user.id}", 20, 3600)
    staging = get_settings().data_dir / "uploads" / uuid.uuid4().hex
    try:
        files = await _receive_files(request, staging)
        files, unpacked, zip_problems = await asyncio.to_thread(archives.unpack_all, staging, files)
        # a folder upload arrives as "folder/..."; use its contents as the project root
        tops = {f.split("/")[0] for f in files}
        root_dir = staging / next(iter(tops)) if len(tops) == 1 and all("/" in f for f in files) else staging
        project_name = name.strip() or (next(iter(tops)) if root_dir != staging else "Uploaded project")
        scan = await _scan_or_block(db, user, staging, None, "upload")
        project = await _new_project(db, user, project_name, "Uploaded from your computer", "upload")
        dest = project_root(user.id, project.slug)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(root_dir), str(dest))
        await db.commit()
        await snapshots.create(db, project, kind="initial", label="Uploaded", created_by="user")
        return {**project_json(project), "scanned": scan["scanned"], "scan_status": scan["status"], "unpacked": unpacked,
                "warnings": zip_problems}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@router.post("/projects/{project_id}/upload")
async def upload_into_project(request: Request, project_id: str, folder: str = "", user: User = Depends(require_user),
                              db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    await ratelimit.enforce(f"upload:{user.id}", 30, 3600)
    base = normalize_rel(folder, allow_empty=True)
    staging = get_settings().data_dir / "uploads" / uuid.uuid4().hex
    try:
        files = await _receive_files(request, staging)
        files, unpacked, zip_problems = await asyncio.to_thread(archives.unpack_all, staging, files)
        scan = await _scan_or_block(db, user, staging, project.id, "upload")
        root = project_root(project.owner_id, project.slug)
        ops = []
        for rel in files:
            dest = f"{base}/{rel}" if base else rel
            resolve_in(root, dest, for_write=True)
            ops.append(journal.Op("write", dest, await asyncio.to_thread((staging / rel).read_bytes)))
        await asyncio.to_thread(journal.apply_ops, root, journal.journal_dir_for(get_settings().data_dir, project.id), ops)
        await _autosave(db, user, project, f"Uploaded {len(files)} file(s)", force=True)
        return {"ok": True, "files": len(files), "scanned": scan["scanned"], "unpacked": unpacked, "warnings": zip_problems}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------------------- editor
async def _autosave(db: AsyncSession, user: User, project: Project, label: str, *, force: bool = False) -> str | None:
    """Timestamped version after a manual change (at most one per minute unless forced)."""
    now = time.time()
    if user.versioning_enabled and (force or now - _last_autosave.get(project.id, 0) > 60):
        _last_autosave[project.id] = now
        snap = await snapshots.create(db, project, kind="autosave", label=label[:200], created_by="user")
        return str(snap.id)
    await snapshots.refresh_index(db, project)
    await db.commit()
    return None


@router.get("/projects/{project_id}/tree")
async def tree(project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    root = project_root(project.owner_id, project.slug)
    if not root.is_dir():
        return {"children": [], "missing": True}
    return await asyncio.to_thread(build_tree, root, max_entries=limits.MAX_TREE_ENTRIES)


@router.get("/projects/{project_id}/folders")
async def folders(project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    return {"folders": await asyncio.to_thread(list_dirs, project_root(project.owner_id, project.slug))}


@router.get("/projects/{project_id}/file")
async def read_file(project_id: str, path: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    target = resolve_in(project_root(project.owner_id, project.slug), path)
    if not target.is_file():
        raise HTTPException(404, "That file no longer exists (it may have been deleted outside Elvin).")
    size = target.stat().st_size
    if size > limits.MAX_EDITOR_FILE_BYTES:
        raise HTTPException(413, f"Files over {limits.MAX_EDITOR_FILE_BYTES // (1024 * 1024)} MB can't be opened in the editor.")
    data = await asyncio.to_thread(target.read_bytes)
    rel = normalize_rel(path)
    if not is_text(data):
        return {"path": rel, "binary": True, "size": size, "sha256": sha256_bytes(data)}
    return {"path": rel, "binary": False, "content": data.decode("utf-8", errors="replace"), "size": size,
            "sha256": sha256_bytes(data), "language": language_for(rel)}


class FileSave(BaseModel):
    path: str = Field(max_length=1000)
    content: str = Field(max_length=limits.MAX_EDITOR_FILE_BYTES)
    base_sha256: str | None = Field(default=None, max_length=64)


@router.put("/projects/{project_id}/file")
async def save_file(project_id: str, body: FileSave, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    root = project_root(project.owner_id, project.slug)
    rel = normalize_rel(body.path)
    target = resolve_in(root, rel, for_write=True)
    data = body.content.encode("utf-8")
    if target.is_file() and body.base_sha256:
        current = sha256_bytes(await asyncio.to_thread(target.read_bytes))
        if current != body.base_sha256:
            raise HTTPException(409, "This file changed since you opened it (maybe Elvin or another program edited it). Reload it first.")
    await asyncio.to_thread(journal.apply_ops, root, journal.journal_dir_for(get_settings().data_dir, project.id),
                            [journal.Op("write", rel, data)])
    snap = await _autosave(db, user, project, f"Edited {rel}")
    return {"ok": True, "sha256": sha256_bytes(data), "snapshot_id": snap, "saved_at": utcnow().isoformat()}


class FsOp(BaseModel):
    op: str = Field(pattern="^(new_file|new_folder|rename|delete)$")
    path: str = Field(max_length=1000)
    new_path: str | None = Field(default=None, max_length=1000)
    password: str | None = Field(default=None, max_length=200)


@router.post("/projects/{project_id}/fs")
async def fs_op(project_id: str, body: FsOp, request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    root = project_root(project.owner_id, project.slug)
    rel = normalize_rel(body.path)
    target = resolve_in(root, rel, for_write=True)
    if body.op == "delete":
        if not target.exists():
            raise HTTPException(404, "Not found.")
        await confirm_password(db, request, user, body.password, action=f"delete {rel}")
    jroot = journal.journal_dir_for(get_settings().data_dir, project.id)
    if body.op == "new_file":
        if target.exists():
            raise HTTPException(409, "A file or folder with that name already exists.")
        ops = [journal.Op("write", rel, b"")]
    elif body.op == "new_folder":
        if target.exists():
            raise HTTPException(409, "A file or folder with that name already exists.")
        ops = [journal.Op("mkdir", rel)]
    elif body.op == "delete":
        if target.is_dir():
            if any(target.iterdir()):
                raise HTTPException(400, "Only empty folders can be deleted here.")
            await asyncio.to_thread(target.rmdir)
            ops = []
        elif target.is_file():
            ops = [journal.Op("delete", rel)]
        else:
            raise HTTPException(404, "Not found.")
    else:
        new_rel = normalize_rel(body.new_path or "")
        new_target = resolve_in(root, new_rel, for_write=True)
        if not target.is_file():
            raise HTTPException(400, "Only files can be renamed.")
        if new_target.exists():
            raise HTTPException(409, "Something with the new name already exists.")
        ops = [journal.Op("write", new_rel, await asyncio.to_thread(target.read_bytes)), journal.Op("delete", rel)]
    if ops:
        await asyncio.to_thread(journal.apply_ops, root, jroot, ops)
    await _autosave(db, user, project, f"{body.op.replace('_', ' ')}: {rel}", force=True)
    return {"ok": True}


class CompleteIn(BaseModel):
    path: str = Field(max_length=1000)
    prefix: str = Field(max_length=20_000)
    suffix: str = Field(default="", max_length=8_000)


@router.post("/projects/{project_id}/complete")
async def autocomplete(project_id: str, body: CompleteIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    await db.close()
    ok, _ = await ratelimit.hit(f"complete:{user.id}", limits.AUTOCOMPLETE_PER_MINUTE, 60)
    if not ok:
        return {"completion": "", "provider": None, "throttled": True}
    token = usage.set_context(user_id=user.id, project_id=project.id, feature="editor", effort="low")
    try:
        text, label = await asyncio.wait_for(ai.fim(body.prefix[-6000:], body.suffix[:2000], max_tokens=96), timeout=20)
    except (ai.AIError, asyncio.TimeoutError) as exc:
        return {"completion": "", "provider": None, "error": str(exc) or "Autocomplete timed out."}
    finally:
        usage.reset_context(token)
    lines = text.split("\n")
    return {"completion": "\n".join(lines[:8]).rstrip() if len(lines) > 1 else text.rstrip("\n"), "provider": label}


# --------------------------------------------------------------------------- integrity
@router.get("/projects/{project_id}/integrity")
async def check_integrity(project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    return await integrity.check(db, project)


@router.post("/projects/{project_id}/integrity/accept")
async def accept_changes(project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    return await integrity.accept(db, project)


class RestorePaths(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=500)


@router.post("/projects/{project_id}/integrity/restore")
async def restore_deleted(project_id: str, body: RestorePaths, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    if project.status == "missing":
        latest = await snapshots.latest(db, project.id)
        if latest is None:
            raise HTTPException(404, "There is no saved version to restore from.")
        res = await snapshots.restore(db, project, latest, label="Restored missing project folder")
        project.status = "ok"
        await db.commit()
        return {"restored": list(latest.manifest)[:50], "result": res}
    return await integrity.restore_paths(db, project, [normalize_rel(p) for p in body.paths])


# --------------------------------------------------------------------------- history
def snapshot_json(s: Snapshot) -> dict:
    return {"id": str(s.id), "created_at": s.created_at.isoformat(), "kind": s.kind, "label": s.label, "created_by": s.created_by,
            "file_count": s.file_count, "total_bytes": s.total_bytes, "chain_hash": s.chain_hash, "hidden": s.hidden,
            "verified_at": s.verified_at.isoformat() if s.verified_at else None, "verify_ok": s.verify_ok,
            "task_id": str(s.task_id) if s.task_id else None}


@router.get("/projects/{project_id}/snapshots")
async def list_snapshots(project_id: str, limit: int = 100, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    rows = (await db.execute(select(Snapshot).where(Snapshot.project_id == project.id, Snapshot.hidden.is_(False))
                             .order_by(Snapshot.created_at.desc()).limit(max(1, min(limit, 300))))).scalars().all()
    return {"snapshots": [snapshot_json(s) for s in rows], "versioning_enabled": user.versioning_enabled}


class SnapIn(BaseModel):
    label: str = Field(default="Manual save", max_length=200)


@router.post("/projects/{project_id}/snapshots")
async def manual_snapshot(project_id: str, body: SnapIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    snap = await snapshots.create(db, project, kind="manual", label=body.label or "Manual save", created_by="user")
    report = await snapshots.verify(db, snap)
    return {**snapshot_json(snap), "verify": report}


async def _owned_snapshot(db: AsyncSession, user: User, snapshot_id: str) -> tuple[Snapshot, Project]:
    snap = await db.get(Snapshot, parse_uuid(snapshot_id))
    if snap is None:
        raise HTTPException(404, "Version not found.")
    project = await owned_project(db, user, snap.project_id)
    return snap, project


@router.post("/snapshots/{snapshot_id}/verify")
async def verify_snapshot(snapshot_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    snap, _ = await _owned_snapshot(db, user, snapshot_id)
    return await snapshots.verify(db, snap)


class RestoreIn(BaseModel):
    password: str | None = Field(default=None, max_length=200)


@router.post("/snapshots/{snapshot_id}/restore")
async def restore_snapshot(snapshot_id: str, request: Request, body: RestoreIn | None = None, user: User = Depends(require_user),
                           db: AsyncSession = Depends(get_db)):
    snap, project = await _owned_snapshot(db, user, snapshot_id)
    for r in tasks.manager.user_tasks(user.id):
        if r.project_id == project.id and r.kind in ("code", "design"):
            raise HTTPException(409, "Stop Elvin's running task in this project before restoring a version.")
    removed = await asyncio.to_thread(snapshots.files_removed_by_restore, project_root(project.owner_id, project.slug), snap.manifest)
    if removed:   # restoring would delete files that are not in that version
        listed = ", ".join(removed[:5]) + (f" and {len(removed) - 5} more" if len(removed) > 5 else "")
        await confirm_password(db, request, user, body.password if body else None,
                               action=f"restore this version - it deletes {len(removed)} file(s) added since then ({listed})")
    if user.versioning_enabled:
        await snapshots.create(db, project, kind="manual", label="Before restoring an older version", created_by="system")
    return await snapshots.restore(db, project, snap)


@router.get("/snapshots/{snapshot_id}/changes")
async def snapshot_changes(snapshot_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    snap, project = await _owned_snapshot(db, user, snapshot_id)
    prev = (await db.execute(select(Snapshot).where(Snapshot.project_id == project.id, Snapshot.created_at < snap.created_at)
                             .order_by(Snapshot.created_at.desc()).limit(1))).scalar_one_or_none()
    changes = snapshots.compare_manifests(prev.manifest if prev else {}, snap.manifest)
    return {"previous_id": str(prev.id) if prev else None, **{k: v[:300] for k, v in changes.items()}}


@router.get("/snapshots/{snapshot_id}/diff")
async def snapshot_diff(snapshot_id: str, path: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    snap, project = await _owned_snapshot(db, user, snapshot_id)
    rel = normalize_rel(path)
    prev = (await db.execute(select(Snapshot).where(Snapshot.project_id == project.id, Snapshot.created_at < snap.created_at)
                             .order_by(Snapshot.created_at.desc()).limit(1))).scalar_one_or_none()
    a = (prev.manifest.get(rel) or [None])[0] if prev else None
    b = (snap.manifest.get(rel) or [None])[0]
    try:
        diff = await asyncio.to_thread(snapshots.file_diff, project.id, a, b, rel)
    except OSError as exc:
        raise HTTPException(410, "The stored content of this version is missing.") from exc
    return {"path": rel, "diff": diff[:200_000]}


# --------------------------------------------------------------------------- image preview
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
               ".bmp": "image/bmp", ".ico": "image/x-icon", ".avif": "image/avif", ".svg": "image/svg+xml"}


@router.get("/projects/{project_id}/raw")
async def raw_image(project_id: str, path: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """Serve an image from the project for the editor's preview. Images only, and
    with a sandboxing CSP so an SVG opened directly can never run scripts."""
    project = await owned_project(db, user, project_id)
    target = resolve_in(project_root(project.owner_id, project.slug), path)
    ctype = IMAGE_TYPES.get(target.suffix.lower())
    if ctype is None:
        raise HTTPException(415, "Only images can be previewed.")
    if not target.is_file():
        raise HTTPException(404, "That image no longer exists.")
    if target.stat().st_size > limits.MAX_PREVIEW_IMAGE_BYTES:
        raise HTTPException(413, "Image too large to preview.")
    return FileResponse(target, media_type=ctype, headers={
        "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox",
        "Content-Disposition": "inline", "Cache-Control": "private, no-cache"})


# --------------------------------------------------------------------------- run code yourself
class RunIn(BaseModel):
    command: str = Field(min_length=1, max_length=500)
    source: str = Field(default="editor", pattern="^(editor|elvin)$")
    force: bool = False


def run_json(r: CommandRun) -> dict:
    return {"id": r.id, "command": r.command, "exit_code": r.exit_code, "timed_out": r.timed_out, "duration_ms": r.duration_ms,
            "output": r.output, "source": r.source, "created_at": r.created_at.isoformat(), "ok": r.exit_code == 0 and not r.timed_out}


@router.post("/projects/{project_id}/run")
async def run_command(project_id: str, body: RunIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """The user runs a command in the isolated sandbox (no approval needed - they typed it)."""
    from ..services.agent import BLOCKED_COMMANDS

    project = await owned_project(db, user, project_id)
    await ratelimit.enforce(f"userrun:{user.id}", limits.USER_RUNS_PER_MINUTE, 60, "Too many runs.")
    command = body.command.strip()
    for rx, why in BLOCKED_COMMANDS:
        if rx.search(command):
            raise HTTPException(400, f"This command is not allowed in the sandbox: {why}.")
    if not body.force:
        blocked = []
        token = usage.set_context(user_id=user.id, project_id=project.id, feature="vulns", effort="low", model_prefs=user.model_prefs or {})
        try:
            for eco, name, ver in vulns.packages_in_command(command):
                v = await vulns.check_library(db, ecosystem=eco, name=name, version=ver, project_id=project.id, team=user.ai_team,
                                              with_alternatives=False)
                if v["verdict"] == "blocked":
                    blocked.append({"name": name, "ecosystem": eco, "reason": v["reason"]})
        finally:
            usage.reset_context(token)
        if blocked:
            raise HTTPException(409, {"message": "This command installs packages the vulnerability detector blocked.", "blocked": blocked})
    root = project_root(project.owner_id, project.slug)
    await db.close()
    try:
        res = await runner_client.run(root, command, timeout=limits.RUN_COMMAND_TIMEOUT_S)
    except runner_client.RunnerError as exc:
        raise HTTPException(503, str(exc)) from exc
    output = ((res.get("stdout") or "") + ("\n" if res.get("stderr") else "") + (res.get("stderr") or ""))[-limits.RUN_OUTPUT_BYTES:]
    async with dbm_session() as s2:
        row = CommandRun(project_id=project.id, user_id=user.id, source=body.source, command=command[:500],
                         exit_code=res.get("exit_code"), timed_out=bool(res.get("timed_out")),
                         duration_ms=int(res.get("duration_ms") or 0), output=output)
        s2.add(row)
        if body.source == "elvin":
            s2.add(Message(project_id=project.id, user_id=user.id, role="system", content=f"$ {command}",
                           meta={"kind": "run", "exit_code": row.exit_code, "timed_out": row.timed_out, "duration_ms": row.duration_ms,
                                 "output": output[-8000:]}))
        await s2.commit()
        old = (await s2.execute(select(CommandRun.id).where(CommandRun.project_id == project.id)
                                .order_by(CommandRun.id.desc()).offset(limits.USER_RUNS_KEPT))).scalars().all()
        if old:
            await s2.execute(delete(CommandRun).where(CommandRun.id.in_(old)))
            await s2.commit()
        return run_json(row)


@router.get("/projects/{project_id}/runs")
async def list_runs(project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    rows = (await db.execute(select(CommandRun).where(CommandRun.project_id == project.id).order_by(CommandRun.id.desc()).limit(20))).scalars().all()
    return {"runs": [run_json(r) for r in rows]}


class RenderIn(BaseModel):
    path: str = Field(max_length=1000)


@router.post("/projects/{project_id}/render")
async def render_page(project_id: str, body: RenderIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """Render an HTML file in the sandbox's network-blocked Chromium and return a screenshot."""
    from ..services.agent import save_image

    project = await owned_project(db, user, project_id)
    await ratelimit.enforce(f"userrun:{user.id}", limits.USER_RUNS_PER_MINUTE, 60, "Too many runs.")
    rel = normalize_rel(body.path)
    root = project_root(project.owner_id, project.slug)
    if not resolve_in(root, rel).is_file() or not rel.lower().endswith((".html", ".htm")):
        raise HTTPException(400, "Choose an .html file to render.")
    await db.close()
    try:
        png = await runner_client.screenshot(root, rel)
    except runner_client.RunnerError as exc:
        raise HTTPException(503, str(exc)) from exc
    att = await save_image(user.id, project.id, png, kind="screenshot", filename=f"render-{rel.replace('/', '_')}.png")
    return {"url": f"/api/attachments/{att}", "path": rel}
