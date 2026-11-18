"""Client for the isolated sandbox runner container (no secrets, no access to
Elvin's database). Projects are sent as a tar.gz, commands run on a throwaway
copy, and screenshots are taken with a network-blocked headless Chromium."""
from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path
from typing import Any

import httpx

from .. import limits
from ..config import get_settings
from .ai.providers import http
from .fs import walk_files


class RunnerError(Exception):
    pass


def make_bundle(root: Path, max_bytes: int = limits.MAX_RUN_UPLOAD_BYTES) -> bytes:
    buf = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=5) as tar:
        for rel, full, st in walk_files(root, max_files=limits.MAX_PROJECT_FILES_TRACKED):
            if st.st_size > 20 * 1024 * 1024:
                continue
            total += st.st_size
            if total > max_bytes:
                raise RunnerError("The project is too large to copy into the sandbox.")
            info = tarfile.TarInfo(rel)
            info.size = st.st_size
            info.mtime = int(st.st_mtime)
            info.mode = 0o755 if st.st_mode & 0o111 else 0o644
            with open(full, "rb") as fh:
                tar.addfile(info, fh)
    return buf.getvalue()


def _headers() -> dict[str, str]:
    return {"X-Runner-Token": get_settings().runner_token}


async def run(root: Path, command: str, timeout: int = limits.RUN_COMMAND_TIMEOUT_S) -> dict[str, Any]:
    bundle = await asyncio.to_thread(make_bundle, root)
    try:
        resp = await http().post(
            f"{get_settings().runner_url}/run", headers=_headers(),
            files={"bundle": ("project.tar.gz", bundle, "application/gzip")},
            data={"command": command, "timeout": str(timeout)},
            timeout=httpx.Timeout(connect=10.0, read=timeout + 60.0, write=60.0, pool=10.0),
        )
    except httpx.HTTPError as exc:
        raise RunnerError("The sandbox runner is not reachable, so the command was not run.") from exc
    if resp.status_code != 200:
        raise RunnerError(f"The sandbox refused the command (HTTP {resp.status_code}): {resp.text[:200]}")
    return resp.json()


async def screenshot(root: Path, entry: str, *, width: int = 1280, height: int = 800) -> bytes:
    bundle = await asyncio.to_thread(make_bundle, root)
    try:
        resp = await http().post(
            f"{get_settings().runner_url}/screenshot", headers=_headers(),
            files={"bundle": ("project.tar.gz", bundle, "application/gzip")},
            data={"entry": entry, "width": str(width), "height": str(height)},
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0),
        )
    except httpx.HTTPError as exc:
        raise RunnerError("The sandbox runner is not reachable, so no screenshot was taken.") from exc
    if resp.status_code != 200 or resp.headers.get("content-type") != "image/png":
        raise RunnerError(f"The sandbox could not render the page (HTTP {resp.status_code}): {resp.text[:200]}")
    return resp.content


async def health() -> tuple[bool, str]:
    try:
        resp = await http().get(f"{get_settings().runner_url}/health", headers=_headers(), timeout=5)
    except httpx.HTTPError:
        return False, "offline"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    return True, f"ready (python {data.get('python', '?')}, node {data.get('node', '?')})"
