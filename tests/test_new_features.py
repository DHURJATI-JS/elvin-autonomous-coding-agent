"""Local Docker Gemma fallback + effort cap, 60-second status monitor and
gateway PCs, all GitHub repositories, running code yourself, image preview,
automatic (safe) zip unpacking."""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
import uuid
import zipfile

import pytest
from sqlalchemy import select, update

from elvin import db as dbm
from elvin.models import Project, TaskEvent, UsageRecord, User
from elvin.services import gitops, status_monitor, usage
from elvin.services.ai import router as ai
from elvin.services.ai.fake import FakeProvider

from conftest import EICAR, PASSWORD, finished, pending_batch, project_dir, wait_for


@pytest.fixture(scope="module")
def fake_gateway(tmp_path_factory):
    """Serves a gateway-style /status page (two PCs) like the user's real gateway."""
    site = tmp_path_factory.mktemp("gw")
    (site / "status").write_text(json.dumps({"this_pc_cpu_percent": 7.5, "backends": [
        {"name": "this-pc", "url": "http://127.0.0.1:8080", "online": True, "busy_slots": "1/2", "overloaded": False},
        {"name": "other-pc", "url": "http://192.168.1.24:8080", "online": False, "busy_slots": "0/2", "overloaded": False}]}))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"], cwd=site,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/v1"
    proc.terminate()


async def _project(user, name):
    p = await user.new_project(name)
    async with dbm.session() as s:
        proj = await s.get(Project, uuid.UUID(p["id"]))
    return p["id"], project_dir(str(proj.owner_id), proj.slug)


# ------------------------------------------------------------------ local Gemma fallback + effort cap
def test_chain_puts_local_gemma_right_after_the_gateway(app):
    reg = ai.registry()
    assert reg.chain("gemma_qwen", "planner")[:2] == ["gemma", "gemma_local"]
    assert reg.chain("gemma_qwen", "coder") == ["qwen", "gemma", "qwen_local", "gemma_local"], "GPU models code first, CPU Docker models last"
    assert reg.chain("claude", "coder")[-1] == "gemma_local", "last resort for every team"
    assert ai.capped_effort("gemma_local", "high") == "medium" and ai.capped_effort("gemma_local", "max") == "medium"
    assert ai.capped_effort("gemma_local", "low") == "low" and ai.capped_effort("gemma", "high") == "high"


async def test_gateway_down_falls_back_and_reduces_effort(user, monkeypatch):
    pid, _ = await _project(user, "fallback")
    assert (await user.api("POST", "/api/settings", {"ai_effort": "high"})).status_code == 200
    monkeypatch.setenv("ELVIN_FAKE_AI_FAIL_KEYS", "gemma")
    for b in ai.registry().breakers.values():
        b.ok()
    r = await user.api("POST", f"/api/projects/{pid}/messages", {"content": "what does this project contain?", "idempotency_key": uuid.uuid4().hex})
    tid = r.json()["task_id"]
    t = await wait_for(lambda: finished(tid))
    assert t.status == "completed", t.error
    async with dbm.session() as s:
        evs = list((await s.execute(select(TaskEvent).where(TaskEvent.task_id == uuid.UUID(tid)))).scalars())
    alerts = [e.data for e in evs if e.type == "alert"]
    assert alerts and "reduced from high to medium" in alerts[0]["text"]
    assert any(e.type == "model" and e.data["provider"] == "gemma_local" for e in evs)
    await usage.flush()
    async with dbm.session() as s:
        rows = list((await s.execute(select(UsageRecord).where(UsageRecord.task_id == uuid.UUID(tid), UsageRecord.provider == "gemma_local"))).scalars())
    assert rows and all(r.effort != "high" for r in rows), "the local Gemma never runs at high effort"
    for b in ai.registry().breakers.values():
        b.ok()


# ------------------------------------------------------------------ status monitor
async def test_status_monitor_latency_and_gateway_pcs(user, fake_gateway, monkeypatch):
    ai.set_overrides({"gateway_url": fake_gateway})
    try:
        snap = await status_monitor.monitor.check_once()
        keys = {p["key"]: p for p in snap["providers"]}
        assert {"gemma", "gemma_local", "qwen", "claude_opus", "chatgpt"} <= set(keys)
        assert keys["gemma"]["gen_ms"] is not None and keys["gemma"]["api_ms"] is not None
        assert keys["claude_opus"]["gen_ms"] is None, "cloud models only get the free API check"
        gw = snap["gateway"]
        assert gw["reachable"] and gw["pcs_total"] == 2 and gw["pcs_online"] == 1 and gw["slots_busy"] == 1 and gw["host_cpu_percent"] == 7.5
        assert snap["interval_s"] == 60 and not snap["fallback_active"]
        monkeypatch.setenv("ELVIN_FAKE_AI_DOWN_KEYS", "gemma")
        await status_monitor.monitor.check_once()
        summ = (await user.api("GET", "/api/system/summary")).json()
        assert summ["fallback_active"] is True and summ["effort_cap"] == "medium" and summ["pcs_total"] == 2
        mon = (await user.api("GET", "/api/system/monitor")).json()
        assert len(mon["providers"][0]["history"]) >= 2
        assert (await user.get("/status")).status_code == 200
    finally:
        ai.set_overrides({})
        monkeypatch.delenv("ELVIN_FAKE_AI_DOWN_KEYS", raising=False)
        await status_monitor.monitor.check_once()


async def test_only_admin_changes_gateway_address(user, fake_gateway):
    r = await user.api("POST", "/api/system/gateway", {"url": fake_gateway})
    assert r.status_code == 403
    async with dbm.session() as s:
        await s.execute(update(User).where(User.username == user.username).values(is_admin=True))
        await s.commit()
    assert (await user.api("POST", "/api/system/gateway", {"url": "file:///etc/passwd"})).status_code == 400
    r = await user.api("POST", "/api/system/gateway", {"url": fake_gateway.removesuffix("/v1")})
    assert r.status_code == 200 and r.json()["gateway_url"] == fake_gateway and r.json()["gateway"]["pcs_total"] == 2
    from elvin.models import AppSetting

    async with dbm.session() as s:
        row = await s.get(AppSetting, "gateway_url")
        await s.delete(row)
        await s.commit()
    ai.set_overrides({})
    ai.reset_registry()


# ------------------------------------------------------------------ all GitHub repositories
async def test_all_github_repositories_are_listed(user, monkeypatch):
    from elvin.security.crypto import encrypt
    from elvin.models import GitHubAccount

    async with dbm.session() as s:
        u = (await s.execute(select(User).where(User.username == user.username))).scalar_one()
        s.add(GitHubAccount(user_id=u.id, login="octo", gh_id=1, token_enc=encrypt("ghp_" + "x" * 36), scopes="repo, read:org"))
        await s.commit()

    def repo(i, owner="octo"):
        return {"name": f"r{i}", "full_name": f"{owner}/r{i}", "html_url": f"https://github.com/{owner}/r{i}", "private": i % 3 == 0,
                "fork": i % 5 == 0, "archived": False, "stargazers_count": i, "forks_count": 0, "open_issues_count": 0, "language": "Python",
                "pushed_at": f"2026-10-{(i % 28) + 1:02d}T00:00:00Z", "description": f"repo {i}", "license": None, "owner": {"login": owner}}

    async def fake_run(args, **kw):
        joined = " ".join(args)
        if "user/repos" in joined:
            pages = [[repo(i) for i in range(100)], [repo(i) for i in range(100, 130)] + [repo(999, owner="some-org")]]
            return gitops.CmdResult(0, json.dumps(pages), "")
        if args[:3] == ["gh", "api", "user"]:
            return gitops.CmdResult(0, json.dumps({"login": "octo", "name": "Octo", "html_url": "https://github.com/octo",
                                                   "followers": 1, "public_repos": 87, "total_private_repos": 44}), "")
        if "graphql" in joined:
            return gitops.CmdResult(0, json.dumps({"data": {"viewer": {"contributionsCollection": {"contributionCalendar": {"totalContributions": 5}},
                                                                       "pullRequests": {"totalCount": 0}, "issues": {"totalCount": 0}}}}), "")
        if "notifications" in joined:
            return gitops.CmdResult(0, "[]", "")
        if "search/issues" in joined:
            return gitops.CmdResult(0, json.dumps({"total_count": 0}), "")
        return gitops.CmdResult(0, "", "")
    monkeypatch.setattr(gitops, "run", fake_run)
    monkeypatch.setattr(gitops, "_tools_cache", None)   # don't let the fake poison the git/gh version cache
    stats = (await user.api("POST", "/api/github/stats/refresh")).json()
    repos = stats["data"]["repos"]
    assert len(repos) == 131, "every page of repositories is fetched"
    assert stats["data"]["totals"]["repos"] == 130 and any(r["owner"] == "some-org" for r in repos)
    assert repos[0]["pushed_at"] >= repos[-1]["pushed_at"]
    assert (await user.get("/github")).status_code == 200


# ------------------------------------------------------------------ run code yourself
async def test_user_runs_code_and_elvin_sees_it(user):
    pid, root = await _project(user, "runner")
    (root / "app.py").write_text("import sys\nprint('the answer is', 6 * 7)\nsys.exit(3)\n")
    r = await user.api("POST", f"/api/projects/{pid}/run", {"command": "python app.py", "source": "elvin"})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["exit_code"] == 3 and "the answer is 42" in run["output"] and not run["ok"]
    conv = (await user.api("GET", f"/api/projects/{pid}/conversation")).json()
    assert any((m["meta"] or {}).get("kind") == "run" for m in conv["messages"])
    assert (await user.api("POST", f"/api/projects/{pid}/run", {"command": "sudo rm -rf /"})).status_code == 400
    blocked = await user.api("POST", f"/api/projects/{pid}/run", {"command": "pip install malpkg"})
    assert blocked.status_code == 409 and blocked.json()["detail"]["blocked"][0]["name"] == "malpkg"
    runs = (await user.api("GET", f"/api/projects/{pid}/runs")).json()["runs"]
    assert runs[0]["command"] == "python app.py"
    FakeProvider.log.clear()
    m = await user.api("POST", f"/api/projects/{pid}/messages", {"content": "why did my script fail?", "idempotency_key": uuid.uuid4().hex})
    await wait_for(lambda: finished(m.json()["task_id"]))
    planner = [t for role, t in FakeProvider.log if role == "planner"]
    assert planner and "LATEST COMMAND THE USER RAN" in planner[-1] and "the answer is 42" in planner[-1]


async def test_render_html_page(user):
    pid, root = await _project(user, "render")
    (root / "index.html").write_text("<!doctype html><h1 style='color:purple'>Rendered by Elvin</h1>")
    r = await user.api("POST", f"/api/projects/{pid}/render", {"path": "index.html"})
    assert r.status_code == 200, r.text
    img = await user.get(r.json()["url"])
    assert img.headers["content-type"] == "image/png" and img.content[:4] == b"\x89PNG"
    assert (await user.api("POST", f"/api/projects/{pid}/render", {"path": "README.md"})).status_code == 400


# ------------------------------------------------------------------ image preview + zip unpacking
def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (139, 92, 246)).save(buf, "PNG")
    return buf.getvalue()


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


async def test_image_preview(user):
    pid, root = await _project(user, "images")
    (root / "img").mkdir()
    (root / "img" / "logo.png").write_bytes(_png())
    (root / "evil.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
    r = await user.get(f"/api/projects/{pid}/raw?path=img/logo.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    svg = await user.get(f"/api/projects/{pid}/raw?path=evil.svg")
    assert "sandbox" in svg.headers["content-security-policy"], "SVGs can never run scripts"
    assert (await user.get(f"/api/projects/{pid}/raw?path=README.md")).status_code == 415
    assert (await user.get(f"/api/projects/{pid}/raw?path=../../etc/passwd")).status_code == 400


async def test_zip_uploads_are_unpacked_into_their_folders(user):
    pid, root = await _project(user, "zips")
    flat = _zip({"a.txt": b"A", "sub/b.txt": b"B"})            # no common folder -> "assets/"
    rooted = _zip({"site/index.html": b"<h1>x</h1>", "site/css/s.css": b"body{}"})  # one top folder -> kept
    files = [("files", ("assets.zip", flat, "application/zip")), ("files", ("site.zip", rooted, "application/zip"))]
    r = await user.http.post(f"/api/projects/{pid}/upload", files=files, headers={"X-CSRF-Token": user.csrf})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["unpacked"]) == ["assets.zip", "site.zip"] and not body["warnings"]
    assert (root / "assets" / "a.txt").read_text() == "A" and (root / "assets" / "sub" / "b.txt").read_text() == "B"
    assert (root / "site" / "css" / "s.css").exists() and not (root / "assets.zip").exists()
    slip = _zip({"../../escape.txt": b"nope", "ok.txt": b"ok"})
    r = await user.http.post(f"/api/projects/{pid}/upload", files=[("files", ("slip.zip", slip, "application/zip"))], headers={"X-CSRF-Token": user.csrf})
    assert r.status_code == 200 and "unsafe path" in r.json()["warnings"][0]
    assert (root / "slip.zip").exists() and not (root.parent / "escape.txt").exists()
    infected = _zip({"readme.txt": b"hi", "tools/eicar.com": EICAR})
    r = await user.http.post(f"/api/projects/{pid}/upload", files=[("files", ("bad.zip", infected, "application/zip"))], headers={"X-CSRF-Token": user.csrf})
    assert r.status_code == 422 and "malware" in r.json()["detail"]
    assert not (root / "bad").exists()
    # a new project uploaded as a single zip becomes a project named after its folder
    r = await user.http.post("/api/projects/upload", files=[("files", ("myapp.zip", _zip({"myapp/main.py": b"print(1)\n"}), "application/zip"))],
                             headers={"X-CSRF-Token": user.csrf})
    assert r.status_code == 200 and r.json()["name"] == "myapp" and r.json()["file_count"] >= 1
    _ = os


# ------------------------------------------------------------------ GitHub is read-only once pushed
def test_github_guard_blocks_destructive_commands():
    from elvin.services.gitops import GitError, guard

    for args in (["gh", "repo", "delete", "o/r", "--yes"], ["gh", "repo", "edit", "o/r", "--visibility", "public"],
                 ["gh", "repo", "archive", "o/r"], ["gh", "repo", "rename", "x"], ["gh", "api", "-X", "DELETE", "repos/o/r"],
                 ["gh", "api", "--method", "PATCH", "repos/o/r"], ["git", "-C", "/x", "push", "--force", "origin", "main"],
                 ["git", "push", "origin", "+main"], ["git", "push", "origin", ":main"], ["git", "push", "-d", "origin", "main"],
                 ["git", "push", "--force-with-lease"], ["git", "push", "--mirror"]):
        with pytest.raises(GitError):
            guard(args)
    for args in (["gh", "repo", "create", "o/r", "--private"], ["gh", "repo", "view", "o/r", "--json", "url"],
                 ["gh", "api", "-H", "Accept: application/vnd.github.raw", "repos/o/r/readme"],
                 ["git", "-c", "credential.helper=", "-C", "/x", "push", "-u", "origin", "main"],
                 ["gh", "api", "graphql", "-f", "query={viewer{login}}"]):
        guard(args)


async def test_read_only_repo_viewer(user, monkeypatch):
    import base64

    from elvin.models import GitHubAccount
    from elvin.security.crypto import encrypt

    async with dbm.session() as s:
        u = (await s.execute(select(User).where(User.username == user.username))).scalar_one()
        s.add(GitHubAccount(user_id=u.id, login="octo", gh_id=1, token_enc=encrypt("ghp_" + "y" * 36), scopes="repo"))
        await s.commit()

    async def fake_run(args, **kw):
        gitops.guard(args)
        path = args[-1]
        if path == "repos/octo/demo":
            return gitops.CmdResult(0, json.dumps({"full_name": "octo/demo", "html_url": "https://github.com/octo/demo", "private": True,
                                                   "default_branch": "main", "stargazers_count": 3, "language": "Python"}), "")
        if path.endswith("/readme"):
            return gitops.CmdResult(0, "# Demo\nHello", "")
        if path == "repos/octo/demo/contents":
            return gitops.CmdResult(0, json.dumps([{"name": "src", "path": "src", "type": "dir"},
                                                   {"name": "README.md", "path": "README.md", "type": "file", "size": 12}]), "")
        if "commits" in path:
            return gitops.CmdResult(0, json.dumps([{"sha": "abcdef1234567", "html_url": "u", "commit": {"message": "Initial commit\n\nbody",
                                                                                                       "author": {"name": "Octo", "date": "2026-11-01T00:00:00Z"}}}]), "")
        if path == "repos/octo/demo/contents/src/app.py":
            return gitops.CmdResult(0, json.dumps({"type": "file", "size": 15, "encoding": "base64", "html_url": "u",
                                                   "content": base64.b64encode(b"print('hello')\n").decode()}), "")
        return gitops.CmdResult(1, "", "Not Found")
    monkeypatch.setattr(gitops, "run", fake_run)
    monkeypatch.setattr(gitops, "_tools_cache", None)   # don't let the fake poison the git/gh version cache
    v = (await user.api("GET", "/api/github/repos/octo/demo")).json()
    assert v["readme"].startswith("# Demo") and v["files"][0]["type"] == "dir" and v["commits"][0]["message"] == "Initial commit"
    f = (await user.api("GET", "/api/github/repos/octo/demo/contents?path=src/app.py")).json()
    assert f["type"] == "file" and f["content"] == "print('hello')\n"
    assert (await user.api("GET", "/api/github/repos/bad..name/x")).status_code in (400, 404)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert (await user.api(method, "/api/github/repos/octo/demo")).status_code == 405, "no way to change a repository"


# ------------------------------------------------------------------ "use the local Gemma for now"
def test_gemma_source_and_gateway_down_routing(app):
    reg = ai.registry()
    try:
        ai.note_gateway(False)   # the 60-second monitor saw the gateway down: don't wait for it
        assert reg.chain("gemma_qwen", "planner") == ["gemma_local", "qwen", "qwen_local", "gemma"]
        assert reg.chain("gemma_qwen", "coder") == ["qwen", "qwen_local", "gemma_local", "gemma"]
        ai.note_gateway(True)
        assert reg.chain("gemma_qwen", "planner") == ["gemma", "gemma_local", "qwen", "qwen_local"]
        ai.note_status("qwen", False)   # e.g. Elvin's Ollama on the Mac isn't running: the gateway Gemma codes, then Docker
        assert reg.chain("gemma_qwen", "coder") == ["gemma", "qwen_local", "gemma_local", "qwen"]
        ai.note_status("qwen", True)
        ai.set_overrides({"gemma_source": "local"})   # admin: local Docker Gemma only
        assert ai.gemma_source() == "local"
        assert reg.chain("gemma_qwen", "planner") == ["gemma_local", "qwen", "qwen_local"]
        assert "gemma" not in reg.chain("qwen_claude_gemma", "vision")
        assert reg.chain("claude", "coder") == ["claude_opus", "qwen_local", "gemma_local"], "a local coder is the last resort"
        assert reg.chain("gemma_qwen", "coder") == ["qwen", "qwen_local", "gemma_local"], "local mode: no gateway for coding either"
        assert reg.chain("qwen_claude", "coder") == ["qwen", "claude_opus", "qwen_local", "gemma_local"]
        ai.set_overrides({"gemma_source": "something-else"})
        assert ai.gemma_source() == "auto"
    finally:
        ai.note_gateway(True)
        ai.note_status("qwen", True)
        ai.set_overrides({})


async def test_admin_switches_to_local_gemma_for_now(user):
    from elvin.models import AppSetting, AuditLog

    async with dbm.session() as s:
        await s.execute(update(User).where(User.username == user.username).values(is_admin=False))
        await s.commit()
    assert (await user.api("POST", "/api/system/gemma-source", {"source": "local"})).status_code == 403
    async with dbm.session() as s:
        await s.execute(update(User).where(User.username == user.username).values(is_admin=True))
        await s.commit()
    assert (await user.api("POST", "/api/system/gemma-source", {"source": "cloud"})).status_code == 422
    pid, _ = await _project(user, "localgemma")
    for b in ai.registry().breakers.values():
        b.ok()
    r = await user.api("POST", "/api/system/gemma-source", {"source": "local"})
    try:
        assert r.status_code == 200 and r.json()["gemma_source"] == "local"
        await status_monitor.monitor.check_once()
        summ = (await user.api("GET", "/api/system/summary")).json()
        assert summ["gemma_source"] == "local" and summ["fallback_active"] is True and summ["gateway_online"] is True
        r = await user.api("POST", f"/api/projects/{pid}/messages", {"content": "what does this project contain?", "idempotency_key": uuid.uuid4().hex})
        tid = r.json()["task_id"]
        t = await wait_for(lambda: finished(tid))
        assert t.status == "completed", t.error
        async with dbm.session() as s:
            evs = list((await s.execute(select(TaskEvent).where(TaskEvent.task_id == uuid.UUID(tid)))).scalars())
            row = await s.get(AppSetting, "gemma_source")
            audited = (await s.execute(select(AuditLog).where(AuditLog.event == "settings_changed"))).scalars().all()
        used = {e.data["provider"] for e in evs if e.type == "model"}
        assert "gemma_local" in used and "gemma" not in used, used
        alerts = [e.data["text"] for e in evs if e.type == "alert"]
        assert alerts and "set to use the local Gemma" in alerts[0]
        assert row.value == "local" and any(a.detail.get("gemma_source") == "local" for a in audited)
    finally:
        r = await user.api("POST", "/api/system/gemma-source", {"source": "auto"})
        assert r.json()["gemma_source"] == "auto"
        async with dbm.session() as s:
            await s.delete(await s.get(AppSetting, "gemma_source"))
            await s.commit()
        ai.set_overrides({})


async def test_admin_command_line_changes_settings(app):
    from elvin.services import app_settings

    def cli(*args):
        return subprocess.run([sys.executable, "-m", "elvin.admin", *args], capture_output=True, text=True, timeout=60, env=os.environ.copy())

    try:
        out = cli("gemma-source", "local")
        assert out.returncode == 0 and "local Docker Gemma only" in out.stdout, out.stderr
        assert await app_settings.refresh() and ai.gemma_source() == "local", "the running app picks it up on its next check"
        assert cli("gateway", "ftp://10.0.0.5").returncode == 2
        out = cli("gateway", "http://10.1.2.3:8000")
        assert out.returncode == 0 and "http://10.1.2.3:8000/v1" in out.stdout
        await app_settings.refresh()
        assert ai.gateway_url() == "http://10.1.2.3:8000/v1" and ai.registry().providers["gemma"] is not None
        out = cli("show")
        assert out.returncode == 0 and "gemma source : local" in out.stdout and "api" not in out.stdout.lower()
    finally:
        from elvin.models import AppSetting

        async with dbm.session() as s:
            for key in ("gemma_source", "gateway_url"):
                row = await s.get(AppSetting, key)
                if row is not None:
                    await s.delete(row)
            await s.commit()
        await app_settings.refresh()
        ai.reset_registry()
    assert ai.gemma_source() == "auto"


async def test_local_gemma_uses_the_configured_cpu_threads(monkeypatch):
    import httpx

    from elvin.services.ai import providers

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "OK"}, "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 1})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(providers, "http", lambda: client)
    p = providers.OllamaProvider("gemma_local", "Gemma", "http://gemma:11434", "gemma3:4b", context=8192, family="gemma", threads=4)
    await p.chat([{"role": "user", "content": "hi"}], max_tokens=3)
    assert seen[-1]["options"]["num_thread"] == 4 and seen[-1]["options"]["num_ctx"] == 8192
    p0 = providers.OllamaProvider("qwen", "Qwen", "http://q:11434", "qwen", context=4096)
    await p0.chat([{"role": "user", "content": "hi"}], max_tokens=3)
    assert "num_thread" not in seen[-1]["options"], "0 = Ollama decides"
    await client.aclose()


# ------------------------------------------------------------------ deleting always needs the user's password
async def _uid(user):
    async with dbm.session() as s:
        return (await s.execute(select(User.id).where(User.username == user.username))).scalar_one()


async def test_every_deletion_needs_the_password(user):
    from elvin.models import AuditLog

    pid, root = await _project(user, "deletes")
    await user.api("PUT", f"/api/projects/{pid}/file", {"path": "a.txt", "content": "a"})
    r = await user.api("POST", f"/api/projects/{pid}/fs", {"op": "delete", "path": "a.txt"})
    assert r.status_code == 403 and r.json()["detail"]["reauth"] is True and (root / "a.txt").exists()
    r = await user.api("POST", f"/api/projects/{pid}/fs", {"op": "delete", "path": "a.txt", "password": "not-my-password"})
    assert r.status_code == 403 and r.json()["detail"]["wrong"] is True and (root / "a.txt").exists()
    r = await user.api("POST", f"/api/projects/{pid}/fs", {"op": "delete", "path": "a.txt", "password": PASSWORD})
    assert r.status_code == 200 and not (root / "a.txt").exists()
    for outside in ("../../etc/passwd", "/etc/passwd", "../other-project/x.txt"):   # never outside the project, password or not
        r = await user.api("POST", f"/api/projects/{pid}/fs", {"op": "delete", "path": outside, "password": PASSWORD})
        assert r.status_code == 400, outside
    # restoring an older version that would delete newer files needs it too; one that deletes nothing doesn't
    s1 = (await user.api("POST", f"/api/projects/{pid}/snapshots", {"label": "before"})).json()
    await user.api("PUT", f"/api/projects/{pid}/file", {"path": "new.txt", "content": "new"})
    r = await user.api("POST", f"/api/snapshots/{s1['id']}/restore")
    assert r.status_code == 403 and "new.txt" in r.json()["detail"]["message"] and (root / "new.txt").exists()
    r = await user.api("POST", f"/api/snapshots/{s1['id']}/restore", {"password": PASSWORD})
    assert r.status_code == 200 and r.json()["ok"] and not (root / "new.txt").exists()
    assert (await user.api("POST", f"/api/snapshots/{s1['id']}/restore")).status_code == 200, "nothing to delete -> no password"
    # the whole project
    assert (await user.api("POST", f"/api/projects/{pid}/delete", {"confirm_name": "deletes", "delete_files": True})).status_code == 403
    r = await user.api("POST", f"/api/projects/{pid}/delete", {"confirm_name": "deletes", "delete_files": True, "password": PASSWORD})
    assert r.status_code == 200 and not root.exists()
    uid = await _uid(user)
    async with dbm.session() as s:
        events = [a.event for a in (await s.execute(select(AuditLog).where(AuditLog.user_id == uid))).scalars()]
    assert "reauth_failed" in events and events.count("reauth_ok") >= 3


async def test_elvin_cannot_delete_anything_without_the_users_password(user):
    pid, root = await _project(user, "agentdel")
    await user.api("PUT", f"/api/projects/{pid}/file", {"path": "old.py", "content": "x = 1\n"})
    sentinel = root.parent / "not-in-this-project.txt"
    sentinel.write_text("keep me")
    try:
        # outside the project: refused before it ever reaches the permission dialog
        r = await user.api("POST", f"/api/projects/{pid}/messages", {"content": "delete the file ../not-in-this-project.txt",
                                                                     "idempotency_key": uuid.uuid4().hex})
        tid = r.json()["task_id"]
        await wait_for(lambda: finished(tid), timeout=30)
        async with dbm.session() as s:
            from elvin.models import ApprovalBatch

            batches = list((await s.execute(select(ApprovalBatch).where(ApprovalBatch.task_id == uuid.UUID(tid)))).scalars())
        assert not any(a.get("type") == "delete" for b in batches for a in b.actions) and sentinel.read_text() == "keep me"
        # inside the project: Elvin may only propose it; approving needs the password
        r = await user.api("POST", f"/api/projects/{pid}/messages", {"content": "delete the file old.py", "idempotency_key": uuid.uuid4().hex})
        tid = r.json()["task_id"]
        batch = await wait_for(lambda: pending_batch(tid))
        dels = [a for a in batch.actions if a["type"] == "delete"]
        assert dels and dels[0]["path"] == "old.py"
        r = await user.api("POST", f"/api/approvals/{batch.id}", {"decision": "approve", "approved": [dels[0]["idx"]]})
        assert r.status_code == 403 and r.json()["detail"]["reauth"] and (root / "old.py").exists()
        r = await user.api("POST", f"/api/approvals/{batch.id}", {"decision": "approve", "approved": [dels[0]["idx"]], "password": "guess"})
        assert r.status_code == 403 and (root / "old.py").exists()
        r = await user.api("POST", f"/api/approvals/{batch.id}", {"decision": "approve", "approved": [dels[0]["idx"]], "password": PASSWORD})
        assert r.status_code == 200
        t = await wait_for(lambda: finished(tid))
        assert t.status == "completed", t.error
        assert not (root / "old.py").exists() and sentinel.read_text() == "keep me"
    finally:
        sentinel.unlink(missing_ok=True)


async def test_wrong_delete_passwords_lock_like_sign_in(user):
    pid, root = await _project(user, "lockout")
    await user.api("PUT", f"/api/projects/{pid}/file", {"path": "b.txt", "content": "b"})
    try:
        for _ in range(5):
            r = await user.api("POST", f"/api/projects/{pid}/fs", {"op": "delete", "path": "b.txt", "password": "brute-force-guess"})
            assert r.status_code == 403
        r = await user.api("POST", f"/api/projects/{pid}/fs", {"op": "delete", "path": "b.txt", "password": PASSWORD})
        assert r.status_code == 429 and (root / "b.txt").exists(), "locked: even the right password waits 15 minutes"
    finally:
        async with dbm.session() as s:
            await s.execute(update(User).where(User.username == user.username).values(locked_until=None, failed_logins=0))
            await s.commit()


# ------------------------------------------------------------------ local Qwen Coder in Docker
async def test_docker_qwen_codes_when_qwen_is_down(user, monkeypatch):
    pid, root = await _project(user, "dockerqwen")
    # Claude plans; Qwen and Claude Opus can't code right now -> the Docker Qwen writes the code
    assert (await user.api("POST", "/api/settings", {"ai_effort": "high", "ai_team": "qwen_claude"})).status_code == 200
    monkeypatch.setenv("ELVIN_FAKE_AI_FAIL_KEYS", "qwen,claude_opus")
    for b in ai.registry().breakers.values():
        b.ok()
    try:
        r = await user.api("POST", f"/api/projects/{pid}/messages", {"content": "create a file called hi.py", "idempotency_key": uuid.uuid4().hex})
        tid = r.json()["task_id"]
        batch = await wait_for(lambda: pending_batch(tid))
        r = await user.api("POST", f"/api/approvals/{batch.id}", {"decision": "approve", "approved": [a["idx"] for a in batch.actions]})
        assert r.status_code == 200
        t = await wait_for(lambda: finished(tid))
        assert t.status == "completed", t.error
        async with dbm.session() as s:
            evs = list((await s.execute(select(TaskEvent).where(TaskEvent.task_id == uuid.UUID(tid)))).scalars())
        coders = [e.data["provider"] for e in evs if e.type == "model" and e.data.get("stage") in ("code", "coder", "step")]
        used = {e.data["provider"] for e in evs if e.type == "model"}
        assert "qwen_local" in used, (used, coders)
        alerts = [e.data["text"] for e in evs if e.type == "alert"]
        assert alerts and "local Qwen Coder running in Docker" in alerts[0] and "reduced from high to medium" in alerts[0]
        assert (root / "hi.py").exists()
    finally:
        for b in ai.registry().breakers.values():
            b.ok()


async def test_autocomplete_falls_back_to_docker_qwen(app):
    try:
        text, label = await ai.fim("def add(a, b):", "")
        assert label == "Qwen Coder (test)"
        ai.note_status("qwen", False)
        text, label = await ai.fim("def add(a, b):", "")
        assert label == "Qwen Coder local (test)" and text
    finally:
        ai.note_status("qwen", True)


async def test_monitor_never_loads_a_local_model_just_to_time_it(app, monkeypatch):
    import httpx

    from elvin.services.ai import providers

    state = {"loaded": False}
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:3b"}]})
        if request.url.path == "/api/ps":
            if not state["loaded"]:
                return httpx.Response(200, json={"models": []})
            exp = (dt_now() + __import__("datetime").timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
            return httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:3b", "model": "qwen2.5-coder:3b", "expires_at": exp}]})
        return httpx.Response(200, json={"message": {"content": "OK"}, "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 1})

    def dt_now():
        import datetime

        return datetime.datetime.now(datetime.timezone.utc)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(providers, "http", lambda: client)
    monkeypatch.setattr(status_monitor, "http", lambda: client)
    p = providers.OllamaProvider("qwen_local", "Qwen Coder (local Docker)", "http://gemma:11434", "qwen2.5-coder:3b", context=8192, threads=4)
    mon = status_monitor.monitor
    saved = dict(mon.latest)
    try:
        await mon._provider("qwen_local", p)
        assert not any(path == "/api/chat" for path, _ in seen), "an unloaded model is never loaded just for the probe"
        assert mon.latest["qwen_local"]["online"] and "loads on first use" in mon.latest["qwen_local"]["detail"]
        state["loaded"] = True
        await mon._provider("qwen_local", p)
        chats = [body for path, body in seen if path == "/api/chat"]
        assert chats and chats[-1]["keep_alive"].endswith("s") and 500 < int(chats[-1]["keep_alive"][:-1]) <= 600, chats[-1]["keep_alive"]
        assert chats[-1]["options"]["num_thread"] == 4 and mon.latest["qwen_local"]["gen_ms"] is not None
    finally:
        mon.latest.clear()
        mon.latest.update(saved)
        await client.aclose()


def test_gateway_address_is_optional_and_completed(app):
    try:
        assert ai.gateway_url() == "", "no gateway unless one is configured"
        ai.set_overrides({"gateway_url": "http://10.0.0.7:8000/"})
        assert ai.gateway_url() == "http://10.0.0.7:8000/v1", "a missing /v1 is added"
        with pytest.raises(ValueError):
            ai.normalize_gateway_url("ftp://10.0.0.7")
    finally:
        ai.set_overrides({})


# ------------------------------------------------------------------ self-check: code that doesn't parse gets one repair round
async def test_elvin_repairs_code_that_does_not_compile(user):
    pid, root = await _project(user, "selfcheck")
    r = await user.api("POST", f"/api/projects/{pid}/messages", {"content": "write broken python please", "idempotency_key": uuid.uuid4().hex})
    tid = r.json()["task_id"]
    batch = await wait_for(lambda: pending_batch(tid))
    act = next(a for a in batch.actions if a["path"] == "broken.py")
    assert not act.get("warnings"), "the repaired version is offered - no pasted test output"
    r = await user.api("POST", f"/api/approvals/{batch.id}", {"decision": "approve", "approved": [act["idx"]]})
    assert r.status_code == 200
    t = await wait_for(lambda: finished(tid))
    assert t.status == "completed", t.error
    assert (root / "broken.py").read_text() == "print('fixed')\n"


async def test_code_in_markdown_fences_is_resent_as_files(user):
    pid, root = await _project(user, "fences")
    r = await user.api("POST", f"/api/projects/{pid}/messages", {"content": "write it but answer in markdown", "idempotency_key": uuid.uuid4().hex})
    tid = r.json()["task_id"]
    batch = await wait_for(lambda: pending_batch(tid))
    assert [a["path"] for a in batch.actions] == ["fenced.py"]
    await user.api("POST", f"/api/approvals/{batch.id}", {"decision": "approve", "approved": [batch.actions[0]["idx"]]})
    t = await wait_for(lambda: finished(tid))
    assert t.status == "completed" and (root / "fenced.py").read_text() == "print('hi')\n"


def test_selfcheck_flags_broken_python_and_json_only():
    from elvin.services.selfcheck import syntax_problem

    assert syntax_problem("a.py", "def f():\n    return 1\n") is None
    assert "does not compile" in syntax_problem("a.py", "print('x'\n1 failed in 0.01s\n")
    assert "not valid JSON" in syntax_problem("package.json", '{"name": "x",}')
    assert syntax_problem("tsconfig.json", "{ // comments are fine here\n}") is None
    assert syntax_problem("notes.md", "```oops") is None
