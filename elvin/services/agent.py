"""Elvin - the autonomous coding agent.

Flow of one task (kind "code"):
  plan (planner model) -> library safety gate (OSV) -> for each step:
  write code (coder model) -> validate (folder boundary, sizes, vulnerable
  dependencies, dangerous commands) -> PERMISSION DIALOG -> apply through the
  write-ahead journal -> autosave a timestamped, verified snapshot -> run the
  approved commands in the sandbox -> fix failures (again with permission).

Kind "design": an uploaded image is described by a vision model, the coder
builds the page, the sandbox renders it in network-blocked Chromium and the
vision model compares the screenshot with the target, iterating.

Stop (or the 90-second absence timer) cancels the task and puts every file
back exactly as it was when the task began; Retry recreates the task from that
same starting point."""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select, update

from .. import db as dbm
from .. import limits
from ..config import get_settings
from ..models import ApprovalBatch, Attachment, CommandRun, Message, Project, Snapshot, Task, User, utcnow
from . import journal, memory, runner_client, selfcheck, snapshots, tasks, usage, vulns
from .ai import blocks, prompts
from .ai import router as ai
from .ai.providers import ImageInput
from .fs import PathError, is_text, normalize_rel, project_root, resolve_in, walk_files

log = logging.getLogger("elvin.agent")

BLOCKED_COMMANDS = [
    (re.compile(r"\bsudo\b"), "sudo is not allowed"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(/|~|\$HOME|/\*)(\s|$)"), "it would delete system or home folders"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "it is a fork bomb"),
    (re.compile(r"\bmkfs\b|\bdd\s+[^|;]*of=/dev/"), "it writes to raw disks"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "it is a power command"),
]
WARN_COMMANDS = [
    (re.compile(r"\b(curl|wget)\b[^|;]*\|\s*(ba|z)?sh\b"), "downloads a script and runs it"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r"), "deletes folders"),
    (re.compile(r"\b(chmod|chown)\s+-R\b"), "changes permissions recursively"),
    (re.compile(r"\bgit\s+push\b"), "pushes code - use Elvin's Git page instead"),
    (re.compile(r"\b(nc|ncat|netcat|nmap)\b"), "opens raw network connections"),
    (re.compile(r"\b(printenv|env)\b"), "prints environment variables"),
    (re.compile(r"\b(pip3?|npm|yarn|pnpm)\b.*\b(install|add|i)\b"), "installs packages (checked against OSV.dev)"),
]
LOADTEST_WORDS = re.compile(r"\b(stress[- ]?test|load[- ]?test|locust|benchmark (my|the) (app|api|site|server))", re.I)
MANIFEST_RE = re.compile(r"(^|/)(requirements[^/]*\.(txt|in)|pyproject\.toml|package\.json|Pipfile)$")

# Approval waiters: batch id -> (future, {idx: sha256})
_waiters: dict[uuid.UUID, tuple[asyncio.Future, dict[int, str]]] = {}


class AgentStop(Exception):
    """The user ended the task from the permission dialog (or never answered)."""


FORMAT_REMINDER = ("Your answer had code but no '=== FILE: path ===' blocks. Send every file in that format: "
                   "=== FILE: path ===, the complete content, then === END FILE ===.")


@dataclass
class Action:
    idx: int
    type: str  # write | delete | mkdir | run
    path: str = ""
    content: bytes | None = None
    command: str = ""
    is_new: bool = False
    diff: str = ""
    added: int = 0
    removed: int = 0
    warnings: list[str] = field(default_factory=list)
    sha256: str = ""
    public_extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {"idx": self.idx, "type": self.type, "path": self.path, "command": self.command, "is_new": self.is_new,
                "bytes": len(self.content or b""), "diff": self.diff[:60_000], "diff_truncated": len(self.diff) > 60_000,
                "added": self.added, "removed": self.removed, "warnings": self.warnings, "sha256": self.sha256,
                **self.public_extra}


def resolve_decision(batch_id: uuid.UUID, decision: dict[str, Any]) -> bool:
    waiter = _waiters.get(batch_id)
    if not waiter or waiter[0].done():
        return False
    waiter[0].set_result(decision)
    return True


def has_waiter(batch_id: uuid.UUID) -> bool:
    return batch_id in _waiters and not _waiters[batch_id][0].done()


def _file_listing(root: Path, limit: int = 300) -> str:
    lines = []
    for rel, _, st in walk_files(root, max_files=limit + 1):
        lines.append(f"{rel} ({st.st_size} B)")
    if len(lines) > limit:
        lines = lines[:limit] + ["... (more files not shown)"]
    return "\n".join(lines) or "(the project is empty)"


def _read_files(root: Path, paths: list[str], budget: int) -> dict[str, str]:
    out: dict[str, str] = {}
    used = 0
    for raw in paths[:12]:
        try:
            rel = normalize_rel(str(raw))
            full = resolve_in(root, rel)
        except PathError:
            continue
        if not full.is_file():
            continue
        try:
            data = full.read_bytes()
        except OSError:
            continue
        if not is_text(data):
            continue
        text = data.decode("utf-8", errors="replace")
        if used + len(text) > budget:
            text = text[: max(0, budget - used)] + "\n... (truncated)"
        out[rel] = text
        used += len(text)
        if used >= budget:
            break
    return out


def _diff(old: str | None, new: str | None, path: str) -> tuple[str, int, int]:
    a = (old or "").splitlines(keepends=True)
    b = (new or "").splitlines(keepends=True)
    lines = list(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=3))
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return "".join(lines), added, removed


class Agent:
    def __init__(self, task: Task, project: Project, user: User) -> None:
        self.task_id = task.id
        self.user_id = user.id
        self.project_id = project.id
        self.project_name = project.name
        self.kind = task.kind
        self.prompt = task.prompt
        self.team = task.team or ai.registry().default_team()
        self.params = dict(task.params or {})
        self.effort = user.ai_effort if user.ai_effort in usage.EFFORTS else "medium"
        self.model_prefs = dict(user.model_prefs or {})
        self.versioning = bool(user.versioning_enabled)
        self.root = project_root(project.owner_id, project.slug)
        self.jroot = journal.journal_dir_for(get_settings().data_dir, project.id)
        self.applied = 0
        self.changed: dict[str, str] = {}  # path -> what happened
        self.command_log: list[dict[str, Any]] = []
        self.lib_verdicts: dict[tuple[str, str], dict[str, Any]] = {}
        self.checkpoint_id: uuid.UUID | None = task.start_snapshot_id
        self.last_snapshot_id: uuid.UUID | None = None
        self.stop_note = ""
        self.files_ctx: dict[str, str] = {}
        self.local_fallback = False

    # ------------------------------------------------------------------ plumbing
    async def emit(self, type_: str, data: dict[str, Any] | None = None, *, persist: bool = True) -> None:
        await tasks.emit(self.task_id, self.user_id, self.project_id, type_, data, persist=persist)

    def _on_attempt(self, stage: str):
        async def cb(key: str, label: str) -> None:
            if key in ai.LOCAL_DOCKER and not self.local_fallback:
                await self._fallback_alert(key)
            await self.emit("model", {"stage": stage, "provider": key, "label": label})
        return cb

    async def _fallback_alert(self, key: str = ai.LOCAL_FALLBACK) -> None:
        """A local CPU model in Docker answers (gateway down / switched off, or no faster Qwen): cap the effort and say so."""
        self.local_fallback = True
        capped = ai.capped_effort(key, self.effort)
        ctx = usage.current()
        if key == ai.LOCAL_QWEN:
            why = ("Qwen Coder on this computer (or in the cloud) isn't available right now, so Elvin is writing code with "
                   "the local Qwen Coder running in Docker on this computer")
        elif ai.gemma_source() == "local":
            why = ("Elvin is set to use the local Gemma 3 running in Docker on this computer for now (the gateway is switched "
                   "off on the System Status page)")
        else:
            why = "The Gemma gateway isn't answering, so Elvin switched to the local Gemma 3 running in Docker on this computer"
        if capped != self.effort:
            text = (f"{why}. It is slower, so effort was reduced from {self.effort} to {capped} for this task "
                    f"(fewer fix attempts and review passes).")
            self.effort = capped
            if ctx:
                ctx.effort = capped
        else:
            text = (f"{why}. It is slower; effort stays at {self.effort} (the local model never runs above "
                    f"{get_settings().local_gemma_effort_cap}).")
        await self.emit("alert", {"level": "warn", "text": text, "provider": key, "effort": self.effort})

    @property
    def fix_attempts(self) -> int:
        return 1 if self.effort == "low" else limits.MAX_FIX_ATTEMPTS

    def _context_budget(self, role: str) -> int:
        reg = ai.registry()
        chain = reg.chain(self.team, role)
        ctx = min((reg.providers[k].context for k in chain[:1]), default=8192)
        return max(4000, min(int(ctx * 2.6) - 14_000, 90_000))

    async def _say(self, text: str, meta: dict[str, Any] | None = None) -> None:
        async with dbm.session() as db:
            db.add(Message(project_id=self.project_id, user_id=self.user_id, role="elvin", content=text[:20_000],
                           task_id=self.task_id, meta=meta or {}))
            await db.commit()
        await self.emit("message", {"role": "elvin", "content": text[:20_000], "meta": meta or {}})

    # ------------------------------------------------------------------ entry point
    async def run(self) -> None:
        token = usage.set_context(user_id=self.user_id, project_id=self.project_id, task_id=self.task_id,
                                  feature="design" if self.kind == "design" else "elvin", effort=self.effort,
                                  model_prefs=self.model_prefs)
        try:
            await tasks.set_status(self.task_id, "running")
            await self.emit("status", {"stage": "starting", "text": "Elvin is reading your request..."})
            if self.params.get("restore_from"):
                await self._restore_retry_start(uuid.UUID(self.params["restore_from"]))
            if self.kind == "design":
                await self._design()
            else:
                await self._code()
            await self._finish()
        except asyncio.CancelledError:
            reason = tasks.manager.reason(self.task_id) or "Stopped."
            await asyncio.shield(self._rollback(reason))
            raise
        except AgentStop as exc:
            self.stop_note = str(exc)
            await self._finish()
        except (ai.AIError, runner_client.RunnerError, PathError, RuntimeError) as exc:
            await self._fail(str(exc))
        except Exception as exc:  # pragma: no cover - last-resort safety net
            log.exception("agent task %s failed", self.task_id)
            await self._fail(f"Unexpected error ({exc.__class__.__name__}).")
        finally:
            usage.reset_context(token)
            await self._expire_batches()

    async def _restore_retry_start(self, snap_id: uuid.UUID) -> None:
        """Retry: begin again from the point where the original task began."""
        async with dbm.session() as db:
            snap = await db.get(Snapshot, snap_id)
            project = await db.get(Project, self.project_id)
            if snap is None or project is None or snap.project_id != self.project_id:
                return
            if self.versioning:
                await snapshots.create(db, project, kind="partial", label="State before retry", created_by="system", task_id=self.task_id)
            res = await snapshots.restore(db, project, snap, label="Back to where the original task began (retry)",
                                          created_by="system", hidden=not self.versioning)
        if res.get("ok") and res.get("changed"):
            await self.emit("rolled_back", {"text": f"Restored {res['changed']} file(s) to where the original task began."})

    async def _ensure_checkpoint(self) -> None:
        if self.checkpoint_id is not None:
            return
        async with dbm.session() as db:
            project = await db.get(Project, self.project_id)
            snap = await snapshots.create(db, project, kind="task_start", label=f"Before: {self.prompt[:120]}",
                                          created_by="system", task_id=self.task_id, hidden=not self.versioning)
            await db.execute(update(Task).where(Task.id == self.task_id).values(start_snapshot_id=snap.id))
            await db.commit()
            self.checkpoint_id = snap.id

    # ------------------------------------------------------------------ outcomes
    async def _finish(self) -> None:
        end_id = None
        verified = None
        if self.applied and self.versioning:
            async with dbm.session() as db:
                project = await db.get(Project, self.project_id)
                snap = await snapshots.create(db, project, kind="task_end", label=f"Task finished: {self.prompt[:120]}",
                                              created_by="elvin", task_id=self.task_id)
                report = await snapshots.verify(db, snap)
                end_id, verified = snap.id, report["ok"]
            await self.emit("verified", {"snapshot_id": str(end_id), "ok": verified,
                                         "text": "Final version timestamped and verified." if verified else "Verification found a problem - see History."})
        await tasks.set_status(self.task_id, "completed", end_snapshot_id=end_id,
                               result={"changed": self.changed, "commands": self.command_log[-10:], "note": self.stop_note})
        await self.emit("done", {"status": "completed", "changed": list(self.changed), "note": self.stop_note})

    async def _fail(self, message: str) -> None:
        kept = (" Steps you already approved are kept - each one was saved as a timestamped version. "
                "Retry starts the task again from where it originally began.") if self.applied else " No files were changed."
        await tasks.set_status(self.task_id, "failed", error=message[:2000])
        await self._say(f"I couldn't finish this task: {message}.{kept}", {"status": "failed"})
        await self.emit("done", {"status": "failed", "error": message[:2000]})

    async def _rollback(self, reason: str) -> None:
        try:
            if self.applied and self.checkpoint_id:
                async with dbm.session() as db:
                    project = await db.get(Project, self.project_id)
                    snap = await db.get(Snapshot, self.checkpoint_id)
                    if self.versioning:
                        await snapshots.create(db, project, kind="partial", label=f"Partial work (stopped): {self.prompt[:100]}",
                                               created_by="elvin", task_id=self.task_id)
                    await snapshots.restore(db, project, snap, label="Rolled back to before the stopped task",
                                            created_by="system", hidden=not self.versioning)
                text = (f"{reason} I put every file back exactly as it was before this task began"
                        f"{' (the partial work is saved in History)' if self.versioning else ''}. Use Retry to start over.")
            else:
                text = f"{reason} No files were changed."
            await tasks.set_status(self.task_id, "cancelled", error=reason[:500])
            await self._say(text, {"status": "cancelled"})
            await self.emit("done", {"status": "cancelled", "reason": reason})
        except Exception:  # pragma: no cover
            log.exception("rollback for task %s failed", self.task_id)
            await tasks.set_status(self.task_id, "cancelled", error=reason[:500])

    async def _expire_batches(self) -> None:
        async with dbm.session() as db:
            await db.execute(update(ApprovalBatch).where(ApprovalBatch.task_id == self.task_id, ApprovalBatch.status == "pending")
                             .values(status="expired"))
            await db.commit()

    # ------------------------------------------------------------------ context
    async def _history(self) -> str:
        async with dbm.session() as db:
            rows = (await db.execute(select(Message).where(Message.project_id == self.project_id, Message.task_id != self.task_id)
                                     .order_by(Message.id.desc()).limit(6))).scalars().all()
        return "\n".join(f"{m.role}: {m.content[:1200]}" for m in reversed(rows))

    async def _base_context(self) -> str:
        mem = await memory.render_for(self.project_id)
        listing = await asyncio.to_thread(_file_listing, self.root)
        ctx = (f"PROJECT: {self.project_name}\n{prompts.platform_note()}\n\n"
               f"PROJECT MEMORY (things the user told you before):\n{mem or '(none yet)'}\n\nFILES:\n{listing}")
        last_run = await self._last_user_run()
        return ctx + (f"\n\n{last_run}" if last_run else "")

    async def _last_user_run(self) -> str:
        """The user's most recent sandbox run (editor or chat), so Elvin can check and fix it."""
        import datetime as _dt

        async with dbm.session() as db:
            run = (await db.execute(select(CommandRun).where(CommandRun.project_id == self.project_id)
                                    .order_by(CommandRun.id.desc()).limit(1))).scalar_one_or_none()
        if run is None or utcnow() - run.created_at > _dt.timedelta(hours=2):
            return ""
        status = "timed out" if run.timed_out else f"exit code {run.exit_code}"
        return (f"LATEST COMMAND THE USER RAN IN THE SANDBOX: `{run.command}` ({status}). OUTPUT (last part):\n"
                f"{run.output[-3000:] or '(no output)'}")

    def _libraries_text(self) -> str:
        ok, bad = [], []
        for v in self.lib_verdicts.values():
            if v["verdict"] == "blocked":
                bad.append(f"- {v['name']} ({v['ecosystem']}): BLOCKED - {v['reason']}"
                           + (f" Use instead: {v['recommended']}" if v.get("recommended") else ""))
            else:
                ok.append(f"- {v.get('recommended') or v['name']} ({v['ecosystem']})")
        return ("APPROVED LIBRARIES:\n" + ("\n".join(ok) or "(none - use only the standard library or what the project already has)")
                + ("\nBLOCKED LIBRARIES (never use these):\n" + "\n".join(bad) if bad else ""))

    def _files_text(self) -> str:
        if not self.files_ctx:
            return "EXISTING FILE CONTENTS: (none needed)"
        return "EXISTING FILE CONTENTS:\n" + "\n".join(f"----- {p} -----\n{t}" for p, t in self.files_ctx.items())

    # ------------------------------------------------------------------ library gate
    async def _gate(self, ecosystem: str, name: str, version: str = "") -> dict[str, Any]:
        eco = vulns.norm_ecosystem(ecosystem)
        key = (eco, vulns.norm_name(eco, name))
        if key in self.lib_verdicts:
            return self.lib_verdicts[key]
        if len(self.lib_verdicts) >= limits.MAX_LIBRARY_CHECKS_PER_TASK:
            return {"ecosystem": eco, "name": name, "verdict": "blocked", "reason": "Too many libraries in one task.", "recommended": ""}
        await self.emit("status", {"stage": "checking", "text": f"Checking {name} with the vulnerability detector..."}, persist=False)
        async with dbm.session() as db:
            verdict = await vulns.check_library(db, ecosystem=eco, name=name, version=version, project_id=self.project_id,
                                                task_id=self.task_id, team=self.team)
        self.lib_verdicts[key] = verdict
        await self.emit("library", {k: verdict.get(k) for k in ("ecosystem", "name", "version", "verdict", "reason", "recommended", "alternatives")})
        return verdict

    # ------------------------------------------------------------------ proposals
    async def _build_actions(self, changes: blocks.ParsedChanges, finish: str, prefix: str = "") -> tuple[list[Action], list[str]]:
        actions: list[Action] = []
        problems: list[str] = []
        if prefix:  # design mode: keep generated files inside the chosen folder
            def inside(p: str) -> str:
                p = p.strip().lstrip("./")
                return p if p == prefix or p.startswith(prefix + "/") else f"{prefix}/{p}"
            changes.files = [(inside(p), c) for p, c in changes.files]
            changes.incomplete = [(inside(p), c) for p, c in changes.incomplete]
            changes.deletes = [inside(p) for p in changes.deletes]
            changes.mkdirs = [inside(p) for p in changes.mkdirs]
        files = list(changes.files)
        for path, content in changes.incomplete:
            if finish == "stop":
                files.append((path, content))  # the model ended normally but forgot the END marker
            else:
                problems.append(f"Your output was cut off while writing '{path}'. Send that file again, complete.")

        def next_idx() -> int:
            return len(actions)

        for path, content in files:
            try:
                rel = normalize_rel(path)
                target = resolve_in(self.root, rel, for_write=True)
            except PathError as exc:
                problems.append(f"Write to '{path}' was blocked: {exc}")
                await self.emit("blocked", {"path": path, "reason": str(exc)})
                continue
            data = content.encode("utf-8")
            if len(data) > limits.MAX_AI_FILE_BYTES:
                problems.append(f"'{rel}' is larger than {limits.MAX_AI_FILE_BYTES // 1024} KB - split it into smaller files.")
                continue
            old = await asyncio.to_thread(lambda: target.read_bytes() if target.is_file() else None)
            if target.is_dir():
                problems.append(f"'{rel}' is a folder, not a file.")
                continue
            if old == data:
                continue
            if old is not None and not is_text(old):
                problems.append(f"'{rel}' is a binary file; Elvin won't overwrite it.")
                continue
            old_text = old.decode("utf-8", errors="replace") if old is not None else None
            diff, added, removed = _diff(old_text, content, rel)
            act = Action(next_idx(), "write", rel, data, is_new=old is None, diff=diff, added=added, removed=removed,
                         sha256=hashlib.sha256(data).hexdigest())
            broken = selfcheck.syntax_problem(rel, content)
            if broken:   # one automatic repair round; if it still doesn't parse, the dialog says so
                problems.append(broken)
                act.warnings.append(broken.split(". Send only")[0])
            if MANIFEST_RE.search(rel):
                for dep in vulns.deps_added(rel, old_text, content)[:10]:
                    verdict = await self._gate(dep["ecosystem"], dep["name"], dep["version"])
                    if verdict["verdict"] == "blocked":
                        problems.append(f"Dependency {dep['name']} in {rel} is blocked: {verdict['reason']}")
                        act = None
                        break
                    if verdict["verdict"] == "go_with_version":
                        act.warnings.append(f"{dep['name']}: use {verdict['recommended']} (older versions are vulnerable)")
            if act is not None:
                actions.append(act)
        for path in changes.deletes:
            try:
                rel = normalize_rel(path)
                target = resolve_in(self.root, rel, for_write=True)
            except PathError as exc:
                problems.append(f"Delete of '{path}' was blocked: {exc}")
                continue
            if not target.is_file():
                continue
            old = await asyncio.to_thread(target.read_bytes)
            diff, added, removed = _diff(old.decode("utf-8", errors="replace") if is_text(old) else "<binary>\n", "", rel)
            actions.append(Action(next_idx(), "delete", rel, diff=diff, added=added, removed=removed, warnings=["deletes this file"]))
        for path in changes.mkdirs:
            try:
                rel = normalize_rel(path)
                target = resolve_in(self.root, rel, for_write=True)
            except PathError as exc:
                problems.append(f"Folder '{path}' was blocked: {exc}")
                continue
            if not target.exists():
                actions.append(Action(next_idx(), "mkdir", rel))
        for eco, name, ver in changes.libraries:
            verdict = await self._gate(eco, name, ver)
            if verdict["verdict"] == "blocked":
                problems.append(f"Library {name} is blocked: {verdict['reason']}")
        for cmd in changes.commands[:5]:
            cmd = cmd.strip()
            if not cmd or len(cmd) > 500:
                continue
            reasons = [why for rx, why in BLOCKED_COMMANDS if rx.search(cmd)]
            if reasons:
                problems.append(f"Command '{cmd}' is not allowed: {reasons[0]}.")
                continue
            blocked_pkg = None
            for eco, name, ver in vulns.packages_in_command(cmd):
                verdict = await self._gate(eco, name, ver)
                if verdict["verdict"] == "blocked":
                    blocked_pkg = f"Command '{cmd}' installs {name}, which is blocked: {verdict['reason']}"
                    break
            if blocked_pkg:
                problems.append(blocked_pkg)
                continue
            actions.append(Action(next_idx(), "run", command=cmd, warnings=[why for rx, why in WARN_COMMANDS if rx.search(cmd)]))
        if len(actions) > limits.MAX_ACTIONS_PER_STEP:
            problems.append(f"Too many changes in one step (max {limits.MAX_ACTIONS_PER_STEP}); only the first ones are shown.")
            actions = actions[: limits.MAX_ACTIONS_PER_STEP]
        return actions, problems

    async def _generate(self, system: str, message: str, *, task_role: str, stage: str, role: str = "coder",
                        images: list[ImageInput] | None = None, prefix: str = "") -> tuple[list[Action], list[str], str]:
        await self.emit("status", {"stage": stage, "text": f"Elvin is writing code ({stage})..."}, persist=False)
        res = await ai.call(role, team=self.team, system=system, task_role=task_role, max_tokens=5000,
                            messages=[{"role": "user", "content": message}], images=images,
                            on_attempt=self._on_attempt(stage))
        changes = blocks.parse(res.text)
        actions, problems = await self._build_actions(changes, res.finish, prefix)
        if not actions and not problems and changes.empty and not changes.incomplete and "```" in res.text:
            problems.append(FORMAT_REMINDER)   # code in markdown fences instead of FILE blocks - a common small-model slip
        if problems:
            await self.emit("status", {"stage": stage, "text": "Fixing problems in Elvin's proposal..."}, persist=False)
            retry_msg = (message + "\n\nYOUR PREVIOUS ANSWER HAD PROBLEMS - fix them and send the complete answer again:\n- "
                         + "\n- ".join(problems) + "\n\n" + self._libraries_text())
            res = await ai.call(role, team=self.team, system=system, task_role=task_role, max_tokens=5000,
                                messages=[{"role": "user", "content": retry_msg}], images=images,
                                on_attempt=self._on_attempt(stage))
            changes = blocks.parse(res.text)
            actions, problems = await self._build_actions(changes, res.finish, prefix)
            for p in problems:
                await self.emit("warning", {"text": p})
        return actions, problems, changes.summary

    # ------------------------------------------------------------------ permission
    async def _approve(self, step_no: int, title: str, actions: list[Action]) -> list[Action]:
        batch_id = uuid.uuid4()
        public = [a.public() for a in actions]
        async with dbm.session() as db:
            db.add(ApprovalBatch(id=batch_id, task_id=self.task_id, user_id=self.user_id, project_id=self.project_id,
                                 step_no=step_no, title=title[:200], actions=public))
            await db.commit()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        _waiters[batch_id] = (fut, {a.idx: a.sha256 for a in actions})
        await tasks.set_status(self.task_id, "awaiting_approval")
        await self.emit("approval_required", {"batch_id": str(batch_id), "step": step_no, "title": title, "actions": public,
                                              "versioning": self.versioning})
        try:
            decision = await asyncio.wait_for(fut, timeout=limits.APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise AgentStop("Nobody answered the permission request within 30 minutes, so Elvin stopped.") from exc
        finally:
            _waiters.pop(batch_id, None)
        await tasks.set_status(self.task_id, "running")
        choice = decision.get("decision")
        approved_idx = set(decision.get("approved") or [])
        await self.emit("approval_decided", {"batch_id": str(batch_id), "decision": choice, "approved": sorted(approved_idx)})
        if choice == "stop":
            raise AgentStop(f"You stopped Elvin at step {step_no}. Changes approved before this point are kept.")
        if choice != "approve":
            return []
        return [a for a in actions if a.idx in approved_idx]

    # ------------------------------------------------------------------ apply
    async def _apply(self, step_no: int, title: str, actions: list[Action]) -> None:
        file_actions = [a for a in actions if a.type in ("write", "delete", "mkdir")]
        if not file_actions:
            return
        await self._ensure_checkpoint()
        ops = [journal.Op(a.type, a.path, a.content) for a in file_actions]
        await asyncio.to_thread(journal.apply_ops, self.root, self.jroot, ops)
        self.applied += len(ops)
        for a in file_actions:
            self.changed[a.path] = {"write": "created" if a.is_new else "updated", "delete": "deleted", "mkdir": "folder"}[a.type]
            if a.type == "write" and a.content is not None and is_text(a.content):
                self.files_ctx[a.path] = a.content.decode("utf-8", errors="replace")[:40_000]
            elif a.type == "delete":
                self.files_ctx.pop(a.path, None)
        await self.emit("applied", {"step": step_no, "files": [{"path": a.path, "type": a.type} for a in file_actions]})
        async with dbm.session() as db:
            project = await db.get(Project, self.project_id)
            if self.versioning:
                snap = await snapshots.create(db, project, kind="step", label=f"Step {step_no}: {title}"[:200],
                                              created_by="elvin", task_id=self.task_id)
                report = await snapshots.verify(db, snap)
                self.last_snapshot_id = snap.id
                await self.emit("autosaved", {"snapshot_id": str(snap.id), "created_at": snap.created_at.isoformat(),
                                              "verified": report["ok"], "files": snap.file_count})
            else:
                await snapshots.refresh_index(db, project)
                await db.commit()

    async def _run_commands(self, actions: list[Action], step_message: str) -> None:
        for act in [a for a in actions if a.type == "run"]:
            command = act.command
            for attempt in range(self.fix_attempts + 1):
                await self.emit("status", {"stage": "running", "text": f"Running `{command}` in the sandbox..."}, persist=False)
                result = await runner_client.run(self.root, command)
                ok = result.get("exit_code") == 0 and not result.get("timed_out")
                entry = {"command": command, "exit_code": result.get("exit_code"), "timed_out": result.get("timed_out", False),
                         "stdout": (result.get("stdout") or "")[-limits.RUN_OUTPUT_BYTES:],
                         "stderr": (result.get("stderr") or "")[-limits.RUN_OUTPUT_BYTES:],
                         "duration_ms": result.get("duration_ms"), "ok": ok, "attempt": attempt + 1}
                self.command_log.append({k: entry[k] for k in ("command", "exit_code", "ok", "attempt")})
                await self.emit("command", entry)
                if ok or attempt >= self.fix_attempts:
                    break
                output = (entry["stdout"][-6000:] + "\n" + entry["stderr"][-6000:]).strip()
                fix_msg = (f"{step_message}\n\nCOMMAND: {command}\nEXIT CODE: {entry['exit_code']}\nOUTPUT:\n{output}\n\n"
                           f"{self._files_text()}")
                fix_actions, _, _ = await self._generate(prompts.FIXER, fix_msg, task_role="fixer", stage=f"fix attempt {attempt + 1}")
                fix_actions = [a for a in fix_actions if a.type != "run"]  # re-run the original command
                if not fix_actions:
                    break
                approved = await self._approve(0, f"Fix for failing command: {command}", fix_actions)
                if not approved:
                    break
                await self._apply(0, f"Fix: {command}", approved)

    # ------------------------------------------------------------------ code task
    async def _code(self) -> None:
        base = await self._base_context()
        history = await self._history()
        planner_msg = f"{base}\n\nRECENT CONVERSATION:\n{history or '(none)'}\n\nUSER REQUEST:\n{self.prompt}"
        await self.emit("status", {"stage": "planning", "text": "Elvin is planning..."}, persist=False)
        res = await ai.call("planner", team=self.team, system=prompts.PLANNER, task_role="planner", json_mode=True,
                            max_tokens=1500, messages=[{"role": "user", "content": planner_msg}],
                            on_attempt=self._on_attempt("planning"))
        plan = self._normalize_plan(res.data)
        asyncio.create_task(self._remember(), name=f"memory-{self.task_id}")
        if plan["intent"] == "loadtest" or LOADTEST_WORDS.search(self.prompt):
            await self._loadtest(plan, res.data if isinstance(res.data, dict) else {})
            return
        await self.emit("plan", {"reply": plan["reply"], "steps": plan["steps"], "needs_changes": plan["needs_changes"],
                                 "libraries": plan["libraries"], "provider": res.provider})
        if not plan["needs_changes"] or not plan["steps"]:
            await self._say(plan["reply"] or "Done.", {"kind": "answer"})
            return
        for lib in plan["libraries"][:limits.MAX_LIBRARY_CHECKS_PER_TASK]:
            await self._gate(lib["ecosystem"], lib["name"], lib.get("version", ""))
        self.files_ctx = await asyncio.to_thread(_read_files, self.root, plan["files_to_read"], self._context_budget("coder") // 2)
        steps = plan["steps"][: limits.MAX_STEPS_PER_TASK]
        outline = "\n".join(f"{i}. {s['title']}" for i, s in enumerate(steps, 1))
        summaries: list[str] = []
        for i, step in enumerate(steps, 1):
            await self.emit("step", {"no": i, "total": len(steps), "title": step["title"], "state": "coding"})
            step_message = (f"{base}\n\nUSER REQUEST:\n{self.prompt}\n\nPLAN:\n{outline}\n\n"
                            f"CURRENT STEP {i}/{len(steps)}: {step['title']}\n{step.get('detail', '')}\n\n{self._libraries_text()}")
            actions, _problems, summary = await self._generate(prompts.CODER, f"{step_message}\n\n{self._files_text()}",
                                                               task_role="coder", stage=f"step {i}")
            if not actions:
                await self.emit("step", {"no": i, "total": len(steps), "title": step["title"], "state": "nothing_to_do"})
                continue
            approved = await self._approve(i, step["title"], actions)
            if not approved:
                await self.emit("step", {"no": i, "total": len(steps), "title": step["title"], "state": "rejected"})
                continue
            await self._apply(i, step["title"], approved)
            await self._run_commands(approved, step_message)
            if summary:
                summaries.append(summary)
            await self.emit("step", {"no": i, "total": len(steps), "title": step["title"], "state": "done"})
        if self.effort in ("high", "max") and self.changed:
            await self._review()
        await self._summarize(plan["reply"], summaries)

    def _normalize_plan(self, data: Any) -> dict[str, Any]:
        data = data if isinstance(data, dict) else {}
        steps = []
        for s in data.get("steps") or []:
            if isinstance(s, dict) and str(s.get("title", "")).strip():
                steps.append({"title": str(s["title"])[:200], "detail": str(s.get("detail", ""))[:1500]})
            elif isinstance(s, str) and s.strip():
                steps.append({"title": s[:200], "detail": ""})
        libs = []
        for lib in data.get("libraries") or []:
            if isinstance(lib, dict) and lib.get("name"):
                libs.append({"ecosystem": str(lib.get("ecosystem") or "PyPI"), "name": str(lib["name"])[:214],
                             "version": str(lib.get("version") or "")[:64]})
        files = [str(f) for f in (data.get("files_to_read") or []) if isinstance(f, str)][:8]
        needs = data.get("needs_changes")
        needs = bool(steps) if needs is None else bool(needs)
        intent = str(data.get("intent") or "").lower()
        return {"reply": str(data.get("reply") or "")[:6000], "steps": steps, "libraries": libs, "files_to_read": files,
                "needs_changes": needs, "intent": intent if intent in ("code", "answer", "loadtest") else ""}

    async def _loadtest(self, plan: dict[str, Any], raw_plan: dict[str, Any]) -> None:
        """'Stress test my app' -> a Locust run launched through Elvin (after permission)."""
        from . import loadtest

        lt = raw_plan.get("loadtest") if isinstance(raw_plan.get("loadtest"), dict) else {}
        url = re.search(r"https?://[^\s'\"<>)]+", self.prompt)
        host = (url.group(0) if url else "") or str(lt.get("host") or "")
        if not host:
            await self._say("I can stress test your app with Locust. Which address is it running on (for example "
                            "`http://localhost:5000`)? You can also list the pages to hit and how busy each should be.",
                            {"kind": "answer"})
            return
        endpoints = lt.get("endpoints") if isinstance(lt.get("endpoints"), list) and lt.get("endpoints") else [
            {"method": "GET", "path": "/", "weight": 1}]
        users_m = re.search(r"(\d+)\s*(?:concurrent\s+)?users", self.prompt, re.I)
        dur_m = re.search(r"(\d+)\s*(seconds|secs|sec|s|minutes|mins|min)\b", self.prompt, re.I)
        users = int(users_m.group(1)) if users_m else lt.get("users", 20)
        duration = lt.get("duration_s", 60)
        if dur_m:
            duration = int(dur_m.group(1)) * (60 if dur_m.group(2).lower().startswith("min") else 1)
        raw = {"host": host, "endpoints": endpoints, "users": users, "spawn_rate": lt.get("spawn_rate", 5),
               "duration_s": duration}
        try:
            norm_host, note = await loadtest.validate_target(host)
            eps = loadtest.clean_endpoints(endpoints)
        except ValueError as exc:
            await self._say(f"I can't run that load test: {exc}", {"kind": "answer"})
            return
        table = "\n".join(f"{e['method']:6} {e['path']}  (weight {e['weight']})" for e in eps)
        action = Action(0, "loadtest", command=f"Locust: {raw['users']} users for {raw['duration_s']} s against {norm_host}",
                        diff=table, warnings=[note] if note else [])
        action.public_extra = {"loadtest": {**raw, "host": norm_host, "endpoints": eps}}
        approved = await self._approve(1, "Run a Locust stress test", [action])
        if not approved:
            await self._say("OK - I didn't start the load test.", {"kind": "answer"})
            return
        async with dbm.session() as db:
            user = await db.get(User, self.user_id)
            project = await db.get(Project, self.project_id)
            try:
                run, _ = await loadtest.launch(db, user, project, raw, origin="elvin")
            except ValueError as exc:
                await self._say(f"I couldn't start the load test: {exc}", {"kind": "answer"})
                return
        await self.emit("loadtest_launched", {"run_id": str(run.id), "host": norm_host})
        await self._say(f"Locust is now hammering `{norm_host}` with {raw['users']} users for {run.config['duration_s']} s. "
                        f"Watch the live charts on the **Load Test** page - I'll post the results here when it finishes.",
                        {"kind": "loadtest_started", "run_id": str(run.id)})

    async def _remember(self) -> None:
        token = usage.set_context(user_id=self.user_id, project_id=self.project_id, task_id=self.task_id, feature="memory",
                                  effort="low", model_prefs=self.model_prefs)
        try:
            saved = await memory.extract_and_store(self.project_id, self.prompt, self.team)
            if saved:
                await self.emit("memory", {"facts": saved})
        except Exception as exc:  # pragma: no cover - memory is best-effort
            log.info("memory extraction failed: %s", exc)
        finally:
            usage.reset_context(token)

    async def _review(self) -> None:
        diffs = []
        async with dbm.session() as db:
            start = await db.get(Snapshot, self.checkpoint_id) if self.checkpoint_id else None
            end_manifest = None
            if self.last_snapshot_id:
                end = await db.get(Snapshot, self.last_snapshot_id)
                end_manifest = end.manifest if end else None
        if start and end_manifest:
            for path in list(self.changed)[:8]:
                a = start.manifest.get(path, [None])[0]
                b = end_manifest.get(path, [None])[0]
                try:
                    diffs.append(await asyncio.to_thread(snapshots.file_diff, self.project_id, a, b, path, 400))
                except OSError:
                    continue
        if not diffs:
            return
        try:
            res = await ai.call("verifier", team=self.team, task_role="review", max_tokens=900,
                                system=prompts.IDENTITY + "\nReview these changes for bugs, security problems and missed "
                                                          "requirements. Be brief: a short list, or 'Looks good.'",
                                messages=[{"role": "user", "content": f"REQUEST:\n{self.prompt}\n\nCHANGES:\n" + "\n".join(diffs)[:20_000]}],
                                on_attempt=self._on_attempt("review"))
            await self.emit("review", {"text": res.text[:4000], "provider": res.provider})
        except ai.AIError as exc:
            await self.emit("warning", {"text": f"Review pass skipped: {exc}"})

    async def _summarize(self, plan_reply: str, summaries: list[str]) -> None:
        if not self.changed and not self.command_log:
            await self._say(plan_reply + "\n\nNo changes were applied.", {"kind": "summary"})
            return
        facts = {"changed_files": self.changed, "commands": self.command_log[-6:], "step_summaries": summaries[-8:]}
        try:
            res = await ai.call("utility", team=self.team, system=prompts.SUMMARY + "\n" + prompts.platform_note(),
                                task_role="summary", max_tokens=700,
                                messages=[{"role": "user", "content": f"REQUEST:\n{self.prompt}\n\nWHAT HAPPENED (JSON):\n{json.dumps(facts)[:8000]}"}])
            text = res.text.strip()
        except ai.AIError:
            text = "Done. Changed files: " + ", ".join(f"`{p}` ({w})" for p, w in list(self.changed.items())[:20])
        await self._say(text, {"kind": "summary", "changed": self.changed})

    # ------------------------------------------------------------------ design task
    async def _design(self) -> None:
        att_ids = [uuid.UUID(a) for a in self.params.get("attachments", [])][:2]
        images: list[ImageInput] = []
        async with dbm.session() as db:
            for aid in att_ids:
                att = await db.get(Attachment, aid)
                if att and att.user_id == self.user_id and att.project_id == self.project_id:
                    path = attachment_path(att.id)
                    images.append(ImageInput(await asyncio.to_thread(path.read_bytes), att.mime))
        if not images:
            raise RuntimeError("The design image is missing - attach it again")
        target = images[0]
        folder = self.params.get("folder") or "design"
        try:
            folder = normalize_rel(folder)
        except PathError:
            folder = "design"
        await self.emit("status", {"stage": "vision", "text": "Elvin is studying your design..."}, persist=False)
        spec_res = await ai.call("vision", team=self.team, system=prompts.VISION, task_role="vision", json_mode=True,
                                 max_tokens=1200, images=[target],
                                 messages=[{"role": "user", "content": f"Describe this design. User notes: {self.prompt}"}],
                                 on_attempt=self._on_attempt("vision"))
        spec = spec_res.data
        await self.emit("design_spec", {"spec": spec, "provider": spec_res.provider})
        asyncio.create_task(self._remember(), name=f"memory-{self.task_id}")
        iterations = {"low": 1, "medium": 2}.get(self.effort, limits.DESIGN_MAX_ITERATIONS)
        feedback = ""
        best_score = None
        for it in range(1, iterations + 1):
            existing = await asyncio.to_thread(_read_files, self.root, [f"{folder}/index.html", f"{folder}/styles.css"], 30_000)
            msg = (f"{prompts.platform_note()}\nWrite the files inside the folder '{folder}/' (index.html and styles.css).\n\n"
                   f"DESIGN SPEC (JSON):\n{json.dumps(spec)[:6000]}\n\nUSER NOTES:\n{self.prompt}\n")
            if feedback:
                msg += "\nCURRENT FILES:\n" + "\n".join(f"----- {p} -----\n{t}" for p, t in existing.items())
                msg += f"\n\nREVIEW OF THE LAST VERSION (fix these):\n{feedback}"
            actions, _, _ = await self._generate(prompts.DESIGN, msg, task_role="design", stage=f"design iteration {it}", prefix=folder)
            actions = [a for a in actions if a.type != "run"]
            if not actions:
                break
            approved = await self._approve(it, f"Design iteration {it}", actions)
            if not approved:
                break
            await self._apply(it, f"Design iteration {it}", approved)
            entry = f"{folder}/index.html"
            if not (self.root / entry).is_file():
                await self.emit("warning", {"text": f"{entry} was not created, so there is nothing to screenshot."})
                break
            await self.emit("status", {"stage": "screenshot", "text": "Rendering the page in the sandbox..."}, persist=False)
            shot = await runner_client.screenshot(self.root, entry)
            shot_id = await save_image(self.user_id, self.project_id, shot, kind="screenshot", task_id=self.task_id,
                                       filename=f"iteration-{it}.png", meta={"iteration": it})
            await self.emit("screenshot", {"attachment_id": str(shot_id), "iteration": it})
            try:
                verdict_res = await ai.call("vision", team=self.team, system=prompts.VERIFY, task_role="verify", json_mode=True,
                                            max_tokens=900, images=[target, ImageInput(shot, "image/png")],
                                            messages=[{"role": "user", "content": "Image 1 = target design, image 2 = screenshot."}],
                                            on_attempt=self._on_attempt("verify"))
            except ai.AIError as exc:
                await self.emit("warning", {"text": f"Visual check skipped: {exc}"})
                break
            verdict = verdict_res.data or {}
            try:
                score = float(verdict.get("score", 0))
            except (TypeError, ValueError):
                score = 0.0
            best_score = score if best_score is None else max(best_score, score)
            await self.emit("design_verdict", {"iteration": it, "score": score, "matches": verdict.get("matches", [])[:8],
                                               "differences": verdict.get("differences", [])[:8], "provider": verdict_res.provider})
            if score >= 8:
                break
            feedback = str(verdict.get("fix_instructions") or "") + "\n" + "\n".join(f"- {d}" for d in verdict.get("differences", [])[:8])
        note = f" Best visual match score: {best_score:.0f}/10." if best_score is not None else ""
        await self._say(f"The design is in `{folder}/`.{note} Open `{folder}/index.html` in a browser to see it.",
                        {"kind": "summary", "changed": self.changed})


# ---------------------------------------------------------------------- attachments
def attachment_path(att_id: uuid.UUID) -> Path:
    return get_settings().data_dir / "attachments" / f"{att_id}.png"


async def save_image(user_id: uuid.UUID, project_id: uuid.UUID, png: bytes, *, kind: str, task_id: uuid.UUID | None = None,
                     filename: str = "", meta: dict | None = None) -> uuid.UUID:
    from .fs import atomic_write_bytes, sha256_bytes

    att_id = uuid.uuid4()
    await asyncio.to_thread(atomic_write_bytes, attachment_path(att_id), png)
    async with dbm.session() as db:
        db.add(Attachment(id=att_id, user_id=user_id, project_id=project_id, kind=kind, filename=filename[:200], mime="image/png",
                          size=len(png), sha256=sha256_bytes(png), task_id=task_id, meta=meta or {}))
        await db.commit()
    return att_id


async def start_task(task: Task) -> None:
    """Load the rows the agent needs and launch it in the background."""
    async with dbm.session() as db:
        project = await db.get(Project, task.project_id)
        user = await db.get(User, task.user_id)
    agent = Agent(task, project, user)
    tasks.manager.launch(task, agent.run())


def is_text_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip"}
