"""Sign up, sign in, sign out and password reset. Only reachable when nobody
is signed in (except sign-out)."""
from __future__ import annotations

import datetime as dt
import logging
import re
import secrets

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits, redisx
from ..config import get_settings
from ..db import get_db
from ..models import PasswordResetToken, User, utcnow
from ..security import ratelimit
from ..security.auth import audit, end_session, require_anonymous, require_user, safe_next, start_session, token_hash
from ..security.passwords import hash_password, needs_rehash, password_problems, verify_password
from ..services import tasks
from ..services.ai import router as ai
from ..services.fs import atomic_write_bytes
from ..web import render

router = APIRouter()
log = logging.getLogger("elvin.auth")
USERNAME = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


@router.get("/signup", dependencies=[Depends(require_anonymous)])
async def signup_form(request: Request, db: AsyncSession = Depends(get_db)):
    return await render(request, db, "auth/signup.html", {"title": "Create your account", "values": {}, "errors": []})


@router.post("/signup", dependencies=[Depends(require_anonymous)])
async def signup(request: Request, username: str = Form(""), email: str = Form(""), password: str = Form(""),
                 confirm: str = Form(""), db: AsyncSession = Depends(get_db)):
    values = {"username": username.strip()[:64], "email": email.strip()[:254]}
    errors: list[str] = []
    ok, retry = await ratelimit.hit(f"signup:{ratelimit.client_ip(request)}", limits.SIGNUPS_PER_IP_HOUR, 3600)
    if not ok:
        errors.append(f"Too many sign-ups from this network. Try again in {retry // 60 + 1} minutes.")
    if not USERNAME.fullmatch(values["username"]):
        errors.append("Username: 3-32 letters, numbers, '.', '-' or '_'.")
    try:
        email_norm = validate_email(values["email"], check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        email_norm = ""
        errors.append("Enter a valid email address.")
    if password != confirm:
        errors.append("The two passwords don't match.")
    errors += password_problems(password, username=values["username"], email=values["email"])
    if not errors:
        total = (await db.execute(select(func.count()).select_from(User))).scalar_one()
        if total >= limits.MAX_USERS:
            errors.append("This Elvin installation has reached its maximum number of accounts.")
    if not errors:
        taken = (await db.execute(select(User.id).where((User.email == email_norm) | (User.username_key == values["username"].lower())))).first()
        if taken:
            errors.append("An account with that email or username already exists.")
    if errors:
        return await render(request, db, "auth/signup.html", {"title": "Create your account", "values": values, "errors": errors},
                            status_code=400)
    first = (await db.execute(select(func.count()).select_from(User))).scalar_one() == 0
    user = User(email=email_norm, username=values["username"], username_key=values["username"].lower(),
                password_hash=await hash_password(password), is_admin=first, ai_team=ai.registry().default_team(),
                password_changed_at=utcnow())
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return await render(request, db, "auth/signup.html", {"title": "Create your account", "values": values,
                                                              "errors": ["An account with that email or username already exists."]},
                            status_code=400)
    await audit(db, "signup", request, user.id)
    await start_session(request, db, user)
    await tasks.presence.heartbeat(user.id)
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/login", dependencies=[Depends(require_anonymous)])
async def login_form(request: Request, next: str = "", db: AsyncSession = Depends(get_db)):
    return await render(request, db, "auth/login.html", {"title": "Sign in", "next": safe_next(next), "values": {}, "errors": []})


@router.post("/login", dependencies=[Depends(require_anonymous)])
async def login(request: Request, identifier: str = Form(""), password: str = Form(""), next: str = Form(""),
                db: AsyncSession = Depends(get_db)):
    ident = identifier.strip().lower()[:254]
    values = {"identifier": identifier.strip()[:254]}
    ctx = {"title": "Sign in", "next": safe_next(next), "values": values}
    ok, retry = await ratelimit.hit(f"login-ip:{ratelimit.client_ip(request)}", limits.LOGIN_ATTEMPTS_PER_IP_15MIN, 900)
    if not ok:
        return await render(request, db, "auth/login.html", {**ctx, "errors": [f"Too many attempts. Try again in {retry // 60 + 1} min."]},
                            status_code=429)
    user = (await db.execute(select(User).where((User.email == ident) | (User.username_key == ident)))).scalar_one_or_none()
    now = utcnow()
    if user is not None and user.locked_until and user.locked_until > now:
        await verify_password(None, password)  # keep timing uniform
        mins = int((user.locked_until - now).total_seconds() // 60) + 1
        return await render(request, db, "auth/login.html", {**ctx, "errors": [f"Too many failed attempts. Try again in {mins} min."]},
                            status_code=429)
    valid = await verify_password(user.password_hash if user else None, password)
    if not valid or user is None:
        if user is not None:
            user.failed_logins = (user.failed_logins or 0) + 1
            if user.failed_logins >= limits.LOGIN_FAILS_PER_ACCOUNT:
                user.locked_until = now + dt.timedelta(minutes=limits.LOGIN_LOCK_MINUTES)
                user.failed_logins = 0
            await audit(db, "login_failed", request, user.id)
            await db.commit()
        return await render(request, db, "auth/login.html", {**ctx, "errors": ["Wrong email/username or password."]}, status_code=400)
    if needs_rehash(user.password_hash):
        user.password_hash = await hash_password(password)
    await audit(db, "login", request, user.id)
    await start_session(request, db, user)  # replaces any other session: one login per account
    await tasks.presence.heartbeat(user.id)
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/logout")
async def logout(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    await tasks.presence.leave(user.id)  # AI tasks are cancelled if no tab returns within 90 s
    await audit(db, "logout", request, user.id)
    await end_session(request, db, user)
    request.session["flash"] = "You are signed out. Running AI tasks stop in 90 seconds unless you sign back in."
    return RedirectResponse("/", status_code=303)


@router.get("/forgot", dependencies=[Depends(require_anonymous)])
async def forgot_form(request: Request, db: AsyncSession = Depends(get_db)):
    return await render(request, db, "auth/forgot.html", {"title": "Reset your password", "sent": False, "errors": []})


async def _deliver_reset(email: str, link: str) -> None:
    s = get_settings()
    body = (f"Someone (hopefully you) asked to reset the password of your Elvin account.\n\nOpen this link within "
            f"{limits.RESET_TOKEN_MINUTES} minutes to choose a new password:\n{link}\n\nIf it wasn't you, ignore this email.")
    if s.smtp_host:
        try:
            import aiosmtplib
            from email.message import EmailMessage

            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = s.smtp_from, email, "Reset your Elvin password"
            msg.set_content(body)
            await aiosmtplib.send(msg, hostname=s.smtp_host, port=s.smtp_port, username=s.smtp_user or None,
                                  password=s.smtp_password or None, start_tls=s.smtp_starttls, timeout=20)
            return
        except Exception as exc:  # fall back to the outbox
            log.warning("SMTP delivery failed: %s", exc)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    atomic_write_bytes(s.data_dir / "outbox" / f"{stamp}-password-reset.txt", f"To: {email}\n\n{body}\n".encode())
    log.warning("No SMTP configured - password reset link for %s written to data/outbox/ and here: %s", email, link)


@router.post("/forgot", dependencies=[Depends(require_anonymous)])
async def forgot(request: Request, email: str = Form(""), db: AsyncSession = Depends(get_db)):
    email_l = email.strip().lower()[:254]
    ip_ok, _ = await ratelimit.hit(f"reset-ip:{ratelimit.client_ip(request)}", limits.RESET_REQUESTS_PER_IP_HOUR, 3600)
    mail_ok, _ = await ratelimit.hit(f"reset-mail:{email_l}", limits.RESET_REQUESTS_PER_EMAIL_HOUR, 3600)
    user = (await db.execute(select(User).where(User.email == email_l))).scalar_one_or_none()
    if user is not None and ip_ok and mail_ok:
        await db.execute(update(PasswordResetToken).where(PasswordResetToken.user_id == user.id,
                                                          PasswordResetToken.used_at.is_(None)).values(used_at=utcnow()))
        raw = secrets.token_urlsafe(32)
        db.add(PasswordResetToken(user_id=user.id, token_hash=token_hash(raw), request_ip=ratelimit.client_ip(request),
                                  expires_at=utcnow() + dt.timedelta(minutes=limits.RESET_TOKEN_MINUTES)))
        await audit(db, "password_reset_requested", request, user.id)
        await db.commit()
        base = get_settings().base_url.rstrip("/")
        await _deliver_reset(user.email, f"{base}/reset/{raw}")
    # same answer whether or not the account exists
    return await render(request, db, "auth/forgot.html", {"title": "Reset your password", "sent": True, "errors": []})


async def _valid_token(db: AsyncSession, raw: str) -> PasswordResetToken | None:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{20,100}", raw or ""):
        return None
    tok = (await db.execute(select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash(raw)))).scalar_one_or_none()
    if tok is None or tok.used_at is not None or tok.expires_at < utcnow():
        return None
    return tok


@router.get("/reset/{raw}", dependencies=[Depends(require_anonymous)])
async def reset_form(request: Request, raw: str, db: AsyncSession = Depends(get_db)):
    tok = await _valid_token(db, raw)
    return await render(request, db, "auth/reset.html", {"title": "Choose a new password", "valid": tok is not None,
                                                         "token": raw, "errors": []}, status_code=200 if tok else 400)


@router.post("/reset/{raw}", dependencies=[Depends(require_anonymous)])
async def reset(request: Request, raw: str, password: str = Form(""), confirm: str = Form(""), db: AsyncSession = Depends(get_db)):
    tok = await _valid_token(db, raw)
    if tok is None:
        return await render(request, db, "auth/reset.html", {"title": "Choose a new password", "valid": False, "token": raw, "errors": []},
                            status_code=400)
    user = await db.get(User, tok.user_id)
    errors = ["The two passwords don't match."] if password != confirm else []
    errors += password_problems(password, username=user.username, email=user.email)
    if errors:
        return await render(request, db, "auth/reset.html", {"title": "Choose a new password", "valid": True, "token": raw,
                                                             "errors": errors}, status_code=400)
    user.password_hash = await hash_password(password)
    user.password_changed_at = utcnow()
    user.session_token_hash = None  # sign out every device
    user.failed_logins = 0
    user.locked_until = None
    tok.used_at = utcnow()
    await audit(db, "password_reset", request, user.id)
    await db.commit()
    await redisx.call("delete", f"elvin:sess:{user.id}")
    request.session["flash"] = "Your password was changed. Sign in with the new password."
    return RedirectResponse("/login", status_code=303)
