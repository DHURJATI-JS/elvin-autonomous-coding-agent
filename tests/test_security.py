"""Authentication, sessions (one login per account), CSRF, CSP/headers, XSS."""
from __future__ import annotations

import re

from conftest import PASSWORD, Client


async def test_security_headers_and_fresh_nonce(anon):
    r1 = await anon.get("/login")
    r2 = await anon.get("/login")
    csp = r1.headers["content-security-policy"]
    assert "script-src 'self' 'nonce-" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    n1 = re.search(r"nonce-([A-Za-z0-9_-]+)", csp).group(1)
    n2 = re.search(r"nonce-([A-Za-z0-9_-]+)", r2.headers["content-security-policy"]).group(1)
    assert n1 != n2
    scripts = re.findall(r"<script[^>]*>", r1.text)
    assert scripts and all(f'nonce="{n1}"' in s for s in scripts)
    assert ' style="' not in r1.text  # inline style attributes would be blocked by the CSP
    for h in ("x-content-type-options", "x-frame-options", "referrer-policy", "permissions-policy"):
        assert h in r1.headers
    local = await anon.http.get("/login", headers={"Host": "localhost:8765"})
    assert local.headers.get("cross-origin-opener-policy") == "same-origin"
    lan = await anon.http.get("/login", headers={"Host": "192.168.1.20:8765"})
    assert "cross-origin-opener-policy" not in lan.headers
    fresh = Client(anon.http._transport.app)
    cookie = (await fresh.get("/login")).headers.get("set-cookie", "").lower()
    assert "elvin_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie


async def test_csrf_and_origin_checks(user):
    r = await user.http.post("/api/projects", json={"name": "no-token"})
    assert r.status_code == 403
    r = await user.http.post("/api/projects", json={"name": "bad-token"}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403
    r = await user.api("POST", "/api/projects", {"name": "evil-origin"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = await user.api("POST", "/api/projects", {"name": "good-origin"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200


async def test_signup_validation(anon):
    await anon.load("/signup")
    r = await anon.form("/signup", {"username": "ab", "email": "bad", "password": "short", "confirm": "nomatch"})
    assert r.status_code == 400
    assert "Username" in r.text and "valid email" in r.text and "match" in r.text


async def test_duplicate_signup_case_insensitive(app):
    a = Client(app)
    await a.signup("dupe_user1")
    b = Client(app)
    await b.load("/signup")
    r = await b.form("/signup", {"username": "DUPE_USER1", "email": "other@example.com", "password": PASSWORD, "confirm": PASSWORD})
    assert r.status_code == 400 and "already exists" in r.text


async def test_auth_pages_only_when_signed_out(user, anon):
    for p in ("/login", "/signup", "/forgot"):
        r = await user.get(p)
        assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    assert (await user.get("/")).status_code == 303
    r = await anon.get("/")
    assert r.status_code == 200 and "Meet" in r.text
    assert (await anon.get("/dashboard")).status_code == 303


async def test_wrong_password_then_lockout(app):
    a = Client(app)
    await a.signup()
    b = Client(app)
    for _ in range(5):
        r = await b.login(a.username, "Wrong-password-1!")
        assert r.status_code in (400, 429)
        assert "Wrong email/username or password" in r.text or "Too many" in r.text
    r = await b.login(a.username, PASSWORD)
    assert r.status_code == 429 and "Too many failed attempts" in r.text


async def test_one_login_per_account_old_device_told_why(app):
    a = Client(app)
    await a.signup()
    assert (await a.get("/dashboard")).status_code == 200
    b = Client(app)
    r = await b.login(a.username)
    assert r.status_code == 303
    r = await a.api("POST", "/api/presence/heartbeat")
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "signed_out" and "signed in on" in body["detail"]
    r = await a.get("/dashboard")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert (await b.get("/dashboard")).status_code == 200


async def test_logout_ends_session(user):
    r = await user.form("/logout", {})
    assert r.status_code == 303
    assert (await user.get("/dashboard")).status_code == 303
    r = await user.api("GET", "/api/projects")
    assert r.status_code == 401


async def test_password_reset_single_use(app):
    a = Client(app)
    await a.signup()
    await a.form("/logout", {})
    c = Client(app)
    await c.load("/forgot")
    r = await c.form("/forgot", {"email": f"{a.username}@example.com"})
    assert r.status_code == 200 and "on its way" in r.text
    r2 = await c.form("/forgot", {"email": "nobody-here@example.com"})
    assert "on its way" in r2.text  # no account enumeration
    from elvin.config import get_settings

    files = sorted((get_settings().data_dir / "outbox").glob("*.txt"))
    token = re.search(r"/reset/([A-Za-z0-9_\-]+)", files[-1].read_text()).group(1)
    await c.load(f"/reset/{token}")
    r = await c.form(f"/reset/{token}", {"password": "Another-Secret-9!", "confirm": "Another-Secret-9!"})
    assert r.status_code == 303
    assert (await c.get(f"/reset/{token}")).status_code == 400
    d = Client(app)
    assert (await d.login(a.username, "Another-Secret-9!")).status_code == 303
    assert (await Client(app).login(a.username, PASSWORD)).status_code == 400


async def test_template_escaping(user):
    p = await user.new_project("<script>alert(1)</script>")
    r = await user.get(f"/p/{p['id']}/elvin")
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


async def test_body_size_limit(user):
    r = await user.api("POST", "/api/projects", {"name": "x" * 2_000_000})
    assert r.status_code == 413


async def test_settings_validation(user):
    assert (await user.api("POST", "/api/settings", {"ai_team": "nope"})).status_code == 400
    assert (await user.api("POST", "/api/settings", {"ai_effort": "ludicrous"})).status_code == 400
    assert (await user.api("POST", "/api/settings", {"model_prefs": {"claude_coder": "gpt-1"}})).status_code == 400
    r = await user.api("POST", "/api/settings", {"ai_effort": "high", "model_prefs": {"claude_coder": "claude-sonnet-5-5"}})
    assert r.status_code == 200 and r.json()["ai_effort"] == "high"
    r = await user.api("POST", "/api/settings/password", {"current": "wrong", "new": "Abcdef12345!x", "confirm": "Abcdef12345!x"})
    assert r.status_code == 400
