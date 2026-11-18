"""File-integrity checks. Run whenever a project is opened and at startup, so
edits or deletions made while Elvin was closed are detected (and can be
restored or accepted) instead of breaking anything."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits
from ..models import Project, ProjectFile, Snapshot, utcnow
from . import snapshots
from .fs import project_root, sha256_file, walk_files

log = logging.getLogger("elvin.integrity")


def _compare(root: Path, index: dict[str, tuple[str, int, int]]) -> dict[str, Any]:
    current: dict[str, Any] = {}
    for rel, full, st in walk_files(root, max_files=limits.MAX_PROJECT_FILES_TRACKED):
        if st.st_size > limits.MAX_SNAPSHOT_FILE_BYTES:
            continue
        current[rel] = (full, st)
    deleted = sorted(p for p in index if p not in current)
    added = sorted(p for p in current if p not in index)
    modified: list[str] = []
    for rel, (full, st) in current.items():
        known = index.get(rel)
        if known is None or (known[1] == st.st_size and known[2] == st.st_mtime_ns):
            continue
        try:
            if sha256_file(full) != known[0]:
                modified.append(rel)
        except OSError:
            deleted.append(rel)
    return {"deleted": deleted, "added": added, "modified": sorted(modified), "tracked": len(current)}


async def check(db: AsyncSession, project: Project) -> dict[str, Any]:
    root = project_root(project.owner_id, project.slug)
    now = utcnow()
    if not await asyncio.to_thread(root.is_dir):
        project.status = "missing"
        report = {"ok": False, "missing_root": True, "checked_at": now.isoformat(), "deleted": [], "added": [], "modified": []}
        project.integrity = report
        project.integrity_checked_at = now
        await db.commit()
        return report
    rows = (await db.execute(select(ProjectFile.path, ProjectFile.sha256, ProjectFile.size, ProjectFile.mtime_ns)
                             .where(ProjectFile.project_id == project.id))).all()
    index = {r.path: (r.sha256, r.size, r.mtime_ns) for r in rows}
    diff = await asyncio.to_thread(_compare, root, index)
    report = {
        "ok": not (diff["deleted"] or diff["modified"] or diff["added"]),
        "missing_root": False,
        "checked_at": now.isoformat(),
        "counts": {k: len(diff[k]) for k in ("deleted", "added", "modified")},
        "deleted": diff["deleted"][:200],
        "added": diff["added"][:200],
        "modified": diff["modified"][:200],
        "tracked": diff["tracked"],
    }
    project.status = "ok"
    project.integrity = report
    project.integrity_checked_at = now
    await db.commit()
    return report


async def accept(db: AsyncSession, project: Project, created_by: str = "user") -> dict[str, Any]:
    """Accept outside changes as the new baseline (recorded as a version)."""
    snap = await snapshots.create(db, project, kind="external", label="Changes made outside Elvin (accepted)", created_by=created_by)
    report = await check(db, project)
    return {"snapshot_id": str(snap.id), "report": report}


async def restore_paths(db: AsyncSession, project: Project, paths: list[str]) -> dict[str, Any]:
    """Restore deleted/changed files from the newest version that has them."""
    wanted = set(paths[:500])
    snaps = (await db.execute(select(Snapshot).where(Snapshot.project_id == project.id)
                              .order_by(Snapshot.created_at.desc()).limit(50))).scalars().all()
    restored: list[str] = []
    for snap in snaps:
        have = {p for p in wanted if p in snap.manifest}
        if not have:
            continue
        res = await snapshots.restore(db, project, snap, only=have, label="Restored files changed/deleted outside Elvin", created_by="user")
        if res.get("ok"):
            restored.extend(sorted(have))
            wanted -= have
        if not wanted:
            break
    report = await check(db, project)
    return {"restored": restored, "not_found": sorted(wanted), "report": report}


async def check_all(owner_projects: list[Project], db: AsyncSession) -> None:
    for project in owner_projects:
        try:
            await check(db, project)
        except Exception as exc:  # pragma: no cover - never block startup
            log.warning("integrity check failed for %s: %s", project.id, exc)
