"""Real-browser check of every Elvin page (Chromium via Playwright).

* every page at 390 / 820 / 1280 / 1680 px: no horizontal overflow, no JS errors,
  no Content-Security-Policy violations, no HTTP errors;
* clicks through the main flows: sign up, Elvin chat -> permission dialog ->
  approve -> sandbox run, editor ghost-text autocomplete + save, running code
  from the editor and from the chat, image preview, the System Status page
  (switch to the local Gemma and back), all GitHub repositories, a Locust run,
  theme switch;
* saves screenshots to /out for the README.
Runs only against the throwaway elvin-e2e stack."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys

import asyncpg
from playwright.async_api import async_playwright

BASE = os.environ["E2E_BASE"]
DB = os.environ["E2E_DB"]
WIDTHS = [390, 820, 1280, 1680]
OUT = "/out"
R: dict = {"pages_checked": 0, "overflow": [], "console": [], "csp": [], "http": [], "flows": [], "failed_flows": []}

INIT = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective + ' ' + (e.blockedURI || 'inline') + ' ' + (e.sourceFile || '') + ':' + e.lineNumber));
"""
OVERFLOW_JS = """() => {
  const w = document.documentElement.clientWidth;
  if (document.documentElement.scrollWidth <= w + 1) return [];
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.right <= w + 1) continue;
    let p = el.parentElement, clipped = false;
    while (p && p !== document.body) { const ox = getComputedStyle(p).overflowX; if (ox !== 'visible') { clipped = true; break; } p = p.parentElement; }
    if (clipped) continue;
    out.push(el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + '.' + [...el.classList].slice(0, 4).join('.') + ' right=' + Math.round(r.right) + ' (viewport ' + w + ')');
  }
  return out.slice(0, 6);
}"""


async def api(page, method, path, body=None):
    return await page.evaluate("""async ([m, p, b]) => {
      const r = await fetch(p, {method: m, headers: {'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content}, body: b ? JSON.stringify(b) : undefined});
      let body = null; try { body = await r.json(); } catch (e) {}
      return {status: r.status, body};
    }""", [method, path, body])


async def check(page, path, width):
    await page.set_viewport_size({"width": width, "height": 900})
    resp = await page.goto(BASE + path, wait_until="load")
    await page.wait_for_timeout(1400)
    if resp is None or resp.status >= 400:
        R["http"].append(f"{path} -> {resp.status if resp else 'no response'}")
    over = await page.evaluate(OVERFLOW_JS)
    if over:
        R["overflow"].append({"page": path, "width": width, "elements": over})
    for c in await page.evaluate("window.__csp || []"):
        R["csp"].append(f"{path}: {c}")
    R["pages_checked"] += 1


async def flow(name, fn):
    try:
        await fn()
        R["flows"].append(name)
    except Exception as exc:  # report and carry on
        R["failed_flows"].append(f"{name}: {exc.__class__.__name__}: {str(exc)[:300]}")


async def seed_github(username: str) -> None:
    conn = await asyncpg.connect(DB)
    try:
        uid = await conn.fetchval("select id from users where username=$1", username)
        long = "A very long repository description that keeps going and going to make sure the card wraps or truncates instead of overflowing " * 3
        data = {
            "profile": {"login": "octo-example", "name": "Octo Example", "url": "https://github.com/octo-example", "followers": 95, "following": 3,
                        "public_repos": 17, "private_repos": 2},
            "totals": {"repos": 19, "stars": 42, "forks": 3, "open_issues": 4},
            "languages": [["Python", 9], ["TypeScript", 4], ["JavaScript", 3], ["Go", 1], ["Rust", 1], ["Shell", 1]],
            "contributions": 120, "open_prs": 1, "open_issues_authored": 2, "review_requests": 1, "notifications": 3,
            "recent": [{"name": f"repo-{i}", "full_name": f"octo-example/a-really-long-repository-name-number-{i}", "url": "https://github.com/octo-example",
                        "private": i % 2 == 0, "stars": i, "forks": 0, "open_issues": 0, "language": "Python",
                        "pushed_at": dt.datetime.now(dt.timezone.utc).isoformat(), "description": long, "license": None, "archived": False}
                       for i in range(6)],
        }
        data["repos"] = data["recent"] + [
            {"name": n, "full_name": f"{owner}/{n}", "owner": owner, "url": "https://github.com/octo-example", "private": priv, "fork": fork,
             "archived": arch, "stars": st, "forks": 1, "open_issues": 0, "language": lang, "license": "MIT", "visibility": "private" if priv else "public",
             "pushed_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=d)).isoformat(), "description": desc}
            for n, owner, priv, fork, arch, st, lang, d, desc in [
                ("dotfiles", "octo-example", False, False, False, 12, "Shell", 3, "My shell and editor configuration"),
                ("cpython", "octo-example", False, True, False, 0, "Python", 9, "Fork of the Python interpreter"),
                ("old-blog", "octo-example", False, False, True, 2, "HTML", 400, "Archived personal blog"),
                ("team-api", "acme-org", True, False, False, 5, "Go", 1, "Organisation repository I collaborate on")]]
        for r in data["recent"]:
            r.setdefault("owner", "octo-example")
        sugg = [{"level": "recommended", "title": "2 public repositories have no license", "detail": "Without a license nobody may legally reuse the code: " + long[:120], "action": ""},
                {"level": "info", "title": "3 unread GitHub notifications", "detail": "", "action": "https://github.com/notifications"}]
        await conn.execute("""insert into github_stats(user_id, data, suggestions, fetched_at, error) values($1, $2::jsonb, $3::jsonb, now(), null)
                              on conflict (user_id) do update set data=excluded.data, suggestions=excluded.suggestions, fetched_at=now()""",
                           uid, json.dumps(data), json.dumps(sugg))
    finally:
        await conn.close()


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=False)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark", device_scale_factor=1)
        await ctx.add_init_script(INIT)
        page = await ctx.new_page()
        page.on("console", lambda m: R["console"].append(f"{page.url}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: R["console"].append(f"{page.url}: pageerror {e}"))

        for path in ["/", "/login", "/signup", "/forgot", "/about"]:
            for w in WIDTHS:
                await check(page, path, w)
        await page.set_viewport_size({"width": 1440, "height": 900})
        await page.goto(BASE + "/")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=f"{OUT}/landing-dark.png")

        async def signup():
            await page.goto(BASE + "/signup")
            await page.fill("#username", "e2e_user")
            await page.fill("#email", "e2e_user@example.com")
            await page.fill("#password", "Browser-Check-2026!")
            await page.fill("#confirm", "Browser-Check-2026!")
            await page.click("button[type=submit]")
            await page.wait_for_url("**/dashboard", timeout=15000)
        await flow("sign up through the form", signup)

        r = await api(page, "POST", "/api/projects", {"name": "Showcase API", "description": "A demo project created by the browser check"})
        if not (r["body"] or {}).get("id"):
            print("project create failed:", r, "flows:", R["flows"], R["failed_flows"], "url:", page.url, "console:", R["console"][-5:])
            print(await page.content())
            return 2
        pid = r["body"]["id"]
        await api(page, "PUT", f"/api/projects/{pid}/file", {"path": "app/main.py", "content": "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get('/')\ndef home():\n    return {'hello': 'world'}\n"})
        await api(page, "PUT", f"/api/projects/{pid}/file", {"path": "requirements.txt", "content": "fastapi==0.110.0\njinja2==2.10\n"})
        await seed_github("e2e_user")

        pages = ["/dashboard", "/projects", "/clone", "/settings", "/usage", "/status", "/github", "/about"] + [f"/p/{pid}/{f}" for f in
                 ("elvin", "editor", "bughunt", "vulns", "loadtest", "git", "history")]
        for path in pages:
            for w in WIDTHS:
                await check(page, path, w)
        await page.set_viewport_size({"width": 1440, "height": 900})

        async def dashboard_shot():
            await page.goto(BASE + "/dashboard")
            await page.wait_for_selector("#gh-body >> text=Octo Example", timeout=10000)
            await page.wait_for_timeout(800)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(300)
            await page.screenshot(path=f"{OUT}/dashboard-dark.png", full_page=True)
        await flow("dashboard with GitHub stats", dashboard_shot)

        async def elvin_flow():
            await page.goto(BASE + f"/p/{pid}/elvin")
            await page.fill("#prompt", "create a file called hello.py that prints hello and run it")
            await page.click("#send-btn")
            await page.wait_for_selector("text=Permission needed", timeout=30000)
            await page.wait_for_timeout(500)
            await page.screenshot(path=f"{OUT}/permission-dialog.png")
            await page.click("button:has-text('Approve selected')")
            await page.wait_for_selector("text=exit 0", timeout=90000)
            await page.wait_for_selector("text=All done", timeout=60000)
            await page.wait_for_timeout(800)
            await page.screenshot(path=f"{OUT}/elvin-chat.png")
            f = await api(page, "GET", f"/api/projects/{pid}/file?path=hello.py")
            assert f["status"] == 200 and "hello" in f["body"]["content"]
        await flow("Elvin chat -> permission dialog -> approve -> sandbox run", elvin_flow)

        async def editor_flow():
            await page.goto(BASE + f"/p/{pid}/editor#path=hello.py")
            await page.wait_for_selector(".CodeMirror", timeout=10000)
            await page.click(".CodeMirror-code")
            await page.keyboard.press("Meta+End")
            await page.keyboard.press("Control+End")
            await page.keyboard.type("\n\ndef answer():")
            await page.wait_for_selector(".cm-ghost", timeout=10000)
            await page.screenshot(path=f"{OUT}/editor-autocomplete.png")
            await page.keyboard.press("Tab")
            await page.keyboard.press("Control+s")
            await page.wait_for_selector("text=Saved hello.py", timeout=10000)
            f = await api(page, "GET", f"/api/projects/{pid}/file?path=hello.py")
            assert "return None" in f["body"]["content"], f["body"]["content"][-200:]
        await flow("editor ghost-text autocomplete + Tab + save", editor_flow)

        async def editor_run_flow():
            await page.goto(BASE + f"/p/{pid}/editor#path=hello.py")
            await page.wait_for_selector(".CodeMirror", timeout=10000)
            await page.wait_for_function("document.querySelector('#run-cmd').value.includes('hello.py')", timeout=10000)
            await page.click("#run-go")
            await page.wait_for_selector("#run-meta >> text=exit 0", timeout=90000)
            out = await page.inner_text("#run-text")
            assert "hello" in out.lower(), out
            await page.wait_for_timeout(400)
            await page.screenshot(path=f"{OUT}/editor-run.png")
            runs = await api(page, "GET", f"/api/projects/{pid}/runs")
            assert runs["body"]["runs"][0]["source"] == "editor"
        await flow("run code from the editor (sandbox)", editor_run_flow)

        async def editor_image_flow():
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="480" height="240"><defs><linearGradient id="g" x1="0" x2="1">'
                   '<stop offset="0" stop-color="#7c3aed"/><stop offset="1" stop-color="#06b6d4"/></linearGradient></defs>'
                   '<rect width="480" height="240" rx="32" fill="url(#g)"/><text x="240" y="140" font-size="64" text-anchor="middle" '
                   'fill="white" font-family="sans-serif">Elvin</text></svg>')
            r = await api(page, "PUT", f"/api/projects/{pid}/file", {"path": "assets/logo.svg", "content": svg})
            assert r["status"] == 200, r
            await page.goto(BASE + f"/p/{pid}/editor#path=assets/logo.svg")
            await page.wait_for_selector("#image-view:not(.hidden) #image-el", timeout=10000)
            await page.wait_for_function("document.querySelector('#image-el').naturalWidth > 0", timeout=10000)
            await page.wait_for_timeout(300)
            await page.screenshot(path=f"{OUT}/editor-image.png")
        await flow("image preview in the editor", editor_image_flow)

        async def delete_needs_password_flow():
            before = len(R["console"])
            try:
                await _delete_needs_password()
            finally:  # the deliberate wrong password (403) and the "is it gone?" check (404) are expected
                new = R["console"][before:]
                del R["console"][before:]
                R.setdefault("expected_console", []).extend(c for c in new if "status of 403" in c or "status of 404" in c)
                R["console"].extend(c for c in new if not ("status of 403" in c or "status of 404" in c))

        async def _delete_needs_password():
            r = await api(page, "PUT", f"/api/projects/{pid}/file", {"path": "scratch.txt", "content": "temporary"})
            assert r["status"] == 200, r
            await page.goto(BASE + f"/p/{pid}/editor")
            row = page.locator(".tree-item", has_text="scratch.txt")
            await row.hover()
            await row.locator("button[title=Delete]").click()
            await page.wait_for_selector(".modal-panel >> text=Deleting always needs your password", timeout=10000)
            await page.fill(".modal-panel input[type=password]", "not-the-password")
            await page.click(".modal-panel button:has-text('Confirm & delete')")
            await page.wait_for_selector("text=Wrong password - nothing was deleted", timeout=10000)
            await page.wait_for_timeout(500)
            f = await api(page, "GET", f"/api/projects/{pid}/file?path=scratch.txt")
            assert f["status"] == 200, "a wrong password deletes nothing"
            await row.hover()
            await row.locator("button[title=Delete]").click()
            await page.fill(".modal-panel input[type=password]", "Browser-Check-2026!")
            await page.wait_for_timeout(300)
            await page.screenshot(path=f"{OUT}/delete-password.png")
            await page.press(".modal-panel input[type=password]", "Enter")
            for _ in range(40):   # (don't match the earlier "nothing was deleted" toast - ask the server)
                f = await api(page, "GET", f"/api/projects/{pid}/file?path=scratch.txt")
                if f["status"] == 404:
                    break
                await page.wait_for_timeout(250)
            assert f["status"] == 404, f
        await flow("deleting a file asks for the password (wrong one deletes nothing)", delete_needs_password_flow)

        async def elvin_run_flow():
            await page.goto(BASE + f"/p/{pid}/elvin")
            await page.click("#run-btn")
            await page.fill(".modal-panel input.input", "python hello.py")
            await page.click(".modal-panel button:has-text('Run')")
            for _ in range(180):
                runs = await api(page, "GET", f"/api/projects/{pid}/runs")
                latest = (runs["body"] or {}).get("runs") or [{}]
                if latest[0].get("source") == "elvin":
                    break
                await page.wait_for_timeout(500)
            else:
                raise AssertionError("the run started from the chat never finished")
            assert latest[0].get("exit_code") == 0, latest[0]
            await page.wait_for_selector("text=python hello.py", timeout=10000)
            await page.wait_for_timeout(500)
            await page.screenshot(path=f"{OUT}/elvin-run.png")
        await flow("run code from Elvin's chat", elvin_run_flow)

        async def status_flow():
            await page.goto(BASE + "/status")
            await page.wait_for_selector("#providers > div", timeout=30000)
            await page.wait_for_selector("#services >> text=PostgreSQL", timeout=30000)
            await page.click("#gemma-source button[data-src=local]")
            await page.wait_for_selector("#fallback-note >> text=local Gemma 3 in Docker for now", timeout=10000)
            await page.evaluate("window.scrollTo(0, 0)")   # a full-page shot taken while scrolled draws the sticky menu mid-page
            await page.wait_for_timeout(600)
            await page.screenshot(path=f"{OUT}/status.png", full_page=True)
            await page.click("#gemma-source button[data-src=auto]")
            await page.wait_for_selector("#fallback-note.hidden", state="attached", timeout=10000)
        await flow("system status: latency, gateway, switch to local Gemma and back", status_flow)

        async def github_repos_flow():
            await page.goto(BASE + "/github")
            await page.wait_for_selector("#repos >> text=acme-org/team-api", timeout=10000)
            assert "Showing 10 of 10" in await page.inner_text("#count")
            await page.click("#filter button[data-f=forks]")
            assert "Showing 1 of 10" in await page.inner_text("#count")
            await page.click("#filter button[data-f=all]")
            await page.fill("#q", "dotfiles")
            assert "Showing 1 of 10" in await page.inner_text("#count")
            await page.fill("#q", "")
            await page.dispatch_event("#q", "input")
            await page.wait_for_timeout(300)
            await page.screenshot(path=f"{OUT}/github-repos.png")
        await flow("all GitHub repositories (read-only)", github_repos_flow)

        async def loadtest_flow():
            await page.goto(BASE + f"/p/{pid}/loadtest")
            await page.fill("#lt-host", BASE)
            await page.fill("#lt-users", "5")
            await page.fill("#lt-rate", "5")
            await page.fill("#lt-dur", "12")
            await page.click("#run")
            await page.wait_for_selector("#live-badge:not(.hidden)", timeout=10000)
            await page.wait_for_selector("#export:not(.hidden)", timeout=60000)
            await page.wait_for_timeout(800)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(300)
            await page.screenshot(path=f"{OUT}/loadtest.png", full_page=True)
            runs = await api(page, "GET", f"/api/projects/{pid}/loadtest/runs")
            assert runs["body"]["runs"][0]["total_requests"] > 0
        await flow("Locust load test with live charts", loadtest_flow)

        async def vuln_flow():
            await page.goto(BASE + f"/p/{pid}/vulns")
            await page.click("#scan")
            await page.wait_for_selector("text=jinja2", timeout=60000)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(300)
            await page.screenshot(path=f"{OUT}/vulns.png", full_page=True)
        await flow("vulnerability scan (live OSV.dev)", vuln_flow)

        async def history_flow():
            await page.goto(BASE + f"/p/{pid}/history")
            await page.wait_for_selector("text=Verify", timeout=10000)
            await page.click("button:has-text('Verify') >> nth=0")
            await page.wait_for_selector("text=Verified:", timeout=10000)
            await page.screenshot(path=f"{OUT}/history.png")
        await flow("version history verify", history_flow)

        async def light_mode():
            await page.goto(BASE + "/dashboard")
            await page.click("[data-theme-toggle]")
            await page.wait_for_timeout(700)
            assert not await page.evaluate("document.documentElement.classList.contains('dark')")
            await page.screenshot(path=f"{OUT}/dashboard-light.png")
            await page.goto(BASE + f"/p/{pid}/elvin")
            await page.wait_for_timeout(1200)
            await page.screenshot(path=f"{OUT}/elvin-light.png")
        await flow("light / dark theme switch", light_mode)

        async def mobile_shot():
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.goto(BASE + "/dashboard")
            await page.wait_for_timeout(1200)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(300)
            await page.screenshot(path=f"{OUT}/dashboard-mobile.png", full_page=True)
        await flow("mobile layout screenshot", mobile_shot)
        await browser.close()
    print(json.dumps(R, indent=1))
    bad = R["overflow"] or R["csp"] or R["console"] or R["http"] or R["failed_flows"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
