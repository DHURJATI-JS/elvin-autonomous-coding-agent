"""Clone a repository safely.

1. Shallow-clone into a private quarantine folder (no hooks, no submodules, no
   symlinks, size and time limits).
2. Read the README + LICENSE/NOTICE/TERMS files. Known licenses get exact,
   pre-written plain statements; the AI adds terms stated in the README.
3. The user can ask for a plain-language version, must tick every statement to
   agree, and only then is the repository scanned with ClamAV and moved into
   their workspace as a new project."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import shutil
import socket
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select, update

from .. import db as dbm
from .. import limits
from ..config import get_settings
from ..models import CloneRequest, Project, ScanResult, User, utcnow
from . import clamav, gitops, snapshots, tasks, usage
from .ai import prompts
from .ai import router as ai
from .fs import project_root, slugify

log = logging.getLogger("elvin.clone")

LICENSE_TERMS: dict[str, list[tuple[str, str]]] = {
    "MIT": [("permission", "You may use, copy, change, merge, publish, distribute, sublicense and sell copies of the software."),
            ("condition", "You must keep the copyright notice and the MIT license text in all copies or substantial parts."),
            ("limitation", "The software is provided 'as is', without any warranty; the authors are not liable for any damage.")],
    "ISC": [("permission", "You may use, copy, change and distribute the software for any purpose."),
            ("condition", "You must keep the copyright notice and permission notice in all copies."),
            ("limitation", "The software is provided 'as is' without warranty; the authors are not liable for damages.")],
    "Apache-2.0": [("permission", "You may use, change, distribute and sublicense the code, and you receive a patent license from contributors."),
                   ("condition", "You must include the license and any NOTICE file, and mark files you changed."),
                   ("limitation", "You may not use the project's trademarks, and the software comes without warranty or liability."),
                   ("condition", "If you sue anyone claiming the software infringes a patent, your patent license ends.")],
    "GPL-3.0": [("permission", "You may use, study, change and share the software."),
                ("condition", "If you distribute it (or a modified version), you must release the full source code under GPL-3.0 too."),
                ("condition", "You must keep license and copyright notices and state your changes."),
                ("limitation", "The software comes without any warranty.")],
    "GPL-2.0": [("permission", "You may use, change and share the software."),
                ("condition", "If you distribute it (or a modified version), you must provide the source code under GPL-2.0."),
                ("limitation", "The software comes without any warranty.")],
    "AGPL-3.0": [("permission", "You may use, change and share the software."),
                 ("condition", "If you run a modified version as a network service, users of that service must be able to get your source code."),
                 ("condition", "Distributed copies and network-service versions must stay under AGPL-3.0, with the source available."),
                 ("limitation", "The software comes without any warranty.")],
    "LGPL-3.0": [("permission", "You may use the library, including from closed-source programs."),
                 ("condition", "Changes to the library itself must be shared under LGPL-3.0, and users must be able to replace the library."),
                 ("limitation", "The software comes without any warranty.")],
    "LGPL-2.1": [("permission", "You may use the library, including from closed-source programs."),
                 ("condition", "Changes to the library itself must be shared under LGPL-2.1."),
                 ("limitation", "The software comes without any warranty.")],
    "MPL-2.0": [("permission", "You may use, change and distribute the code, also inside larger closed-source works."),
                ("condition", "Files you change that came from this project must stay under MPL-2.0 and their source must be available."),
                ("limitation", "No trademark rights and no warranty.")],
    "BSD-3-Clause": [("permission", "You may use, change and redistribute the code, in source or binary form."),
                     ("condition", "You must keep the copyright notice, the conditions and the disclaimer."),
                     ("restriction", "You may not use the authors' names to endorse your product without permission."),
                     ("limitation", "No warranty; the authors are not liable for damages.")],
    "BSD-2-Clause": [("permission", "You may use, change and redistribute the code, in source or binary form."),
                     ("condition", "You must keep the copyright notice, the conditions and the disclaimer."),
                     ("limitation", "No warranty; the authors are not liable for damages.")],
    "Unlicense": [("permission", "The code is dedicated to the public domain - you may do anything with it."),
                  ("limitation", "It comes without any warranty.")],
    "CC0-1.0": [("permission", "The authors waived their rights (public domain dedication) - you may do anything with it."),
                ("limitation", "It comes without any warranty, and no trademark or patent rights are granted.")],
    "CC-BY-4.0": [("permission", "You may share and adapt the material for any purpose, even commercially."),
                  ("condition", "You must credit the authors, link the license and say if you changed anything.")],
    "CC-BY-SA-4.0": [("permission", "You may share and adapt the material, even commercially."),
                     ("condition", "You must credit the authors and share your adaptations under the same license.")],
    "CC-BY-NC-4.0": [("permission", "You may share and adapt the material."),
                     ("restriction", "Commercial use is NOT allowed."),
                     ("condition", "You must credit the authors.")],
    "BUSL-1.1": [("restriction", "Business Source License: production use is limited by the 'Additional Use Grant' in the license file."),
                 ("notice", "The code becomes open source (the 'Change License') only after the stated 'Change Date'.")],
    "SSPL-1.0": [("restriction", "Server Side Public License: offering the software as a service requires releasing your whole service's source code.")],
}
NO_LICENSE = [("restriction", "No license was found. By default the authors keep all rights: you may read the code, "
                              "but you may not copy, change or share it without their permission.")]


def detect_license(text: str) -> str:
    t = re.sub(r"\s+", " ", text)
    tl = t.lower()
    checks = [
        ("GNU AFFERO GENERAL PUBLIC LICENSE", "AGPL-3.0"), ("GNU LESSER GENERAL PUBLIC LICENSE", None),
        ("GNU GENERAL PUBLIC LICENSE", None), ("Mozilla Public License", "MPL-2.0"), ("Apache License", "Apache-2.0"),
        ("Business Source License", "BUSL-1.1"), ("Server Side Public License", "SSPL-1.0"),
        ("free and unencumbered software released into the public domain", "Unlicense"), ("CC0 1.0 Universal", "CC0-1.0"),
        ("Attribution-NonCommercial", "CC-BY-NC-4.0"), ("Attribution-ShareAlike", "CC-BY-SA-4.0"),
        ("Creative Commons Attribution 4.0", "CC-BY-4.0"), ("ISC License", "ISC"),
        ("Permission to use, copy, modify, and/or distribute this software for any purpose", "ISC"),
        ("Permission is hereby granted, free of charge", "MIT"), ("MIT License", "MIT"),
    ]
    for needle, spdx in checks:
        if needle.lower() in tl:
            if needle == "GNU LESSER GENERAL PUBLIC LICENSE":
                return "LGPL-3.0" if "version 3" in tl else "LGPL-2.1"
            if needle == "GNU GENERAL PUBLIC LICENSE":
                return "GPL-3.0" if "version 3" in tl else "GPL-2.0"
            return spdx or ""
    if "redistribution and use in source and binary forms" in tl:
        return "BSD-3-Clause" if "neither the name" in tl or "endorse or promote" in tl else "BSD-2-Clause"
    return ""


def parse_repo_url(raw: str) -> tuple[str, str, str, str]:
    """-> (clone_url, host, owner, repo). Only public https git hosts."""
    raw = (raw or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", raw):
        raw = f"https://github.com/{raw}"
    if raw.startswith("github.com/"):
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("Use an https:// repository address (e.g. https://github.com/owner/repo) or owner/repo.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Remove credentials, ?query or #fragment from the address.")
    host = parts.hostname.lower()
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 2:
        raise ValueError("The address must include the owner and repository name.")
    owner, repo = segs[0], segs[1].removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo):
        raise ValueError("That repository name looks invalid.")
    if host != "github.com":
        try:
            for info in socket.getaddrinfo(host, 443):
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    raise ValueError("Cloning from private or local network addresses is not allowed.")
        except OSError as exc:
            raise ValueError(f"Can't resolve {host}.") from exc
        path = "/".join(segs).removesuffix(".git")
        return f"https://{host}/{path}.git", host, owner, repo
    return f"https://github.com/{owner}/{repo}.git", host, owner, repo


def quarantine_dir(clone_id: uuid.UUID) -> Path:
    return get_settings().data_dir / "quarantine" / str(clone_id)


def _dir_stats(root: Path) -> tuple[int, int]:
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for fn in filenames:
            files += 1
            try:
                size += (Path(dirpath) / fn).lstat().st_size
            except OSError:
                pass
    return files, size


def _read_terms_files(root: Path) -> tuple[str, str, str]:
    readme = license_text = other = ""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return "", "", ""
    for name in names:
        full = root / name
        if not full.is_file() or full.is_symlink():
            continue
        low = name.lower()
        try:
            if low.startswith("readme") and not readme:
                readme = full.read_text(errors="replace")[:60_000]
            elif low.startswith(("license", "licence", "copying", "unlicense")) and not license_text:
                license_text = full.read_text(errors="replace")[:60_000]
            elif low.startswith(("terms", "notice", "eula", "contributing", "code_of_conduct", "security")):
                other += f"\n\n----- {name} -----\n" + full.read_text(errors="replace")[:8_000]
        except OSError:
            continue
    return readme, license_text, other[:30_000]


async def inspect(clone_id: uuid.UUID, user: User, task_id: uuid.UUID) -> None:
    """Background job: quarantine clone + terms extraction."""
    token = usage.set_context(user_id=user.id, task_id=task_id, feature="clone", effort="low", model_prefs=user.model_prefs or {})
    qdir = quarantine_dir(clone_id)

    async def emit(type_: str, data: dict[str, Any]) -> None:
        await tasks.emit(task_id, user.id, None, type_, {"clone_id": str(clone_id), **data})

    async def fail(msg: str) -> None:
        shutil.rmtree(qdir, ignore_errors=True)
        async with dbm.session() as db:
            await db.execute(update(CloneRequest).where(CloneRequest.id == clone_id).values(status="failed", error=msg[:1000], updated_at=utcnow()))
            await db.commit()
        await tasks.set_status(task_id, "failed", error=msg[:500])
        await emit("clone_status", {"status": "failed", "error": msg})

    try:
        await tasks.set_status(task_id, "running")
        async with dbm.session() as db:
            req = await db.get(CloneRequest, clone_id)
            gh_token = await gitops.token_for(db, user.id)
        tool_info = await gitops.tools()
        if not tool_info.get("git"):
            await fail("Git is not installed in Elvin's container.")
            return
        clone_url, host, owner, repo = parse_repo_url(req.url)
        await emit("clone_status", {"status": "cloning", "text": f"Downloading {owner}/{repo} into quarantine..."})
        qdir.parent.mkdir(parents=True, exist_ok=True)
        args = ["git"]
        env: dict[str, str] = {}
        if gh_token and host == "github.com":
            args += gitops.auth_args()
            env["GH_TOKEN"] = gh_token
        args += ["-c", "core.symlinks=false", "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
                 "-c", "submodule.recurse=false", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                 "clone", "--depth", "1", "--single-branch", "--no-tags", "--quiet", clone_url, str(qdir)]
        clone_task = asyncio.create_task(gitops.run(args, env_extra=env, timeout=limits.CLONE_TIMEOUT_S))
        while not clone_task.done():  # watch the size while cloning
            await asyncio.sleep(2)
            if qdir.exists():
                _, size = await asyncio.to_thread(_dir_stats, qdir)
                if size > limits.CLONE_MAX_BYTES:
                    clone_task.cancel()
                    await fail(f"The repository is larger than {limits.CLONE_MAX_BYTES // (1024 * 1024)} MB.")
                    return
        res = await clone_task
        if not res.ok:
            hint = " If it is private, connect GitHub first." if "not found" in res.err.lower() or "authentication" in res.err.lower() else ""
            await fail(f"git clone failed: {res.err.strip()[-300:]}{hint}")
            return
        files, size = await asyncio.to_thread(_dir_stats, qdir)
        if files > limits.CLONE_MAX_FILES or size > limits.CLONE_MAX_BYTES:
            await fail("The repository is too large to import.")
            return
        sha = (await gitops.run(["git", "-C", str(qdir), "rev-parse", "HEAD"], timeout=15)).out.strip()
        readme, license_text, other = await asyncio.to_thread(_read_terms_files, qdir)
        spdx = detect_license(license_text) if license_text else ""
        if not spdx and readme:
            m = re.search(r"licen[cs]ed? under (?:the )?([A-Za-z0-9.\- ]{2,40}?) licen", readme, re.I)
            spdx = detect_license(m.group(0)) if m else ""
        statements: list[dict[str, Any]] = []
        if spdx:
            base = LICENSE_TERMS.get(spdx, [])
        elif license_text:
            base = [("notice", "A license file exists, but Elvin could not identify it automatically - read it carefully below.")]
        else:
            base = NO_LICENSE
        for kind, text in base:
            statements.append({"id": f"L{len(statements) + 1}", "kind": kind, "text": text, "source": f"license ({spdx or 'unknown'})"})
        summary = ""
        if readme or other:
            await emit("clone_status", {"status": "reading", "text": "Elvin is reading the README for terms and conditions..."})
            try:
                team = user.ai_team or ai.registry().default_team()
                r = await ai.call("utility", team=team, system=prompts.TERMS, task_role="terms", json_mode=True, max_tokens=1500,
                                  messages=[{"role": "user", "content": f"README:\n{readme[:14_000]}\n\nOTHER FILES:\n{other[:6_000]}\n\n"
                                                                        f"LICENSE (first part):\n{license_text[:3_000]}"}])
                for s in (r.data or {}).get("statements", [])[:12]:
                    text = str(s.get("text", "")).strip() if isinstance(s, dict) else ""
                    if text:
                        kind = str(s.get("kind", "notice")).lower()
                        statements.append({"id": f"R{len(statements) + 1}", "kind": kind if kind in ("permission", "condition", "limitation", "restriction", "notice") else "notice",
                                           "text": text[:600], "source": "README (AI summary - check the original)"})
                summary = str((r.data or {}).get("summary", ""))[:2000]
            except ai.AIError as exc:
                statements.append({"id": f"N{len(statements) + 1}", "kind": "notice", "source": "Elvin",
                                   "text": f"Elvin could not summarise the README ({exc}). Read it yourself before agreeing."})
        statements.append({"id": f"E{len(statements) + 1}", "kind": "notice", "source": "Elvin",
                           "text": "You are responsible for following the repository's license and terms; this summary is not legal advice."})
        async with dbm.session() as db:
            await db.execute(update(CloneRequest).where(CloneRequest.id == clone_id).values(
                status="awaiting_agreement", owner=owner, repo=repo, commit_sha=sha[:64], readme=readme[:60_000],
                license_spdx=spdx, license_name=spdx or ("Unknown license" if license_text else "No license"),
                terms=statements, summary=summary, file_count=files, size_bytes=size, updated_at=utcnow()))
            await db.commit()
        await tasks.set_status(task_id, "completed")
        await emit("clone_status", {"status": "awaiting_agreement"})
    except ValueError as exc:
        await fail(str(exc))
    except asyncio.CancelledError:
        await fail("Stopped.")
        raise
    except Exception as exc:  # pragma: no cover
        log.exception("clone inspection failed")
        await fail(f"Clone failed ({exc.__class__.__name__}).")
    finally:
        usage.reset_context(token)


async def simplify(db, req: CloneRequest, user: User) -> list[dict[str, str]]:
    token = usage.set_context(user_id=user.id, feature="clone", effort="low", model_prefs=user.model_prefs or {})
    try:
        items = [{"id": s["id"], "text": s["text"]} for s in req.terms]
        team = user.ai_team or ai.registry().default_team()
        r = await ai.call("utility", team=team, system=prompts.SIMPLIFY, task_role="simplify", json_mode=True, max_tokens=1500,
                          messages=[{"role": "user", "content": json_dumps(items)}])
        by_id = {s["id"] for s in req.terms}
        out = [{"id": str(i.get("id")), "plain": str(i.get("plain", ""))[:600]} for i in (r.data or {}).get("items", [])
               if isinstance(i, dict) and str(i.get("id")) in by_id and i.get("plain")]
    finally:
        usage.reset_context(token)
    req.simplified = out
    req.updated_at = utcnow()
    await db.commit()
    return out


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


async def finalize(clone_id: uuid.UUID, user: User, task_id: uuid.UUID) -> None:
    """After the user agreed: ClamAV scan, then move into the workspace."""
    qdir = quarantine_dir(clone_id)

    async def emit(type_: str, data: dict[str, Any]) -> None:
        await tasks.emit(task_id, user.id, None, type_, {"clone_id": str(clone_id), **data})

    async def set_req(**values: Any) -> None:
        async with dbm.session() as db:
            await db.execute(update(CloneRequest).where(CloneRequest.id == clone_id).values(updated_at=utcnow(), **values))
            await db.commit()

    try:
        await tasks.set_status(task_id, "running")
        await emit("clone_status", {"status": "scanning", "text": "ClamAV is scanning every file..."})
        scan = await clamav.scan_dir(qdir)
        async with dbm.session() as db:
            db.add(ScanResult(user_id=user.id, context="clone", status=scan["status"], scanned=scan["scanned"], infected=scan["infected"][:50]))
            await db.commit()
        reason = clamav.must_block(scan)
        if reason:
            if scan["status"] == "infected":
                shutil.rmtree(qdir, ignore_errors=True)
                await set_req(status="infected", scan=scan, error=reason)
            else:
                await set_req(status="awaiting_agreement", scan=scan, error=reason)
            await tasks.set_status(task_id, "failed", error=reason)
            await emit("clone_status", {"status": "blocked", "error": reason})
            return
        async with dbm.session() as db:
            req = await db.get(CloneRequest, clone_id)
            await db.execute(select(User.id).where(User.id == user.id).with_for_update())
            count = (await db.execute(select(func.count()).select_from(Project).where(Project.owner_id == user.id))).scalar_one()
            if count >= limits.MAX_PROJECTS_PER_USER:
                raise ValueError(f"You already have {limits.MAX_PROJECTS_PER_USER} projects - delete or archive one first.")
            base = slugify(req.repo)
            slug, n = base, 1
            while (await db.execute(select(Project.id).where(Project.owner_id == user.id, Project.slug == slug))).first():
                n += 1
                slug = f"{base}-{n}"
            dest = project_root(user.id, slug)
            dest.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(qdir), str(dest))
            project = Project(owner_id=user.id, name=req.repo[:80], slug=slug, source="clone", repo_url=req.url[:500],
                              description=f"Cloned from {req.owner}/{req.repo} ({req.license_name})"[:500])
            if req.url.startswith("https://github.com/"):
                project.github_repo = f"{req.owner}/{req.repo}"[:200]
            db.add(project)
            await db.commit()
            await snapshots.create(db, project, kind="initial", label=f"Cloned {req.owner}/{req.repo} @ {req.commit_sha[:10]}", created_by="system")
            await db.execute(update(CloneRequest).where(CloneRequest.id == clone_id).values(
                status="completed", project_id=project.id, scan=scan, updated_at=utcnow()))
            await db.commit()
            project_id = project.id
        await tasks.set_status(task_id, "completed")
        await emit("clone_status", {"status": "completed", "project_id": str(project_id)})
    except ValueError as exc:
        await set_req(status="awaiting_agreement", error=str(exc))
        await tasks.set_status(task_id, "failed", error=str(exc))
        await emit("clone_status", {"status": "blocked", "error": str(exc)})
    except Exception as exc:  # pragma: no cover
        log.exception("clone finalize failed")
        await set_req(status="failed", error=f"Import failed ({exc.__class__.__name__}).")
        await tasks.set_status(task_id, "failed", error=str(exc)[:500])
        await emit("clone_status", {"status": "failed", "error": "Import failed."})


def cleanup_quarantine(max_age_s: int = 24 * 3600) -> int:
    import time

    root = get_settings().data_dir / "quarantine"
    removed = 0
    if root.is_dir():
        for d in root.iterdir():
            try:
                if time.time() - d.stat().st_mtime > max_age_s:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    return removed
