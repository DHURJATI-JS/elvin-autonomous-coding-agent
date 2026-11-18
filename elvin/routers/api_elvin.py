"""Elvin chat: send (with spam / double-send protection), stop, retry,
permission decisions, image attachments and project memory."""
from __future__ import annotations

import asyncio
import hashlib
import io
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits, redisx
from ..db import get_db
from ..models import ApprovalBatch, Attachment, Memory, Message, ScanResult, Task, TaskEvent, User, utcnow
from ..security import ratelimit
from ..security.auth import confirm_password, require_user
from ..services import agent, clamav, memory, tasks
from ..services.ai import router as ai
from ..web import owned_project, parse_uuid

router = APIRouter(prefix="/api")
AGENT_KINDS = {"code", "design"}


def task_json(t: Task) -> dict:
    return {"id": str(t.id), "kind": t.kind, "status": t.status, "title": t.title, "prompt": t.prompt, "error": t.error,
            "created_at": t.created_at.isoformat(), "finished_at": t.finished_at.isoformat() if t.finished_at else None,
            "retry_of": str(t.retry_of) if t.retry_of else None, "team": t.team, "params": {"attachments": (t.params or {}).get("attachments", [])},
            "result": t.result or {}}


@router.get("/projects/{project_id}/conversation")
async def conversation(project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    msgs = list(reversed((await db.execute(select(Message).where(Message.project_id == project.id)
                                           .order_by(Message.id.desc()).limit(80))).scalars().all()))
    task_ids = {m.task_id for m in msgs if m.task_id}
    recent = (await db.execute(select(Task).where(Task.project_id == project.id, Task.kind.in_(AGENT_KINDS))
                               .order_by(Task.created_at.desc()).limit(25))).scalars().all()
    task_ids |= {t.id for t in recent}
    task_rows = (await db.execute(select(Task).where(Task.id.in_(task_ids)))).scalars().all() if task_ids else []
    events = (await db.execute(select(TaskEvent).where(TaskEvent.task_id.in_(task_ids)).order_by(TaskEvent.id)
                               .limit(4000))).scalars().all() if task_ids else []
    by_task: dict[str, list] = {}
    for ev in events:
        by_task.setdefault(str(ev.task_id), []).append({"id": ev.id, "type": ev.type, "data": ev.data, "ts": ev.ts.isoformat()})
    pending = (await db.execute(select(ApprovalBatch).where(ApprovalBatch.project_id == project.id, ApprovalBatch.user_id == user.id,
                                                            ApprovalBatch.status == "pending"))).scalars().all()
    active = next((t for t in task_rows if t.status in tasks.ACTIVE and t.kind in AGENT_KINDS), None)
    mems = await memory.list_for(db, project.id)
    return {
        "messages": [{"id": m.id, "role": m.role, "content": m.content, "task_id": str(m.task_id) if m.task_id else None,
                      "meta": m.meta, "created_at": m.created_at.isoformat()} for m in msgs],
        "tasks": {str(t.id): {**task_json(t), "events": by_task.get(str(t.id), [])} for t in task_rows},
        "active_task": str(active.id) if active else None,
        "pending_approvals": [{"batch_id": str(b.id), "task_id": str(b.task_id), "title": b.title, "step": b.step_no,
                               "actions": b.actions, "live": agent.has_waiter(b.id)} for b in pending],
        "memory": [{"id": m.id, "content": m.content, "source": m.source, "created_at": m.created_at.isoformat()} for m in mems],
        "team": user.ai_team if ai.registry().team_available(user.ai_team) else ai.registry().default_team(),
        "teams": ai.registry().teams(), "effort": user.ai_effort, "versioning": user.versioning_enabled,
    }


class SendIn(BaseModel):
    content: str = Field(min_length=1, max_length=limits.MAX_PROMPT_CHARS)
    idempotency_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    attachments: list[str] = Field(default_factory=list, max_length=2)
    mode: str = Field(default="code", pattern="^(code|design)$")
    folder: str = Field(default="design", max_length=200)
    team: str | None = None


async def _check_spam(user: User, project_id: uuid.UUID, content: str) -> None:
    ok_m, retry_m = await ratelimit.hit(f"chat-min:{user.id}", limits.CHAT_PER_MINUTE, 60)
    ok_h, retry_h = await ratelimit.hit(f"chat-hour:{user.id}", limits.CHAT_PER_HOUR, 3600)
    if not ok_m or not ok_h:
        raise HTTPException(429, f"Slow down a little - you can send {limits.CHAT_PER_MINUTE} messages a minute and "
                                 f"{limits.CHAT_PER_HOUR} an hour. Try again in {min(retry_m if not ok_m else 9999, retry_h if not ok_h else 9999)}s.")
    norm = re.sub(r"\s+", " ", content.strip().lower())
    digest = hashlib.sha256(f"{project_id}:{norm}".encode()).hexdigest()
    first = await redisx.call("set", f"elvin:dup:{user.id}:{digest}", "1", nx=True, ex=limits.DUPLICATE_MESSAGE_WINDOW_S, default=True)
    if not first:
        raise HTTPException(409, "You just sent exactly this message. Wait a few seconds or change it.")


@router.post("/projects/{project_id}/messages", status_code=202)
async def send_message(project_id: str, body: SendIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "Write a message first.")
    # Double-send: the same idempotency key always maps to the same task.
    existing = (await db.execute(select(Task).where(Task.user_id == user.id, Task.idempotency_key == body.idempotency_key))).scalar_one_or_none()
    if existing:
        return {"task_id": str(existing.id), "status": existing.status, "duplicate": True}
    # Serialise sends per user so two tabs can't both start a task.
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    active_here = (await db.execute(select(Task.id).where(Task.project_id == project.id, Task.kind.in_(AGENT_KINDS),
                                                          Task.status.in_(tasks.ACTIVE)))).first()
    if active_here:
        await db.rollback()
        raise HTTPException(409, "Elvin is still working on your last request. Stop it first, or wait until it finishes.")
    running = (await db.execute(select(func.count()).select_from(Task).where(Task.user_id == user.id, Task.status.in_(tasks.ACTIVE)))).scalar_one()
    if running >= limits.MAX_CONCURRENT_TASKS_PER_USER:
        await db.rollback()
        raise HTTPException(429, f"You already have {running} tasks running. Wait for one to finish.")
    await _check_spam(user, project.id, content)
    att_ids: list[str] = []
    for a in body.attachments:
        att = await db.get(Attachment, parse_uuid(a))
        if att is None or att.user_id != user.id or att.project_id != project.id or att.kind != "image":
            raise HTTPException(400, "That image attachment is not available.")
        att_ids.append(str(att.id))
    mode = "design" if body.mode == "design" or att_ids else "code"
    if mode == "design" and not att_ids:
        raise HTTPException(400, "Attach an image of the design you want.")
    reg = ai.registry()
    team = body.team if body.team in ai.TEAMS and reg.team_available(body.team) else (
        user.ai_team if reg.team_available(user.ai_team) else reg.default_team())
    task = Task(user_id=user.id, project_id=project.id, kind=mode, status="queued", title=content[:120], prompt=content,
                params={"attachments": att_ids, "folder": body.folder}, team=team, idempotency_key=body.idempotency_key)
    db.add(task)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        existing = (await db.execute(select(Task).where(Task.user_id == user.id, Task.idempotency_key == body.idempotency_key))).scalar_one()
        return {"task_id": str(existing.id), "status": existing.status, "duplicate": True}
    db.add(Message(project_id=project.id, user_id=user.id, role="user", content=content, task_id=task.id,
                   meta={"attachments": att_ids, "mode": mode}))
    count = (await db.execute(select(func.count()).select_from(Message).where(Message.project_id == project.id))).scalar_one()
    if count > limits.MAX_MESSAGES_PER_PROJECT:
        old = select(Message.id).where(Message.project_id == project.id).order_by(Message.id).limit(count - limits.MAX_MESSAGES_PER_PROJECT)
        await db.execute(delete(Message).where(Message.id.in_(old.scalar_subquery())))
    await db.commit()
    await tasks.presence.heartbeat(user.id)
    await agent.start_task(task)
    return {"task_id": str(task.id), "status": "queued", "duplicate": False}


async def _owned_task(db: AsyncSession, user: User, task_id: str) -> Task:
    task = await db.get(Task, parse_uuid(task_id))
    if task is None or task.user_id != user.id:
        raise HTTPException(404, "Task not found.")
    return task


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    task = await _owned_task(db, user, task_id)
    await db.close()
    if tasks.manager.is_running(task.id):
        await tasks.manager.cancel(task.id, "Stopped by you.")
        return {"ok": True, "status": "cancelled"}
    if task.status in tasks.ACTIVE:
        await tasks.set_status(task.id, "cancelled", error="Stopped by you.")
    return {"ok": True, "status": "cancelled"}


@router.post("/tasks/{task_id}/retry", status_code=202)
async def retry_task(task_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    old = await _owned_task(db, user, task_id)
    if old.kind not in AGENT_KINDS or old.status not in ("failed", "cancelled", "interrupted"):
        raise HTTPException(400, "Only stopped, failed or interrupted Elvin tasks can be retried.")
    project = await owned_project(db, user, old.project_id)
    newest = (await db.execute(select(Task).where(Task.project_id == project.id, Task.kind.in_(AGENT_KINDS))
                               .order_by(Task.created_at.desc()).limit(1))).scalar_one()
    if newest.id != old.id:
        raise HTTPException(409, "Only the most recent task can be retried (newer work would be undone).")
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    if (await db.execute(select(Task.id).where(Task.project_id == project.id, Task.status.in_(tasks.ACTIVE), Task.kind.in_(AGENT_KINDS)))).first():
        raise HTTPException(409, "Elvin is already working on this project.")
    params = dict(old.params or {})
    if old.start_snapshot_id:
        params["restore_from"] = str(old.start_snapshot_id)  # begin again from where the original task began
    task = Task(user_id=user.id, project_id=project.id, kind=old.kind, status="queued", title=old.title, prompt=old.prompt,
                params=params, team=old.team, retry_of=old.id, idempotency_key=f"retry-{old.id}-{uuid.uuid4().hex[:8]}")
    db.add(task)
    await db.flush()
    db.add(Message(project_id=project.id, user_id=user.id, role="user", content=old.prompt, task_id=task.id,
                   meta={"retry_of": str(old.id), "attachments": params.get("attachments", [])}))
    await db.commit()
    await tasks.presence.heartbeat(user.id)
    await agent.start_task(task)
    return {"task_id": str(task.id), "status": "queued"}


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject|stop)$")
    approved: list[int] = Field(default_factory=list, max_length=limits.MAX_ACTIONS_PER_STEP)
    password: str | None = Field(default=None, max_length=200)


@router.post("/approvals/{batch_id}")
async def decide(batch_id: str, body: DecisionIn, request: Request, user: User = Depends(require_user),
                 db: AsyncSession = Depends(get_db)):
    batch = await db.get(ApprovalBatch, parse_uuid(batch_id))
    if batch is None or batch.user_id != user.id:
        raise HTTPException(404, "Permission request not found.")
    if batch.status != "pending" or not agent.has_waiter(batch.id):
        raise HTTPException(409, "This permission request is no longer active.")
    valid = {a["idx"] for a in batch.actions}
    approved = sorted(set(body.approved) & valid) if body.decision == "approve" else []
    if body.decision == "approve" and not approved:
        raise HTTPException(400, "Select at least one change to approve, or reject the step.")
    deletes = [a["path"] for a in batch.actions if a["idx"] in approved and a.get("type") == "delete"]
    if deletes:   # Elvin may propose deleting a file in this project; only the user's password lets it happen
        listed = ", ".join(deletes[:5]) + (f" and {len(deletes) - 5} more" if len(deletes) > 5 else "")
        await confirm_password(db, request, user, body.password, action=f"let Elvin delete {listed}")
    batch.status = {"approve": "approved" if len(approved) == len(valid) else "partially_approved",
                    "reject": "rejected", "stop": "rejected"}[body.decision]
    batch.decision = {"decision": body.decision, "approved": approved}
    batch.decided_at = utcnow()
    await db.commit()
    agent.resolve_decision(batch.id, {"decision": body.decision, "approved": approved})
    return {"ok": True, "status": batch.status}


@router.post("/projects/{project_id}/attachments")
async def upload_image(request: Request, project_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    await ratelimit.enforce(f"attach:{user.id}", 30, 3600)
    form = await request.form(max_files=1, max_fields=5)
    item = form.get("image")
    if item is None or isinstance(item, str):
        raise HTTPException(400, "Choose an image.")
    raw = await item.read()
    if len(raw) > limits.MAX_IMAGE_BYTES:
        raise HTTPException(413, f"Images can be at most {limits.MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
    try:
        verdict, sig = await clamav.scan_bytes(raw)
    except clamav.ClamUnavailable:
        verdict, sig = "unavailable", ""
    db.add(ScanResult(user_id=user.id, project_id=project.id, context="image", status=verdict, scanned=1,
                      infected=[{"path": item.filename, "signature": sig}] if verdict == "infected" else []))
    await db.commit()
    if verdict == "infected":
        raise HTTPException(422, f"ClamAV found malware in that image ({sig}).")
    if verdict == "unavailable" and clamav.must_block({"status": "unavailable", "infected": []}):
        raise HTTPException(503, clamav.must_block({"status": "unavailable", "infected": []}))

    def reencode(data: bytes) -> bytes:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > 40_000_000:
                raise ValueError("too many pixels")
            im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
            im.thumbnail((1600, 1600))
            out = io.BytesIO()
            im.save(out, format="PNG", optimize=True)  # re-encoding strips metadata and anything hidden in the file
            return out.getvalue()

    try:
        png = await asyncio.to_thread(reencode, raw)
    except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(400, "That file is not a supported image (PNG, JPEG, WebP or GIF).") from exc
    att_id = await agent.save_image(user.id, project.id, png, kind="image", filename=(item.filename or "design.png")[:200])
    return {"id": str(att_id), "url": f"/api/attachments/{att_id}"}


@router.get("/attachments/{attachment_id}")
async def get_attachment(attachment_id: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    att = await db.get(Attachment, parse_uuid(attachment_id))
    if att is None or att.user_id != user.id:
        raise HTTPException(404, "Not found.")
    path = agent.attachment_path(att.id)
    if not path.exists():
        raise HTTPException(410, "This image is no longer stored.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600",
                                                               "Content-Disposition": "inline"})


class MemoryIn(BaseModel):
    content: str = Field(min_length=3, max_length=limits.MAX_MEMORY_CHARS)


@router.post("/projects/{project_id}/memory")
async def add_memory(project_id: str, body: MemoryIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    mem = await memory.add(db, project.id, body.content, "user")
    if mem is None:
        raise HTTPException(409, "Elvin already remembers that.")
    return {"id": mem.id, "content": mem.content, "source": mem.source}


@router.delete("/projects/{project_id}/memory/{memory_id}")
async def forget(project_id: str, memory_id: int, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    project = await owned_project(db, user, project_id)
    res = await db.execute(delete(Memory).where(Memory.id == memory_id, Memory.project_id == project.id))
    await db.commit()
    if not res.rowcount:
        raise HTTPException(404, "Not found.")
    return {"ok": True}
