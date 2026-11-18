"""Git + GitHub CLI integration.

* Each Elvin account connects its own GitHub account (personal access token or
  the gh device-code flow). Tokens are stored encrypted in PostgreSQL and passed
  to `gh`/`git` only through the environment of the single command that needs
  them - never on a command line, never in a config file.
* Every command runs with a throw-away HOME, no system git config, prompts
  disabled and a timeout.
* Push: secret scan -> .gitignore -> commit -> `gh repo create` -> `git push`.
* Stats: profile, repositories, contributions, PRs, issues, notifications,
  with suggestions for anything that needs attention."""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits
from ..models import GitHubAccount, GitHubStats, Project, User, utcnow
from ..security.crypto import decrypt, encrypt
from .fs import IGNORED_DIRS, atomic_write_bytes, project_root

log = logging.getLogger("elvin.git")

BASE_ENV = {
    "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_CONFIG_NOSYSTEM": "1", "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1", "GH_SPINNER_DISABLED": "1", "NO_COLOR": "1", "LANG": "C.UTF-8", "BROWSER": "true",
}
CREDENTIAL_HELPER = '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'
REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|github_pat_[A-Za-z0-9_]{60,}"), "GitHub token"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "Anthropic API key"),
    (re.compile(r"\bsk-(proj-)?[A-Za-z0-9_\-]{32,}"), "OpenAI API key"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "Google API key"),
    (re.compile(r"(?i)(password|secret|api[_-]?key)\s*=\s*['\"][^'\"\s]{12,}['\"]"), "hard-coded secret"),
]
SECRET_FILES = re.compile(r"(^|/)(\.env(\..+)?|id_rsa|id_ed25519|.*\.pem|.*\.p12|.*\.pfx|.*\.key|credentials\.json|service-account.*\.json)$")
DEFAULT_GITIGNORE = [".env", ".env.*", "!.env.example", ".venv/", "venv/", "node_modules/", "__pycache__/", "*.pyc",
                     ".DS_Store", ".elvin/", "*.log", "dist/", "build/"]


class GitError(Exception):
    pass


@dataclass
class CmdResult:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


FORBIDDEN_GH = [("repo", "delete"), ("repo", "edit"), ("repo", "archive"), ("repo", "unarchive"), ("repo", "rename"),
                 ("release", "delete"), ("api", "-X"), ("api", "--method")]
FORCE_FLAGS = {"--force", "-f", "--force-with-lease", "--force-if-includes", "--delete", "-d", "--mirror", "--prune"}


def guard(args: list[str]) -> None:
    """Pushed code is never rewritten or deleted: no repository deletion/edits through
    gh, no write calls through `gh api`, and no force / delete / mirror pushes."""
    if not args:
        return
    if args[0] == "gh":
        rest = [a for a in args[1:] if not a.startswith("-") or a in ("-X", "--method")]
        for first, second in FORBIDDEN_GH:
            if first in rest[:1] and (second in args[1:]):
                raise GitError(f"Elvin never runs 'gh {first} {second}' - your GitHub repositories are read-only here.")
    if args[0] == "git" and "push" in args:
        tail = args[args.index("push") + 1:]
        if any(a in FORCE_FLAGS or a.startswith("--force") for a in tail) or any(a.startswith("+") or a.startswith(":") for a in tail):
            raise GitError("Elvin never force-pushes or deletes remote branches - pushed history stays exactly as it is.")


async def run(args: list[str], *, cwd: Path | None = None, env_extra: dict[str, str] | None = None, timeout: float = 60,
              input_text: str | None = None, home: str | None = None) -> CmdResult:
    guard(args)
    own_home = home is None
    home = home or tempfile.mkdtemp(prefix="elvin-git-")
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": home, **BASE_ENV, **(env_extra or {})}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd) if cwd else None, env=env,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    except FileNotFoundError as exc:
        if own_home:
            shutil.rmtree(home, ignore_errors=True)
        raise GitError(f"'{args[0]}' is not installed.") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(input_text.encode() if input_text is not None else None), timeout)
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        raise GitError(f"'{' '.join(args[:3])}' timed out after {int(timeout)} s.") from exc
    finally:
        if own_home:
            shutil.rmtree(home, ignore_errors=True)
    return CmdResult(proc.returncode or 0, out.decode(errors="replace")[:400_000], err.decode(errors="replace")[:50_000])


def auth_args() -> list[str]:
    return ["-c", "credential.helper=", "-c", f"credential.helper={CREDENTIAL_HELPER}"]


_tools_cache: tuple[float, dict[str, Any]] | None = None


async def tools() -> dict[str, Any]:
    global _tools_cache
    if _tools_cache and time.monotonic() - _tools_cache[0] < 600:
        return _tools_cache[1]
    info: dict[str, Any] = {}
    for name, args in (("git", ["git", "--version"]), ("gh", ["gh", "--version"])):
        try:
            r = await run(args, timeout=10)
            info[name] = r.out.splitlines()[0].strip() if r.ok and r.out else None
        except GitError:
            info[name] = None
    _tools_cache = (time.monotonic(), info)
    return info


# --------------------------------------------------------------------------- accounts
async def account(db: AsyncSession, user_id: uuid.UUID) -> GitHubAccount | None:
    return await db.get(GitHubAccount, user_id)


async def token_for(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    acct = await account(db, user_id)
    return decrypt(acct.token_enc) if acct else None


def _split_headers(raw: str) -> tuple[dict[str, str], str]:
    head, _, body = raw.replace("\r\n", "\n").partition("\n\n")
    headers = {}
    for line in head.splitlines()[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return headers, body


async def validate_token(token: str) -> dict[str, Any]:
    token = token.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{20,255}", token):
        raise GitError("That doesn't look like a GitHub token.")
    r = await run(["gh", "api", "-i", "user"], env_extra={"GH_TOKEN": token}, timeout=30)
    if not r.ok:
        raise GitError("GitHub rejected this token (it may be expired or revoked).")
    headers, body = _split_headers(r.out)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise GitError("GitHub sent an unexpected answer.") from exc
    return {"login": data.get("login", ""), "id": int(data.get("id") or 0), "name": data.get("name") or "",
            "scopes": headers.get("x-oauth-scopes", "")}


async def connect_token(db: AsyncSession, user: User, token: str) -> GitHubAccount:
    info = await validate_token(token)
    acct = await account(db, user.id)
    if acct is None:
        acct = GitHubAccount(user_id=user.id, login=info["login"], gh_id=info["id"], token_enc=encrypt(token.strip()),
                             scopes=info["scopes"][:500])
        db.add(acct)
    else:
        acct.login, acct.gh_id, acct.token_enc, acct.scopes = info["login"], info["id"], encrypt(token.strip()), info["scopes"][:500]
        acct.connected_at = utcnow()
    stats = await db.get(GitHubStats, user.id)
    if stats is not None:
        await db.delete(stats)
    await db.commit()
    return acct


async def disconnect(db: AsyncSession, user: User) -> None:
    for model in (GitHubAccount, GitHubStats):
        row = await db.get(model, user.id)
        if row is not None:
            await db.delete(row)
    await db.commit()


# --------------------------------------------------------------------------- device flow
@dataclass
class DeviceFlow:
    id: str
    user_id: uuid.UUID
    status: str = "starting"  # starting | waiting | connected | failed | expired
    code: str = ""
    url: str = "https://github.com/login/device"
    error: str = ""
    login: str = ""
    created: float = field(default_factory=time.time)
    proc: asyncio.subprocess.Process | None = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "status": self.status, "code": self.code, "url": self.url, "error": self.error, "login": self.login}


_flows: dict[str, DeviceFlow] = {}


async def device_start(user: User, store) -> DeviceFlow:
    for f in list(_flows.values()):
        if f.user_id == user.id and f.status in ("starting", "waiting"):
            return f
        if time.time() - f.created > 1800:
            _flows.pop(f.id, None)
    flow = DeviceFlow(id=secrets.token_urlsafe(12), user_id=user.id)
    _flows[flow.id] = flow
    asyncio.create_task(_device_worker(flow, store), name=f"gh-device-{flow.id}")
    for _ in range(40):  # wait up to ~10 s for the code to appear
        if flow.code or flow.status not in ("starting",):
            break
        await asyncio.sleep(0.25)
    return flow


async def _device_worker(flow: DeviceFlow, store) -> None:
    cfg = tempfile.mkdtemp(prefix="elvin-gh-")
    home = tempfile.mkdtemp(prefix="elvin-home-")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, **BASE_ENV, "GH_CONFIG_DIR": cfg}
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web", "--skip-ssh-key",
            "--scopes", "workflow", env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        flow.proc = proc
        assert proc.stdin and proc.stdout
        proc.stdin.write(b"\n")
        with contextlib.suppress(Exception):
            await proc.stdin.drain()
        deadline = time.time() + 900

        async def read_output() -> None:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                text = line.decode(errors="replace")
                m = re.search(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b", text)
                if m and not flow.code:
                    flow.code = m.group(1)
                    flow.status = "waiting"
                u = re.search(r"https://github\.com/login/device\S*", text)
                if u:
                    flow.url = u.group(0)
                if "Press Enter" in text:
                    with contextlib.suppress(Exception):
                        proc.stdin.write(b"\n")
                        await proc.stdin.drain()

        reader = asyncio.create_task(read_output())
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=max(1, deadline - time.time()))
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            flow.status, flow.error = "expired", "The code expired - start again."
            return
        await asyncio.wait_for(reader, timeout=5)
        if rc != 0:
            flow.status, flow.error = "failed", "GitHub sign-in was not completed."
            return
        tok = await run(["gh", "auth", "token"], env_extra={"GH_CONFIG_DIR": cfg}, timeout=15)
        token = tok.out.strip()
        if not tok.ok or not token:
            flow.status, flow.error = "failed", "Could not read the new GitHub token."
            return
        login = await store(flow.user_id, token)
        flow.status, flow.login = "connected", login
    except Exception as exc:  # pragma: no cover - external process
        log.warning("device flow failed: %s", exc)
        flow.status, flow.error = "failed", "GitHub sign-in failed."
    finally:
        shutil.rmtree(cfg, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


def device_status(flow_id: str, user_id: uuid.UUID) -> DeviceFlow | None:
    flow = _flows.get(flow_id)
    return flow if flow and flow.user_id == user_id else None


# --------------------------------------------------------------------------- stats
async def _gh_json(args: list[str], token: str, timeout: float = 30) -> Any:
    r = await run(["gh", *args], env_extra={"GH_TOKEN": token}, timeout=timeout)
    if not r.ok:
        err = (r.err or r.out).strip()
        if "401" in err or "Bad credentials" in err:
            raise GitError("auth")
        raise GitError(err[:200] or "gh command failed")
    return json.loads(r.out or "null")


def _light_repo(r: dict) -> dict[str, Any]:
    return {"name": r.get("name"), "full_name": r.get("full_name"), "url": r.get("html_url"), "private": bool(r.get("private")),
            "fork": bool(r.get("fork")), "archived": bool(r.get("archived")), "stars": r.get("stargazers_count", 0),
            "forks": r.get("forks_count", 0), "open_issues": r.get("open_issues_count", 0), "language": r.get("language"),
            "pushed_at": r.get("pushed_at"), "description": (r.get("description") or "")[:300],
            "license": (r.get("license") or {}).get("spdx_id") if r.get("license") else None,
            "owner": (r.get("owner") or {}).get("login"), "default_branch": r.get("default_branch"), "size_kb": r.get("size", 0),
            "visibility": r.get("visibility") or ("private" if r.get("private") else "public")}


async def all_repos(token: str) -> list[dict[str, Any]]:
    """Every repository the account can see (owned, collaborator, organisation), newest push first."""
    r = await run(["gh", "api", "--paginate", "--slurp",
                   "user/repos?per_page=100&sort=pushed&affiliation=owner,collaborator,organization_member"],
                  env_extra={"GH_TOKEN": token}, timeout=120)
    if not r.ok:
        err = (r.err or r.out).strip()
        raise GitError("auth" if "401" in err or "Bad credentials" in err else (err[:200] or "could not list repositories"))
    pages = json.loads(r.out or "[]")
    repos = [repo for page in pages for repo in (page if isinstance(page, list) else [page])]
    light = [_light_repo(x) for x in repos[: limits.GITHUB_MAX_REPOS]]
    light.sort(key=lambda x: x["pushed_at"] or "", reverse=True)
    return light


async def collect_stats(token: str) -> dict[str, Any]:
    gql = ("query { viewer { contributionsCollection { contributionCalendar { totalContributions } } "
           "pullRequests(states: OPEN) { totalCount } issues(states: OPEN) { totalCount } "
           "repositoriesContributedTo(first: 1) { totalCount } starredRepositories { totalCount } } }")
    results = await asyncio.gather(
        _gh_json(["api", "user"], token),
        all_repos(token),
        _gh_json(["api", "graphql", "-f", f"query={gql}"], token),
        _gh_json(["api", "notifications?per_page=50"], token),
        _gh_json(["api", "search/issues?q=is:open+is:pr+review-requested:@me&per_page=1"], token),
        return_exceptions=True,
    )
    user, repos, graph, notes, reviews = results
    if isinstance(user, Exception):
        raise user if isinstance(user, GitError) else GitError(str(user))
    repos = repos if isinstance(repos, list) else []
    viewer = (graph or {}).get("data", {}).get("viewer", {}) if isinstance(graph, dict) else {}
    login = user.get("login")
    owned = [r for r in repos if r.get("owner") == login]
    langs: dict[str, int] = {}
    for r in owned:
        if r.get("language"):
            langs[r["language"]] = langs.get(r["language"], 0) + 1
    recent = repos[:8]
    return {
        "profile": {"login": user.get("login"), "name": user.get("name"), "url": user.get("html_url"), "bio": user.get("bio"),
                    "followers": user.get("followers", 0), "following": user.get("following", 0),
                    "public_repos": user.get("public_repos", 0), "private_repos": user.get("total_private_repos", 0),
                    "created_at": user.get("created_at")},
        "totals": {"repos": len(owned), "all_repos": len(repos), "stars": sum(r["stars"] for r in owned),
                   "forks": sum(r["forks"] for r in owned), "open_issues": sum(r["open_issues"] for r in owned)},
        "languages": sorted(langs.items(), key=lambda kv: -kv[1])[:8],
        "contributions": ((viewer.get("contributionsCollection") or {}).get("contributionCalendar") or {}).get("totalContributions"),
        "open_prs": (viewer.get("pullRequests") or {}).get("totalCount"),
        "open_issues_authored": (viewer.get("issues") or {}).get("totalCount"),
        "review_requests": reviews.get("total_count") if isinstance(reviews, dict) else None,
        "notifications": len(notes) if isinstance(notes, list) else None,
        "notifications_error": str(notes)[:120] if isinstance(notes, Exception) else "",
        "recent": recent,
        "repos": repos,
    }


REPO_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


async def repo_view(token: str, owner: str, repo: str) -> dict[str, Any]:
    """Read-only overview of one repository: details, README, top-level files, recent commits."""
    if not REPO_PART.fullmatch(owner) or not REPO_PART.fullmatch(repo):
        raise GitError("Invalid repository name.")
    base = f"repos/{owner}/{repo}"
    info, readme, listing, commits = await asyncio.gather(
        _gh_json(["api", base], token),
        run(["gh", "api", "-H", "Accept: application/vnd.github.raw", f"{base}/readme"], env_extra={"GH_TOKEN": token}, timeout=30),
        _gh_json(["api", f"{base}/contents"], token),
        _gh_json(["api", f"{base}/commits?per_page=10"], token),
        return_exceptions=True)
    if isinstance(info, Exception):
        raise info if isinstance(info, GitError) else GitError(str(info))
    return {
        "full_name": info.get("full_name"), "url": info.get("html_url"), "description": info.get("description") or "",
        "private": info.get("private"), "default_branch": info.get("default_branch"), "stars": info.get("stargazers_count", 0),
        "forks": info.get("forks_count", 0), "open_issues": info.get("open_issues_count", 0), "language": info.get("language"),
        "license": (info.get("license") or {}).get("spdx_id") if info.get("license") else None, "size_kb": info.get("size", 0),
        "pushed_at": info.get("pushed_at"), "topics": info.get("topics") or [],
        "readme": readme.out[:200_000] if isinstance(readme, CmdResult) and readme.ok else "",
        "files": _listing(listing) if isinstance(listing, list) else [],
        "commits": [{"sha": c.get("sha", "")[:10], "message": ((c.get("commit") or {}).get("message") or "").split("\n")[0][:200],
                     "author": ((c.get("commit") or {}).get("author") or {}).get("name"), "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
                     "url": c.get("html_url")} for c in (commits if isinstance(commits, list) else [])],
    }


def _listing(items: list[dict]) -> list[dict[str, Any]]:
    out = [{"name": i.get("name"), "path": i.get("path"), "type": "dir" if i.get("type") == "dir" else "file", "size": i.get("size", 0)}
           for i in items if i.get("type") in ("file", "dir", "symlink")]
    return sorted(out, key=lambda x: (x["type"] != "dir", (x["name"] or "").lower()))[:1000]


async def repo_contents(token: str, owner: str, repo: str, path: str) -> dict[str, Any]:
    """Read-only: list a folder or show a (text) file of a repository."""
    if not REPO_PART.fullmatch(owner) or not REPO_PART.fullmatch(repo):
        raise GitError("Invalid repository name.")
    clean = "/".join(p for p in path.replace("\\", "/").split("/") if p and p not in (".", ".."))
    from urllib.parse import quote

    data = await _gh_json(["api", f"repos/{owner}/{repo}/contents/{quote(clean)}"], token)
    if isinstance(data, list):
        return {"type": "dir", "path": clean, "files": _listing(data)}
    if not isinstance(data, dict):
        raise GitError("Unexpected answer from GitHub.")
    size = int(data.get("size") or 0)
    if size > 300_000:
        return {"type": "file", "path": clean, "too_large": True, "size": size, "url": data.get("html_url")}
    import base64

    raw = base64.b64decode(data.get("content") or "") if data.get("encoding") == "base64" else b""
    is_text_file = b"\x00" not in raw[:8000]
    return {"type": "file", "path": clean, "size": size, "url": data.get("html_url"), "binary": not is_text_file,
            "content": raw.decode("utf-8", errors="replace") if is_text_file else ""}


def suggestions(tool_info: dict[str, Any], acct: GitHubAccount | None, data: dict[str, Any] | None,
                projects: list[Project], error: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not tool_info.get("git"):
        out.append({"level": "required", "title": "Git is not installed", "detail": "Elvin's container needs git. Re-run the installer.", "action": ""})
    if not tool_info.get("gh"):
        out.append({"level": "required", "title": "GitHub CLI is not installed", "detail": "Re-run the installer to add the gh CLI.", "action": ""})
    if acct is None:
        out.append({"level": "required", "title": "Connect your GitHub account",
                    "detail": "Needed to push projects, clone private repositories and show your stats.", "action": "connect"})
        return out
    if error == "auth":
        out.append({"level": "required", "title": "Your GitHub connection stopped working",
                    "detail": "The token was revoked or expired. Reconnect GitHub.", "action": "connect"})
        return out
    scopes = {s.strip() for s in (acct.scopes or "").split(",") if s.strip()}
    if "delete_repo" in scopes:
        out.append({"level": "info", "title": "Your token could delete repositories",
                    "detail": "Elvin never modifies or deletes anything already on GitHub, but a token without the 'delete_repo' scope is safer.",
                    "action": ""})
    if scopes:  # classic tokens report scopes; fine-grained tokens don't
        if "repo" not in scopes:
            out.append({"level": "required", "title": "Token can't push to private repositories",
                        "detail": "Reconnect with a token that has the 'repo' scope.", "action": "connect"})
        has_workflows = any((project_root(p.owner_id, p.slug) / ".github" / "workflows").is_dir() for p in projects[:20])
        if "workflow" not in scopes and has_workflows:
            out.append({"level": "recommended", "title": "Add the 'workflow' scope",
                        "detail": "A project contains GitHub Actions files; pushing them needs the 'workflow' scope.", "action": "connect"})
    if data:
        login = (data.get("profile") or {}).get("login")
        repos = [r for r in data.get("repos", []) if not r.get("archived") and r.get("owner") == login]
        if data.get("review_requests"):
            out.append({"level": "recommended", "title": f"{data['review_requests']} pull request(s) await your review",
                        "detail": "Open GitHub to review them.", "action": "https://github.com/pulls/review-requested"})
        if data.get("notifications"):
            out.append({"level": "info", "title": f"{data['notifications']} unread GitHub notification(s)", "detail": "",
                        "action": "https://github.com/notifications"})
        no_desc = [r["name"] for r in repos if not r.get("description")]
        if no_desc:
            out.append({"level": "info", "title": f"{len(no_desc)} repositor{'y has' if len(no_desc) == 1 else 'ies have'} no description",
                        "detail": "e.g. " + ", ".join(no_desc[:3]), "action": ""})
        no_lic = [r["name"] for r in repos if not r.get("private") and not r.get("license")]
        if no_lic:
            out.append({"level": "recommended", "title": f"{len(no_lic)} public repositor{'y has' if len(no_lic) == 1 else 'ies have'} no license",
                        "detail": "Without a license nobody may legally reuse the code: " + ", ".join(no_lic[:3]), "action": ""})
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=365)
        stale = [r["name"] for r in repos if r.get("pushed_at") and dt.datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00")) < cutoff]
        if stale:
            out.append({"level": "info", "title": f"{len(stale)} repositor{'y' if len(stale) == 1 else 'ies'} inactive for a year",
                        "detail": "Consider archiving: " + ", ".join(stale[:3]), "action": ""})
        busy = [r for r in repos if (r.get("open_issues") or 0) >= 20]
        for r in busy[:2]:
            out.append({"level": "info", "title": f"{r['name']} has {r['open_issues']} open issues/PRs", "detail": "", "action": r.get("url") or ""})
    unpushed = [p for p in projects if not p.github_repo and not p.archived]
    if unpushed:
        out.append({"level": "recommended", "title": f"{len(unpushed)} project(s) are not on GitHub yet",
                    "detail": "Back them up: " + ", ".join(p.name for p in unpushed[:3]), "action": f"/p/{unpushed[0].id}/git"})
    return out[:10]


async def refresh_stats(db: AsyncSession, user: User, *, force: bool = False) -> GitHubStats:
    row = await db.get(GitHubStats, user.id)
    if row is None:
        row = GitHubStats(user_id=user.id, data={}, suggestions=[])
        db.add(row)
    elif not force and row.fetched_at and (utcnow() - row.fetched_at).total_seconds() < limits.GITHUB_REFRESH_MIN_INTERVAL_S:
        return row
    acct = await account(db, user.id)
    tool_info = await tools()
    projects = list((await db.execute(select(Project).where(Project.owner_id == user.id))).scalars().all())
    error = None
    data: dict[str, Any] | None = None
    if acct is not None:
        token = decrypt(acct.token_enc)
        try:
            data = await collect_stats(token or "")
        except GitError as exc:
            error = "auth" if str(exc) == "auth" else str(exc)
        except Exception as exc:  # pragma: no cover
            error = f"GitHub stats failed ({exc.__class__.__name__})"
    row.data = data or row.data or {}
    row.suggestions = suggestions(tool_info, acct, data, projects, error)
    row.error = error
    row.fetched_at = utcnow()
    await db.commit()
    return row


# --------------------------------------------------------------------------- projects
async def project_status(project: Project) -> dict[str, Any]:
    root = project_root(project.owner_id, project.slug)
    if not (root / ".git").is_dir():
        return {"is_repo": False}
    out: dict[str, Any] = {"is_repo": True}
    r = await run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
    out["branch"] = r.out.strip() if r.ok else ""
    r = await run(["git", "-C", str(root), "remote", "get-url", "origin"], timeout=15)
    out["remote"] = re.sub(r"//[^@/]+@", "//", r.out.strip()) if r.ok else ""
    r = await run(["git", "-C", str(root), "log", "-1", "--pretty=format:%H%x1f%s%x1f%an%x1f%cI"], timeout=15)
    if r.ok and r.out:
        sha, subject, author, when = (r.out.split("\x1f") + ["", "", "", ""])[:4]
        out["last_commit"] = {"sha": sha[:12], "subject": subject, "author": author, "date": when}
    r = await run(["git", "-C", str(root), "status", "--porcelain"], timeout=30)
    out["changes"] = len([ln for ln in r.out.splitlines() if ln.strip()]) if r.ok else None
    return out


def secret_scan(root: Path, max_files: int = 5000) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = full.relative_to(root).as_posix()
            count += 1
            if count > max_files:
                return found
            if SECRET_FILES.search(rel) and not rel.endswith(".env.example"):
                found.append({"path": rel, "kind": "secret file", "fixable": "yes"})
                continue
            try:
                if full.stat().st_size > 1_000_000:
                    continue
                text = full.read_text(errors="ignore")
            except OSError:
                continue
            for rx, label in SECRET_PATTERNS:
                if rx.search(text):
                    found.append({"path": rel, "kind": label, "fixable": "no"})
                    break
    return found


def ensure_gitignore(root: Path, extra: list[str]) -> list[str]:
    path = root / ".gitignore"
    lines = path.read_text(errors="replace").splitlines() if path.exists() else []
    have = {ln.strip() for ln in lines}
    added = [e for e in DEFAULT_GITIGNORE + extra if e not in have]
    if added:
        text = "\n".join(lines + (["", "# added by Elvin"] if lines else ["# added by Elvin"]) + added) + "\n"
        atomic_write_bytes(path, text.encode())
    return added


async def push(db: AsyncSession, user: User, project: Project, *, repo_name: str, private: bool, description: str,
               message: str, secrets_action: str) -> dict[str, Any]:
    acct = await account(db, user.id)
    if acct is None:
        raise GitError("Connect your GitHub account first.")
    token = decrypt(acct.token_enc) or ""
    if not REPO_NAME.fullmatch(repo_name) or repo_name in (".", ".."):
        raise GitError("Repository names may only contain letters, numbers, '.', '-' and '_'.")
    root = project_root(project.owner_id, project.slug)
    if not root.is_dir():
        raise GitError("The project folder is missing.")
    found = await asyncio.to_thread(secret_scan, root)
    hard = [f for f in found if f["fixable"] == "no"]
    if found and secrets_action not in ("ignore_files", "push_anyway"):
        return {"needs_confirmation": True, "secrets": found[:50]}
    if hard and secrets_action != "push_anyway":
        return {"needs_confirmation": True, "secrets": found[:50],
                "message": "Secrets were found inside source files. Remove them, or explicitly choose 'push anyway'."}
    extra = [f["path"] for f in found if f["fixable"] == "yes"] if secrets_action in ("ignore_files", "push_anyway") else []
    await asyncio.to_thread(ensure_gitignore, root, extra)
    env = {"GH_TOKEN": token}
    git = ["git", "-C", str(root)]
    steps: list[str] = []
    if not (root / ".git").is_dir():
        r = await run(git + ["init", "-b", "main"], timeout=30)
        if not r.ok:
            raise GitError(f"git init failed: {r.err[:200]}")
        steps.append("Initialised a git repository")
    await run(git + ["config", "user.name", acct.login], timeout=15)
    await run(git + ["config", "user.email", f"{acct.gh_id}+{acct.login}@users.noreply.github.com"], timeout=15)
    for f in extra:  # make sure ignored secret files are not tracked
        await run(git + ["rm", "--cached", "--quiet", "--ignore-unmatch", "--", f], timeout=15)
    r = await run(git + ["add", "-A"], timeout=120)
    if not r.ok:
        raise GitError(f"git add failed: {r.err[:200]}")
    r = await run(git + ["commit", "-m", message[:500] or "Update from Elvin"], timeout=60)
    if r.ok:
        steps.append("Committed your changes")
    elif "nothing to commit" not in (r.out + r.err):
        raise GitError(f"git commit failed: {(r.err or r.out)[:200]}")
    remote = await run(git + ["remote", "get-url", "origin"], timeout=15)
    full_name = f"{acct.login}/{repo_name}"
    if not remote.ok:
        view = await run(["gh", "repo", "view", full_name, "--json", "url"], env_extra=env, timeout=30)
        if not view.ok:
            args = ["gh", "repo", "create", full_name, "--private" if private else "--public"]
            if description.strip():
                args += ["--description", description.strip()[:300]]
            created = await run(args, env_extra=env, timeout=60)
            if not created.ok:
                raise GitError(f"Could not create the repository: {(created.err or created.out)[:300]}")
            steps.append(f"Created {'private' if private else 'public'} repository {full_name}")
        r = await run(git + ["remote", "add", "origin", f"https://github.com/{full_name}.git"], timeout=15)
        if not r.ok:
            raise GitError(f"Could not add the remote: {r.err[:200]}")
    else:
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", remote.out.strip())
        if m:
            full_name = m.group(1)
    branch = (await run(git + ["rev-parse", "--abbrev-ref", "HEAD"], timeout=15)).out.strip() or "main"
    pushed = await run(["git", *auth_args(), "-C", str(root), "push", "-u", "origin", branch], env_extra=env, timeout=300)
    if not pushed.ok:
        raise GitError(f"git push failed: {(pushed.err or pushed.out)[-400:]}")
    steps.append(f"Pushed branch '{branch}'")
    info = await run(["gh", "repo", "view", full_name, "--json",
                      "name,nameWithOwner,url,visibility,description,stargazerCount,forkCount,defaultBranchRef,pushedAt,diskUsage"],
                     env_extra=env, timeout=30)
    repo: dict[str, Any] = {}
    if info.ok:
        with contextlib.suppress(ValueError):
            repo = json.loads(info.out)
    project.github_repo = full_name[:200]
    project.repo_url = (repo.get("url") or f"https://github.com/{full_name}")[:500]
    await db.commit()
    return {"ok": True, "steps": steps, "repo": repo, "url": project.repo_url, "ignored": extra}
