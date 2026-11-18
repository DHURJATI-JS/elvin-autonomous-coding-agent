"""Timestamped versions ("snapshots") of a project.

* File contents go to a content-addressed blob store on the user's disk
  (<data>/snapshots/<project>/blobs/ab/abcdef...).
* The manifest ({path: [sha256, size]}) is stored in PostgreSQL AND written as a
  local JSON copy (<data>/snapshots/<project>/manifests/<id>.json).
* Snapshots are hash-chained per project (chain = sha256(prev_chain | time |
  kind | manifest)), so Elvin can verify that a version was not altered and that
  every blob still matches its hash."""
from __future__ import annotations

import asyncio
import datetime as dt
import difflib
import json
import logging
import shutil
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits
from ..config import get_settings
from ..models import Project, ProjectFile, Snapshot, utcnow
from . import journal
from .fs import atomic_write_bytes, is_text, project_root, sha256_bytes, walk_files

log = logging.getLogger("elvin.snapshots")
_locks: dict[uuid.UUID, asyncio.Lock] = defaultdict(asyncio.Lock)


def project_lock(project_id: uuid.UUID) -> asyncio.Lock:
    return _locks[project_id]


def store_dir(project_id: Any) -> Path:
    return get_settings().data_dir / "snapshots" / str(project_id)


def blob_path(project_id: Any, sha: str) -> Path:
    return store_dir(project_id) / "blobs" / sha[:2] / sha


def canonical(manifest: dict) -> str:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def chain_hash(prev: str, created_at: dt.datetime, kind: str, manifest: dict) -> str:
    return sha256_bytes(f"{prev}|{created_at.isoformat()}|{kind}|{canonical(manifest)}".encode())


def _scan_and_store(root: Path, project_id: Any, index: dict[str, tuple[str, int, int]]) -> tuple[dict, dict, list[str]]:
    """Walk the project, hashing only files whose size/mtime changed; copy new
    contents into the blob store. Returns (manifest, new_index, skipped)."""
    manifest: dict[str, list] = {}
    new_index: dict[str, tuple[str, int, int]] = {}
    skipped: list[str] = []
    for rel, full, st in walk_files(root, max_files=limits.MAX_PROJECT_FILES_TRACKED):
        if st.st_size > limits.MAX_SNAPSHOT_FILE_BYTES:
            skipped.append(rel)
            continue
        cached = index.get(rel)
        sha = None
        if cached and cached[1] == st.st_size and cached[2] == st.st_mtime_ns and blob_path(project_id, cached[0]).exists():
            sha = cached[0]
        else:
            try:
                data = full.read_bytes()
            except OSError:
                continue
            sha = sha256_bytes(data)
            bp = blob_path(project_id, sha)
            if not bp.exists():
                atomic_write_bytes(bp, data)
        manifest[rel] = [sha, st.st_size]
        new_index[rel] = (sha, st.st_size, st.st_mtime_ns)
    return manifest, new_index, skipped


async def _load_index(db: AsyncSession, project_id: uuid.UUID) -> dict[str, tuple[str, int, int]]:
    rows = (await db.execute(select(ProjectFile.path, ProjectFile.sha256, ProjectFile.size, ProjectFile.mtime_ns)
                             .where(ProjectFile.project_id == project_id))).all()
    return {r.path: (r.sha256, r.size, r.mtime_ns) for r in rows}


async def save_index(db: AsyncSession, project_id: uuid.UUID, new_index: dict[str, tuple[str, int, int]]) -> None:
    old = await _load_index(db, project_id)
    removed = [p for p in old if p not in new_index]
    if removed:
        for i in range(0, len(removed), 500):
            await db.execute(delete(ProjectFile).where(ProjectFile.project_id == project_id, ProjectFile.path.in_(removed[i:i + 500])))
    changed = [p for p, v in new_index.items() if old.get(p) != v]
    if changed:
        existing = [p for p in changed if p in old]
        for i in range(0, len(existing), 500):
            await db.execute(delete(ProjectFile).where(ProjectFile.project_id == project_id, ProjectFile.path.in_(existing[i:i + 500])))
        db.add_all(ProjectFile(project_id=project_id, path=p, sha256=new_index[p][0], size=new_index[p][1], mtime_ns=new_index[p][2]) for p in changed)


async def refresh_index(db: AsyncSession, project: Project) -> dict:
    """Re-index the folder without creating a visible version."""
    root = project_root(project.owner_id, project.slug)
    index = await _load_index(db, project.id)
    manifest, new_index, _ = await asyncio.to_thread(_scan_and_store, root, project.id, index)
    await save_index(db, project.id, new_index)
    project.file_count = len(manifest)
    project.total_bytes = sum(v[1] for v in manifest.values())
    return manifest


async def create(
    db: AsyncSession,
    project: Project,
    *,
    kind: str,
    label: str = "",
    created_by: str = "system",
    task_id: uuid.UUID | None = None,
    hidden: bool = False,
) -> Snapshot:
    async with project_lock(project.id):
        root = project_root(project.owner_id, project.slug)
        index = await _load_index(db, project.id)
        manifest, new_index, skipped = await asyncio.to_thread(_scan_and_store, root, project.id, index)
        prev = (await db.execute(select(Snapshot).where(Snapshot.project_id == project.id)
                                 .order_by(Snapshot.created_at.desc()).limit(1))).scalar_one_or_none()
        created_at = utcnow()
        if prev is not None and prev.created_at >= created_at:
            created_at = prev.created_at + dt.timedelta(microseconds=1)
        prev_chain = prev.chain_hash if prev else ""
        snap = Snapshot(
            project_id=project.id, created_at=created_at, kind=kind, label=label[:200], created_by=created_by,
            task_id=task_id, manifest=manifest, file_count=len(manifest),
            total_bytes=sum(v[1] for v in manifest.values()), prev_chain=prev_chain,
            chain_hash=chain_hash(prev_chain, created_at, kind, manifest), hidden=hidden,
        )
        db.add(snap)
        await db.flush()
        local = {
            "id": str(snap.id), "project_id": str(project.id), "created_at": created_at.isoformat(), "kind": kind,
            "label": snap.label, "prev_chain": prev_chain, "chain_hash": snap.chain_hash, "manifest": manifest,
            "skipped_large_files": skipped[:200],
        }
        await asyncio.to_thread(
            atomic_write_bytes, store_dir(project.id) / "manifests" / f"{snap.id}.json", json.dumps(local).encode()
        )
        await save_index(db, project.id, new_index)
        project.file_count = len(manifest)
        project.total_bytes = snap.total_bytes
        project.updated_at = utcnow()
        await db.commit()
        await _prune(db, project)
        return snap


async def latest(db: AsyncSession, project_id: uuid.UUID, *, include_hidden: bool = True) -> Snapshot | None:
    q = select(Snapshot).where(Snapshot.project_id == project_id)
    if not include_hidden:
        q = q.where(Snapshot.hidden.is_(False))
    return (await db.execute(q.order_by(Snapshot.created_at.desc()).limit(1))).scalar_one_or_none()


def _verify_blobs(project_id: Any, manifest: dict) -> tuple[list[str], list[str]]:
    missing, corrupt = [], []
    seen: set[str] = set()
    for path, (sha, _size) in manifest.items():
        if sha in seen:
            continue
        seen.add(sha)
        bp = blob_path(project_id, sha)
        try:
            data = bp.read_bytes()
        except OSError:
            missing.append(path)
            continue
        if sha256_bytes(data) != sha:
            corrupt.append(path)
    return missing, corrupt


async def verify(db: AsyncSession, snap: Snapshot) -> dict[str, Any]:
    chain_ok = chain_hash(snap.prev_chain, snap.created_at, snap.kind, snap.manifest) == snap.chain_hash
    prev = (await db.execute(select(Snapshot).where(Snapshot.project_id == snap.project_id,
                                                    Snapshot.created_at < snap.created_at)
                             .order_by(Snapshot.created_at.desc()).limit(1))).scalar_one_or_none()
    link_ok = prev.chain_hash == snap.prev_chain if prev is not None else True  # predecessor may be pruned
    missing, corrupt = await asyncio.to_thread(_verify_blobs, snap.project_id, snap.manifest)
    local_ok = False
    try:
        raw = await asyncio.to_thread((store_dir(snap.project_id) / "manifests" / f"{snap.id}.json").read_text)
        local = json.loads(raw)
        local_ok = local.get("chain_hash") == snap.chain_hash and local.get("manifest") == snap.manifest
    except (OSError, ValueError):
        local_ok = False
    ok = chain_ok and link_ok and not missing and not corrupt and local_ok
    snap.verified_at = utcnow()
    snap.verify_ok = ok
    await db.commit()
    return {
        "ok": ok, "chain_ok": chain_ok, "link_ok": link_ok, "local_copy_ok": local_ok,
        "missing": missing[:50], "corrupt": corrupt[:50], "files": len(snap.manifest),
        "verified_at": snap.verified_at.isoformat(),
    }


def files_removed_by_restore(root: Path, manifest: dict) -> list[str]:
    """Files a full restore of `manifest` would delete (they were added after that version)."""
    if not root.is_dir():
        return []
    return sorted(rel for rel, _, st in walk_files(root, max_files=limits.MAX_PROJECT_FILES_TRACKED)
                  if rel not in manifest and st.st_size <= limits.MAX_SNAPSHOT_FILE_BYTES)


def _plan_restore(root: Path, project_id: Any, manifest: dict, only: set[str] | None) -> list[journal.Op]:
    ops: list[journal.Op] = []
    current = {rel: st for rel, _, st in walk_files(root, max_files=limits.MAX_PROJECT_FILES_TRACKED)}
    for rel, (sha, _size) in manifest.items():
        if only is not None and rel not in only:
            continue
        full = root / rel
        if rel in current:
            try:
                if sha256_bytes(full.read_bytes()) == sha:
                    continue
            except OSError:
                pass
        ops.append(journal.Op("write", rel, blob_path(project_id, sha).read_bytes()))
    if only is None:
        for rel, st in current.items():
            if rel not in manifest and st.st_size <= limits.MAX_SNAPSHOT_FILE_BYTES:
                ops.append(journal.Op("delete", rel))
    return ops


async def restore(db: AsyncSession, project: Project, snap: Snapshot, *, only: set[str] | None = None,
                  label: str | None = None, created_by: str = "user", hidden: bool = False) -> dict[str, Any]:
    """Bring the project folder back to `snap` (optionally only some paths).
    Applied through the journal, then recorded as a new 'restore' version."""
    root = project_root(project.owner_id, project.slug)
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    async with project_lock(project.id):
        missing, corrupt = await asyncio.to_thread(_verify_blobs, project.id,
                                                   {k: v for k, v in snap.manifest.items() if only is None or k in only})
        if missing or corrupt:
            return {"ok": False, "error": "Some stored file versions are missing or damaged.", "missing": missing[:20], "corrupt": corrupt[:20]}
        ops = await asyncio.to_thread(_plan_restore, root, project.id, snap.manifest, only)
        jroot = journal.journal_dir_for(get_settings().data_dir, project.id)
        await asyncio.to_thread(journal.apply_ops, root, jroot, ops)
    when = snap.created_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    new = await create(db, project, kind="restore", label=label or f"Restored version from {when}", created_by=created_by, hidden=hidden)
    return {"ok": True, "changed": len(ops), "snapshot_id": str(new.id)}


def file_diff(project_id: Any, a_sha: str | None, b_sha: str | None, path: str, max_lines: int = 2000) -> str:
    def load(sha: str | None) -> list[str]:
        if not sha:
            return []
        data = blob_path(project_id, sha).read_bytes()
        if not is_text(data):
            return ["<binary file>\n"]
        return data.decode("utf-8", errors="replace").splitlines(keepends=True)[:max_lines]

    return "".join(difflib.unified_diff(load(a_sha), load(b_sha), f"a/{path}", f"b/{path}", n=3))


def compare_manifests(old: dict, new: dict) -> dict[str, list[str]]:
    added = sorted(p for p in new if p not in old)
    removed = sorted(p for p in old if p not in new)
    modified = sorted(p for p in new if p in old and new[p][0] != old[p][0])
    return {"added": added, "removed": removed, "modified": modified}


async def _prune(db: AsyncSession, project: Project) -> None:
    count = (await db.execute(select(func.count()).select_from(Snapshot).where(Snapshot.project_id == project.id))).scalar_one()
    excess = count - limits.MAX_SNAPSHOTS_PER_PROJECT
    if excess <= 0:
        return
    old_ids = (await db.execute(select(Snapshot.id).where(Snapshot.project_id == project.id)
                                .order_by(Snapshot.created_at.asc()).limit(excess))).scalars().all()
    await db.execute(delete(Snapshot).where(Snapshot.id.in_(old_ids)))
    await db.commit()
    for sid in old_ids:
        try:
            (store_dir(project.id) / "manifests" / f"{sid}.json").unlink()
        except OSError:
            pass
    manifests = (await db.execute(select(Snapshot.manifest).where(Snapshot.project_id == project.id))).scalars().all()
    keep = {v[0] for m in manifests for v in m.values()}
    await asyncio.to_thread(_gc_blobs, project.id, keep)


def _gc_blobs(project_id: Any, keep: set[str]) -> None:
    blobs = store_dir(project_id) / "blobs"
    if not blobs.is_dir():
        return
    for sub in blobs.iterdir():
        for blob in sub.iterdir() if sub.is_dir() else []:
            if blob.name not in keep:
                try:
                    blob.unlink()
                except OSError:
                    pass


def remove_store(project_id: Any) -> None:
    shutil.rmtree(store_dir(project_id), ignore_errors=True)
