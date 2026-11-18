"""Test harness. Runs only inside docker-compose.test.yml against the
throwaway elvin_test database (refuses anything else), with deterministic fake
AI models, a fake ClamAV daemon and a fake OSV.dev."""
from __future__ import annotations

import asyncio
import os
import random
import re
import shutil
import struct
import time
import uuid
from typing import Any

import httpx
import pytest

DB_URL = os.environ.get("DATABASE_URL", "")
if not DB_URL.rsplit("/", 1)[-1].endswith("_test") or os.environ.get("ENVIRONMENT") != "test":
    raise RuntimeError("Refusing to run tests: DATABASE_URL must point at a *_test database and ENVIRONMENT=test.")

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
PASSWORD = "Sup3r-Secret-Pass!"


async def _fake_clamd(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        cmd = await reader.readuntil(b"\0")
        if cmd == b"zPING\0":
            writer.write(b"PONG\0")
        elif cmd == b"zVERSION\0":
            writer.write(b"ClamAV 1.5.4/27000/fake\0")
        elif cmd == b"zINSTREAM\0":
            data = b""
            while True:
                size = struct.unpack("!I", await reader.readexactly(4))[0]
                if size == 0:
                    break
                data += await reader.readexactly(size)
            writer.write(b"stream: Eicar-Test-Signature FOUND\0" if EICAR in data else b"stream: OK\0")
        await writer.drain()
    finally:
        writer.close()


@pytest.fixture(scope="session")
async def clamd():
    server = await asyncio.start_server(_fake_clamd, "127.0.0.1", 0)
    os.environ["CLAMAV_HOST"] = "127.0.0.1"
    os.environ["CLAMAV_PORT"] = str(server.sockets[0].getsockname()[1])
    yield server
    server.close()


# ---------------------------------------------------------------- fake OSV / registries
VULN_DB: dict[str, dict] = {
    "GHSA-test-0001": {"id": "GHSA-test-0001", "summary": "Remote code execution in vulnlib",
                       "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
                       "affected": [{"package": {"ecosystem": "PyPI", "name": "vulnlib"},
                                     "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.0.0"}]}]}]},
    "MAL-2025-0001": {"id": "MAL-2025-0001", "summary": "Malicious code in malpkg",
                      "affected": [{"package": {"ecosystem": "PyPI", "name": "malpkg"}, "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}]}]},
    "GHSA-nofix": {"id": "GHSA-nofix", "summary": "Unfixed bug", "database_specific": {"severity": "HIGH"},
                   "affected": [{"package": {"ecosystem": "npm", "name": "nofix"}, "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}]}]}]},
}


def _fake_ids(eco: str, name: str, version: str) -> list[str]:
    from packaging.version import Version

    n = name.lower()
    if n == "vulnlib":
        try:
            return ["GHSA-test-0001"] if not version or Version(version) < Version("2.0.0") else []
        except Exception:
            return ["GHSA-test-0001"]
    if n == "malpkg":
        return ["MAL-2025-0001"]
    if n == "nofix":
        return ["GHSA-nofix"]
    return []


@pytest.fixture(autouse=True)
def fake_osv(monkeypatch):
    from elvin.services import vulns

    async def osv_batch(packages):
        return [_fake_ids(e, n, v) for e, n, v in packages]

    async def osv_details(vid):
        return VULN_DB.get(vid, {"id": vid})

    async def latest_version(eco, name):
        if "doesnotexist" in name.lower():
            return False, ""
        return True, {"vulnlib": "2.1.0", "requests": "2.32.4"}.get(name.lower(), "1.0.0")

    monkeypatch.setattr(vulns, "osv_batch", osv_batch)
    monkeypatch.setattr(vulns, "osv_details", osv_details)
    monkeypatch.setattr(vulns, "latest_version", latest_version)


# ---------------------------------------------------------------- app & clients
@pytest.fixture(scope="session")
async def app(clamd):
    home = os.environ["ELVIN_HOME"]
    shutil.rmtree(home, ignore_errors=True)
    from elvin.config import get_settings

    get_settings.cache_clear()
    from elvin import db as dbm
    from elvin import redisx
    from elvin.models import Base
    from elvin.services.ai import router

    async with dbm.engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await redisx.call("flushdb")
    router.reset_registry()
    from elvin.main import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


class Client:
    def __init__(self, app) -> None:
        ip = f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 40000)),
                                      base_url="http://testserver", follow_redirects=False, timeout=60)
        self.csrf = ""
        self.username = ""
        self.user_id = ""

    async def load(self, path: str = "/login") -> httpx.Response:
        r = await self.http.get(path)
        m = re.search(r'name="csrf-token" content="([^"]+)"', r.text)
        if m:
            self.csrf = m.group(1)
        return r

    async def form(self, path: str, data: dict[str, Any]) -> httpx.Response:
        return await self.http.post(path, data={**data, "csrf_token": self.csrf})

    async def api(self, method: str, path: str, json: Any = None, **kw) -> httpx.Response:
        headers = {"X-CSRF-Token": self.csrf, **kw.pop("headers", {})}
        return await self.http.request(method, path, json=json, headers=headers, **kw)

    async def get(self, path: str) -> httpx.Response:
        return await self.http.get(path)

    async def signup(self, username: str | None = None, password: str = PASSWORD) -> "Client":
        self.username = username or f"u{uuid.uuid4().hex[:10]}"
        await self.load("/signup")
        r = await self.form("/signup", {"username": self.username, "email": f"{self.username}@example.com",
                                        "password": password, "confirm": password})
        assert r.status_code == 303, r.text[:500]
        await self.load("/dashboard")
        return self

    async def login(self, identifier: str, password: str = PASSWORD) -> httpx.Response:
        await self.load("/login")
        r = await self.form("/login", {"identifier": identifier, "password": password})
        if r.status_code == 303:
            await self.load("/dashboard")
        return r

    async def new_project(self, name: str = "demo") -> dict:
        r = await self.api("POST", "/api/projects", {"name": name, "description": "test"})
        assert r.status_code == 200, r.text
        return r.json()

    async def close(self) -> None:
        await self.http.aclose()


@pytest.fixture
async def anon(app):
    c = Client(app)
    await c.load("/login")
    yield c
    await c.close()


@pytest.fixture
async def user(app):
    c = Client(app)
    await c.signup()
    yield c
    await c.close()


@pytest.fixture
async def user2(app):
    c = Client(app)
    await c.signup()
    yield c
    await c.close()


async def wait_for(predicate, timeout: float = 20.0, interval: float = 0.1):
    """Poll an async predicate until it returns something truthy."""
    deadline = time.monotonic() + timeout
    import inspect

    while time.monotonic() < deadline:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


async def task_row(task_id: str):
    from elvin import db as dbm
    from elvin.models import Task

    async with dbm.session() as s:
        return await s.get(Task, uuid.UUID(task_id))


async def pending_batch(task_id: str):
    from sqlalchemy import select

    from elvin import db as dbm
    from elvin.models import ApprovalBatch
    from elvin.services import agent

    async with dbm.session() as s:
        rows = (await s.execute(select(ApprovalBatch).where(ApprovalBatch.task_id == uuid.UUID(task_id),
                                                            ApprovalBatch.status == "pending"))).scalars().all()
    rows = [b for b in rows if agent.has_waiter(b.id)]
    return rows[0] if rows else None


async def finished(task_id: str):
    t = await task_row(task_id)
    return t if t and t.status in ("completed", "failed", "cancelled", "interrupted") else None


def project_dir(user_id: str, slug: str):
    from elvin.services.fs import project_root

    return project_root(user_id, slug)


@pytest.fixture(scope="session")
def target_url(tmp_path_factory):
    """A tiny HTTP site on the container's private IP for Locust to hit."""
    import socket
    import subprocess
    import sys

    site = tmp_path_factory.mktemp("site")
    (site / "index.html").write_text("<h1>ok</h1>")
    (site / "about.html").write_text("<p>about</p>")
    port = 18765
    proc = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "0.0.0.0"], cwd=site,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ip = socket.gethostbyname(socket.gethostname())
    for _ in range(50):
        try:
            socket.create_connection((ip, port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://{ip}:{port}"
    proc.terminate()
