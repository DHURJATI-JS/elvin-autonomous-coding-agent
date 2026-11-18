"""Filesystem primitives: confinement to a project folder, atomic writes,
directory walking. Everything here is synchronous - call it through
asyncio.to_thread from async code."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat as stat_mod
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import get_settings

IGNORED_DIRS = {
    ".git", ".elvin", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".next", ".nuxt", ".cache", "dist", "build", "target", ".gradle", ".idea",
    ".terraform", "coverage", ".parcel-cache", ".svelte-kit", ".turbo",
}
IGNORED_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
PROTECTED_TOP = {".git", ".elvin"}  # never written by the editor or by the AI
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"|?*]')


class PathError(ValueError):
    """A path that would leave the project folder or is otherwise unsafe."""


def project_root(owner_id: Any, slug: str) -> Path:
    return get_settings().workspace / str(owner_id) / slug


def normalize_rel(rel: str, *, allow_empty: bool = False) -> str:
    if rel is None:
        raise PathError("Missing path.")
    rel = str(rel).replace("\\", "/").strip()
    if "\x00" in rel:
        raise PathError("Invalid character in path.")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or rel.startswith("~"):
        raise PathError("Absolute paths are not allowed - use a path inside the project.")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathError("Paths may not go above the project folder ('..').")
    for p in parts:
        if len(p) > 255 or _BAD_CHARS.search(p) or p.split(".")[0].lower() in _WINDOWS_RESERVED or p.endswith((" ", ".")) and p not in (".",):
            raise PathError(f"'{p}' is not a valid file or folder name.")
    if not parts and not allow_empty:
        raise PathError("Missing path.")
    if len(parts) > 40:
        raise PathError("Path is nested too deeply.")
    return "/".join(parts)


def resolve_in(root: Path, rel: str, *, for_write: bool = False, allow_root: bool = False) -> Path:
    """Map a project-relative path to an absolute path that is guaranteed to be
    inside `root` (symlinks included). Raises PathError otherwise."""
    clean = normalize_rel(rel, allow_empty=allow_root)
    if for_write and clean.split("/")[0] in PROTECTED_TOP:
        raise PathError(f"'{clean.split('/')[0]}' is managed by Elvin/git and cannot be edited.")
    root_resolved = root.resolve()
    candidate = root / PurePosixPath(clean) if clean else root
    resolved = candidate.resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathError("That path points outside the project folder.")
    if for_write and candidate.is_symlink():
        raise PathError("Refusing to write through a symbolic link.")
    return candidate


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via temp file + fsync + rename: a crash leaves the old file or the
    new file, never a half-written one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PathError("Refusing to write through a symbolic link.")
    tmp = path.parent / f".{path.name}.elvin-{secrets.token_hex(4)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_text(data: bytes) -> bool:
    head = data[:8192]
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        return exc.start >= len(head) - 4  # cut in the middle of a multi-byte char


def walk_files(root: Path, *, max_files: int | None = None) -> Iterator[tuple[str, Path, os.stat_result]]:
    """Yield (relative posix path, absolute path, stat) for regular files,
    skipping ignored folders and never following symlinks."""
    count = 0
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            if name in IGNORED_FILES or ".elvin-" in name and name.endswith(".tmp"):
                continue
            full = Path(dirpath) / name
            try:
                st = full.lstat()
            except OSError:
                continue
            if not stat_mod.S_ISREG(st.st_mode):
                continue
            rel = full.relative_to(root).as_posix()
            yield rel, full, st
            count += 1
            if max_files is not None and count >= max_files:
                return


def build_tree(root: Path, *, max_entries: int, base: str = "") -> dict[str, Any]:
    """Nested folder tree for the UI (folders first, alphabetical)."""
    entries = 0
    truncated = False

    def node_for(path: Path, rel: str) -> list[dict[str, Any]]:
        nonlocal entries, truncated
        out: list[dict[str, Any]] = []
        try:
            items = sorted(os.scandir(path), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError:
            return out
        for item in items:
            if entries >= max_entries:
                truncated = True
                break
            if item.is_symlink():
                continue
            child_rel = f"{rel}/{item.name}" if rel else item.name
            if item.is_dir(follow_symlinks=False):
                if item.name in IGNORED_DIRS:
                    out.append({"name": item.name, "path": child_rel, "type": "dir", "ignored": True, "children": []})
                    entries += 1
                    continue
                entries += 1
                out.append({"name": item.name, "path": child_rel, "type": "dir", "children": node_for(Path(item.path), child_rel)})
            elif item.is_file(follow_symlinks=False):
                if item.name in IGNORED_FILES:
                    continue
                entries += 1
                try:
                    size = item.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
                out.append({"name": item.name, "path": child_rel, "type": "file", "size": size})
        return out

    start = resolve_in(root, base, allow_root=True) if base else root
    children = node_for(start, normalize_rel(base, allow_empty=True)) if start.is_dir() else []
    return {"children": children, "truncated": truncated, "entries": entries}


def list_dirs(root: Path, *, max_entries: int = 2000) -> list[str]:
    """All (non-ignored) folders, for folder pickers."""
    out: list[str] = [""]
    if not root.is_dir():
        return out
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for d in dirnames:
            out.append((Path(dirpath) / d).relative_to(root).as_posix())
            if len(out) >= max_entries:
                return out
    return out


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (slug or "project")[:60]


def language_for(path: str) -> str:
    ext = PurePosixPath(path).suffix.lower()
    name = PurePosixPath(path).name.lower()
    if name == "dockerfile":
        return "dockerfile"
    return {
        ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "jsx",
        ".ts": "typescript", ".tsx": "tsx", ".json": "json", ".html": "html", ".htm": "html", ".css": "css",
        ".scss": "css", ".md": "markdown", ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".sql": "sql",
        ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c", ".cpp": "cpp",
        ".hpp": "cpp", ".cs": "csharp", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".php": "php",
        ".rb": "ruby", ".swift": "swift", ".xml": "xml", ".svg": "xml", ".ps1": "powershell", ".txt": "text",
        ".ini": "text", ".cfg": "text", ".env": "text",
    }.get(ext, "text")
