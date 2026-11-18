"""Background task manager, live event bus and presence tracking.

* Tasks run as asyncio tasks inside the single app process; their events are
  stored in PostgreSQL (so a reconnecting tab can replay them) and pushed to
  every open tab of that user over Server-Sent Events.
* Presence: each open Elvin tab sends a heartbeat every 15 s and a beacon when
  it closes. When a user has no open tab (closed it, or logged out) for 90 s,
  their AI tasks are cancelled automatically. Signing in on another device does
  NOT cancel them - the new device keeps the heartbeat going."""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update

from .. import limits, redisx
from .. import db as dbm
from ..models import ApprovalBatch, Task, TaskEvent, utcnow

log = logging.getLogger("elvin.tasks")

ACTIVE = ("queued", "running", "awaiting_approval")
AI_KINDS = {"code", "design", "bughunt"}


# --------------------------------------------------------------------------- event bus
class EventBus:
    def __init__(self) -> None:
        self.subs: dict[uuid.UUID, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, user_id: uuid.UUID) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subs[user_id].add(q)
        return q

    def unsubscribe(self, user_id: uuid.UUID, q: asyncio.Queue) -> None:
        self.subs[user_id].discard(q)
        if not self.subs[user_id]:
            self.subs.pop(user_id, None)

    def publish(self, user_id: uuid.UUID, event: dict[str, Any]) -> None:
        for q in list(self.subs.get(user_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # a stuck tab must not block Elvin
                pass

    def online(self, user_id: uuid.UUID) -> int:
        return len(self.subs.get(user_id, ()))


bus = EventBus()


async def emit(task_id: uuid.UUID, user_id: uuid.UUID, project_id: uuid.UUID | None, type_: str,
               data: dict[str, Any] | None = None, *, persist: bool = True) -> int | None:
    data = data or {}
    event_id = None
    ts = utcnow()
    if persist:
        async with dbm.session() as s:
            ev = TaskEvent(task_id=task_id, user_id=user_id, project_id=project_id, ts=ts, type=type_, data=data)
            s.add(ev)
            await s.commit()
            event_id = ev.id
    bus.publish(user_id, {"id": event_id, "task_id": str(task_id), "project_id": str(project_id) if project_id else None,
                          "type": type_, "data": data, "ts": ts.isoformat()})
    return event_id


def notify(user_id: uuid.UUID, type_: str, data: dict[str, Any]) -> None:
    """Ephemeral, user-level notification (not stored)."""
    bus.publish(user_id, {"id": None, "task_id": None, "project_id": data.get("project_id"), "type": type_,
                          "data": data, "ts": utcnow().isoformat()})


async def set_status(task_id: uuid.UUID, status: str, **fields: Any) -> None:
    values: dict[str, Any] = {"status": status, **fields}
    if status == "running" and "started_at" not in fields:
        values["started_at"] = utcnow()
    if status in ("completed", "failed", "cancelled", "interrupted") and "finished_at" not in fields:
        values["finished_at"] = utcnow()
    async with dbm.session() as s:
        await s.execute(update(Task).where(Task.id == task_id).values(**values))
        await s.commit()


# --------------------------------------------------------------------------- task manager
@dataclass
class Running:
    task_id: uuid.UUID
    user_id: uuid.UUID
    project_id: uuid.UUID | None
    kind: str
    started: float
    handle: asyncio.Task | None = None
    cancel_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class TaskManager:
    def __init__(self) -> None:
        self.running: dict[uuid.UUID, Running] = {}
        self._reaper: asyncio.Task | None = None

    def launch(self, task: Task, coro: Coroutine[Any, Any, Any]) -> None:
        info = Running(task.id, task.user_id, task.project_id, task.kind, time.time())
        self.running[task.id] = info

        async def wrapper() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - engine reports its own errors
                log.exception("task %s crashed", task.id)
                await set_status(task.id, "failed", error="Elvin hit an unexpected error. Your files were left unchanged.")
            finally:
                self.running.pop(task.id, None)

        info.handle = asyncio.create_task(wrapper(), name=f"task-{task.id}")

    def is_running(self, task_id: uuid.UUID) -> bool:
        return task_id in self.running

    def reason(self, task_id: uuid.UUID) -> str:
        info = self.running.get(task_id)
        return info.cancel_reason if info else ""

    async def cancel(self, task_id: uuid.UUID, reason: str) -> bool:
        info = self.running.get(task_id)
        if info is None or info.handle is None:
            return False
        info.cancel_reason = reason
        info.handle.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(info.handle), timeout=60)
        return True

    def user_tasks(self, user_id: uuid.UUID, kinds: set[str] | None = None) -> list[Running]:
        return [r for r in self.running.values() if r.user_id == user_id and (kinds is None or r.kind in kinds)]

    async def cancel_user(self, user_id: uuid.UUID, reason: str, kinds: set[str] | None = None) -> int:
        tasks = self.user_tasks(user_id, kinds)
        await asyncio.gather(*(self.cancel(r.task_id, reason) for r in tasks), return_exceptions=True)
        return len(tasks)

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        for info in list(self.running.values()):
            info.cancel_reason = "Elvin is shutting down"
            if info.handle:
                info.handle.cancel()
        handles = [i.handle for i in self.running.values() if i.handle]
        if handles:
            await asyncio.wait(handles, timeout=20)

    def start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop(), name="presence-reaper")

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.reap_once()
            except Exception:  # pragma: no cover
                log.exception("presence reaper failed")

    async def reap_once(self, now: float | None = None) -> list[uuid.UUID]:
        now = now or time.time()
        cancelled: list[uuid.UUID] = []
        users = {r.user_id for r in self.running.values() if r.kind in AI_KINDS}
        for uid in users:
            deadline = await presence.deadline(uid, default_since=min(r.started for r in self.user_tasks(uid, AI_KINDS)))
            if now >= deadline:
                n = await self.cancel_user(uid, "Cancelled automatically: no Elvin tab was open for 90 seconds.", AI_KINDS)
                if n:
                    log.info("auto-cancelled %d task(s) for absent user %s", n, uid)
                    cancelled.append(uid)
        return cancelled


manager = TaskManager()


# --------------------------------------------------------------------------- presence
class Presence:
    def __init__(self) -> None:
        self.last_seen: dict[uuid.UUID, float] = {}
        self.left_at: dict[uuid.UUID, float] = {}

    async def heartbeat(self, user_id: uuid.UUID) -> None:
        now = time.time()
        self.last_seen[user_id] = now
        self.left_at.pop(user_id, None)
        await redisx.call("set", f"elvin:presence:{user_id}", str(now), ex=3600)
        await redisx.call("delete", f"elvin:left:{user_id}")

    async def leave(self, user_id: uuid.UUID) -> None:
        now = time.time()
        self.left_at[user_id] = now
        await redisx.call("set", f"elvin:left:{user_id}", str(now), ex=3600)

    async def _get(self, key: str, local: dict[uuid.UUID, float], user_id: uuid.UUID) -> float | None:
        raw = await redisx.call("get", key, default=None)
        if raw is not None:
            with contextlib.suppress(ValueError):
                return float(raw)
        return local.get(user_id)

    async def deadline(self, user_id: uuid.UUID, *, default_since: float) -> float:
        """Moment at which this user's AI tasks should be cancelled."""
        last = await self._get(f"elvin:presence:{user_id}", self.last_seen, user_id)
        left = await self._get(f"elvin:left:{user_id}", self.left_at, user_id)
        if bus.online(user_id):  # an open SSE stream counts as presence
            return float("inf")
        if left is not None and (last is None or last <= left):
            return left + limits.PRESENCE_CANCEL_S
        if last is not None:
            return last + limits.PRESENCE_CANCEL_S + limits.HEARTBEAT_INTERVAL_S
        return default_since + limits.PRESENCE_CANCEL_S + limits.HEARTBEAT_INTERVAL_S


presence = Presence()


async def recover_on_startup() -> int:
    """Tasks that were running when Elvin stopped (crash, power cut) are marked
    interrupted; their half-written files were already rolled back by the journal."""
    async with dbm.session() as s:
        res = await s.execute(
            update(Task).where(Task.status.in_(ACTIVE)).values(
                status="interrupted", finished_at=utcnow(),
                error="Elvin stopped while this task was running (restart, crash or power loss). "
                      "Any half-applied change was rolled back. Use Retry to start it again from the beginning.")
        )
        await s.execute(update(ApprovalBatch).where(ApprovalBatch.status == "pending").values(status="expired"))
        await s.commit()
        return res.rowcount or 0


async def active_task_for_project(project_id: uuid.UUID, kinds: set[str]) -> Task | None:
    async with dbm.session() as s:
        return (await s.execute(select(Task).where(Task.project_id == project_id, Task.kind.in_(kinds),
                                                   Task.status.in_(ACTIVE)).order_by(Task.created_at.desc()).limit(1))).scalar_one_or_none()


def iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None
