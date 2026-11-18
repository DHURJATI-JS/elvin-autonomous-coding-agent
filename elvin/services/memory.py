"""Per-project memory. Facts the user states once ("use PostgreSQL", "tabs, not
spaces") are remembered for that project only - every query is scoped by
project_id, so Elvin can never mix one project's memory into another's."""
from __future__ import annotations

import hashlib
import logging
import re
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db as dbm
from .. import limits
from ..models import Memory, Project

log = logging.getLogger("elvin.memory")


def _key(text: str) -> str:
    return hashlib.sha256(re.sub(r"\W+", " ", text.lower()).strip().encode()).hexdigest()


async def list_for(db: AsyncSession, project_id: uuid.UUID) -> list[Memory]:
    return list((await db.execute(select(Memory).where(Memory.project_id == project_id)
                                  .order_by(Memory.created_at.desc()))).scalars().all())


async def add(db: AsyncSession, project_id: uuid.UUID, content: str, source: str = "auto") -> Memory | None:
    content = re.sub(r"\s+", " ", content).strip()[: limits.MAX_MEMORY_CHARS]
    if len(content) < 4:
        return None
    # lock the project row so two concurrent adds can't both pass the cap check
    await db.execute(select(Project.id).where(Project.id == project_id).with_for_update())
    key = _key(content)
    exists = (await db.execute(select(Memory.id).where(Memory.project_id == project_id, Memory.content_key == key))).scalar_one_or_none()
    if exists:
        await db.commit()
        return None
    count = (await db.execute(select(func.count()).select_from(Memory).where(Memory.project_id == project_id))).scalar_one()
    if count >= limits.MAX_MEMORIES_PER_PROJECT:
        if source == "user":
            oldest = (await db.execute(select(Memory.id).where(Memory.project_id == project_id)
                                       .order_by(Memory.source.asc(), Memory.created_at.asc()).limit(1))).scalar_one_or_none()
        else:
            oldest = (await db.execute(select(Memory.id).where(Memory.project_id == project_id, Memory.source == "auto")
                                       .order_by(Memory.created_at.asc()).limit(1))).scalar_one_or_none()
        if oldest is None:
            await db.commit()
            return None
        await db.execute(delete(Memory).where(Memory.id == oldest))
    mem = Memory(project_id=project_id, content=content, content_key=key, source=source)
    db.add(mem)
    await db.commit()
    return mem


async def render_for(project_id: uuid.UUID, max_chars: int = 2000) -> str:
    async with dbm.session() as db:
        mems = await list_for(db, project_id)
    lines, total = [], 0
    for m in mems[:25]:
        line = f"- {m.content}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


async def extract_and_store(project_id: uuid.UUID, text: str, team: str) -> list[str]:
    from .ai import prompts
    from .ai import router as ai

    if len(text.strip()) < 12:
        return []
    try:
        res = await ai.call("utility", team=team, system=prompts.MEMORY, task_role="memory", json_mode=True, max_tokens=300,
                            messages=[{"role": "user", "content": f"USER MESSAGE:\n{text[:3000]}"}])
    except ai.AIError as exc:
        log.info("memory extraction skipped: %s", exc)
        return []
    facts = [str(f).strip() for f in (res.data or {}).get("facts", []) if isinstance(f, (str, int, float))][:3]
    saved: list[str] = []
    async with dbm.session() as db:
        for fact in facts:
            if await add(db, project_id, fact, "auto"):
                saved.append(fact)
    return saved
