"""CSRF protection: a per-session synchronizer token (kept in the signed,
HttpOnly session cookie) that every state-changing request must echo back in
the X-CSRF-Token header or a csrf_token form/JSON field, plus an Origin check."""
from __future__ import annotations

import hmac
import json
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from ..config import get_settings

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def rotate_csrf_token(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session["csrf"] = token
    return token


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else ""


def allowed_origins(request: Request) -> set[str]:
    s = get_settings()
    host = request.headers.get("host", "")
    allowed = {f"http://{host}".lower(), f"https://{host}".lower(), _origin_of(s.base_url)}
    for extra in s.trusted_origins.split(","):
        if extra.strip():
            allowed.add(_origin_of(extra.strip()))
    allowed.discard("")
    return allowed


async def csrf_protect(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    source = origin if origin and origin != "null" else (_origin_of(referer) if referer else "")
    if origin == "null" or (source and source.lower() not in allowed_origins(request)):
        raise HTTPException(status_code=403, detail="Cross-site request blocked.")

    sent = request.headers.get("x-csrf-token")
    if not sent:
        ctype = request.headers.get("content-type", "")
        try:
            if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
                form = await request.form()
                value = form.get("csrf_token")
                sent = value if isinstance(value, str) else None
            elif ctype.startswith(("application/json", "text/plain")):
                raw = await request.body()
                if raw and len(raw) < 1_000_000:
                    data = json.loads(raw)
                    if isinstance(data, dict) and isinstance(data.get("csrf_token"), str):
                        sent = data["csrf_token"]
        except (ValueError, UnicodeDecodeError):
            sent = None
    expected = request.session.get("csrf")
    if not sent or not expected or not hmac.compare_digest(str(sent), str(expected)):
        raise HTTPException(status_code=403, detail="Security token expired - reload the page and try again.")
