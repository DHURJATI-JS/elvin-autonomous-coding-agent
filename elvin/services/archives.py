"""Safe automatic unpacking of uploaded .zip files.

A zip lands in its "designated" folder: if every entry already sits under one
top-level folder, that folder is used as-is; otherwise the contents go into a
folder named after the archive (assets.zip -> assets/). Path tricks (absolute
paths, "..", drive letters), symlinks, encrypted entries and zip bombs are
refused, and the extracted files are virus-scanned with everything else."""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path, PurePosixPath

from .. import limits
from .fs import PathError, normalize_rel, resolve_in

JUNK = ("__MACOSX/", ".DS_Store", "Thumbs.db")


class ArchiveError(ValueError):
    pass


def extract_zip(staging: Path, rel_zip: str, *, budget_files: int, budget_bytes: int) -> list[str]:
    zpath = resolve_in(staging, rel_zip)
    try:
        zf = zipfile.ZipFile(zpath)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(f"{rel_zip} is not a valid zip file") from exc
    with zf:
        entries = []
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if info.is_dir() or any(name.startswith(j) or name.endswith("/" + j) or name == j for j in JUNK):
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                continue  # symlink entries are never extracted
            if info.flag_bits & 0x1:
                raise ArchiveError(f"{rel_zip} is password-protected - unpack it yourself first")
            if info.compress_size and info.file_size > 50 * 1024 * 1024 and info.file_size / info.compress_size > limits.ZIP_MAX_RATIO:
                raise ArchiveError(f"{rel_zip} looks like a zip bomb - refused")
            try:
                clean = normalize_rel(name)
            except PathError as exc:
                raise ArchiveError(f"{rel_zip} contains an unsafe path ({name!r})") from exc
            entries.append((info, clean))
        if len(entries) > budget_files:
            raise ArchiveError(f"{rel_zip} has too many files")
        if sum(i.file_size for i, _ in entries) > budget_bytes:
            raise ArchiveError(f"{rel_zip} is too large when unpacked")
        tops = {c.split("/", 1)[0] for _, c in entries}
        single_top = len(tops) == 1 and all("/" in c for _, c in entries)
        parent = str(PurePosixPath(rel_zip).parent)
        parent = "" if parent == "." else parent
        folder = parent if single_top else "/".join(x for x in (parent, PurePosixPath(rel_zip).stem) if x)
        written: list[str] = []
        for info, clean in entries:
            rel = normalize_rel(f"{folder}/{clean}" if folder else clean)
            target = resolve_in(staging, rel, for_write=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                copied = 0
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > info.file_size + 1024:  # lying header
                        raise ArchiveError(f"{rel_zip} is corrupt")
                    dst.write(chunk)
            written.append(rel)
    zpath.unlink()
    return written


def unpack_all(staging: Path, files: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Unpack every uploaded .zip in place. Returns (files, unpacked zips, problems)."""
    out = list(files)
    unpacked: list[str] = []
    problems: list[str] = []
    for rel in [f for f in files if f.lower().endswith(".zip")]:
        budget_files = limits.MAX_UPLOAD_FILES - len(out)
        budget_bytes = limits.MAX_UPLOAD_BYTES - sum((staging / f).stat().st_size for f in out if (staging / f).exists())
        try:
            extracted = extract_zip(staging, rel, budget_files=budget_files, budget_bytes=budget_bytes)
        except ArchiveError as exc:
            problems.append(str(exc))
            continue
        out.remove(rel)
        out.extend(extracted)
        unpacked.append(rel)
    return out, unpacked, problems


def clear(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
