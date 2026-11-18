"""Vulnerability detector, bug hunting, Locust load testing, cloning, GitHub stats, usage."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from sqlalchemy import select

from elvin import db as dbm
from elvin.models import Project

from conftest import EICAR, project_dir, wait_for


async def _project(user, name):
    p = await user.new_project(name)
    async with dbm.session() as s:
        proj = await s.get(Project, uuid.UUID(p["id"]))
    return p["id"], project_dir(str(proj.owner_id), proj.slug)


async def test_vuln_scan_and_fix(user):
    pid, root = await _project(user, "vulns")
    (root / "requirements.txt").write_text("vulnlib==1.5.0\nrequests>=2.0\nmalpkg\ndoesnotexist-abc==1.0\n")
    (root / "package.json").write_text('{"dependencies": {"nofix": "^1.0.0", "left-pad": "1.3.0"}}')
    r = await user.api("POST", f"/api/projects/{pid}/vulns/scan")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["scan"]["status"] == "completed"
    by_pkg = {f["package"]: f for f in data["findings"]}
    assert by_pkg["vulnlib"]["severity"] == "CRITICAL" and by_pkg["vulnlib"]["fixed_version"] == "2.0.0"
    assert "Upgrade vulnlib" in by_pkg["vulnlib"]["recommendation"]
    assert by_pkg["malpkg"]["details"]["malicious"] and "MALICIOUS" in by_pkg["malpkg"]["recommendation"]
    assert by_pkg["nofix"]["fixed_version"] is None and "No fixed release" in by_pkg["nofix"]["recommendation"]
    assert by_pkg["doesnotexist-abc"]["details"].get("missing")
    assert "requests" not in by_pkg and "left-pad" not in by_pkg
    prev = (await user.api("POST", f"/api/vulns/findings/{by_pkg['vulnlib']['id']}/fix-preview")).json()
    assert prev["automatic"] and "+vulnlib==2.0.0" in prev["diff"]
    r = await user.api("POST", f"/api/vulns/findings/{by_pkg['vulnlib']['id']}/fix-apply", {"sha256": prev["sha256"]})
    assert r.status_code == 200 and "vulnlib==2.0.0" in (root / "requirements.txt").read_text()
    (root / "requirements.txt").write_text("vulnlib==1.5.0\nnew-line-added==1.0\n")
    r = await user.api("POST", f"/api/vulns/findings/{by_pkg['vulnlib']['id']}/fix-apply", {"sha256": prev["sha256"]})
    assert r.status_code == 409, "a preview made before the file changed must not be applied"
    v = (await user.api("POST", f"/api/projects/{pid}/vulns/check", {"ecosystem": "pypi", "name": "vulnlib", "version": "1.0"})).json()
    assert v["verdict"] == "go_with_version"
    v = (await user.api("POST", f"/api/projects/{pid}/vulns/check", {"ecosystem": "npm", "name": "nofix", "version": "1.0.0"})).json()
    assert v["verdict"] == "blocked" and v["alternatives"]


async def test_bug_hunt_static_and_ai(user):
    pid, root = await _project(user, "bughunt")
    (root / "api").mkdir()
    (root / "api" / "views.py").write_text("import os\n\ndef run(cmd):\n    os.system(cmd)\n    return eval(cmd)\n")
    (root / "other.py").write_text("print('fine')\n")
    opts = (await user.api("GET", f"/api/projects/{pid}/bughunt/options?folder=api")).json()
    assert opts["file_count"] == 1 and {p["key"] for p in opts["providers"] if p["available"]} == {"claude_haiku", "chatgpt", "qwen"}
    r = await user.api("POST", f"/api/projects/{pid}/bughunt", {"folder": "api", "provider": "qwen"})
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]

    async def done():
        d = (await user.api("GET", f"/api/bughunt/{run_id}")).json()
        return d if d["run"]["status"] != "running" else None
    d = await wait_for(done, timeout=60)
    assert d["run"]["status"] == "completed"
    titles = {(f["source"], f["file"]) for f in d["findings"]}
    assert ("static", "api/views.py") in titles and ("ai", "api/views.py") in titles
    assert all(f["file"].startswith("api/") for f in d["findings"]), "only the chosen folder is reviewed"
    csv = await user.get(f"/api/bughunt/{run_id}/export.csv")
    assert csv.status_code == 200 and "severity,title" in csv.text


async def test_qwen_not_offered_for_large_projects(user):
    from elvin import limits

    pid, root = await _project(user, "bigproject")
    for i in range(limits.QWEN_LARGE_PROJECT_FILES + 5):
        (root / f"m{i}.py").write_text(f"x = {i}\n")
    opts = (await user.api("GET", f"/api/projects/{pid}/bughunt/options")).json()
    assert opts["large"] is True
    qwen = next(p for p in opts["providers"] if p["key"] == "qwen")
    assert qwen["available"] is False and "large" in qwen["reason"]
    r = await user.api("POST", f"/api/projects/{pid}/bughunt", {"folder": "", "provider": "qwen"})
    run_id = r.json()["run_id"]

    async def done():
        d = (await user.api("GET", f"/api/bughunt/{run_id}")).json()
        return d if d["run"]["status"] != "running" else None
    d = await wait_for(done, timeout=60)
    assert d["run"]["status"] == "failed" and "large" in d["run"]["error"]


async def test_load_test_run_csv_and_validation(user, target_url):
    pid, _ = await _project(user, "loadtest")
    bad = await user.api("POST", f"/api/projects/{pid}/loadtest/run", {"host": "http://8.8.8.8", "users": 2, "duration_s": 10,
                                                                        "endpoints": [{"method": "GET", "path": "/", "weight": 1}]})
    assert bad.status_code == 400 and "local network" in bad.json()["detail"]
    bad = await user.api("POST", f"/api/projects/{pid}/loadtest/run", {"host": target_url, "users": 2, "duration_s": 10,
                                                                        "endpoints": [{"method": "GET", "path": "http://evil/", "weight": 1}]})
    assert bad.status_code == 400
    body = {"name": "smoke", "host": target_url, "users": 4, "spawn_rate": 4, "duration_s": 10,
            "endpoints": [{"method": "GET", "path": "/", "weight": 3}, {"method": "GET", "path": "/about.html", "weight": 1},
                          {"method": "GET", "path": "/missing", "weight": 1}]}
    r = await user.api("POST", f"/api/projects/{pid}/loadtest/run", body)
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    again = await user.api("POST", f"/api/projects/{pid}/loadtest/run", body)
    assert again.status_code == 409

    async def done():
        d = (await user.api("GET", f"/api/loadtest/{run_id}")).json()
        return d if d["run"]["status"] != "running" else None
    d = await wait_for(done, timeout=90)
    run = d["run"]
    assert run["status"] == "completed", run["error"]
    assert run["total_requests"] > 10 and run["total_failures"] > 0 and d["samples"]
    eps = {e["name"]: e for e in d["endpoints"]}
    assert eps["GET /"]["requests"] > eps["GET /about.html"]["requests"], "weights must shape the traffic"
    assert eps["GET /missing"]["failures"] == eps["GET /missing"]["requests"]
    csv = await user.get(f"/api/loadtest/{run_id}/export.csv")
    assert csv.status_code == 200 and "Endpoint" in csv.text and "GET /about.html" in csv.text
    cfgs = (await user.api("GET", f"/api/projects/{pid}/loadtest/configs")).json()["configs"]
    assert cfgs[0]["name"] == "smoke" and len(cfgs[0]["endpoints"]) == 3


async def test_clone_flow_with_terms_scan_and_import(user, monkeypatch, tmp_path):
    from elvin.services import clone, gitops

    fixture = tmp_path / "repo"
    fixture.mkdir()
    (fixture / "README.md").write_text("# Demo\nCredit the authors when you reuse this project.\n")
    (fixture / "LICENSE").write_text("MIT License\n\nPermission is hereby granted, free of charge, to any person ...")
    (fixture / "main.py").write_text("print('demo')\n")
    real_run = gitops.run

    async def fake_run(args, **kw):
        if "clone" in args:
            shutil.copytree(fixture, args[-1])
            return gitops.CmdResult(0, "", "")
        if "rev-parse" in args:
            return gitops.CmdResult(0, "a" * 40 + "\n", "")
        return await real_run(args, **kw)
    monkeypatch.setattr(gitops, "run", fake_run)
    bad = await user.api("POST", "/api/clone", {"url": "file:///etc"})
    assert bad.status_code == 400
    r = await user.api("POST", "/api/clone", {"url": "https://github.com/someone/demo"})
    assert r.status_code == 202, r.text
    cid = r.json()["clone_id"]

    async def state(s):
        d = (await user.api("GET", f"/api/clone/{cid}")).json()
        return d if d["status"] == s else None
    d = await wait_for(lambda: state("awaiting_agreement"))
    assert d["license_spdx"] == "MIT" and any("copyright notice" in t["text"] for t in d["terms"])
    assert any(t["source"].startswith("README") for t in d["terms"])
    simp = (await user.api("POST", f"/api/clone/{cid}/simplify")).json()["simplified"]
    assert len(simp) == len(d["terms"])
    r = await user.api("POST", f"/api/clone/{cid}/agree", {"agreed": [d["terms"][0]["id"]]})
    assert r.status_code == 400, "every statement must be agreed to"
    r = await user.api("POST", f"/api/clone/{cid}/agree", {"agreed": [t["id"] for t in d["terms"]]})
    assert r.status_code == 202
    d = await wait_for(lambda: state("completed"))
    proj = (await user.api("GET", f"/api/projects/{d['project_id']}/file?path=main.py")).json()
    assert proj["content"] == "print('demo')\n"
    # a repository containing malware is refused and its quarantine deleted
    (fixture / "evil.bin").write_bytes(EICAR)
    r = await user.api("POST", "/api/clone", {"url": "https://github.com/someone/evil"})
    cid = r.json()["clone_id"]
    d = await wait_for(lambda: state("awaiting_agreement"))
    await user.api("POST", f"/api/clone/{cid}/agree", {"agreed": [t["id"] for t in d["terms"]]})
    d = await wait_for(lambda: state("infected"))
    assert "malware" in d["error"] and not clone.quarantine_dir(uuid.UUID(cid)).exists()


async def test_github_stats_without_account_suggests_connect(user):
    s = (await user.api("GET", "/api/github/stats")).json()
    assert any(x["action"] == "connect" and x["level"] == "required" for x in s["suggestions"])
    st = (await user.api("GET", "/api/github/status")).json()
    assert st["account"] is None and st["tools"]["git"] and st["tools"]["gh"]
    r = await user.api("POST", "/api/github/token", {"token": "ghp_" + "x" * 36})
    assert r.status_code == 400


async def test_usage_is_recorded_and_summarised(user):
    pid, _ = await _project(user, "usage")
    r = await user.api("POST", f"/api/projects/{pid}/messages", {"content": "explain the project layout", "idempotency_key": uuid.uuid4().hex})
    from conftest import finished

    await wait_for(lambda: finished(r.json()["task_id"]))
    u = (await user.api("GET", "/api/usage?days=7")).json()
    assert u["totals"]["calls"] >= 1 and any(m["provider"] == "gemma" for m in u["models"])
    assert "recommendations" in u
    o = (await user.api("GET", "/api/settings/options")).json()
    assert len(o["teams"]) == 9 and o["efforts"][0]["id"] == "low"


async def test_system_status_reports_services(user):
    s = (await user.api("GET", "/api/system/status")).json()
    assert s["clamav"]["available"] and s["runner"]["online"] and s["redis"] and s["database"]
    assert s["platform"]["os"] == "macos"


async def test_all_pages_render(user):
    pid, _ = await _project(user, "pages")
    for path in ["/dashboard", "/projects", "/clone", "/settings", "/usage", "/about"] + [f"/p/{pid}/{f}" for f in
                 ("elvin", "editor", "bughunt", "vulns", "loadtest", "git", "history")]:
        r = await user.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert "Traceback" not in r.text
    assert (await user.get("/go/editor")).status_code == 303
    assert (await user.get(f"/p/{pid}/nope")).status_code == 404
    _ = Path
