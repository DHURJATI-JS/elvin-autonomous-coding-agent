"""Elvin agent: permission dialogs, folder boundary, autosave + verify, stop
(rollback to the starting point), retry, double-send/spam protection,
library gate, memory, sandbox commands, design-from-image, presence timer."""
from __future__ import annotations

import io
import time
import uuid

from sqlalchemy import select

from elvin import db as dbm
from elvin import redisx
from elvin.models import LibraryCheck, LoadTestRun, Project, TaskEvent, User
from elvin.services import tasks

from conftest import finished, pending_batch, project_dir, task_row, wait_for


async def _ctx(user, name):
    p = await user.new_project(name)
    async with dbm.session() as s:
        proj = await s.get(Project, uuid.UUID(p["id"]))
    return p["id"], project_dir(str(proj.owner_id), proj.slug)


async def send(user, pid, content, key=None, **extra):
    return await user.api("POST", f"/api/projects/{pid}/messages", {"content": content, "idempotency_key": key or uuid.uuid4().hex, **extra})


async def approve_all(user, batch):
    r = await user.api("POST", f"/api/approvals/{batch.id}", {"decision": "approve", "approved": [a["idx"] for a in batch.actions]})
    assert r.status_code == 200, r.text


async def events(task_id):
    async with dbm.session() as s:
        return list((await s.execute(select(TaskEvent).where(TaskEvent.task_id == uuid.UUID(task_id)).order_by(TaskEvent.id))).scalars().all())


async def test_full_flow_requires_permission_then_autosaves(user):
    pid, root = await _ctx(user, "agent-flow")
    r = await send(user, pid, "create a file called hello.py that prints hello")
    assert r.status_code == 202
    tid = r.json()["task_id"]
    batch = await wait_for(lambda: pending_batch(tid))
    assert not (root / "hello.py").exists(), "nothing may be written before permission"
    act = batch.actions[0]
    assert act["type"] == "write" and act["path"] == "hello.py" and "+def hello" in act["diff"]
    await approve_all(user, batch)
    t = await wait_for(lambda: finished(tid))
    assert t.status == "completed", t.error
    assert (root / "hello.py").read_text().startswith("def hello")
    snaps = (await user.api("GET", f"/api/projects/{pid}/snapshots")).json()["snapshots"]
    assert any(s["kind"] == "step" and s["verify_ok"] for s in snaps)
    assert any(s["kind"] == "task_end" for s in snaps)
    conv = (await user.api("GET", f"/api/projects/{pid}/conversation")).json()
    assert conv["messages"][-1]["role"] == "elvin"
    types = [e.type for e in await events(tid)]
    for expected in ("plan", "approval_required", "approval_decided", "applied", "autosaved", "verified", "done"):
        assert expected in types


async def test_reject_changes_nothing(user):
    pid, root = await _ctx(user, "agent-reject")
    tid = (await send(user, pid, "create a file called nope.py")).json()["task_id"]
    batch = await wait_for(lambda: pending_batch(tid))
    await user.api("POST", f"/api/approvals/{batch.id}", {"decision": "reject", "approved": []})
    t = await wait_for(lambda: finished(tid))
    assert t.status == "completed" and not (root / "nope.py").exists()


async def test_partial_approval_only_applies_ticked(user):
    pid, root = await _ctx(user, "agent-partial")
    tid = (await send(user, pid, "create a file called keep.py and run it")).json()["task_id"]
    batch = await wait_for(lambda: pending_batch(tid))
    writes = [a["idx"] for a in batch.actions if a["type"] == "write"]
    assert any(a["type"] == "run" for a in batch.actions)
    await user.api("POST", f"/api/approvals/{batch.id}", {"decision": "approve", "approved": writes})
    t = await wait_for(lambda: finished(tid))
    assert t.status == "completed" and (root / "keep.py").exists()
    assert not any(e.type == "command" for e in await events(tid)), "an unapproved command must not run"


async def test_ai_cannot_write_outside_project(user):
    pid, root = await _ctx(user, "agent-escape")
    tid = (await send(user, pid, "create a file called inside.py and escape the folder")).json()["task_id"]
    batch = await wait_for(lambda: pending_batch(tid))
    assert all(".." not in a["path"] for a in batch.actions)
    await approve_all(user, batch)
    await wait_for(lambda: finished(tid))
    assert not (root.parent / "outside.txt").exists()
    assert any(e.type in ("blocked", "warning") for e in await events(tid))


async def test_stop_restores_starting_point_then_retry(user):
    pid, root = await _ctx(user, "agent-stop")
    readme_before = (root / "README.md").read_text()
    tid = (await send(user, pid, "create a file called a.py in two steps")).json()["task_id"]
    b1 = await wait_for(lambda: pending_batch(tid))
    await approve_all(user, b1)
    await wait_for(lambda: (root / "a.py").exists())
    b2 = await wait_for(lambda: pending_batch(tid))  # step 2 waiting for permission
    assert b2.id != b1.id
    r = await user.api("POST", f"/api/tasks/{tid}/cancel")
    assert r.status_code == 200
    t = await wait_for(lambda: finished(tid))
    assert t.status == "cancelled"
    assert not (root / "a.py").exists(), "stop must restore the state from before the task"
    assert (root / "README.md").read_text() == readme_before
    snaps = (await user.api("GET", f"/api/projects/{pid}/snapshots")).json()["snapshots"]
    assert any(s["kind"] == "partial" for s in snaps), "partial work is kept in history"
    # retry recreates the task from the original starting point
    r = await user.api("POST", f"/api/tasks/{tid}/retry")
    assert r.status_code == 202
    new_tid = r.json()["task_id"]
    assert new_tid != tid and str((await task_row(new_tid)).retry_of) == tid
    b = await wait_for(lambda: pending_batch(new_tid))
    await approve_all(user, b)
    b2 = await wait_for(lambda: pending_batch(new_tid))
    await approve_all(user, b2)
    t = await wait_for(lambda: finished(new_tid))
    assert t.status == "completed" and (root / "a.py").exists() and (root / "README.md").exists()


async def test_double_send_and_busy_and_duplicate(user):
    pid, _ = await _ctx(user, "agent-dup")
    key = uuid.uuid4().hex
    r1 = await send(user, pid, "create a file called d.py", key=key)
    r2 = await send(user, pid, "create a file called d.py", key=key)
    assert r1.json()["task_id"] == r2.json()["task_id"] and r2.json()["duplicate"] is True
    tid = r1.json()["task_id"]
    await wait_for(lambda: pending_batch(tid))
    r3 = await send(user, pid, "something else entirely")
    assert r3.status_code == 409, "sending is only possible when Elvin is not working"
    await user.api("POST", f"/api/tasks/{tid}/cancel")
    await wait_for(lambda: finished(tid))
    q = await send(user, pid, "what is in this project?")
    await wait_for(lambda: finished(q.json()["task_id"]))
    again = await send(user, pid, "What is in   this project?")
    assert again.status_code == 409 and "exactly this message" in again.json()["detail"]


async def test_chat_rate_limit(user):
    pid, _ = await _ctx(user, "agent-rate")
    statuses = []
    for i in range(8):
        r = await send(user, pid, f"question number {i} about nothing")
        statuses.append(r.status_code)
        if r.status_code == 202:
            await wait_for(lambda: finished(r.json()["task_id"]))
    assert statuses.count(202) == 6 and statuses[-1] == 429


async def test_library_gate(user):
    pid, root = await _ctx(user, "agent-libs")
    tid = (await send(user, pid, "create a file called app.py use library PyPI:vulnlib@1.0.0 use library PyPI:malpkg use library PyPI:doesnotexist-xyz")).json()["task_id"]
    batch = await wait_for(lambda: pending_batch(tid))
    await approve_all(user, batch)
    await wait_for(lambda: finished(tid))
    async with dbm.session() as s:
        checks = {c.name: c for c in (await s.execute(select(LibraryCheck).where(LibraryCheck.project_id == uuid.UUID(pid)))).scalars()}
    assert checks["vulnlib"].verdict == "go_with_version" and "2.1.0" in checks["vulnlib"].recommended
    assert checks["malpkg"].verdict == "blocked"
    assert checks["doesnotexist-xyz"].verdict == "blocked"
    evs = [e for e in await events(tid) if e.type == "library"]
    assert {e.data["name"] for e in evs} >= {"vulnlib", "malpkg", "doesnotexist-xyz"}


async def test_memory_is_learned_once(user):
    pid, _ = await _ctx(user, "agent-memory")
    tid = (await send(user, pid, "Remember: always use type hints. What files exist?")).json()["task_id"]
    await wait_for(lambda: finished(tid))

    async def remembered():
        conv = (await user.api("GET", f"/api/projects/{pid}/conversation")).json()
        return [m for m in conv["memory"] if "type hints" in m["content"]]
    assert await wait_for(remembered)


async def test_command_runs_in_sandbox(user):
    pid, _ = await _ctx(user, "agent-run")
    tid = (await send(user, pid, "create a file called runme.py and run it")).json()["task_id"]
    batch = await wait_for(lambda: pending_batch(tid))
    await approve_all(user, batch)
    t = await wait_for(lambda: finished(tid), timeout=90)
    assert t.status == "completed", t.error
    cmds = [e.data for e in await events(tid) if e.type == "command"]
    assert cmds and cmds[0]["exit_code"] == 0 and "hello from elvin" in cmds[0]["stdout"]


async def test_design_from_image(user):
    from PIL import Image

    pid, root = await _ctx(user, "agent-design")
    buf = io.BytesIO()
    Image.new("RGB", (320, 200), (15, 23, 42)).save(buf, "PNG")
    r = await user.http.post(f"/api/projects/{pid}/attachments", files={"image": ("design.png", buf.getvalue(), "image/png")},
                             headers={"X-CSRF-Token": user.csrf})
    assert r.status_code == 200, r.text
    att = r.json()["id"]
    assert (await user.get(f"/api/attachments/{att}")).headers["content-type"] == "image/png"
    tid = (await send(user, pid, "build this landing page", attachments=[att])).json()["task_id"]
    batch = await wait_for(lambda: pending_batch(tid))
    await approve_all(user, batch)
    t = await wait_for(lambda: finished(tid), timeout=120)
    assert t.status == "completed", t.error
    assert (root / "design" / "index.html").exists()
    types = [e.type for e in await events(tid)]
    assert "screenshot" in types and "design_verdict" in types


async def test_not_an_image_is_rejected(user):
    pid, _ = await _ctx(user, "agent-badimg")
    r = await user.http.post(f"/api/projects/{pid}/attachments", files={"image": ("x.png", b"not really a png", "image/png")},
                             headers={"X-CSRF-Token": user.csrf})
    assert r.status_code == 400


async def test_tab_closed_for_90s_cancels_tasks(user):
    pid, root = await _ctx(user, "agent-presence")
    tid = (await send(user, pid, "create a file called p.py")).json()["task_id"]
    await wait_for(lambda: pending_batch(tid))
    t = await task_row(tid)
    long_ago = time.time() - 200
    await redisx.call("set", f"elvin:left:{t.user_id}", str(long_ago))
    await redisx.call("set", f"elvin:presence:{t.user_id}", str(long_ago - 1))
    tasks.presence.left_at[t.user_id] = long_ago
    tasks.presence.last_seen[t.user_id] = long_ago - 1
    cancelled = await tasks.manager.reap_once()
    assert t.user_id in cancelled
    t = await wait_for(lambda: finished(tid))
    assert t.status == "cancelled" and "90 seconds" in (t.error or "")


async def test_present_user_keeps_tasks(user):
    pid, _ = await _ctx(user, "agent-present")
    tid = (await send(user, pid, "create a file called q.py")).json()["task_id"]
    await wait_for(lambda: pending_batch(tid))
    await user.api("POST", "/api/presence/heartbeat")
    t = await task_row(tid)
    assert t.user_id not in await tasks.manager.reap_once()
    await user.api("POST", f"/api/tasks/{tid}/cancel")
    await wait_for(lambda: finished(tid))


async def test_startup_recovery_marks_interrupted(user):
    from elvin.models import Task

    async with dbm.session() as s:
        u = (await s.execute(select(User).where(User.username == user.username))).scalar_one()
        t = Task(user_id=u.id, kind="code", status="running", prompt="x")
        s.add(t)
        await s.commit()
        tid = t.id
    await tasks.recover_on_startup()
    assert (await task_row(str(tid))).status == "interrupted"


async def test_stress_test_request_launches_locust(user, target_url):
    pid, _ = await _ctx(user, "agent-locust")
    tid = (await send(user, pid, f"stress test {target_url} with 3 users for 10 seconds")).json()["task_id"]
    batch = await wait_for(lambda: pending_batch(tid))
    assert batch.actions[0]["type"] == "loadtest" and batch.actions[0]["loadtest"]["users"] == 3
    await approve_all(user, batch)
    await wait_for(lambda: finished(tid))

    async def run_done():
        async with dbm.session() as s:
            run = (await s.execute(select(LoadTestRun).where(LoadTestRun.project_id == uuid.UUID(pid)))).scalar_one_or_none()
        return run if run and run.status != "running" else None
    run = await wait_for(run_done, timeout=60)
    assert run.status == "completed" and run.total_requests > 0, run.error

    async def reported():
        conv = (await user.api("GET", f"/api/projects/{pid}/conversation")).json()
        return [m for m in conv["messages"] if (m["meta"] or {}).get("kind") == "loadtest_report"]
    assert await wait_for(reported)
