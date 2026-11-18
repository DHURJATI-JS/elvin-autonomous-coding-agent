"""Elvin - autonomous agentic coder. FastAPI application factory."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import db as dbm
from . import limits, redisx
from .config import get_settings
from .models import Project
from .security.auth import AlreadyLoggedIn, LoginRequired, wants_json
from .security.csrf import csrf_protect
from .security.headers import SecurityHeadersMiddleware
from .services import clone, integrity, journal, status_monitor, tasks, usage
from .services.ai.providers import close_http
from .services.ai.router import Busy
from .services.fs import PathError

log = logging.getLogger("elvin")
BASE = Path(__file__).parent


class BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Reject oversized request bodies before they are parsed."""

    RULES = [
        ("/api/projects/upload", limits.MAX_UPLOAD_BYTES + 5 * 1024 * 1024),
        ("/upload", limits.MAX_UPLOAD_BYTES + 5 * 1024 * 1024),
        ("/attachments", limits.MAX_IMAGE_BYTES + 1024 * 1024),
        ("/file", limits.MAX_EDITOR_FILE_BYTES * 2 + 64 * 1024),
    ]

    def __init__(self, app: ASGIApp, default: int = 1024 * 1024) -> None:
        self.app = app
        self.default = default

    def limit_for(self, path: str) -> int:
        for suffix, value in self.RULES:
            if path.endswith(suffix) or path.startswith(suffix):
                return value
        return self.default

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope.get("path", ""))
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                with contextlib.suppress(ValueError):
                    if int(value) > limit:
                        await self._reject(send, limit)
                        return
        received = 0
        started = False

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge()
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted, tracking_send)
        except BodyTooLarge:
            if not started:
                await self._reject(send, limit)

    @staticmethod
    async def _reject(send: Send, limit: int) -> None:
        body = json.dumps({"detail": f"Upload too large (limit {limit // (1024 * 1024)} MB)."}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


async def load_overrides() -> None:
    """Apply installation-wide settings changed in the app (e.g. a new gateway address)."""
    from .services import app_settings
    from .services.ai import router as ai_router

    ai_router.set_overrides(await app_settings.load())
    ai_router.reset_registry()


async def _startup_integrity() -> None:
    await asyncio.sleep(3)
    async with dbm.session() as db:
        projects = list((await db.execute(select(Project).where(Project.archived.is_(False)))).scalars().all())
        await integrity.check_all(projects, db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    problems = s.startup_problems()
    if problems and not s.is_test:
        for p in problems:
            log.error("ELVIN CANNOT START: %s", p)
        print("\n".join(["", "=" * 70, "Elvin cannot start:", *[f"  - {p}" for p in problems], "=" * 70, ""]), file=sys.stderr)
        raise SystemExit(3)
    for sub in ("workspace", "data/snapshots", "data/journals", "data/attachments", "data/quarantine", "data/outbox",
                "data/uploads", "logs"):
        (s.home / sub).mkdir(parents=True, exist_ok=True)
    await dbm.create_all()
    await load_overrides()
    from .services.ai import router as ai_router

    # (the browser check and stress test run with test-double models, so there is no hardware to judge)
    gemma_problems = [] if ai_router.registry().fake else s.gemma_problems(ai_router.gateway_url())
    if gemma_problems and not s.is_test:
        for p in gemma_problems:
            log.error("ELVIN CANNOT START: %s", p)
        print("\n".join(["", "=" * 70, "Elvin cannot start:", *[f"  - {p}" for p in gemma_problems], "=" * 70, ""]), file=sys.stderr)
        raise SystemExit(3)
    recovered = await asyncio.to_thread(journal.recover_all, s.data_dir / "journals")
    if recovered:
        log.warning("rolled back %d interrupted change set(s) after an unclean shutdown", len(recovered))
    interrupted = await tasks.recover_on_startup()
    if interrupted:
        log.warning("%d task(s) were interrupted by the last shutdown", interrupted)
    await asyncio.to_thread(clone.cleanup_quarantine)
    usage.start()
    tasks.manager.start_reaper()
    if not s.is_test:
        status_monitor.monitor.start()
    integrity_job = asyncio.create_task(_startup_integrity())
    log.info("Elvin is ready on %s (platform: %s %s, local model backend: %s)", s.base_url, s.host_os, s.host_arch, s.local_llm_backend)
    yield
    integrity_job.cancel()
    await status_monitor.monitor.stop()
    await tasks.manager.shutdown()
    await usage.stop()
    await close_http()
    await redisx.close()
    await dbm.dispose()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Elvin", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
                  dependencies=[Depends(csrf_protect)])
    from .routers import api_core, api_elvin, api_git, api_projects, api_tools, auth, pages

    for module in (pages, auth, api_core, api_projects, api_elvin, api_tools, api_git):
        app.include_router(module.router)
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if wants_json(request):
            return JSONResponse({"detail": exc.message or "Please sign in.", "error": "signed_out", "reason": exc.reason}, status_code=401)
        nxt = request.url.path if request.url.path not in ("/", "/logout") else "/dashboard"
        return RedirectResponse(f"/login?next={nxt}", status_code=303)

    @app.exception_handler(AlreadyLoggedIn)
    async def _already(request: Request, exc: AlreadyLoggedIn):
        return RedirectResponse("/dashboard", status_code=303)

    @app.exception_handler(PathError)
    async def _path_error(request: Request, exc: PathError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(Busy)
    async def _busy(request: Request, exc: Busy):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "input"
        return JSONResponse({"detail": f"Invalid {field}: {first.get('msg', 'bad value')}"}, status_code=422)

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        if wants_json(request) or request.method != "GET":
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        from .db import session
        from .security.auth import resolve_user
        from .web import render

        async with session() as db:
            user = await resolve_user(request, db)
            return await render(request, db, "error.html", {"code": exc.status_code, "message": exc.detail}, user=user,
                                status_code=exc.status_code)

    # middleware order: last added runs first
    app.add_middleware(SessionMiddleware, secret_key=s.secret_key or "test-secret-key-not-for-production-use!!",
                       session_cookie="elvin_session", max_age=limits.SESSION_MAX_AGE_DAYS * 86400,
                       same_site="lax", https_only=s.cookie_secure)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, force_https=s.cookie_secure)
    return app


app = create_app()
