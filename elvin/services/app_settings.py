"""Installation-wide settings that can change while Elvin runs (gateway address,
Gemma source). They live in the `app_settings` table and are changed by the
administrator on the System Status page or with `python -m elvin.admin`. Every
app process re-reads them at startup and on each 60-second status check."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db as dbm
from ..models import AppSetting, AuditLog, utcnow
from .ai import router as ai


async def load() -> dict[str, str]:
    async with dbm.session() as db:
        rows = (await db.execute(select(AppSetting))).scalars().all()
    return {r.key: r.value for r in rows}


async def save(db: AsyncSession, values: dict[str, str], *, user_id=None, ip: str | None = None, via: str = "app") -> None:
    """Upsert the values and write an audit entry (secrets are never logged). The caller commits."""
    for key, value in values.items():
        row = await db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value, row.updated_at = value, utcnow()
    shown = {k: ("(changed)" if "key" in k else v) for k, v in values.items()}
    db.add(AuditLog(user_id=user_id, event="settings_changed", ip=ip, detail={**shown, "via": via}))


async def refresh() -> bool:
    """Apply what is stored now. Returns True if anything changed."""
    return ai.apply_overrides(await load())
