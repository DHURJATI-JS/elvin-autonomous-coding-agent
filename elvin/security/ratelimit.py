"""Fixed-window rate limiting in Redis with an in-process fallback."""
from __future__ import annotations

import time

from fastapi import HTTPException, Request

from .. import redisx
from ..config import get_settings

_memory: dict[str, int] = {}


async def hit(key: str, limit: int, window_s: int) -> tuple[bool, int]:
    """Count one event for `key`; return (allowed, retry_after_seconds)."""
    limit = limit * max(1, get_settings().rate_limit_multiplier)
    now = time.time()
    bucket = int(now // window_s)
    rkey = f"elvin:rl:{key}:{bucket}"
    count = await redisx.call("incr", rkey, default=None)
    if count is None:  # Redis unavailable -> local counting
        if len(_memory) > 50_000:
            _memory.clear()
        _memory[rkey] = _memory.get(rkey, 0) + 1
        count = _memory[rkey]
    elif count == 1:
        await redisx.call("expire", rkey, window_s + 1)
    retry_after = int(window_s - (now % window_s)) + 1
    return count <= limit, retry_after


async def peek(key: str, window_s: int) -> int:
    bucket = int(time.time() // window_s)
    rkey = f"elvin:rl:{key}:{bucket}"
    value = await redisx.call("get", rkey, default=None)
    if value is None:
        return _memory.get(rkey, 0)
    return int(value)


async def enforce(key: str, limit: int, window_s: int, message: str = "Too many requests - slow down a little.") -> None:
    ok, retry = await hit(key, limit, window_s)
    if not ok:
        raise HTTPException(status_code=429, detail=f"{message} Try again in {retry}s.", headers={"Retry-After": str(retry)})


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
