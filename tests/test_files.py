"""Folder boundary, crash-safe journal, timestamped versions, integrity checks, editor API, uploads + ClamAV."""
from __future__ import annotations

import json
import os
import shutil
import uuid

import pytest

from elvin import db as dbm
from elvin.models import Project
from elvin.services import journal, snapshots
from elvin.services.fs import PathError, normalize_rel, resolve_in

from conftest import EICAR, PASSWORD, project_dir


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../b", "C:\\Windows\\x", "~/x", "a\x00b", "", "con.txt", "a/b:c"])
def test_normalize_rejects(bad):
    with pytest.raises(PathError):
        normalize_rel(bad)


def test_normalize_accepts():
    assert normalize_rel("src//app/./main.py") == "src/app/main.py"
    assert normalize_rel("a\\b.txt") == "a/b.txt"
    assert normalize_rel(".env") == ".env"


def test_resolve_blocks_symlink_escape_and_git(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", root / "link")
    with pytest.raises(PathError):
        resolve_in(root, "link/secret.txt")
    with pytest.raises(PathError):
        resolve_in(root, ".git/config", for_write=True)
    assert resolve_in(root, "ok/file.py") == root / "ok" / "file.py"


def test_journal_all_or_nothing(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "a.txt").write_text("original")
    (root / "blocker").write_text("I am a file, not a folder")
    ops = [journal.Op("write", "a.txt", b"changed"), journal.Op("write", "blocker/b.txt", b"cannot")]
    with pytest.raises(Exception):
        journal.apply_ops(root, tmp_path / "j", ops)
    assert (root / "a.txt").read_text() == "original", "first write must be rolled back when a later one fails"
    assert not list((tmp_path / "j").iterdir())


def test_journal_recovers_after_power_cut(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "a.txt").write_text("before")
    jroot = tmp_path / "journals" / "proj"
    jroot.mkdir(parents=True)
    txn = journal.Transaction(root, jroot)
    ops = [journal.Op("write", "a.txt", b"half-done"), journal.Op("write", "new.txt", b"new")]
    txn.begin(ops)
    txn.apply(ops)  # ...and the machine dies before commit()
    assert (root / "a.txt").read_text() == "half-done"
    recovered = journal.recover_all(tmp_path / "journals")
    assert recovered and (root / "a.txt").read_text() == "before" and not (root / "new.txt").exists()


async def _project(user, name):
    p = await user.new_project(name)
    async with dbm.session() as s:
        proj = await s.get(Project, uuid.UUID(p["id"]))
    return p["id"], proj, project_dir(str(proj.owner_id), proj.slug)


async def test_editor_save_conflict_and_ops(user):
    pid, _, root = await _project(user, "editor")
    r = await user.api("PUT", f"/api/projects/{pid}/file", {"path": "src/app.py", "content": "print(1)\n"})
    assert r.status_code == 200
    sha = r.json()["sha256"]
    (root / "src" / "app.py").write_text("print('changed outside')\n")
    r = await user.api("PUT", f"/api/projects/{pid}/file", {"path": "src/app.py", "content": "print(2)\n", "base_sha256": sha})
    assert r.status_code == 409
    f = (await user.api("GET", f"/api/projects/{pid}/file?path=src/app.py")).json()
    assert f["content"].startswith("print('changed") and f["language"] == "python"
    for path in ("../evil.py", "/etc/passwd", ".git/hooks/x"):
        assert (await user.api("PUT", f"/api/projects/{pid}/file", {"path": path, "content": "x"})).status_code == 400
    assert (await user.api("POST", f"/api/projects/{pid}/fs", {"op": "rename", "path": "src/app.py", "new_path": "src/main.py"})).status_code == 200
    assert (root / "src" / "main.py").exists() and not (root / "src" / "app.py").exists()
    assert (await user.api("POST", f"/api/projects/{pid}/fs", {"op": "delete", "path": "src/main.py"})).status_code == 403, "needs the password"
    assert (await user.api("POST", f"/api/projects/{pid}/fs", {"op": "delete", "path": "src/main.py", "password": PASSWORD})).status_code == 200
    tree = (await user.api("GET", f"/api/projects/{pid}/tree")).json()
    assert any(c["name"] == "README.md" for c in tree["children"])


async def test_versions_verify_tamper_and_restore(user):
    pid, proj, root = await _project(user, "versions")
    await user.api("PUT", f"/api/projects/{pid}/file", {"path": "data.txt", "content": "v1\n"})
    s1 = (await user.api("POST", f"/api/projects/{pid}/snapshots", {"label": "v1"})).json()
    assert s1["verify"]["ok"] is True
    (root / "data.txt").write_text("v2\n")
    s2 = (await user.api("POST", f"/api/projects/{pid}/snapshots", {"label": "v2"})).json()
    changes = (await user.api("GET", f"/api/snapshots/{s2['id']}/changes")).json()
    assert "data.txt" in changes["modified"]
    diff = (await user.api("GET", f"/api/snapshots/{s2['id']}/diff?path=data.txt")).json()["diff"]
    assert "-v1" in diff and "+v2" in diff
    r = (await user.api("POST", f"/api/snapshots/{s1['id']}/restore")).json()
    assert r["ok"] and (root / "data.txt").read_text() == "v1\n"
    # tamper with a stored blob -> verification must fail
    sha = (await user.api("GET", f"/api/projects/{pid}/snapshots")).json()
    blob = snapshots.blob_path(proj.id, snapshots.sha256_bytes(b"v1\n"))
    blob.write_bytes(b"tampered")
    v = (await user.api("POST", f"/api/snapshots/{s1['id']}/verify")).json()
    assert v["ok"] is False and "data.txt" in v["corrupt"]
    # the local manifest copy is checked too
    blob.write_bytes(b"v1\n")
    mf = snapshots.store_dir(proj.id) / "manifests" / f"{s1['id']}.json"
    data = json.loads(mf.read_text())
    data["manifest"]["evil.txt"] = ["0" * 64, 1]
    mf.write_text(json.dumps(data))
    v = (await user.api("POST", f"/api/snapshots/{s1['id']}/verify")).json()
    assert v["ok"] is False and v["local_copy_ok"] is False
    assert sha


async def test_integrity_detects_outside_changes(user):
    pid, _, root = await _project(user, "integrity")
    await user.api("PUT", f"/api/projects/{pid}/file", {"path": "keep.txt", "content": "keep\n"})
    await user.api("PUT", f"/api/projects/{pid}/file", {"path": "gone.txt", "content": "gone\n"})
    await user.api("POST", f"/api/projects/{pid}/snapshots", {"label": "baseline"})
    (root / "gone.txt").unlink()
    (root / "keep.txt").write_text("edited while Elvin was closed\n")
    (root / "new.txt").write_text("new\n")
    rep = (await user.api("GET", f"/api/projects/{pid}/integrity")).json()
    assert not rep["ok"] and "gone.txt" in rep["deleted"] and "keep.txt" in rep["modified"] and "new.txt" in rep["added"]
    # the editor doesn't break on the deleted file
    assert (await user.api("GET", f"/api/projects/{pid}/file?path=gone.txt")).status_code == 404
    res = (await user.api("POST", f"/api/projects/{pid}/integrity/restore", {"paths": ["gone.txt", "keep.txt"]})).json()
    assert set(res["restored"]) == {"gone.txt", "keep.txt"} and (root / "gone.txt").read_text() == "gone\n"
    res = (await user.api("POST", f"/api/projects/{pid}/integrity/accept")).json()
    assert res["report"]["ok"]


async def test_missing_project_folder_recovers(user):
    pid, _, root = await _project(user, "vanished")
    await user.api("PUT", f"/api/projects/{pid}/file", {"path": "main.py", "content": "print('hi')\n"})
    await user.api("POST", f"/api/projects/{pid}/snapshots", {"label": "before deletion"})
    shutil.rmtree(root)
    rep = (await user.api("GET", f"/api/projects/{pid}/integrity")).json()
    assert rep["missing_root"] is True
    assert (await user.api("GET", f"/api/projects/{pid}/tree")).status_code == 200
    assert (await user.get(f"/p/{pid}/elvin")).status_code == 200
    r = await user.api("POST", f"/api/projects/{pid}/integrity/restore", {"paths": []})
    assert r.status_code == 200 and (root / "main.py").read_text() == "print('hi')\n"


async def test_upload_scanned_and_infected_rejected(user):
    files = [("files", ("site/index.html", b"<h1>hi</h1>", "text/html")), ("files", ("site/js/app.js", b"console.log(1)", "text/javascript"))]
    r = await user.http.post("/api/projects/upload", files=files, headers={"X-CSRF-Token": user.csrf})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["name"] == "site" and p["scan_status"] == "clean" and p["scanned"] == 2
    bad = [("files", ("pkg/readme.txt", b"hello", "text/plain")), ("files", ("pkg/eicar.com", EICAR, "application/octet-stream"))]
    r = await user.http.post("/api/projects/upload", files=bad, headers={"X-CSRF-Token": user.csrf})
    assert r.status_code == 422 and "malware" in r.json()["detail"]
    trav = [("files", ("../../escape.txt", b"x", "text/plain"))]
    r = await user.http.post("/api/projects/upload", files=trav, headers={"X-CSRF-Token": user.csrf})
    assert r.status_code == 400


async def test_delete_project_requires_name_and_keeps_files(user):
    pid, proj, root = await _project(user, "to-delete")
    assert (await user.api("POST", f"/api/projects/{pid}/delete", {"confirm_name": "wrong"})).status_code == 400
    r = await user.api("POST", f"/api/projects/{pid}/delete", {"confirm_name": "to-delete", "delete_files": False})
    assert r.status_code == 403 and root.exists(), "needs the password"
    r = await user.api("POST", f"/api/projects/{pid}/delete", {"confirm_name": "to-delete", "delete_files": False, "password": PASSWORD})
    assert r.status_code == 200 and not root.exists()
    from elvin.config import get_settings

    assert any(p.name.startswith("to-delete") for p in (get_settings().home / "removed-projects").iterdir())
    assert (await user.api("GET", f"/api/projects/{pid}/tree")).status_code == 404


async def test_archive_hides_from_nav(user):
    pid, _, _ = await _project(user, "archive-me")
    await user.api("PATCH", f"/api/projects/{pid}", {"archived": True})
    page = (await user.get("/dashboard")).text
    assert f'data-nav-project="{pid}"' not in page
    await user.get(f"/p/{pid}/elvin")  # opening it brings it back
    assert f'data-nav-project="{pid}"' in (await user.get("/dashboard")).text
