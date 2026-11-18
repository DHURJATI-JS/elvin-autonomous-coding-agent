"""Elvin sandbox runner.

Receives a project as a tar.gz, unpacks it into a fresh temporary folder,
runs ONE user-approved command as an unprivileged user with CPU / memory /
process / file-size limits and a timeout, and returns the (capped) output.
Also renders HTML pages in headless Chromium with every network request
blocked except local files, and returns a PNG screenshot.
This container has no secrets and no access to Elvin's database."""
from __future__ import annotations

import asyncio
import hmac
import io
import os
import resource
import shutil
import signal
import tarfile
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response

TOKEN = os.environ.get("RUNNER_TOKEN", "")
JOBS = Path(os.environ.get("RUNNER_JOBS", "/jobs"))
MAX_BUNDLE = 80 * 1024 * 1024
MAX_EXTRACTED = 400 * 1024 * 1024
OUTPUT_CAP = 64 * 1024
SEM = asyncio.Semaphore(int(os.environ.get("RUNNER_CONCURRENCY", "2")))
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _auth(token: str) -> None:
    if not TOKEN or not hmac.compare_digest(token or "", TOKEN):
        raise HTTPException(status_code=401, detail="bad runner token")


def _extract(data: bytes, dest: Path) -> None:
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                raise HTTPException(400, f"unsafe path in bundle: {name}")
            target = (dest / name).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise HTTPException(400, "path escapes job folder")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue  # no symlinks, devices or fifos
            total += member.size
            if total > MAX_EXTRACTED:
                raise HTTPException(413, "bundle too large when extracted")
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            with open(target, "wb") as fh:
                shutil.copyfileobj(src, fh)
            os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)


def _limits(timeout: int):
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (timeout + 5, timeout + 10))
        resource.setrlimit(resource.RLIMIT_NPROC, (512, 512))
        resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 ** 2, 256 * 1024 ** 2))
        resource.setrlimit(resource.RLIMIT_NOFILE, (2048, 2048))
        os.setsid()
    return apply


async def _read_capped(stream: asyncio.StreamReader) -> tuple[str, bool]:
    chunks: list[bytes] = []
    size = 0
    truncated = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if size < OUTPUT_CAP:
            chunks.append(chunk[: OUTPUT_CAP - size])
        else:
            truncated = True
        size += len(chunk)
    return b"".join(chunks).decode(errors="replace"), truncated


@app.get("/health")
async def health(x_runner_token: str = Header("")):
    _auth(x_runner_token)
    node = shutil.which("node")
    proc = await asyncio.create_subprocess_exec("node", "--version", stdout=asyncio.subprocess.PIPE) if node else None
    node_v = (await proc.communicate())[0].decode().strip() if proc else "missing"
    import platform

    return {"ok": True, "python": platform.python_version(), "node": node_v}


@app.post("/run")
async def run(bundle: UploadFile = File(...), command: str = Form(...), timeout: int = Form(120),
              x_runner_token: str = Header("")):
    _auth(x_runner_token)
    timeout = max(5, min(int(timeout), 900))
    data = await bundle.read()
    if len(data) > MAX_BUNDLE:
        raise HTTPException(413, "bundle too large")
    if not command.strip() or len(command) > 2000:
        raise HTTPException(400, "bad command")
    async with SEM:
        JOBS.mkdir(parents=True, exist_ok=True)
        job = Path(tempfile.mkdtemp(prefix="job-", dir=JOBS))
        try:
            await asyncio.to_thread(_extract, data, job)
            home = job / ".home"
            home.mkdir()
            env = {"PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
                   "PIP_CACHE_DIR": "/cache/pip", "npm_config_cache": "/cache/npm", "PYTHONDONTWRITEBYTECODE": "1",
                   "CI": "1", "TERM": "dumb", "PYTHONUSERBASE": str(home / ".local")}
            started = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                "bash", "-lc", command, cwd=str(job), env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, preexec_fn=_limits(timeout))
            reader = asyncio.gather(_read_capped(proc.stdout), _read_capped(proc.stderr))
            timed_out = False
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            (out, out_trunc), (err, err_trunc) = await reader
            if out_trunc:
                out += "\n... (output truncated)"
            if err_trunc:
                err += "\n... (output truncated)"
            return {"exit_code": proc.returncode if not timed_out else None, "timed_out": timed_out, "stdout": out,
                    "stderr": err + (f"\nCommand timed out after {timeout}s." if timed_out else ""),
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        finally:
            shutil.rmtree(job, ignore_errors=True)


@app.post("/screenshot")
async def screenshot(bundle: UploadFile = File(...), entry: str = Form("index.html"), width: int = Form(1280),
                     height: int = Form(800), x_runner_token: str = Header("")):
    _auth(x_runner_token)
    from playwright.async_api import async_playwright

    data = await bundle.read()
    if len(data) > MAX_BUNDLE:
        raise HTTPException(413, "bundle too large")
    width = max(320, min(int(width), 1920))
    height = max(320, min(int(height), 1600))
    async with SEM:
        JOBS.mkdir(parents=True, exist_ok=True)
        job = Path(tempfile.mkdtemp(prefix="shot-", dir=JOBS))
        try:
            await asyncio.to_thread(_extract, data, job)
            target = (job / entry).resolve()
            if job.resolve() not in target.parents or not target.is_file():
                raise HTTPException(404, f"{entry} not found in the project")
            async with async_playwright() as p:
                browser = await p.chromium.launch(chromium_sandbox=False)
                try:
                    ctx = await browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
                    page = await ctx.new_page()

                    async def guard(route):
                        if route.request.url.startswith("file://"):
                            await route.continue_()
                        else:
                            await route.abort()

                    await page.route("**/*", guard)
                    await page.goto(target.as_uri(), wait_until="load", timeout=20_000)
                    await page.wait_for_timeout(700)
                    png = await page.screenshot(full_page=False, type="png")
                finally:
                    await browser.close()
            return Response(png, media_type="image/png")
        finally:
            shutil.rmtree(job, ignore_errors=True)
