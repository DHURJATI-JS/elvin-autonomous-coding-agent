"""Thin, failure-tolerant Redis helpers. Redis speeds things up (session
cross-checks, rate limits, presence) but Elvin keeps working from PostgreSQL /
memory when Redis is briefly unavailable."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from .config import get_settings

log = logging.getLogger("elvin.redis")
_client: aioredis.Redis | None = None
_last_error_at = 0.0


def client() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
            health_check_interval=30,
        )
    return _client


def healthy() -> bool:
    return time.monotonic() - _last_error_at > 10


async def call(op: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    global _last_error_at
    try:
        return await getattr(client(), op)(*args, **kwargs)
    except (RedisError, OSError, asyncio.TimeoutError) as exc:  # pragma: no cover - depends on infra
        if healthy():
            log.warning("redis %s failed: %s", op, exc)
        _last_error_at = time.monotonic()
        return default


async def ping() -> bool:
    return bool(await call("ping", default=False))


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # pragma: no cover
            pass
    _client = None
