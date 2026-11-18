"""Vulnerability detector (OSV.dev) and the library safety gate.

* `scan_project` reads dependency manifests (requirements*.txt, pyproject.toml,
  package.json / package-lock.json, go.mod, Cargo.lock, Gemfile.lock,
  composer.lock), asks OSV.dev for known vulnerabilities and recommends the
  smallest upgrade that fixes them (or a replacement when no fix exists).
* `check_library` is the gate Elvin must pass before it may use a library:
  "go", "go_with_version" (use the safe version) or "blocked" (with a checked
  alternative). Packages that do not exist on their registry are blocked too -
  AI models sometimes invent package names that attackers then register."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import tomllib
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from packaging.version import InvalidVersion, Version
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import limits, redisx
from ..config import get_settings
from ..models import LibraryCheck, Project, VulnFinding, VulnScan, utcnow
from .ai.providers import http
from .fs import IGNORED_DIRS, project_root

log = logging.getLogger("elvin.vulns")

ECOSYSTEMS = {
    "pypi": "PyPI", "python": "PyPI", "pip": "PyPI", "npm": "npm", "node": "npm", "javascript": "npm", "go": "Go",
    "golang": "Go", "crates.io": "crates.io", "cargo": "crates.io", "rust": "crates.io", "rubygems": "RubyGems",
    "ruby": "RubyGems", "gem": "RubyGems", "packagist": "Packagist", "composer": "Packagist", "php": "Packagist",
}
MANIFEST_NAMES = {"package.json", "package-lock.json", "pyproject.toml", "go.mod", "Cargo.lock", "Gemfile.lock",
                  "composer.lock", "Pipfile"}
_details_cache: dict[str, dict] = {}


def norm_ecosystem(value: str) -> str:
    return ECOSYSTEMS.get((value or "").strip().lower(), value)


def norm_name(ecosystem: str, name: str) -> str:
    name = name.strip()
    if ecosystem == "PyPI":
        return re.sub(r"[-_.]+", "-", name).lower()
    return name


# --------------------------------------------------------------------------- versions
def _generic_key(v: str) -> tuple:
    parts = re.split(r"[.\-+]", v.lstrip("vV"))
    key = []
    for p in parts:
        key.append((0, int(p), "") if p.isdigit() else (1, 0, p))
    return tuple(key)


def vless(ecosystem: str, a: str, b: str) -> bool:
    if ecosystem == "PyPI":
        try:
            return Version(a) < Version(b)
        except InvalidVersion:
            pass
    return _generic_key(a) < _generic_key(b)


def base_version(spec: str) -> str:
    m = re.search(r"(\d+(?:\.\d+){0,3}(?:[-.]?[A-Za-z0-9.]+)?)", spec or "")
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- CVSS
_CVSS3 = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}, "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62}, "C": {"H": 0.56, "L": 0.22, "N": 0.0},
}


def _roundup(x: float) -> float:
    i = round(x * 100000)
    return i / 100000.0 if i % 10000 == 0 else (math.floor(i / 10000) + 1) / 10.0


def cvss3_score(vector: str) -> float | None:
    try:
        metrics = dict(part.split(":", 1) for part in vector.split("/")[1:])
        scope_changed = metrics["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if scope_changed else 0.62, "H": 0.5 if scope_changed else 0.27}[metrics["PR"]]
        c, i, a = (_CVSS3["C"][metrics[k]] for k in ("C", "I", "A"))
        iss = 1 - (1 - c) * (1 - i) * (1 - a)
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if scope_changed else 6.42 * iss
        exploit = 8.22 * _CVSS3["AV"][metrics["AV"]] * _CVSS3["AC"][metrics["AC"]] * pr * _CVSS3["UI"][metrics["UI"]]
        if impact <= 0:
            return 0.0
        return _roundup(min(1.08 * (impact + exploit), 10) if scope_changed else min(impact + exploit, 10))
    except (KeyError, ValueError, IndexError):
        return None


def severity_of(vuln: dict) -> tuple[str, float | None]:
    best: float | None = None
    for sev in vuln.get("severity") or []:
        if str(sev.get("type", "")).startswith("CVSS_V3"):
            score = cvss3_score(sev.get("score", ""))
            if score is not None and (best is None or score > best):
                best = score
    if best is not None:
        label = "CRITICAL" if best >= 9 else "HIGH" if best >= 7 else "MEDIUM" if best >= 4 else "LOW" if best > 0 else "NONE"
        return label, best
    ds = str((vuln.get("database_specific") or {}).get("severity", "")).upper()
    label = {"MODERATE": "MEDIUM"}.get(ds, ds)
    if label in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        return label, None
    if vuln.get("id", "").startswith("MAL-"):
        return "CRITICAL", 10.0
    return "UNKNOWN", None


SEVERITY_RANK = {"CRITICAL": 5, "HIGH": 4, "MEDIUM": 3, "LOW": 2, "UNKNOWN": 1, "NONE": 0}


# --------------------------------------------------------------------------- manifests
def _pep508(spec: str) -> tuple[str, str, bool] | None:
    spec = spec.split(";", 1)[0].strip()
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$", spec)
    if not m:
        return None
    name, rest = m.group(1), m.group(3).strip().strip("()")
    exact = re.match(r"^===?\s*([^,\s]+)", rest)
    if exact:
        return name, exact.group(1), True
    lower = re.search(r"(?:>=|~=|\^)\s*([^,\s]+)", rest)
    return name, lower.group(1) if lower else "", False


def parse_manifest(path: str, text: str) -> list[dict[str, Any]]:
    """Return [{ecosystem, name, version, pinned}] for one manifest file."""
    deps: list[dict[str, Any]] = []
    name = Path(path).name

    def add(eco: str, n: str, v: str, pinned: bool) -> None:
        if n:
            deps.append({"ecosystem": eco, "name": n.strip(), "version": (v or "").strip(), "pinned": pinned, "manifest": path})

    try:
        if re.match(r"^requirements.*\.(txt|in)$", name) or name.endswith(".requirements.txt"):
            for raw in text.splitlines():
                line = raw.split(" #", 1)[0].strip()
                if not line or line.startswith(("#", "-", "git+", "http:", "https:", "file:")):
                    continue
                parsed = _pep508(line)
                if parsed:
                    add("PyPI", *parsed)
        elif name == "pyproject.toml":
            data = tomllib.loads(text)
            for spec in (data.get("project", {}).get("dependencies") or []):
                parsed = _pep508(spec)
                if parsed:
                    add("PyPI", *parsed)
            for group in (data.get("project", {}).get("optional-dependencies") or {}).values():
                for spec in group:
                    parsed = _pep508(spec)
                    if parsed:
                        add("PyPI", *parsed)
            poetry = data.get("tool", {}).get("poetry", {})
            sections = [poetry.get("dependencies") or {}, poetry.get("dev-dependencies") or {}]
            sections += [g.get("dependencies") or {} for g in (poetry.get("group") or {}).values()]
            for section in sections:
                for n, v in section.items():
                    if n.lower() == "python":
                        continue
                    ver = v.get("version", "") if isinstance(v, dict) else str(v)
                    add("PyPI", n, base_version(ver), ver.startswith("=="))
        elif name == "Pipfile":
            data = tomllib.loads(text)
            for section in ("packages", "dev-packages"):
                for n, v in (data.get(section) or {}).items():
                    ver = v.get("version", "") if isinstance(v, dict) else str(v)
                    add("PyPI", n, base_version(ver) if ver != "*" else "", ver.startswith("=="))
        elif name == "package.json":
            data = json.loads(text)
            for section in ("dependencies", "devDependencies", "optionalDependencies"):
                for n, v in (data.get(section) or {}).items():
                    v = str(v)
                    if v.startswith(("git", "file:", "link:", "workspace:", "http", "npm:")):
                        continue
                    add("npm", n, base_version(v), bool(re.fullmatch(r"\d+\.\d+\.\d+.*", v)))
        elif name == "package-lock.json":
            data = json.loads(text)
            if "packages" in data:
                for key, info in data["packages"].items():
                    if not key or "version" not in info:
                        continue
                    add("npm", key.split("node_modules/")[-1], info["version"], True)
            else:
                def walk(d: dict) -> None:
                    for n, info in (d or {}).items():
                        if isinstance(info, dict) and "version" in info:
                            add("npm", n, info["version"], True)
                            walk(info.get("dependencies") or {})
                walk(data.get("dependencies") or {})
        elif name == "go.mod":
            block = False
            for raw in text.splitlines():
                line = raw.split("//", 1)[0].strip()
                if line.startswith("require ("):
                    block = True
                    continue
                if block and line == ")":
                    block = False
                    continue
                m = re.match(r"^(?:require\s+)?([\w.\-~/]+\.[\w.\-~/]+)\s+(v[\w.\-+]+)$", line)
                if m and (block or raw.strip().startswith("require")):
                    add("Go", m.group(1), m.group(2).lstrip("v"), True)
        elif name == "Cargo.lock":
            for pkg in tomllib.loads(text).get("package", []):
                if pkg.get("source"):  # skip the workspace's own crates
                    add("crates.io", pkg.get("name", ""), pkg.get("version", ""), True)
        elif name == "Gemfile.lock":
            in_specs = False
            for raw in text.splitlines():
                if raw.strip() == "specs:":
                    in_specs = True
                    continue
                if in_specs and raw and not raw.startswith(" "):
                    in_specs = False
                m = re.match(r"^ {4}([A-Za-z0-9_.\-]+) \(([^)]+)\)$", raw)
                if in_specs and m:
                    add("RubyGems", m.group(1), m.group(2).split("-")[0], True)
        elif name == "composer.lock":
            data = json.loads(text)
            for pkg in (data.get("packages") or []) + (data.get("packages-dev") or []):
                add("Packagist", pkg.get("name", ""), str(pkg.get("version", "")).lstrip("v"), True)
    except (ValueError, tomllib.TOMLDecodeError, AttributeError, TypeError) as exc:
        log.info("could not parse %s: %s", path, exc)
    return deps


def find_manifests(root: Path, max_files: int = 50) -> list[tuple[str, str]]:
    import os

    found: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for fn in sorted(filenames):
            if fn in MANIFEST_NAMES or re.match(r"^requirements.*\.(txt|in)$", fn):
                full = Path(dirpath) / fn
                try:
                    if full.stat().st_size > 5_000_000:
                        continue
                    found.append((full.relative_to(root).as_posix(), full.read_text(errors="replace")))
                except OSError:
                    continue
                if len(found) >= max_files:
                    return found
    return found


# --------------------------------------------------------------------------- registries & OSV
async def latest_version(ecosystem: str, name: str) -> tuple[bool | None, str]:
    """(exists?, latest version). exists is None when the registry can't be asked."""
    s = get_settings()
    cache_key = f"elvin:reg:{ecosystem}:{name.lower()}"
    cached = await redisx.call("get", cache_key, default=None)
    if cached:
        data = json.loads(cached)
        return data["exists"], data["version"]
    try:
        if ecosystem == "PyPI":
            r = await http().get(f"{s.pypi_url}/pypi/{quote(name)}/json", timeout=15)
            exists, version = (r.status_code == 200, r.json()["info"]["version"] if r.status_code == 200 else "")
        elif ecosystem == "npm":
            r = await http().get(f"{s.npm_registry_url}/{quote(name, safe='@')}/latest", timeout=15)
            exists, version = (r.status_code == 200, r.json().get("version", "") if r.status_code == 200 else "")
        else:
            return None, ""
        if r.status_code not in (200, 404):
            return None, ""
    except (httpx.HTTPError, ValueError, KeyError):
        return None, ""
    await redisx.call("set", cache_key, json.dumps({"exists": exists, "version": version}), ex=6 * 3600)
    return exists, version


async def osv_batch(packages: list[tuple[str, str, str]]) -> list[list[str]]:
    """packages: (ecosystem, name, version). Returns vuln ids per package."""
    s = get_settings()
    results: list[list[str]] = [[] for _ in packages]
    for start in range(0, len(packages), 500):
        chunk = packages[start:start + 500]
        queries = []
        for eco, name, ver in chunk:
            q: dict[str, Any] = {"package": {"name": name, "ecosystem": eco}}
            if ver:
                q["version"] = ver
            queries.append(q)
        try:
            r = await http().post(f"{s.osv_url}/v1/querybatch", json={"queries": queries}, timeout=60)
            r.raise_for_status()
            for i, res in enumerate(r.json().get("results", [])):
                results[start + i] = [v["id"] for v in (res.get("vulns") or [])]
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"OSV.dev could not be reached ({exc.__class__.__name__}). Check the internet connection.") from exc
    return results


async def osv_details(vuln_id: str) -> dict:
    if vuln_id in _details_cache:
        return _details_cache[vuln_id]
    cached = await redisx.call("get", f"elvin:osv:{vuln_id}", default=None)
    if cached:
        data = json.loads(cached)
    else:
        try:
            r = await http().get(f"{get_settings().osv_url}/v1/vulns/{quote(vuln_id)}", timeout=30)
            data = r.json() if r.status_code == 200 else {"id": vuln_id}
        except (httpx.HTTPError, ValueError):
            data = {"id": vuln_id}
        await redisx.call("set", f"elvin:osv:{vuln_id}", json.dumps(data), ex=24 * 3600)
    if len(_details_cache) > 5000:
        _details_cache.clear()
    _details_cache[vuln_id] = data
    return data


def fixed_versions(vuln: dict, ecosystem: str, name: str, current: str) -> tuple[str | None, bool]:
    """Smallest fixed version above `current` for this vuln -> (version, has_fix)."""
    target = norm_name(ecosystem, name)
    candidates: list[str] = []
    for aff in vuln.get("affected") or []:
        pkg = aff.get("package") or {}
        if pkg.get("ecosystem") != ecosystem or norm_name(ecosystem, pkg.get("name", "")) != target:
            continue
        for rng in aff.get("ranges") or []:
            for ev in rng.get("events") or []:
                if "fixed" in ev:
                    candidates.append(str(ev["fixed"]).lstrip("v") if ecosystem == "Go" else str(ev["fixed"]))
    above = [c for c in candidates if not current or vless(ecosystem, current, c)]
    if not above:
        return None, False
    best = above[0]
    for c in above[1:]:
        if vless(ecosystem, c, best):
            best = c
    return best, True


async def analyse(ecosystem: str, name: str, version: str, ids: list[str]) -> dict[str, Any]:
    details = await asyncio.gather(*(osv_details(i) for i in ids[:40]))
    worst, score, summary = "UNKNOWN", None, ""
    needed: str | None = None
    unfixed: list[str] = []
    malicious = False
    aliases: list[str] = []
    for d in details:
        label, sc = severity_of(d)
        if SEVERITY_RANK.get(label, 0) > SEVERITY_RANK.get(worst, 0) or (label == worst and (sc or 0) > (score or 0)):
            worst, score = label, sc
        summary = summary or d.get("summary") or (d.get("details") or "")[:240]
        malicious = malicious or d.get("id", "").startswith("MAL-")
        aliases += [a for a in d.get("aliases") or [] if a.startswith("CVE-")]
        fx, has_fix = fixed_versions(d, ecosystem, name, version)
        if has_fix and fx:
            if needed is None or vless(ecosystem, needed, fx):
                needed = fx
        else:
            unfixed.append(d.get("id", ""))
    if malicious:
        rec = (f"'{name}' is a known MALICIOUS package. Remove it now, and rotate any passwords or keys on machines "
               "where it was installed.")
    elif unfixed:
        rec = (f"No fixed release of {name} exists for {', '.join(unfixed[:3])}. Replace {name} with a maintained "
               "alternative (use 'Suggest alternatives').")
        if needed:
            rec += f" Upgrading to {needed} at least fixes the other advisories."
    else:
        rec = f"Upgrade {name} from {version or 'the current version'} to {needed} (fixes {len(ids)} known advisor{'y' if len(ids) == 1 else 'ies'})."
    return {"severity": worst, "score": score, "summary": summary, "fixed_version": needed, "recommendation": rec,
            "malicious": malicious, "unfixed": unfixed, "aliases": sorted(set(aliases))[:10]}


# --------------------------------------------------------------------------- project scan
async def scan_project(db: AsyncSession, project: Project) -> VulnScan:
    scan = VulnScan(project_id=project.id, status="running")
    db.add(scan)
    await db.commit()
    try:
        root = project_root(project.owner_id, project.slug)
        manifests = await asyncio.to_thread(find_manifests, root)
        deps: dict[tuple[str, str, str], dict] = {}
        for path, text in manifests:
            for d in parse_manifest(path, text):
                key = (d["ecosystem"], norm_name(d["ecosystem"], d["name"]), d["version"])
                deps.setdefault(key, d)
                if len(deps) >= limits.VULN_MAX_DEPENDENCIES:
                    break
        dep_list = list(deps.values())
        # resolve unpinned PyPI / npm packages to their latest release (bounded)
        sem = asyncio.Semaphore(8)
        missing: list[dict] = []

        async def resolve(d: dict) -> None:
            if d["ecosystem"] not in ("PyPI", "npm"):
                return
            async with sem:
                exists, latest = await latest_version(d["ecosystem"], d["name"])
            if exists is False:
                missing.append(d)
            elif not d["version"] and latest:
                d["version"] = latest
                d["resolved_latest"] = True

        await asyncio.gather(*(resolve(d) for d in dep_list[:300]))
        ids = await osv_batch([(d["ecosystem"], d["name"], d["version"]) for d in dep_list])
        counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}
        vulnerable = 0
        for d, vids in zip(dep_list, ids):
            if not vids:
                continue
            vulnerable += 1
            info = await analyse(d["ecosystem"], d["name"], d["version"], vids)
            counts[info["severity"] if info["severity"] in counts else "UNKNOWN"] += 1
            db.add(VulnFinding(scan_id=scan.id, ecosystem=d["ecosystem"], package=d["name"], version=d["version"],
                               manifest=d["manifest"], vuln_ids=vids[:40], severity=info["severity"], score=info["score"],
                               summary=info["summary"][:2000], fixed_version=info["fixed_version"],
                               recommendation=info["recommendation"],
                               details={"malicious": info["malicious"], "unfixed": info["unfixed"], "aliases": info["aliases"],
                                        "resolved_latest": d.get("resolved_latest", False)}))
        for d in missing[:50]:
            counts["HIGH"] += 1
            db.add(VulnFinding(scan_id=scan.id, ecosystem=d["ecosystem"], package=d["name"], version=d["version"],
                               manifest=d["manifest"], vuln_ids=[], severity="HIGH",
                               summary="This package does not exist on its public registry.",
                               recommendation=f"'{d['name']}' was not found on {d['ecosystem']}. It may be a typo or an invented "
                                              "name - an attacker could register it. Remove it or fix the name.",
                               details={"missing": True}))
        scan.summary = {"manifests": [m for m, _ in manifests], "dependencies": len(dep_list),
                        "vulnerable": vulnerable + len(missing), "counts": counts}
        scan.status = "completed"
    except Exception as exc:  # report, never crash the request
        log.warning("vulnerability scan failed: %s", exc)
        scan.status = "failed"
        scan.error = str(exc)[:500]
    scan.finished_at = utcnow()
    await db.commit()
    await _prune_scans(db, project.id)
    return scan


async def _prune_scans(db: AsyncSession, project_id: uuid.UUID) -> None:
    count = (await db.execute(select(func.count()).select_from(VulnScan).where(VulnScan.project_id == project_id))).scalar_one()
    if count > limits.VULN_SCANS_KEPT:
        old = (await db.execute(select(VulnScan.id).where(VulnScan.project_id == project_id)
                                .order_by(VulnScan.created_at.asc()).limit(count - limits.VULN_SCANS_KEPT))).scalars().all()
        await db.execute(delete(VulnScan).where(VulnScan.id.in_(old)))
        await db.commit()


# --------------------------------------------------------------------------- fixes
def apply_fix_to_text(manifest_name: str, text: str, package: str, old: str, new: str) -> str | None:
    """Rewrite one dependency version in a manifest. None if not supported."""
    name = Path(manifest_name).name
    if re.match(r"^requirements.*\.(txt|in)$", name):
        pat = re.compile(rf"(?im)^(\s*{re.escape(package)}\s*(\[[^\]]*\])?\s*)(==|>=|~=)\s*[^\s;#,]+")
        if pat.search(text):
            return pat.sub(lambda m: f"{m.group(1)}{'==' if m.group(3) == '==' else '>='}{new}", text, count=1)
        pat2 = re.compile(rf"(?im)^(\s*{re.escape(package)}\s*(\[[^\]]*\])?)\s*$")
        if pat2.search(text):
            return pat2.sub(lambda m: f"{m.group(1)}>={new}", text, count=1)
        return None
    if name == "package.json":
        data = json.loads(text)
        changed = False
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            deps = data.get(section) or {}
            if package in deps:
                cur = str(deps[package])
                prefix = cur[0] if cur[:1] in "^~" else ""
                deps[package] = f"{prefix}{new}"
                changed = True
        return json.dumps(data, indent=2) + "\n" if changed else None
    if name == "pyproject.toml":
        pat = re.compile(rf'(["\']\s*{re.escape(package)}\s*(\[[^\]]*\])?\s*)(==|>=|~=)\s*[^"\',;\s]+')
        if pat.search(text):
            return pat.sub(lambda m: f"{m.group(1)}{'==' if m.group(3) == '==' else '>='}{new}", text, count=1)
    return None


# --------------------------------------------------------------------------- library gate
async def suggest_alternatives(ecosystem: str, name: str, team: str) -> list[dict[str, str]]:
    from .ai import prompts
    from .ai import router as ai

    try:
        res = await ai.call("utility", team=team, system=prompts.ALTERNATIVES, task_role="alternatives", json_mode=True,
                            max_tokens=400, messages=[{"role": "user", "content": f"Ecosystem: {ecosystem}\nLibrary: {name}"}])
    except ai.AIError:
        return []
    out: list[dict[str, str]] = []
    for alt in (res.data or {}).get("alternatives", [])[:3]:
        alt_name = str(alt.get("name", "")).strip()
        if not alt_name or norm_name(ecosystem, alt_name) == norm_name(ecosystem, name):
            continue
        exists, latest = await latest_version(ecosystem, alt_name)
        if exists is False:
            continue
        vids = (await osv_batch([(ecosystem, alt_name, latest)]))[0] if latest or ecosystem not in ("PyPI", "npm") else []
        out.append({"name": alt_name, "version": latest, "reason": str(alt.get("reason", ""))[:200], "clean": not vids})
    return out


async def check_library(db: AsyncSession | None, *, ecosystem: str, name: str, version: str = "",
                        project_id: uuid.UUID | None = None, task_id: uuid.UUID | None = None,
                        team: str = "gemma_qwen", with_alternatives: bool = True) -> dict[str, Any]:
    eco = norm_ecosystem(ecosystem)
    name = (name or "").strip()
    if not re.fullmatch(r"[@A-Za-z0-9][\w.\-/@]{0,213}", name):
        return {"ecosystem": eco, "name": name, "version": version, "verdict": "blocked",
                "reason": "That is not a valid package name.", "recommended": "", "vulns": [], "alternatives": []}
    version = base_version(version) if version else ""
    cache_key = f"elvin:gate:{eco}:{norm_name(eco, name)}:{version}"
    cached = await redisx.call("get", cache_key, default=None)
    result: dict[str, Any]
    if cached:
        result = json.loads(cached)
    else:
        exists, latest = await latest_version(eco, name)
        if exists is False:
            result = {"verdict": "blocked", "reason": f"'{name}' does not exist on {eco} - probably a typo or an invented name.",
                      "recommended": "", "vulns": [], "version": version}
        else:
            use_version = version or latest
            try:
                ids = (await osv_batch([(eco, name, use_version)]))[0]
            except RuntimeError as exc:
                return {"ecosystem": eco, "name": name, "version": use_version, "verdict": "blocked", "reason": str(exc),
                        "recommended": "", "vulns": [], "alternatives": []}
            if not ids:
                pin = f"{name}=={use_version}" if eco == "PyPI" and use_version else (f"{name}@{use_version}" if use_version else name)
                result = {"verdict": "go", "reason": "No known vulnerabilities.", "recommended": pin, "vulns": [], "version": use_version}
            else:
                info = await analyse(eco, name, use_version, ids)
                safe_version = info["fixed_version"]
                if not info["malicious"] and not info["unfixed"] and safe_version:
                    if latest and not vless(eco, latest, safe_version):
                        safe_version = latest  # the newest release is the safest choice
                    still = (await osv_batch([(eco, name, safe_version)]))[0]
                    if not still:
                        pin = f"{name}>={safe_version}" if eco == "PyPI" else f"{name}@{safe_version}"
                        result = {"verdict": "go_with_version", "recommended": pin, "vulns": ids[:20], "version": use_version,
                                  "reason": f"{use_version or 'That version'} has {len(ids)} known vulnerabilit{'y' if len(ids) == 1 else 'ies'}; "
                                            f"version {safe_version} fixes them."}
                    else:
                        result = {"verdict": "blocked", "recommended": "", "vulns": ids[:20], "version": use_version,
                                  "reason": f"Every release we checked still has known vulnerabilities ({', '.join(still[:3])})."}
                else:
                    result = {"verdict": "blocked", "recommended": "", "vulns": ids[:20], "version": use_version,
                              "reason": info["recommendation"]}
        await redisx.call("set", cache_key, json.dumps(result), ex=6 * 3600)
    result = {"ecosystem": eco, "name": name, **result, "alternatives": result.get("alternatives", [])}
    if result["verdict"] == "blocked" and with_alternatives and not result["alternatives"] and "valid package" not in result["reason"]:
        alts = await suggest_alternatives(eco, name, team)
        result["alternatives"] = alts
        clean = next((a for a in alts if a["clean"]), None)
        if clean:
            result["recommended"] = clean["name"] + (f"=={clean['version']}" if eco == "PyPI" and clean["version"] else
                                                     f"@{clean['version']}" if clean["version"] else "")
            result["reason"] += f" Use {clean['name']} instead."
    if db is not None and project_id is not None:
        db.add(LibraryCheck(project_id=project_id, task_id=task_id, ecosystem=eco, name=name[:214],
                            version=str(result.get("version") or "")[:64], verdict=result["verdict"],
                            recommended=str(result.get("recommended") or "")[:300],
                            details={"reason": result["reason"], "vulns": result.get("vulns", []), "alternatives": result["alternatives"]}))
        await db.commit()
    return result


def deps_added(manifest_path: str, old_text: str | None, new_text: str) -> list[dict[str, Any]]:
    """Dependencies that are new or changed in a manifest edit."""
    old = {(d["ecosystem"], norm_name(d["ecosystem"], d["name"])): d["version"] for d in parse_manifest(manifest_path, old_text or "")}
    out = []
    for d in parse_manifest(manifest_path, new_text):
        key = (d["ecosystem"], norm_name(d["ecosystem"], d["name"]))
        if key not in old or old[key] != d["version"]:
            out.append(d)
    return out


_INSTALL = re.compile(r"\b(?:pip3?|python3? -m pip|uv pip)\s+install\s+(?P<pip>[^&|;]+)|\b(?:npm\s+(?:install|i|add)|yarn\s+add|pnpm\s+add)\s+(?P<npm>[^&|;]+)")


def packages_in_command(command: str) -> list[tuple[str, str, str]]:
    """Packages a shell command would install: (ecosystem, name, version)."""
    found: list[tuple[str, str, str]] = []
    for m in _INSTALL.finditer(command):
        if m.group("pip"):
            for tok in m.group("pip").split():
                if tok.startswith("-") or "/" in tok or tok.endswith((".txt", ".whl", ".tar.gz")) or tok == ".":
                    continue
                parsed = _pep508(tok)
                if parsed:
                    found.append(("PyPI", parsed[0], parsed[1]))
        if m.group("npm"):
            for tok in m.group("npm").split():
                if tok.startswith("-") or tok.startswith((".", "/")):
                    continue
                mm = re.match(r"^(@?[^@\s]+)(?:@(.+))?$", tok)
                if mm:
                    found.append(("npm", mm.group(1), base_version(mm.group(2) or "")))
    return found[:20]
