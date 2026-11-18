"""Write-ahead journal - the AI failsafe for network drops and blackouts.

Before Elvin touches any file it records the planned changes and a copy of
every file it is about to overwrite or delete in
<data>/journals/<project>/<txn>/. Only after every write has been fsynced is the
journal marked committed and removed. If the process dies part-way (power cut,
crash, container kill), `recover_all()` runs at the next start and rolls the
half-applied change set back, so code is never left half-written."""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .fs import _fsync_dir, atomic_write_bytes, resolve_in

log = logging.getLogger("elvin.journal")


@dataclass
class Op:
    kind: str  # write | delete | mkdir
    rel: str
    data: bytes | None = None


class Transaction:
    def __init__(self, project_root: Path, journal_root: Path) -> None:
        self.root = project_root
        self.dir = journal_root / f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        self.entries: list[dict] = []
        self.status = "new"

    # -- helpers
    def _write_state(self, status: str) -> None:
        self.status = status
        payload = {"status": status, "root": str(self.root), "entries": self.entries}
        atomic_write_bytes(self.dir / "journal.json", json.dumps(payload).encode())

    def begin(self, ops: list[Op]) -> None:
        (self.dir / "pre").mkdir(parents=True, exist_ok=False)
        for i, op in enumerate(ops):
            target = resolve_in(self.root, op.rel, for_write=True)
            entry = {"i": i, "kind": op.kind, "rel": op.rel, "existed": target.exists(), "was_dir": target.is_dir()}
            if op.kind in ("write", "delete") and target.is_file():
                shutil.copy2(target, self.dir / "pre" / str(i))
                entry["pre"] = str(i)
            self.entries.append(entry)
        _fsync_dir(self.dir / "pre")
        self._write_state("prepared")

    def apply(self, ops: list[Op]) -> None:
        self._write_state("applying")
        for op in ops:
            target = resolve_in(self.root, op.rel, for_write=True)
            if op.kind == "write":
                if target.is_dir():
                    raise IsADirectoryError(op.rel)
                atomic_write_bytes(target, op.data or b"")
            elif op.kind == "delete":
                if target.is_file() or target.is_symlink():
                    target.unlink()
                    _fsync_dir(target.parent)
            elif op.kind == "mkdir":
                target.mkdir(parents=True, exist_ok=True)
            else:
                raise ValueError(f"unknown op {op.kind}")

    def commit(self) -> None:
        self._write_state("committed")
        shutil.rmtree(self.dir, ignore_errors=True)

    def rollback(self) -> None:
        rollback_entries(self.root, self.dir, self.entries)
        shutil.rmtree(self.dir, ignore_errors=True)


def rollback_entries(root: Path, txn_dir: Path, entries: list[dict]) -> None:
    for entry in reversed(entries):
        try:
            target = resolve_in(root, entry["rel"], for_write=True)
        except Exception:  # pragma: no cover - journal written by us
            continue
        try:
            if entry["kind"] in ("write", "delete"):
                if entry.get("pre") is not None:
                    data = (txn_dir / "pre" / entry["pre"]).read_bytes()
                    atomic_write_bytes(target, data)
                elif not entry["existed"] and target.is_file():
                    target.unlink()
            elif entry["kind"] == "mkdir" and not entry["existed"] and target.is_dir():
                try:
                    target.rmdir()  # only if still empty
                except OSError:
                    pass
        except OSError as exc:  # pragma: no cover
            log.error("rollback of %s failed: %s", entry.get("rel"), exc)


def apply_ops(project_root: Path, journal_root: Path, ops: list[Op]) -> None:
    """Apply a change set all-or-nothing."""
    if not ops:
        return
    journal_root.mkdir(parents=True, exist_ok=True)
    txn = Transaction(project_root, journal_root)
    txn.begin(ops)
    try:
        txn.apply(ops)
    except BaseException:
        txn.rollback()
        raise
    txn.commit()


def recover_all(journals_root: Path) -> list[str]:
    """Roll back every transaction that never committed. Returns their ids."""
    recovered: list[str] = []
    if not journals_root.is_dir():
        return recovered
    for project_dir in journals_root.iterdir():
        if not project_dir.is_dir():
            continue
        for txn_dir in sorted(project_dir.iterdir()):
            jfile = txn_dir / "journal.json"
            try:
                state = json.loads(jfile.read_text())
            except (OSError, ValueError):
                shutil.rmtree(txn_dir, ignore_errors=True)  # never got past "begin": nothing applied
                continue
            if state.get("status") != "committed":
                root = Path(state["root"])
                if root.is_dir():
                    rollback_entries(root, txn_dir, state.get("entries", []))
                    recovered.append(txn_dir.name)
                    log.warning("recovered interrupted change set %s in %s", txn_dir.name, root)
            shutil.rmtree(txn_dir, ignore_errors=True)
    return recovered


def journal_dir_for(data_dir: Path, project_id: object) -> Path:
    path = data_dir / "journals" / str(project_id)
    os.makedirs(path, exist_ok=True)
    return path
