"""System status: every `status_check_interval_s` (60 s) Elvin measures

* each AI model's API round-trip time and a real one-token response time
  (local / gateway models only - cloud models get a free metadata call, so the
  check never costs money);
* the Gemma gateway's own /status page: how many PCs serve it, which are
  online, busy slots, overload flags and the host CPU;
* PostgreSQL, Redis, ClamAV and the sandbox runner.

The last hour of measurements is kept in memory for the charts on the
System Status page. The snapshot also tells the UI when the local Docker Gemma
fallback is active (gateway down) so it can show the reduced-effort alert."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import text

from .. import db as dbm
from .. import redisx
from ..config import get_settings
from ..models import utcnow
from . import app_settings, clamav, runner_client
from .ai import router as ai
from .ai.providers import OllamaProvider, http

log = logging.getLogger("elvin.status")
GENERATION_PROBE = {"gemma", "gemma_local", "qwen", "qwen_local"}
HISTORY = 60


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def gateway_root(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


class StatusMonitor:
    def __init__(self) -> None:
        self.latest: dict[str, dict[str, Any]] = {}
        self.history: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY))
        self.gateway: dict[str, Any] = {}
        self.services: dict[str, dict[str, Any]] = {}
        self.checked_at = None
        self.started = False
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ checks
    async def _provider(self, key: str, provider) -> None:
        reg = ai.registry()
        t0 = time.perf_counter()
        try:
            ok, detail = await asyncio.wait_for(provider.health(), timeout=12)
        except Exception as exc:  # network / timeout
            ok, detail = False, f"health check failed ({exc.__class__.__name__})"
        api_ms = _ms(t0)
        gen_ms = None
        probe = ok and key in GENERATION_PROBE
        extra: dict[str, Any] = {}
        if probe and isinstance(provider, OllamaProvider):
            # Ollama models on this computer: never load one just to time it (that takes seconds and
            # gigabytes) and never keep it in memory longer than real use does.
            left = await provider.loaded()
            if left is None or left < 5:
                probe = False
                detail = "online · loads on first use"
            else:
                extra = {"keep_alive": f"{int(left)}s"}
        if probe:
            t1 = time.perf_counter()
            try:
                async with reg.slot(key):
                    await asyncio.wait_for(provider.chat([{"role": "user", "content": "Reply with the single word OK."}],
                                                        max_tokens=3, temperature=0.0, role="ping", **extra), timeout=60)
                gen_ms = _ms(t1)
                reg.health_cache[key] = (time.monotonic(), True, "online")
            except Exception as exc:
                detail = f"answers too slowly or not at all ({exc.__class__.__name__})"
                ok = False
        rec = {"key": key, "label": provider.label, "model": provider.model, "family": ai.FAMILY_OF.get(key, key),
               "online": ok, "detail": detail, "api_ms": api_ms, "gen_ms": gen_ms,
               "circuit_open": not reg.breakers[key].available(), "fallback": key in ai.LOCAL_DOCKER,
               "local": key in ("gemma_local", "qwen", "qwen_local"), "checked_at": utcnow().isoformat()}
        self.latest[key] = rec
        self.history[key].append({"t": rec["checked_at"], "ok": ok, "api_ms": api_ms, "gen_ms": gen_ms})

    async def _gateway(self) -> None:
        url = ai.gateway_url()
        if not url:
            self.gateway = {"configured": False}
            return
        root = gateway_root(url)
        t0 = time.perf_counter()
        info: dict[str, Any] = {"configured": True, "url": url, "status_url": f"{root}/status", "reachable": False, "pcs": []}
        try:
            r = await http().get(f"{root}/status", timeout=httpx.Timeout(connect=4.0, read=8.0, write=4.0, pool=4.0))
            info["latency_ms"] = _ms(t0)
            info["reachable"] = True
            if r.status_code == 200:
                data = r.json()
                pcs = []
                for b in data.get("backends") or []:
                    busy, total = 0, 0
                    try:
                        busy, total = (int(x) for x in str(b.get("busy_slots", "0/0")).split("/", 1))
                    except ValueError:
                        pass
                    pcs.append({"name": str(b.get("name", "PC")), "address": str(b.get("url", "")), "online": bool(b.get("online")),
                                "busy_slots": busy, "total_slots": total, "overloaded": bool(b.get("overloaded"))})
                info.update(pcs=pcs, pcs_total=len(pcs), pcs_online=sum(p["online"] for p in pcs),
                            slots_busy=sum(p["busy_slots"] for p in pcs if p["online"]),
                            slots_total=sum(p["total_slots"] for p in pcs if p["online"]),
                            host_cpu_percent=data.get("this_pc_cpu_percent"))
            else:
                info["note"] = f"No PC details (the gateway's /status answered HTTP {r.status_code})."
        except (httpx.HTTPError, ValueError) as exc:
            info["latency_ms"] = None
            info["error"] = f"unreachable ({exc.__class__.__name__})"
        self.gateway = info
        self.history["gateway"].append({"t": utcnow().isoformat(), "ok": info["reachable"], "api_ms": info.get("latency_ms"),
                                        "gen_ms": None, "pcs_online": info.get("pcs_online")})

    async def _services(self) -> None:
        async def timed(name: str, coro) -> None:
            t0 = time.perf_counter()
            try:
                ok, detail = await asyncio.wait_for(coro, timeout=10)
            except Exception as exc:
                ok, detail = False, f"failed ({exc.__class__.__name__})"
            self.services[name] = {"online": ok, "detail": detail, "latency_ms": _ms(t0)}

        async def pg():
            async with dbm.session() as s:
                await s.execute(text("select 1"))
            return True, "online"

        async def rd():
            ok = await redisx.ping()
            return ok, "online" if ok else "offline (PostgreSQL is used instead)"

        async def clam():
            st = await clamav.status()
            return st["available"], (st["version"].split("/")[0] if st["available"] else "starting / unavailable")

        await asyncio.gather(timed("postgres", pg()), timed("redis", rd()), timed("clamav", clam()), timed("runner", runner_client.health()))

    async def check_once(self) -> dict[str, Any]:
        async with self._lock:
            reg = ai.registry()
            await asyncio.gather(self._gateway(), self._services(),
                                 *(self._provider(k, p) for k, p in list(reg.providers.items())))
            for key in list(self.latest):
                if key not in reg.providers:
                    self.latest.pop(key, None)
            for key, rec in self.latest.items():
                ai.note_status(key, bool(rec["online"]))
            self.checked_at = utcnow()
        return self.snapshot()

    # ------------------------------------------------------------------ views
    def fallback_active(self) -> bool:
        """True while the local Docker Gemma does the Gemma work: chosen by the admin, or the gateway is down."""
        if self.latest.get(ai.LOCAL_FALLBACK) is None:
            return False
        gw = self.latest.get("gemma")
        return ai.gemma_source() == "local" or gw is None or not gw["online"]

    def _gemma_state(self) -> dict[str, Any]:
        local = self.latest.get(ai.LOCAL_FALLBACK)
        return {"gemma_source": ai.gemma_source(), "local_online": bool(local and local["online"]),
                "gateway_configured": "gemma" in ai.registry().providers,
                "gateway_online": bool(self.latest.get("gemma", {}).get("online"))}

    def snapshot(self) -> dict[str, Any]:
        s = get_settings()
        local = ai.registry().providers.get(ai.LOCAL_FALLBACK)
        qwen = ai.registry().providers.get(ai.LOCAL_QWEN)
        interval = s.status_check_interval_s
        next_at = None
        if self.checked_at:
            next_at = (self.checked_at.timestamp() + interval)
        providers = []
        for key, rec in self.latest.items():
            providers.append({**rec, "history": list(self.history[key])})
        order = {"gemma": 0, "gemma_local": 1, "qwen": 2, "qwen_local": 3, "claude_opus": 4, "claude_haiku": 5, "chatgpt": 6, "chatgpt_mini": 7}
        providers.sort(key=lambda r: order.get(r["key"], 9))
        return {
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "next_check_at": next_at, "interval_s": interval,
            "providers": providers, "gateway": {**self.gateway, "history": list(self.history["gateway"])},
            "services": self.services, "fallback_active": self.fallback_active(), **self._gemma_state(),
            "effort_cap": s.local_gemma_effort_cap, "local_gemma_model": local.model if local else None,
            "local_qwen_model": qwen.model if qwen else None,
        }

    def summary(self) -> dict[str, Any]:
        gw = self.gateway
        online = [r for r in self.latest.values() if r["online"]]
        return {"checked_at": self.checked_at.isoformat() if self.checked_at else None,
                "fallback_active": self.fallback_active(), "effort_cap": get_settings().local_gemma_effort_cap,
                **self._gemma_state(),
                "pcs_online": gw.get("pcs_online"), "pcs_total": gw.get("pcs_total"),
                "models_online": len(online), "models_total": len(self.latest)}

    # ------------------------------------------------------------------ loop
    async def _loop(self) -> None:
        interval = max(10, get_settings().status_check_interval_s)
        while True:
            try:
                await app_settings.refresh()   # picks up changes made on another process / with `python -m elvin.admin`
            except Exception:  # pragma: no cover - the database may be restarting
                log.warning("could not re-read the app settings", exc_info=True)
            try:
                await self.check_once()
            except Exception:  # pragma: no cover - never let the monitor die
                log.exception("status check failed")
            await asyncio.sleep(interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self.started = True
            self._task = asyncio.create_task(self._loop(), name="status-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None


monitor = StatusMonitor()
