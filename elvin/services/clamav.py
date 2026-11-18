"""ClamAV scanning of everything that enters Elvin (uploads, cloned
repositories, images) using clamd's INSTREAM protocol over TCP."""
from __future__ import annotations

import asyncio
import os
import struct
from pathlib import Path
from typing import Any

from ..config import get_settings

CHUNK = 64 * 1024
MAX_SCAN_BYTES = 100 * 1024 * 1024


class ClamUnavailable(Exception):
    pass


async def _connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    s = get_settings()
    try:
        return await asyncio.wait_for(asyncio.open_connection(s.clamav_host, s.clamav_port), timeout=5)
    except (OSError, asyncio.TimeoutError) as exc:
        raise ClamUnavailable(f"ClamAV is not reachable at {s.clamav_host}:{s.clamav_port}") from exc


async def _command(cmd: bytes, timeout: float = 10) -> str:
    reader, writer = await _connect()
    try:
        writer.write(cmd)
        await writer.drain()
        data = await asyncio.wait_for(reader.readuntil(b"\0"), timeout=timeout)
        return data.rstrip(b"\0").decode(errors="replace")
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
        raise ClamUnavailable("ClamAV did not answer") from exc
    finally:
        writer.close()


async def status() -> dict[str, Any]:
    try:
        pong = await _command(b"zPING\0", timeout=5)
        version = await _command(b"zVERSION\0", timeout=5)
        return {"available": pong == "PONG", "version": version}
    except ClamUnavailable as exc:
        return {"available": False, "version": "", "detail": str(exc)}


async def scan_bytes(data: bytes) -> tuple[str, str]:
    """('clean'|'infected'|'error', signature-or-message)."""
    reader, writer = await _connect()
    try:
        writer.write(b"zINSTREAM\0")
        for i in range(0, len(data), CHUNK):
            chunk = data[i:i + CHUNK]
            writer.write(struct.pack("!I", len(chunk)) + chunk)
            await writer.drain()
        writer.write(struct.pack("!I", 0))
        await writer.drain()
        reply = (await asyncio.wait_for(reader.readuntil(b"\0"), timeout=180)).rstrip(b"\0").decode(errors="replace")
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
        raise ClamUnavailable("ClamAV connection failed during the scan") from exc
    finally:
        writer.close()
    if reply.endswith(" OK"):
        return "clean", ""
    if reply.endswith(" FOUND"):
        return "infected", reply.removeprefix("stream: ").removesuffix(" FOUND")
    return "error", reply


async def scan_files(files: list[tuple[str, Path]], *, concurrency: int = 4) -> dict[str, Any]:
    """Scan a list of (display path, absolute path)."""
    result: dict[str, Any] = {"status": "clean", "scanned": 0, "infected": [], "errors": [], "skipped": []}
    sem = asyncio.Semaphore(concurrency)
    unavailable = False

    async def one(display: str, path: Path) -> None:
        nonlocal unavailable
        if unavailable:
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size > MAX_SCAN_BYTES:
            result["skipped"].append(display)
            return
        async with sem:
            try:
                data = await asyncio.to_thread(path.read_bytes)
                verdict, detail = await scan_bytes(data)
            except ClamUnavailable:
                unavailable = True
                return
            except OSError as exc:
                result["errors"].append(f"{display}: {exc}")
                return
        result["scanned"] += 1
        if verdict == "infected":
            result["infected"].append({"path": display, "signature": detail})
        elif verdict == "error":
            result["errors"].append(f"{display}: {detail}")

    await asyncio.gather(*(one(d, p) for d, p in files))
    if unavailable:
        result["status"] = "unavailable"
    elif result["infected"]:
        result["status"] = "infected"
    return result


def list_all_files(root: Path, *, skip_git: bool = True, max_files: int = 50000) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if skip_git:
            dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            full = Path(dirpath) / fn
            if full.is_symlink() or not full.is_file():
                continue
            out.append((full.relative_to(root).as_posix(), full))
            if len(out) >= max_files:
                return out
    return out


async def scan_dir(root: Path) -> dict[str, Any]:
    files = await asyncio.to_thread(list_all_files, root)
    return await scan_files(files)


def must_block(scan: dict[str, Any]) -> str | None:
    """Reason to refuse the incoming files, or None."""
    if scan["status"] == "infected":
        names = ", ".join(f"{i['path']} ({i['signature']})" for i in scan["infected"][:5])
        return f"ClamAV found malware: {names}. Nothing was imported."
    if scan["status"] == "unavailable" and get_settings().clamav_required:
        return ("The ClamAV virus scanner isn't ready yet (on first start it downloads its signature database, "
                "which takes a few minutes). Please try again shortly.")
    return None
