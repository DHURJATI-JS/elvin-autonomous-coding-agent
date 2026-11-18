"""Cross-account and cross-project leak tests (highest priority)."""
from __future__ import annotations

import uuid

from elvin.services.ai.fake import FakeProvider

from conftest import finished, wait_for


async def test_other_account_cannot_touch_project(user, user2):
    p = await user.new_project("secret-proj")
    pid = p["id"]
    assert (await user.api("PUT", f"/api/projects/{pid}/file", {"path": "secret.txt", "content": "top secret"})).status_code == 200
    assert (await user.api("POST", f"/api/projects/{pid}/memory", {"content": "The password is hunter2"})).status_code == 200
    snaps = (await user.api("GET", f"/api/projects/{pid}/snapshots")).json()["snapshots"]
    probes = [
        ("GET", f"/api/projects/{pid}/tree", None), ("GET", f"/api/projects/{pid}/file?path=secret.txt", None),
        ("GET", f"/api/projects/{pid}/conversation", None), ("GET", f"/api/projects/{pid}/snapshots", None),
        ("GET", f"/api/projects/{pid}/integrity", None), ("GET", f"/api/projects/{pid}/vulns", None),
        ("GET", f"/api/projects/{pid}/bughunt/runs", None), ("GET", f"/api/projects/{pid}/loadtest/runs", None),
        ("GET", f"/api/projects/{pid}/loadtest/configs", None), ("GET", f"/api/projects/{pid}/git", None),
        ("GET", f"/api/projects/{pid}/folders", None), ("PATCH", f"/api/projects/{pid}", {"name": "pwned"}),
        ("POST", f"/api/projects/{pid}/messages", {"content": "hi", "idempotency_key": uuid.uuid4().hex}),
        ("PUT", f"/api/projects/{pid}/file", {"path": "x.txt", "content": "x"}),
        ("POST", f"/api/projects/{pid}/fs", {"op": "delete", "path": "secret.txt"}),
        ("POST", f"/api/projects/{pid}/snapshots", {"label": "x"}), ("POST", f"/api/projects/{pid}/delete", {"confirm_name": "secret-proj"}),
        ("POST", f"/api/projects/{pid}/vulns/scan", None), ("POST", f"/api/projects/{pid}/bughunt", {"folder": "", "provider": "chatgpt"}),
        ("POST", f"/api/projects/{pid}/memory", {"content": "inject"}), ("DELETE", f"/api/projects/{pid}/memory/1", None),
        ("POST", f"/api/snapshots/{snaps[0]['id']}/verify", None), ("POST", f"/api/snapshots/{snaps[0]['id']}/restore", None),
        ("GET", f"/api/snapshots/{snaps[0]['id']}/changes", None), ("GET", f"/p/{pid}/elvin", None), ("GET", f"/p/{pid}/editor", None),
    ]
    for method, path, body in probes:
        r = await user2.api(method, path, json=body)
        assert r.status_code == 404, (method, path, r.status_code, r.text[:200])
        assert "top secret" not in r.text and "hunter2" not in r.text
    listed = (await user2.api("GET", "/api/projects")).json()["projects"]
    assert all(x["id"] != pid for x in listed)
    # the owner still sees everything intact
    r = await user.api("GET", f"/api/projects/{pid}/file?path=secret.txt")
    assert r.json()["content"] == "top secret"


async def test_tasks_and_attachments_are_private(user, user2):
    p = await user.new_project("private-task")
    r = await user.api("POST", f"/api/projects/{p['id']}/messages", {"content": "what does this project do?", "idempotency_key": uuid.uuid4().hex})
    tid = r.json()["task_id"]
    await wait_for(lambda: finished(tid))
    assert (await user2.api("POST", f"/api/tasks/{tid}/cancel")).status_code == 404
    assert (await user2.api("POST", f"/api/tasks/{tid}/retry")).status_code == 404
    assert (await user2.api("POST", f"/api/approvals/{uuid.uuid4()}", {"decision": "approve", "approved": [0]})).status_code == 404
    assert (await user2.api("GET", f"/api/attachments/{uuid.uuid4()}")).status_code == 404


async def test_project_memory_never_crosses_projects(user):
    a = await user.new_project("mem-a")
    b = await user.new_project("mem-b")
    assert (await user.api("POST", f"/api/projects/{a['id']}/memory", {"content": "Project A uses Django"})).status_code == 200
    ca = (await user.api("GET", f"/api/projects/{a['id']}/conversation")).json()
    cb = (await user.api("GET", f"/api/projects/{b['id']}/conversation")).json()
    assert any("Django" in m["content"] for m in ca["memory"]) and not cb["memory"]
    mid = ca["memory"][0]["id"]
    assert (await user.api("DELETE", f"/api/projects/{b['id']}/memory/{mid}")).status_code == 404
    # what the AI is actually sent for project B never contains project A's memory
    FakeProvider.log.clear()
    r = await user.api("POST", f"/api/projects/{b['id']}/messages", {"content": "which framework do we use?", "idempotency_key": uuid.uuid4().hex})
    await wait_for(lambda: finished(r.json()["task_id"]))
    planner_prompts = [t for role, t in FakeProvider.log if role == "planner"]
    assert planner_prompts and all("Django" not in t for t in planner_prompts)
    r = await user.api("POST", f"/api/projects/{a['id']}/messages", {"content": "which framework do we use?", "idempotency_key": uuid.uuid4().hex})
    await wait_for(lambda: finished(r.json()["task_id"]))
    assert any("Django" in t for role, t in FakeProvider.log if role == "planner")


async def test_project_cap_is_race_safe(user):
    from elvin import limits

    existing = len((await user.api("GET", "/api/projects")).json()["projects"])
    import asyncio

    results = await asyncio.gather(*(user.api("POST", "/api/projects", {"name": f"cap-{i}"}) for i in range(limits.MAX_PROJECTS_PER_USER + 5 - existing)))
    ok = sum(1 for r in results if r.status_code == 200)
    assert existing + ok == limits.MAX_PROJECTS_PER_USER
    assert any(r.status_code == 409 for r in results)
