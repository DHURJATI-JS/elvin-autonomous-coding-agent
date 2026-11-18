"""AI usage accounting and data-driven suggestions.

Every AI call is recorded (user, project, feature, provider, model, effort,
tokens, latency, success). Inserts are buffered and flushed in batches so the
accounting never slows an AI call down. The Usage page and the Settings pickers
use these numbers to suggest an effort level, models and a model combination."""
from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db as dbm
from ..models import UsageRecord, utcnow

log = logging.getLogger("elvin.usage")

# Approximate public list prices in USD per 1M tokens (input, output). Local
# models cost nothing. Shown as estimates only - your invoice is authoritative.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
}
LOCAL_PROVIDERS = {"gemma", "gemma_local", "qwen", "qwen_local"}

CLAUDE_CODER_CHOICES = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
CLAUDE_FAST_CHOICES = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
EFFORTS = ["low", "medium", "high", "max"]
EFFORT_BLURB = {
    "low": "Fastest and cheapest. One fix attempt, no extra review pass, one design iteration.",
    "medium": "Balanced (default). Two fix attempts, two design iterations.",
    "high": "More careful. Adds a review pass of every change and up to three design iterations.",
    "max": "Most thorough. Highest reasoning effort on Claude/ChatGPT, review pass, three design iterations.",
}


@dataclass
class UsageCtx:
    user_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    feature: str = ""
    effort: str = "medium"
    model_prefs: dict | None = None


_ctx: contextvars.ContextVar[UsageCtx | None] = contextvars.ContextVar("elvin_usage_ctx", default=None)
_buffer: list[dict[str, Any]] = []
_flusher: asyncio.Task | None = None


def set_context(**kwargs: Any) -> contextvars.Token:
    return _ctx.set(UsageCtx(**kwargs))


def reset_context(token: contextvars.Token) -> None:
    _ctx.reset(token)


def current() -> UsageCtx | None:
    return _ctx.get()


def effort_now() -> str:
    c = _ctx.get()
    return c.effort if c and c.effort in EFFORTS else "medium"


def model_override(provider_key: str) -> str | None:
    """User's per-role model choice for cloud providers (validated)."""
    c = _ctx.get()
    prefs = (c.model_prefs or {}) if c else {}
    if provider_key == "claude_opus" and prefs.get("claude_coder") in CLAUDE_CODER_CHOICES:
        return prefs["claude_coder"]
    if provider_key == "claude_haiku" and prefs.get("claude_fast") in CLAUDE_FAST_CHOICES:
        return prefs["claude_fast"]
    from ..config import get_settings

    choices = openai_choices(get_settings())
    if provider_key == "chatgpt" and prefs.get("openai_coder") in choices:
        return prefs["openai_coder"]
    if provider_key == "chatgpt_mini" and prefs.get("openai_fast") in choices:
        return prefs["openai_fast"]
    return None


def openai_choices(settings: Any) -> list[str]:
    seen: list[str] = []
    for m in (settings.openai_model, settings.openai_fast_model, "gpt-5", "gpt-5-mini"):
        if m and m not in seen:
            seen.append(m)
    return seen


def record(*, provider: str, model: str, role: str, ok: bool, input_tokens: int = 0, output_tokens: int = 0,
           elapsed_ms: int = 0, error_kind: str = "") -> None:
    c = _ctx.get() or UsageCtx()
    _buffer.append({
        "user_id": c.user_id, "project_id": c.project_id, "task_id": c.task_id, "feature": c.feature[:20],
        "provider": provider[:20], "model": (model or "")[:80], "role": role[:20], "effort": c.effort[:10],
        "ok": ok, "error_kind": error_kind[:20], "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0), "elapsed_ms": int(elapsed_ms), "created_at": utcnow(),
    })
    if len(_buffer) > 5000:  # never grow without bound if the DB is down
        del _buffer[:1000]


async def flush() -> int:
    if not _buffer:
        return 0
    rows = _buffer[:]
    del _buffer[: len(rows)]
    try:
        async with dbm.session() as s:
            await s.execute(insert(UsageRecord), rows)
            await s.commit()
    except Exception as exc:  # pragma: no cover - DB outage
        log.warning("could not store %d usage rows: %s", len(rows), exc)
        _buffer[:0] = rows[-1000:]
        return 0
    return len(rows)


async def _flush_loop() -> None:
    while True:
        await asyncio.sleep(2)
        await flush()


def start() -> None:
    global _flusher
    if _flusher is None or _flusher.done():
        _flusher = asyncio.create_task(_flush_loop(), name="usage-flush")


async def stop() -> None:
    global _flusher
    if _flusher is not None:
        _flusher.cancel()
        _flusher = None
    await flush()


def cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price = PRICES.get(model)
    if not price:
        for name, p in PRICES.items():  # tolerate dated / suffixed model names
            if model.startswith(name):
                price = p
                break
    if not price:
        return 0.0
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


async def summary(db: AsyncSession, user_id: uuid.UUID, days: int = 30) -> dict[str, Any]:
    since = utcnow() - dt.timedelta(days=days)
    base = select(UsageRecord).where(UsageRecord.user_id == user_id, UsageRecord.created_at >= since).subquery()
    by_model = (await db.execute(
        select(base.c.provider, base.c.model, func.count().label("calls"),
               func.sum(case((base.c.ok, 1), else_=0)).label("ok_calls"),
               func.sum(base.c.input_tokens).label("tin"), func.sum(base.c.output_tokens).label("tout"),
               func.avg(base.c.elapsed_ms).label("avg_ms"))
        .group_by(base.c.provider, base.c.model).order_by(func.count().desc()))).all()
    models = []
    total_cost = 0.0
    for r in by_model:
        c = 0.0 if r.provider in LOCAL_PROVIDERS else cost(r.model, int(r.tin or 0), int(r.tout or 0))
        total_cost += c
        models.append({"provider": r.provider, "model": r.model, "calls": r.calls, "ok_calls": int(r.ok_calls or 0),
                       "success_rate": round(100 * (r.ok_calls or 0) / r.calls, 1) if r.calls else 0,
                       "input_tokens": int(r.tin or 0), "output_tokens": int(r.tout or 0),
                       "avg_seconds": round(float(r.avg_ms or 0) / 1000, 2), "est_cost_usd": round(c, 4),
                       "local": r.provider in LOCAL_PROVIDERS})
    by_feature = (await db.execute(
        select(base.c.feature, func.count().label("calls"), func.sum(base.c.input_tokens + base.c.output_tokens).label("tok"))
        .group_by(base.c.feature).order_by(func.count().desc()))).all()
    by_effort = (await db.execute(
        select(base.c.effort, func.count().label("calls"), func.sum(case((base.c.ok, 1), else_=0)).label("ok_calls"),
               func.avg(base.c.elapsed_ms).label("avg_ms"))
        .group_by(base.c.effort))).all()
    day = func.date_trunc("day", base.c.created_at)
    daily = (await db.execute(
        select(day.label("day"), base.c.provider, func.count().label("calls"),
               func.sum(base.c.input_tokens + base.c.output_tokens).label("tok"))
        .group_by(day, base.c.provider).order_by(day))).all()
    return {
        "days": days,
        "models": models,
        "features": [{"feature": r.feature or "other", "calls": r.calls, "tokens": int(r.tok or 0)} for r in by_feature],
        "efforts": [{"effort": r.effort or "medium", "calls": r.calls,
                     "success_rate": round(100 * (r.ok_calls or 0) / r.calls, 1) if r.calls else 0,
                     "avg_seconds": round(float(r.avg_ms or 0) / 1000, 2)} for r in by_effort],
        "daily": [{"day": r.day.date().isoformat(), "provider": r.provider, "calls": r.calls, "tokens": int(r.tok or 0)} for r in daily],
        "totals": {"calls": sum(m["calls"] for m in models), "tokens": sum(m["input_tokens"] + m["output_tokens"] for m in models),
                   "est_cost_usd": round(total_cost, 4)},
    }


def recommendations(data: dict[str, Any], *, team: str, effort: str, teams: list[dict]) -> list[dict[str, str]]:
    """Plain-language suggestions derived from the usage numbers."""
    tips: list[dict[str, str]] = []
    models = data.get("models", [])
    by_provider: dict[str, dict[str, float]] = {}
    for m in models:
        agg = by_provider.setdefault(m["provider"], {"calls": 0, "ok": 0, "secs": 0.0, "cost": 0.0})
        agg["calls"] += m["calls"]
        agg["ok"] += m["ok_calls"]
        agg["secs"] += m["avg_seconds"] * m["calls"]
        agg["cost"] += m["est_cost_usd"]
    available = {t["id"] for t in teams if t["available"]}
    for prov, agg in by_provider.items():
        if agg["calls"] >= 5:
            rate = 100 * agg["ok"] / agg["calls"]
            avg = agg["secs"] / agg["calls"]
            if rate < 80:
                alt = "qwen_claude" if prov == "gemma" and "qwen_claude" in available else (
                      "gemma_claude" if prov == "qwen" and "gemma_claude" in available else "")
                tips.append({"level": "warn", "title": f"{prov} failed {100 - rate:.0f}% of calls",
                             "detail": "Elvin fell back to other models. " + (f"Consider the '{alt}' team." if alt else
                                                                                 "Check that the model server is running."),
                             "action_team": alt})
            if avg > 60 and prov in LOCAL_PROVIDERS:
                tips.append({"level": "info", "title": f"{prov} averages {avg:.0f}s per call",
                             "detail": "Lower the effort level, or pick a team with a cloud coder for big tasks.",
                             "action_effort": "low" if effort != "low" else ""})
    total = data.get("totals", {}).get("est_cost_usd", 0)
    if total > 20 and effort in ("high", "max"):
        tips.append({"level": "info", "title": f"About ${total:.2f} of cloud usage in {data.get('days', 30)} days",
                     "detail": "Medium effort usually keeps quality while lowering cost.", "action_effort": "medium"})
    calls = data.get("totals", {}).get("calls", 0)
    if calls == 0:
        tips.append({"level": "info", "title": "No AI usage yet",
                     "detail": "Run a task with Elvin - this page then shows which models and effort level work best for you."})
    elif not any(t["level"] == "warn" for t in tips) and effort == "low" and calls > 20:
        tips.append({"level": "info", "title": "Everything is healthy",
                     "detail": "If answers feel shallow, try the 'high' effort level for complex tasks.", "action_effort": "high"})
    if team not in available and available:
        tips.append({"level": "warn", "title": "Your selected AI team isn't fully available",
                     "detail": "Elvin is using the best available team instead. Pick another team below."})
    return tips[:6]
