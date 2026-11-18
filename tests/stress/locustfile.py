"""Locust stress test of Elvin itself (run against the throwaway elvin-e2e stack
with fake AI models). Every simulated person signs up, creates a project and
then mixes realistic actions: dashboard, chat with Elvin (incl. approving the
permission dialog), editing files, versions, integrity checks, usage, heartbeats.
Expected refusals (409 busy / duplicate) are counted as correct behaviour;
anything 5xx or unexpected is a failure."""
from __future__ import annotations

import random
import re
import time
import uuid

from locust import HttpUser, between, events, task

CSRF = re.compile(r'name="csrf-token" content="([^"]+)"')
STATS = {"tasks_completed": 0, "approvals": 0, "busy_409": 0}


class Person(HttpUser):
    wait_time = between(0.5, 2.5)

    def on_start(self):
        self.name = f"st{uuid.uuid4().hex[:12]}"
        self.csrf = ""
        page = self.client.get("/signup", name="GET /signup")
        self.csrf = CSRF.search(page.text).group(1)
        pw = "Stress-Test-Pass-9!"
        r = self.client.post("/signup", data={"username": self.name, "email": f"{self.name}@example.com", "password": pw,
                                              "confirm": pw, "csrf_token": self.csrf}, allow_redirects=False, name="POST /signup")
        dash = self.client.get("/dashboard", name="GET /dashboard")
        self.csrf = CSRF.search(dash.text).group(1)
        res = self.api("POST", "/api/projects", {"name": f"proj {self.name}"}, name="POST /api/projects")
        self.pid = res.json()["id"] if res.status_code == 200 else None
        self.files = 0

    def api(self, method, path, json=None, name=None, ok=(200,)):
        with self.client.request(method, path, json=json, headers={"X-CSRF-Token": self.csrf}, name=name or path,
                                 catch_response=True) as r:
            if r.status_code in ok:
                r.success()
            else:
                r.failure(f"HTTP {r.status_code}: {r.text[:120]}")
            return r

    @task(5)
    def dashboard(self):
        self.client.get("/dashboard", name="GET /dashboard")
        self.api("GET", "/api/projects", name="GET /api/projects")
        self.api("POST", "/api/presence/heartbeat", name="POST /api/presence/heartbeat")

    @task(4)
    def open_project(self):
        if not self.pid:
            return
        self.client.get(f"/p/{self.pid}/elvin", name="GET /p/[id]/elvin")
        self.api("GET", f"/api/projects/{self.pid}/conversation", name="GET /api/projects/[id]/conversation")
        self.api("GET", f"/api/projects/{self.pid}/integrity", name="GET /api/projects/[id]/integrity")

    @task(4)
    def edit_files(self):
        if not self.pid:
            return
        self.files += 1
        path = f"src/module_{self.files % 7}.py"
        self.api("PUT", f"/api/projects/{self.pid}/file", {"path": path, "content": f"VALUE = {random.randint(0, 10**6)}\n"},
                 name="PUT /api/projects/[id]/file")
        self.api("GET", f"/api/projects/{self.pid}/tree", name="GET /api/projects/[id]/tree")
        self.api("GET", f"/api/projects/{self.pid}/file?path={path}", name="GET /api/projects/[id]/file")

    @task(2)
    def chat_question(self):
        if not self.pid:
            return
        r = self.api("POST", f"/api/projects/{self.pid}/messages",
                     {"content": f"What does this project do? ({uuid.uuid4().hex[:6]})", "idempotency_key": uuid.uuid4().hex},
                     name="POST /api/projects/[id]/messages", ok=(202, 409))
        if r.status_code == 409:
            STATS["busy_409"] += 1
            return
        self._wait_task(r.json()["task_id"])

    @task(2)
    def chat_code_with_approval(self):
        if not self.pid:
            return
        r = self.api("POST", f"/api/projects/{self.pid}/messages",
                     {"content": f"create a file called gen_{uuid.uuid4().hex[:6]}.py", "idempotency_key": uuid.uuid4().hex},
                     name="POST /api/projects/[id]/messages", ok=(202, 409))
        if r.status_code == 409:
            STATS["busy_409"] += 1
            return
        tid = r.json()["task_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            conv = self.api("GET", f"/api/projects/{self.pid}/conversation", name="GET /api/projects/[id]/conversation").json()
            pending = [b for b in conv.get("pending_approvals", []) if b["task_id"] == tid and b["live"]]
            if pending:
                b = pending[0]
                self.api("POST", f"/api/approvals/{b['batch_id']}", {"decision": "approve", "approved": [a["idx"] for a in b["actions"]]},
                         name="POST /api/approvals/[id]", ok=(200, 409))
                STATS["approvals"] += 1
                break
            time.sleep(0.5)
        self._wait_task(tid)

    def _wait_task(self, tid):
        deadline = time.time() + 40
        while time.time() < deadline:
            conv = self.api("GET", f"/api/projects/{self.pid}/conversation", name="GET /api/projects/[id]/conversation").json()
            task = conv.get("tasks", {}).get(tid)
            if task and task["status"] in ("completed", "failed", "cancelled"):
                if task["status"] == "completed":
                    STATS["tasks_completed"] += 1
                else:
                    events.request.fire(request_type="TASK", name="task finished badly", response_time=0, response_length=0,
                                        exception=Exception(f"{task['status']}: {task.get('error')}"))
                return
            time.sleep(0.6)
        events.request.fire(request_type="TASK", name="task timed out", response_time=40000, response_length=0, exception=Exception("timeout"))

    @task(1)
    def versions(self):
        if not self.pid:
            return
        r = self.api("POST", f"/api/projects/{self.pid}/snapshots", {"label": "stress"}, name="POST /api/projects/[id]/snapshots", ok=(200, 409))
        self.api("GET", f"/api/projects/{self.pid}/snapshots", name="GET /api/projects/[id]/snapshots")
        if r.status_code == 200:
            self.api("POST", f"/api/snapshots/{r.json()['id']}/verify", name="POST /api/snapshots/[id]/verify")

    @task(1)
    def misc(self):
        self.api("GET", "/api/usage?days=30", name="GET /api/usage")
        self.api("GET", "/api/settings/options", name="GET /api/settings/options")
        self.client.get("/settings", name="GET /settings")


@events.quitting.add_listener
def _report(environment, **_):
    print("ELVIN-STRESS", STATS)
