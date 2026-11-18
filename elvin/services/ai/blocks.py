"""Parser for Elvin's plain-text change format. Small local models write file
contents far more reliably as raw text between marker lines than as escaped
JSON strings:

    === FILE: src/app.py ===
    ...file content...
    === END FILE ===
    === DELETE: old.py ===
    === MKDIR: assets/img ===
    === RUN: pytest -q ===
    === LIBRARY: pypi requests ===
    === SUMMARY ===
    one-paragraph summary
    === END SUMMARY ===
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_MARK = re.compile(
    r"^\s*(?:#+\s*)?={3,}\s*(END\s+FILE|END\s+SUMMARY|FILE|DELETE|MKDIR|RUN|LIBRARY|SUMMARY)\b\s*:?\s*(.*?)\s*=*\s*$",
    re.I,
)


@dataclass
class ParsedChanges:
    files: list[tuple[str, str]] = field(default_factory=list)  # (path, content)
    deletes: list[str] = field(default_factory=list)
    mkdirs: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    libraries: list[tuple[str, str, str]] = field(default_factory=list)  # (ecosystem, name, version)
    summary: str = ""
    incomplete: list[tuple[str, str]] = field(default_factory=list)  # FILE blocks with no END marker before the output ended

    @property
    def empty(self) -> bool:
        return not (self.files or self.deletes or self.mkdirs or self.commands)


def _strip_fences(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    if len(lines) >= 2 and lines[0].lstrip().startswith("```") and lines[-1].strip() == "```":
        return lines[1:-1]
    return lines


def _clean_path(raw: str) -> str:
    return raw.strip().strip("`'\"").strip()


def parse(text: str) -> ParsedChanges:
    out = ParsedChanges()
    if not text:
        return out
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        m = _MARK.match(lines[i])
        if not m:
            i += 1
            continue
        kind = re.sub(r"\s+", " ", m.group(1).upper())
        arg = m.group(2)
        if kind == "FILE":
            body: list[str] = []
            i += 1
            while i < len(lines):
                m2 = _MARK.match(lines[i])
                if m2 and re.sub(r"\s+", " ", m2.group(1).upper()) in ("END FILE", "FILE", "DELETE", "MKDIR", "RUN", "SUMMARY", "LIBRARY"):
                    break
                body.append(lines[i])
                i += 1
            terminated = i < len(lines)
            if terminated and re.sub(r"\s+", " ", _MARK.match(lines[i]).group(1).upper()) == "END FILE":
                i += 1
            content = "\n".join(_strip_fences(body))
            if content and not content.endswith("\n"):
                content += "\n"
            path = _clean_path(arg)
            if path and terminated:
                out.files.append((path, content))
            elif path:
                out.incomplete.append((path, content))
            continue
        if kind == "SUMMARY":
            body = []
            i += 1
            while i < len(lines):
                m2 = _MARK.match(lines[i])
                if m2:
                    break
                body.append(lines[i])
                i += 1
            if i < len(lines) and re.sub(r"\s+", " ", _MARK.match(lines[i]).group(1).upper()) == "END SUMMARY":
                i += 1
            out.summary = "\n".join(body).strip()
            continue
        if kind == "DELETE" and _clean_path(arg):
            out.deletes.append(_clean_path(arg))
        elif kind == "MKDIR" and _clean_path(arg):
            out.mkdirs.append(_clean_path(arg))
        elif kind == "RUN" and arg.strip():
            out.commands.append(arg.strip().strip("`"))
        elif kind == "LIBRARY" and arg.strip():
            parts = arg.split()
            if len(parts) >= 2:
                out.libraries.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
        i += 1
    return out
