"""Pure unit tests: parsers, scoring, AI output handling, URL validation, policies."""
from __future__ import annotations

import pytest

from elvin.security.auth import describe_device, safe_next
from elvin.security.passwords import password_problems
from elvin.services import clone, vulns
from elvin.services.ai import blocks, jsonx


def test_jsonx_handles_fences_prose_and_trailing_commas():
    assert jsonx.extract('```json\n{"a": 1}\n```') == {"a": 1}
    assert jsonx.extract('Sure! Here it is: {"a": [1, 2,], "b": "x}"} hope that helps') == {"a": [1, 2], "b": "x}"}
    assert jsonx.extract("<think>hmm {not json}</think>{\"ok\": true}") == {"ok": True}
    assert jsonx.extract("no json at all") is None


def test_blocks_parser():
    out = blocks.parse("""Here you go
=== FILE: src/app.py ===
```python
print("hi")
```
=== END FILE ===
=== FILE: README.md ===
# Title
=== Installation ===
text
=== END FILE ===
=== DELETE: old.py ===
=== MKDIR: assets ===
=== RUN: pytest -q ===
=== LIBRARY: pypi requests 2.32.4 ===
=== SUMMARY ===
Did things.
=== END SUMMARY ===
=== FILE: cut.py ===
def half(""")
    assert out.files[0] == ("src/app.py", 'print("hi")\n')
    assert "=== Installation ===" in out.files[1][1], "markdown-ish lines inside a file are kept"
    assert out.deletes == ["old.py"] and out.mkdirs == ["assets"] and out.commands == ["pytest -q"]
    assert out.libraries == [("pypi", "requests", "2.32.4")] and out.summary == "Did things."
    assert out.incomplete and out.incomplete[0][0] == "cut.py", "a file cut off by the end of the output is flagged"


def test_manifest_parsers():
    reqs = vulns.parse_manifest("requirements.txt", "flask==3.0.0\nrequests>=2.31 ; python_version>'3.8'\n# c\n-r other.txt\nuvicorn[standard]==0.30.0\n")
    assert [(d["name"], d["version"], d["pinned"]) for d in reqs] == [("flask", "3.0.0", True), ("requests", "2.31", False), ("uvicorn", "0.30.0", True)]
    pj = vulns.parse_manifest("package.json", '{"dependencies":{"react":"^18.2.0","x":"git+https://a"},"devDependencies":{"vite":"5.0.1"}}')
    assert {(d["name"], d["version"]) for d in pj} == {("react", "18.2.0"), ("vite", "5.0.1")}
    lock = vulns.parse_manifest("package-lock.json", '{"lockfileVersion":3,"packages":{"":{},"node_modules/a":{"version":"1.0.0"},"node_modules/@s/b":{"version":"2.0.0"}}}')
    assert {(d["name"], d["version"]) for d in lock} == {("a", "1.0.0"), ("@s/b", "2.0.0")}
    gomod = vulns.parse_manifest("go.mod", "module x\n\ngo 1.22\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.1\n\tgolang.org/x/net v0.17.0 // indirect\n)\n")
    assert {(d["name"], d["version"]) for d in gomod} == {("github.com/gin-gonic/gin", "1.9.1"), ("golang.org/x/net", "0.17.0")}
    pyproject = vulns.parse_manifest("pyproject.toml", '[project]\ndependencies=["httpx>=0.27","pydantic==2.7.0"]\n[tool.poetry.dependencies]\npython="^3.11"\nrich="^13.0"\n')
    assert {(d["name"], d["version"]) for d in pyproject} == {("httpx", "0.27"), ("pydantic", "2.7.0"), ("rich", "13.0")}
    cargo = vulns.parse_manifest("Cargo.lock", '[[package]]\nname="me"\nversion="0.1.0"\n\n[[package]]\nname="serde"\nversion="1.0.190"\nsource="registry+https://github.com/rust-lang/crates.io-index"\n')
    assert [(d["name"], d["version"]) for d in cargo] == [("serde", "1.0.190")]
    gem = vulns.parse_manifest("Gemfile.lock", "GEM\n  remote: https://rubygems.org/\n  specs:\n    rack (2.2.8)\n      x (>= 1)\n\nPLATFORMS\n  ruby\n")
    assert [(d["name"], d["version"]) for d in gem] == [("rack", "2.2.8")]


@pytest.mark.parametrize("vector,score", [
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1),
    ("CVSS:3.0/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N", 5.5),
    ("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0),
])
def test_cvss3(vector, score):
    assert vulns.cvss3_score(vector) == score


def test_fixed_version_selection_and_fix_rewrite():
    vuln = {"affected": [{"package": {"ecosystem": "PyPI", "name": "Django"}, "ranges": [{"events": [
        {"introduced": "0"}, {"fixed": "3.2.25"}, {"introduced": "4.0"}, {"fixed": "4.2.11"}]}]}]}
    assert vulns.fixed_versions(vuln, "PyPI", "django", "4.1.0") == ("4.2.11", True)
    assert vulns.fixed_versions(vuln, "PyPI", "django", "5.0") == (None, False)
    assert vulns.apply_fix_to_text("requirements.txt", "Django==4.1.0\nflask\n", "Django", "4.1.0", "4.2.11") == "Django==4.2.11\nflask\n"
    out = vulns.apply_fix_to_text("package.json", '{"dependencies":{"lodash":"^4.17.15"}}', "lodash", "4.17.15", "4.17.21")
    assert '"lodash": "^4.17.21"' in out
    assert vulns.packages_in_command("pip install requests==2.0 flask && npm i lodash@4.17.0 -D") == [
        ("PyPI", "requests", "2.0"), ("PyPI", "flask", ""), ("npm", "lodash", "4.17.0")]


def test_license_detection():
    assert clone.detect_license("MIT License\nPermission is hereby granted, free of charge") == "MIT"
    assert clone.detect_license("Apache License\nVersion 2.0, January 2004") == "Apache-2.0"
    assert clone.detect_license("GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007") == "GPL-3.0"
    assert clone.detect_license("GNU AFFERO GENERAL PUBLIC LICENSE") == "AGPL-3.0"
    assert clone.detect_license("Redistribution and use in source and binary forms... Neither the name of") == "BSD-3-Clause"
    assert clone.detect_license("Some custom words") == ""


@pytest.mark.parametrize("url", ["file:///etc", "ssh://git@github.com/a/b", "git@github.com:a/b.git", "https://user:pw@github.com/a/b",
                                 "https://github.com/onlyowner", "http://github.com/a/b", "https://127.0.0.1/a/b", "https://github.com/a/b?x=1"])
def test_clone_url_rejected(url):
    with pytest.raises(ValueError):
        clone.parse_repo_url(url)


def test_clone_url_accepted():
    assert clone.parse_repo_url("pallets/flask")[0] == "https://github.com/pallets/flask.git"
    assert clone.parse_repo_url("https://github.com/pallets/flask.git")[2:] == ("pallets", "flask")


def test_password_policy_and_helpers():
    assert password_problems("short", username="bob")
    assert password_problems("password123", username="bob")
    assert password_problems("alicealice123!", username="alice")
    assert not password_problems("Correct-Horse-9", username="bob", email="bob@x.io")
    assert describe_device("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36") == "Chrome on macOS"
    assert safe_next("//evil.com") == "/dashboard" and safe_next("https://evil.com") == "/dashboard" and safe_next("/p/1/editor") == "/p/1/editor"


def test_startup_requires_cloud_key_when_incompatible(monkeypatch):
    from elvin.config import Settings

    s = Settings(secret_key="x" * 40, host_compatible=False, local_llm_backend="none", anthropic_api_key="", openai_api_key="")
    assert any("ANTHROPIC_API_KEY" in p for p in s.startup_problems())
    s2 = Settings(secret_key="x" * 40, host_compatible=False, local_llm_backend="none", openai_api_key="sk-test")
    assert not s2.startup_problems()
    s3 = Settings(secret_key="x" * 40, host_compatible=True, local_llm_backend="vllm", local_llm_url="http://vllm:8000/v1")
    assert not s3.startup_problems()
    assert Settings(secret_key="short").startup_problems()
    # a GPU that only fits a coder under 3B is too weak -> a cloud key is compulsory
    weak = Settings(secret_key="x" * 40, host_compatible=True, local_llm_backend="vllm", local_llm_url="http://vllm:8000/v1",
                    qwen_model="Qwen/Qwen2.5-Coder-1.5B-Instruct", anthropic_api_key="", openai_api_key="")
    assert any("smaller than 3B" in p for p in weak.startup_problems())
    assert not Settings(secret_key="x" * 40, host_compatible=True, local_llm_backend="vllm", local_llm_url="http://vllm:8000/v1",
                        qwen_model="Qwen/Qwen2.5-Coder-1.5B-Instruct", anthropic_api_key="sk-ant-test").startup_problems()


def test_planning_needs_a_gateway_a_capable_local_gemma_or_a_key():
    from elvin.config import Settings, tiny_model

    base = dict(secret_key="x" * 40, anthropic_api_key="", openai_api_key="", local_gemma_url="http://gemma:11434")
    assert Settings(**base, local_gemma_model="gemma3:1b").gemma_problems("") , "1B can't read images and there is no gateway"
    assert not Settings(**base, local_gemma_model="gemma3:1b").gemma_problems("http://10.0.0.7:8000/v1")
    assert not Settings(**base, local_gemma_model="gemma3:4b").gemma_problems("")
    assert not Settings(**{**base, "openai_api_key": "sk-test"}, local_gemma_model="gemma3:1b").gemma_problems("")
    assert Settings(**{**base, "local_gemma_url": ""}).gemma_problems(""), "no local Gemma at all"
    assert [tiny_model(m) for m in ("gemma3:1b", "gemma3:12b", "qwen2.5-coder:1.5b", "qwen2.5-coder:3b", "Qwen/Qwen2.5-Coder-0.5B")] == [True, False, True, False, True]


def test_installer_sizes_local_models_by_memory_and_cores():
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "install" / "lib" / "local-models.sh"
    if not script.exists():
        pytest.skip("installer scripts are not in this image")
    expect = {("4", "4"): "weak gemma3:1b qwen2.5-coder:1.5b 2g", ("7", "4"): "small gemma3:4b qwen2.5-coder:3b 5g",
              ("12", "4"): "medium gemma3:4b qwen2.5-coder:7b 7g", ("16", "4"): "medium gemma3:4b qwen2.5-coder:7b 7g",
              ("16", "8"): "large gemma3:12b qwen2.5-coder:14b 12g", ("64", "16"): "large gemma3:12b qwen2.5-coder:14b 12g"}
    for (mem, cores), want in expect.items():
        out = subprocess.run(["bash", "-c", f'. "{script}"; pick_local_models {mem} {cores}; '
                              'echo "$LOCAL_TIER $LOCAL_GEMMA_MODEL $LOCAL_QWEN_MODEL $LOCAL_GEMMA_MEM"'],
                             capture_output=True, text=True, timeout=10)
        assert out.stdout.strip() == want, (mem, cores, out.stdout, out.stderr)
    tiny = subprocess.run(["bash", "-c", f'. "{script}"; tiny_model "Qwen/Qwen2.5-Coder-1.5B-Instruct" && tiny_model gemma3:1b '
                           '&& ! tiny_model qwen2.5-coder:7b && ! tiny_model gemma3:12b && echo ok'], capture_output=True, text=True, timeout=10)
    assert tiny.stdout.strip() == "ok", tiny.stderr


async def test_router_fails_over_and_trips_breaker(monkeypatch, app):
    from elvin.services.ai import router

    monkeypatch.setenv("ELVIN_FAKE_AI_FAIL_KEYS", "qwen")
    res = await router.call("coder", team="gemma_qwen", system="x", task_role="summary", messages=[{"role": "user", "content": "hi"}])
    assert res.provider == "gemma", "coding falls back from Qwen to the gateway Gemma (GPU) first"
    monkeypatch.setenv("ELVIN_FAKE_AI_FAIL_KEYS", "qwen,gemma")
    res = await router.call("coder", team="gemma_qwen", system="x", task_role="summary", messages=[{"role": "user", "content": "hi"}])
    assert res.provider == "qwen_local", "...then to the Qwen Coder in Docker"
    router.registry().breakers["gemma"].ok()
    monkeypatch.setenv("ELVIN_FAKE_AI_FAIL_KEYS", "qwen")
    for _ in range(3):
        await router.call("coder", team="gemma_qwen", system="x", task_role="summary", messages=[{"role": "user", "content": "hi"}])
    assert not router.registry().breakers["qwen"].available(), "repeated failures pause the provider"
    router.registry().breakers["qwen"].ok()
