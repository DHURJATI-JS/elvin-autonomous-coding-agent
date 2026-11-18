"""Sessions, Google-style: the signed HttpOnly cookie only carries the user's
primary key plus a random session token. Every request loads the user from
PostgreSQL by id and cross-checks the token against the one stored in
PostgreSQL and Redis. A newer sign-in anywhere replaces the stored token, so any
older tab or device is signed out on its very next request (Elvin's AI tasks
keep running - they belong to the account, not to the device)."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import secrets
import time
import uuid
from typing import Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits, redisx
from ..db import get_db
from ..models import AuditLog, User, utcnow
from .csrf import rotate_csrf_token

SESSION_TTL = limits.SESSION_MAX_AGE_DAYS * 86400


class LoginRequired(Exception):
    def __init__(self, reason: str = "login", message: str = "") -> None:
        self.reason = reason
        self.message = message


class AlreadyLoggedIn(Exception):
    pass


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _redis_key(user_id: uuid.UUID | str) -> str:
    return f"elvin:sess:{user_id}"


def describe_device(user_agent: str) -> str:
    ua = user_agent or ""
    if "Edg/" in ua:
        browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "Firefox/" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua or "CriOS" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    elif "python" in ua.lower() or "httpx" in ua.lower() or "locust" in ua.lower():
        browser = "a script"
    else:
        browser = "a browser"
    if "Windows" in ua:
        os_name = "Windows"
    elif "iPhone" in ua or "iPad" in ua:
        os_name = "iOS"
    elif "Android" in ua:
        os_name = "Android"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_name = "macOS"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = ""
    return f"{browser} on {os_name}" if os_name else browser


def session_fingerprint(request: Request) -> str:
    sid = request.session.get("sid") or ""
    return token_hash("fp:" + sid)[:16] if sid else ""


async def audit(db: AsyncSession, event: str, request: Request | None = None, user_id: Any = None, **detail: Any) -> None:
    ip = request.client.host if request is not None and request.client else None
    db.add(AuditLog(user_id=user_id, event=event, ip=ip, detail=detail))


def _superseded_message(user: User | None) -> str:
    if user is not None and user.session_token_hash and user.session_started_at:
        when = user.session_started_at.astimezone(dt.timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        device = user.session_device or "another device"
        return f"You were signed out because your account signed in on {device} at {when}."
    return "You were signed out. Please sign in again."


async def resolve_user(request: Request, db: AsyncSession) -> User | None:
    """Return the signed-in user or None. Never raises."""
    if getattr(request.state, "user_resolved", False):
        return getattr(request.state, "user", None)
    request.state.user_resolved = True
    request.state.user = None
    uid = request.session.get("uid")
    sid = request.session.get("sid")
    if not uid or not sid:
        return None
    iat = request.session.get("iat", 0)
    if not isinstance(iat, int) or time.time() - iat > SESSION_TTL:
        request.session.clear()
        request.state.session_ended = {"reason": "expired", "message": "Your session expired. Please sign in again."}
        return None
    try:
        user_id = uuid.UUID(str(uid))
    except ValueError:
        request.session.clear()
        return None
    user = await db.get(User, user_id)
    presented = token_hash(str(sid))
    stored = user.session_token_hash if user else None
    if user is None or not stored or not hmac.compare_digest(stored, presented):
        message = _superseded_message(user)
        request.session.clear()
        request.session["flash"] = message
        request.state.session_ended = {"reason": "elsewhere" if stored else "signed_out", "message": message}
        return None
    # Cross-check Redis (heal it if it drifted, e.g. after a Redis restart).
    cached = await redisx.call("get", _redis_key(user.id), default=None)
    if cached != stored:
        await redisx.call("set", _redis_key(user.id), stored, ex=86400)
    request.state.user = user
    return user


async def session_still_valid(user_id: uuid.UUID, sid_hash: str) -> bool:
    """Cheap check used inside long-lived streams (Redis first, DB fallback)."""
    cached = await redisx.call("get", _redis_key(user_id), default=None)
    if cached is not None:
        return hmac.compare_digest(cached, sid_hash)
    from ..db import session

    async with session() as db:
        user = await db.get(User, user_id)
        return bool(user and user.session_token_hash and hmac.compare_digest(user.session_token_hash, sid_hash))


async def optional_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    return await resolve_user(request, db)


async def require_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user = await resolve_user(request, db)
    if user is None:
        ended = getattr(request.state, "session_ended", None) or {}
        raise LoginRequired(ended.get("reason", "login"), ended.get("message", ""))
    return user


async def require_anonymous(request: Request, db: AsyncSession = Depends(get_db)) -> None:
    if await resolve_user(request, db) is not None:
        raise AlreadyLoggedIn()


async def start_session(request: Request, db: AsyncSession, user: User) -> None:
    sid = secrets.token_urlsafe(32)
    user.session_token_hash = token_hash(sid)
    user.session_started_at = utcnow()
    user.session_device = describe_device(request.headers.get("user-agent", ""))[:120]
    user.session_ip = (request.client.host if request.client else "")[:64]
    user.last_login_at = utcnow()
    user.failed_logins = 0
    user.locked_until = None
    await db.commit()
    await redisx.call("set", _redis_key(user.id), user.session_token_hash, ex=86400)
    request.session.clear()  # no session fixation
    request.session.update({"uid": str(user.id), "sid": sid, "iat": int(time.time())})
    rotate_csrf_token(request)


async def end_session(request: Request, db: AsyncSession, user: User) -> None:
    user.session_token_hash = None
    await db.commit()
    await redisx.call("delete", _redis_key(user.id))
    request.session.clear()
    rotate_csrf_token(request)


def wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return request.url.path.startswith("/api/") or (
        "application/json" in accept and "text/html" not in accept
    )


_SAFE_NEXT = re.compile(r"^/(?!/)[A-Za-z0-9_\-/.?=&%]*$")


def safe_next(value: str | None) -> str:
    if value and _SAFE_NEXT.match(value) and "\\" not in value:
        return value
    return "/dashboard"


async def confirm_password(db: AsyncSession, request: Request, user: User, password: str | None, *, action: str) -> None:
    """Step-up authentication for every deletion: the account password, every time.
    Elvin (the AI) can only *propose* a deletion; it happens after the signed-in user
    approves it AND re-enters their password. Wrong passwords count towards the same
    lockout as signing in, so this can't be used to guess the password."""
    from fastapi import HTTPException

    from .passwords import verify_password
    from .ratelimit import enforce

    now = utcnow()
    if user.locked_until and user.locked_until > now:
        mins = int((user.locked_until - now).total_seconds() // 60) + 1
        raise HTTPException(429, f"Too many wrong passwords - try again in {mins} min. Nothing was deleted.")
    if not password:
        raise HTTPException(403, {"reauth": True, "message": f"Enter your password to {action}."})
    await enforce(f"reauth:{user.id}", 30, 900, "Too many password confirmations - wait a few minutes.")
    if not await verify_password(user.password_hash, password):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= limits.LOGIN_FAILS_PER_ACCOUNT:
            user.locked_until = now + dt.timedelta(minutes=limits.LOGIN_LOCK_MINUTES)
            user.failed_logins = 0
        await audit(db, "reauth_failed", request, user.id, action=action[:200])
        await db.commit()
        raise HTTPException(403, {"reauth": True, "wrong": True, "message": "Wrong password - nothing was deleted. Try again."})
    user.failed_logins = 0
    await audit(db, "reauth_ok", request, user.id, action=action[:200])
