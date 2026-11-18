"""Model registry, AI "teams", concurrency limits, circuit breakers and
fail-over. Every AI call in Elvin goes through `call()`:

* each provider family has its own semaphore (the gateway exposes 4 slots, a
  local Qwen 2, cloud APIs 4) so Elvin never floods a model server;
* a global cap on waiting calls returns "busy" instead of piling up work;
* 3 consecutive failures open a provider's circuit for 30 s;
* JSON answers are parsed and, if malformed, re-asked once before the next
  provider in the chain is tried."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ... import limits
from ...config import get_settings
from .. import usage
from . import jsonx
from .providers import (
    AnthropicProvider,
    ChatResult,
    ImageInput,
    OllamaProvider,
    OpenAICompatProvider,
    Provider,
    ProviderError,
)

log = logging.getLogger("elvin.ai.router")

# role -> ordered provider chain, per team
TEAMS: dict[str, dict[str, Any]] = {
    "gemma_qwen": {
        "label": "Gemma 3 + Qwen Coder", "needs": {"gemma", "qwen"}, "local": True,
        "blurb": "Fully local: Gemma plans, sees images and verifies; Qwen writes the code.",
        "planner": ["gemma", "qwen"], "coder": ["qwen", "gemma"], "vision": ["gemma"],
        "verifier": ["gemma", "qwen"], "utility": ["gemma", "qwen"],
    },
    "qwen_claude": {
        "label": "Qwen Coder + Claude", "needs": {"qwen", "claude"},
        "blurb": "Qwen writes code, Claude Opus is the coding fallback, Claude Haiku plans and checks.",
        "planner": ["claude_haiku", "qwen"], "coder": ["qwen", "claude_opus"], "vision": ["claude_haiku", "claude_opus"],
        "verifier": ["claude_haiku", "qwen"], "utility": ["claude_haiku", "qwen"],
    },
    "gemma_claude": {
        "label": "Gemma 3 + Claude", "needs": {"gemma", "claude"},
        "blurb": "Gemma plans and verifies, Claude Opus writes the code.",
        "planner": ["gemma", "claude_haiku"], "coder": ["claude_opus"], "vision": ["gemma", "claude_haiku"],
        "verifier": ["gemma", "claude_haiku"], "utility": ["gemma", "claude_haiku"],
    },
    "qwen_claude_gemma": {
        "label": "Qwen + Claude + Gemma", "needs": {"qwen", "claude", "gemma"},
        "blurb": "Gemma plans and sees, Qwen codes with Claude Opus as fallback, Claude Haiku reviews.",
        "planner": ["gemma", "claude_haiku"], "coder": ["qwen", "claude_opus"], "vision": ["gemma", "claude_haiku"],
        "verifier": ["claude_haiku", "gemma"], "utility": ["gemma", "claude_haiku", "qwen"],
    },
    "claude": {
        "label": "Claude only", "needs": {"claude"},
        "blurb": "Claude Opus writes the code; Claude Haiku plans, reviews and handles small jobs.",
        "planner": ["claude_haiku", "claude_opus"], "coder": ["claude_opus"], "vision": ["claude_opus"],
        "verifier": ["claude_haiku"], "utility": ["claude_haiku"],
    },
    "gemma_chatgpt": {
        "label": "Gemma 3 + ChatGPT", "needs": {"gemma", "chatgpt"},
        "blurb": "Gemma plans and verifies, ChatGPT writes the code.",
        "planner": ["gemma", "chatgpt_mini"], "coder": ["chatgpt"], "vision": ["gemma", "chatgpt_mini"],
        "verifier": ["gemma", "chatgpt_mini"], "utility": ["gemma", "chatgpt_mini"],
    },
    "qwen_chatgpt": {
        "label": "Qwen Coder + ChatGPT", "needs": {"qwen", "chatgpt"},
        "blurb": "Qwen writes code with ChatGPT as the fallback; ChatGPT plans and checks.",
        "planner": ["chatgpt_mini", "qwen"], "coder": ["qwen", "chatgpt"], "vision": ["chatgpt_mini", "chatgpt"],
        "verifier": ["chatgpt_mini", "qwen"], "utility": ["chatgpt_mini", "qwen"],
    },
    "qwen_chatgpt_gemma": {
        "label": "Qwen + ChatGPT + Gemma", "needs": {"qwen", "chatgpt", "gemma"},
        "blurb": "Gemma plans and sees, Qwen codes with ChatGPT as fallback, ChatGPT reviews.",
        "planner": ["gemma", "chatgpt_mini"], "coder": ["qwen", "chatgpt"], "vision": ["gemma", "chatgpt_mini"],
        "verifier": ["chatgpt_mini", "gemma"], "utility": ["gemma", "chatgpt_mini", "qwen"],
    },
    "chatgpt": {
        "label": "ChatGPT only", "needs": {"chatgpt"},
        "blurb": "ChatGPT plans, codes, sees images and verifies.",
        "planner": ["chatgpt_mini", "chatgpt"], "coder": ["chatgpt"], "vision": ["chatgpt"],
        "verifier": ["chatgpt_mini"], "utility": ["chatgpt_mini"],
    },
}
TEAM_ORDER = ["gemma_qwen", "qwen_claude_gemma", "gemma_claude", "qwen_claude", "claude",
              "qwen_chatgpt_gemma", "gemma_chatgpt", "qwen_chatgpt", "chatgpt"]
BUGHUNT_PROVIDERS = ["claude_haiku", "chatgpt", "qwen"]
FAMILY_OF = {"gemma": "gemma", "gemma_local": "gemma", "qwen": "qwen", "qwen_local": "qwen", "claude_opus": "claude",
             "claude_haiku": "claude", "chatgpt": "chatgpt", "chatgpt_mini": "chatgpt"}
# concurrency groups (each has its own semaphore); both Docker models share one CPU Ollama
SEM_GROUP = {"gemma": "gateway", "gemma_local": "docker_llm", "qwen_local": "docker_llm", "qwen": "qwen", "claude_opus": "claude",
             "claude_haiku": "claude", "chatgpt": "chatgpt", "chatgpt_mini": "chatgpt"}
LOCAL_FALLBACK = "gemma_local"           # Gemma 3 running in Docker on this computer
LOCAL_QWEN = "qwen_local"                # Qwen Coder running in the same Docker Ollama: the local coding fallback
LOCAL_DOCKER = (LOCAL_FALLBACK, LOCAL_QWEN)   # CPU-only: effort capped, shorter answers
EFFORT_ORDER = ["low", "medium", "high", "max"]
# "auto": the gateway first, the local Docker Gemma when it is down; "local": the local Docker Gemma only
GEMMA_SOURCES = ("auto", "local")
_GATEWAY_KEYS = ("gateway_url", "gateway_api_key")
_overrides: dict[str, str] = {}
_offline: set[str] = set()               # models the 60-second status monitor found offline


def set_overrides(values: dict[str, str]) -> None:
    """Installation-wide overrides stored in the database (e.g. a new gateway address)."""
    _overrides.clear()
    _overrides.update({k: v for k, v in values.items() if v})


def apply_overrides(values: dict[str, str]) -> bool:
    """Apply the stored settings; rebuild the model registry only when the gateway itself changed.
    Returns True if anything changed."""
    clean = {k: v for k, v in values.items() if v}
    if clean == _overrides:
        return False
    gateway_changed = any(clean.get(k) != _overrides.get(k) for k in _GATEWAY_KEYS)
    set_overrides(clean)
    if gateway_changed:
        reset_registry()
    return True


def gateway_url() -> str:
    url = _overrides.get("gateway_url") or get_settings().gateway_url
    try:
        return normalize_gateway_url(url) if url else ""
    except ValueError:
        return url


def normalize_gateway_url(url: str) -> str:
    """'http://host:8000' -> 'http://host:8000/v1'. Raises ValueError for anything that isn't a plain http(s) address."""
    from urllib.parse import urlsplit

    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Enter an http(s) address like http://192.168.1.50:8000/v1")
    return url if parts.path.rstrip("/").endswith("/v1") else url + "/v1"


def gemma_source() -> str:
    value = _overrides.get("gemma_source") or get_settings().gemma_source
    return value if value in GEMMA_SOURCES else "auto"


def note_status(key: str, online: bool) -> None:
    """The status monitor reports every model after each check, so calls stop waiting
    for a model that is known to be down (it moves to the end of the chain)."""
    if online:
        _offline.discard(key)
    else:
        _offline.add(key)


def note_gateway(online: bool) -> None:
    note_status("gemma", online)


def gateway_down() -> bool:
    return "gemma" in _offline


def demote_offline(keys: list[str]) -> list[str]:
    """Models found offline at the last check go last: still a last resort, but nobody waits for them first."""
    return [k for k in keys if k not in _offline] + [k for k in keys if k in _offline]


def capped_effort(provider_key: str, effort: str) -> str:
    """The local CPU models in Docker never run at high/max effort."""
    if provider_key not in LOCAL_DOCKER:
        return effort
    cap = get_settings().local_gemma_effort_cap
    if cap in EFFORT_ORDER and effort in EFFORT_ORDER and EFFORT_ORDER.index(effort) > EFFORT_ORDER.index(cap):
        return cap
    return effort


class AIError(Exception):
    """No model could complete the call."""


class Busy(AIError):
    pass


@dataclass
class Breaker:
    failures: int = 0
    open_until: float = 0.0

    def available(self) -> bool:
        return time.monotonic() >= self.open_until

    def ok(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def fail(self) -> None:
        self.failures += 1
        if self.failures >= 3:
            self.open_until = time.monotonic() + 30
            self.failures = 0


class Registry:
    def __init__(self) -> None:
        s = get_settings()
        self.fake = os.environ.get("ELVIN_FAKE_AI") == "1"
        self.providers: dict[str, Provider] = {}
        if self.fake:
            from .fake import FakeProvider

            for key, label, fam in [("gemma", "Gemma 3 (test)", "gemma"), ("gemma_local", "Gemma 3 local (test)", "gemma"),
                                    ("qwen", "Qwen Coder (test)", "qwen"), ("qwen_local", "Qwen Coder local (test)", "qwen"),
                                    ("claude_opus", "Claude Opus (test)", "claude"), ("claude_haiku", "Claude Haiku (test)", "claude"),
                                    ("chatgpt", "ChatGPT (test)", "chatgpt"), ("chatgpt_mini", "ChatGPT mini (test)", "chatgpt")]:
                self.providers[key] = FakeProvider(key, label, fam)
        else:
            gw_url = gateway_url()
            if gw_url:
                self.providers["gemma"] = OpenAICompatProvider(
                    "gemma", "Gemma 3 (gateway)", "gemma", gw_url, _overrides.get("gateway_api_key") or s.gateway_api_key,
                    s.gateway_model, vision=True, context=s.gateway_context, read_timeout=420.0)
            if s.local_gemma_url:
                self.providers["gemma_local"] = OllamaProvider(
                    "gemma_local", "Gemma 3 (local Docker)", s.local_gemma_url, s.local_gemma_model,
                    context=s.local_gemma_context, family="gemma", vision="1b" not in s.local_gemma_model,
                    threads=s.local_gemma_threads)
                if s.local_qwen_model:
                    self.providers["qwen_local"] = OllamaProvider(
                        "qwen_local", "Qwen Coder (local Docker)", s.local_gemma_url, s.local_qwen_model,
                        context=s.local_gemma_context, autocomplete_model=s.local_qwen_model, family="qwen",
                        threads=s.local_gemma_threads)
            if s.has_local_qwen:
                if s.local_llm_backend == "ollama":
                    self.providers["qwen"] = OllamaProvider("qwen", "Qwen Coder (Ollama)", s.local_llm_url, s.qwen_model,
                                                            context=s.qwen_context, autocomplete_model=s.qwen_autocomplete_model)
                else:
                    self.providers["qwen"] = OpenAICompatProvider("qwen", "Qwen Coder (vLLM)", "qwen", s.local_llm_url, "",
                                                                  s.qwen_model, vision=False, context=s.qwen_context, read_timeout=420.0)
            if s.has_claude:
                self.providers["claude_opus"] = AnthropicProvider("claude_opus", "Claude Opus", s.claude_coder_model,
                                                                  s.anthropic_api_key, effort="high", refusal_fallback=True)
                self.providers["claude_haiku"] = AnthropicProvider("claude_haiku", "Claude Haiku", s.claude_fast_model,
                                                                   s.anthropic_api_key, effort="medium", refusal_fallback=False)
            if s.has_openai:
                self.providers["chatgpt"] = OpenAICompatProvider("chatgpt", "ChatGPT", "chatgpt", s.openai_base_url, s.openai_api_key,
                                                                 s.openai_model, vision=True, context=128_000, is_openai=True,
                                                                 reasoning_effort="medium")
                self.providers["chatgpt_mini"] = OpenAICompatProvider("chatgpt_mini", "ChatGPT mini", "chatgpt", s.openai_base_url,
                                                                      s.openai_api_key, s.openai_fast_model, vision=True,
                                                                      context=128_000, is_openai=True, reasoning_effort="low")
        conc = {"gateway": s.gateway_concurrency, "docker_llm": 1, "qwen": s.qwen_concurrency, "claude": s.cloud_concurrency,
                "chatgpt": s.cloud_concurrency}
        self.sems = {group: asyncio.Semaphore(max(1, n)) for group, n in conc.items()}
        self.breakers = {key: Breaker() for key in self.providers}
        self.waiting = 0
        self.health_cache: dict[str, tuple[float, bool, str]] = {}

    # -- configuration
    def families(self) -> set[str]:
        return {FAMILY_OF.get(k, k) for k in self.providers}

    def team_available(self, team_id: str) -> bool:
        team = TEAMS.get(team_id)
        return bool(team) and team["needs"] <= self.families()

    def teams(self) -> list[dict[str, Any]]:
        return [{"id": tid, "label": TEAMS[tid]["label"], "blurb": TEAMS[tid]["blurb"],
                 "available": self.team_available(tid), "needs": sorted(TEAMS[tid]["needs"])} for tid in TEAM_ORDER]

    def default_team(self) -> str:
        for tid in TEAM_ORDER:
            if self.team_available(tid):
                return tid
        return "gemma_qwen"

    def chain(self, team_id: str, role: str) -> list[str]:
        team = TEAMS.get(team_id) or TEAMS[self.default_team()]
        if not self.team_available(team_id):
            team = TEAMS[self.default_team()]
        keys = list(team.get(role, []))
        if role == "coder":
            # Writing code: GPU and cloud models first, the CPU-only Docker models last (Qwen before Gemma).
            # Measured: Docker Qwen 3B solved 65% of the evaluation's coding tasks, the gateway Gemma far more.
            if gemma_source() == "local" and "gemma" in keys:
                keys.remove("gemma")                                 # admin chose the local Docker Gemma for now
            keys += [k for k in (LOCAL_QWEN, LOCAL_FALLBACK) if k in self.providers and k not in keys]
            return demote_offline([k for k in keys if k in self.providers])
        if LOCAL_QWEN in self.providers and LOCAL_QWEN not in keys and "qwen" in keys:
            keys.insert(keys.index("qwen") + 1, LOCAL_QWEN)         # right after Qwen on this computer
        if LOCAL_FALLBACK in self.providers and LOCAL_FALLBACK not in keys:
            if "gemma" not in keys:
                keys.append(LOCAL_FALLBACK)                        # the very last resort for every team
            elif gemma_source() == "local":
                keys[keys.index("gemma")] = LOCAL_FALLBACK         # admin chose the local Docker Gemma for now
            else:
                keys.insert(keys.index("gemma") + 1, LOCAL_FALLBACK)  # right after the gateway Gemma
        return demote_offline([k for k in keys if k in self.providers])

    def label(self, key: str) -> str:
        p = self.providers.get(key)
        return p.label if p else key

    # -- health
    async def health(self, key: str, *, max_age: float = 30.0) -> tuple[bool, str]:
        cached = self.health_cache.get(key)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1], cached[2]
        provider = self.providers.get(key)
        if provider is None:
            return False, "not configured"
        try:
            ok, detail = await asyncio.wait_for(provider.health(), timeout=10)
        except Exception as exc:  # pragma: no cover - network dependent
            ok, detail = False, f"health check failed ({exc.__class__.__name__})"
        self.health_cache[key] = (time.monotonic(), ok, detail)
        return ok, detail

    async def status(self) -> list[dict[str, Any]]:
        keys = list(self.providers)
        results = await asyncio.gather(*(self.health(k) for k in keys))
        return [{"key": k, "label": self.providers[k].label, "model": self.providers[k].model,
                 "family": FAMILY_OF.get(k, k), "online": ok, "detail": detail,
                 "circuit_open": not self.breakers[k].available()} for k, (ok, detail) in zip(keys, results)]

    @contextlib.asynccontextmanager
    async def slot(self, key: str):
        if self.waiting >= limits.MAX_QUEUED_AI_CALLS:
            raise Busy("Elvin's AI models are busy right now - please try again in a minute.")
        sem = self.sems[SEM_GROUP.get(key, "gateway")]
        self.waiting += 1
        try:
            await sem.acquire()
        finally:
            self.waiting -= 1
        try:
            yield
        finally:
            sem.release()


_registry: Registry | None = None


def registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry()
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None


OnAttempt = Callable[[str, str], Awaitable[None]] | None


async def call(
    role: str,
    *,
    team: str,
    messages: list[dict],
    system: str,
    task_role: str = "",
    max_tokens: int = 2048,
    json_mode: bool = False,
    want: type | None = dict,
    images: list[ImageInput] | None = None,
    temperature: float = 0.2,
    chain: list[str] | None = None,
    on_attempt: OnAttempt = None,
    effort: str | None = None,
) -> ChatResult:
    """Run one AI call with fail-over. `role` selects the team chain
    (planner/coder/vision/verifier/utility); `task_role` tells test doubles
    which prompt this is."""
    reg = registry()
    effort = effort or usage.effort_now()
    keys = demote_offline(list(chain)) if chain is not None else reg.chain(team, role)
    if images:
        keys = [k for k in keys if reg.providers[k].vision] or keys
    if not keys:
        raise AIError("No AI model is configured for this job. Check Settings -> AI team.")
    errors: list[str] = []
    for key in keys:
        provider = reg.providers.get(key)
        if provider is None:
            continue
        breaker = reg.breakers[key]
        if not breaker.available():
            errors.append(f"{provider.label}: paused after repeated failures")
            continue
        if on_attempt:
            await on_attempt(key, provider.label)
        attempt_msgs = list(messages)
        override = usage.model_override(key)
        call_effort = capped_effort(key, effort)
        call_tokens = min(max_tokens, 3000) if key in LOCAL_DOCKER else max_tokens
        for attempt in range(2):
            started = time.monotonic()
            try:
                async with reg.slot(key):
                    result = await provider.chat(attempt_msgs, system=system, max_tokens=call_tokens, json_mode=json_mode,
                                                 images=images, temperature=temperature, role=task_role or role,
                                                 effort=call_effort, model=override)
            except Busy:
                raise
            except ProviderError as exc:
                usage.record(provider=key, model=override or provider.model, role=task_role or role, ok=False,
                             elapsed_ms=int((time.monotonic() - started) * 1000), error_kind=exc.kind)
                if exc.retryable:
                    breaker.fail()
                errors.append(f"{provider.label}: {exc}")
                if exc.retryable and attempt == 0 and exc.kind in ("timeout", "network", "server"):
                    await asyncio.sleep(1.5)
                    continue
                break
            good = bool(result.text.strip()) and (not json_mode or jsonx.extract(result.text, want=want) is not None)
            usage.record(provider=key, model=result.model, role=task_role or role, ok=good,
                         input_tokens=int(result.usage.get("prompt_tokens") or result.usage.get("input_tokens") or 0),
                         output_tokens=int(result.usage.get("completion_tokens") or result.usage.get("output_tokens") or 0),
                         elapsed_ms=int((time.monotonic() - started) * 1000), error_kind="" if good else "bad_output")
            if not json_mode:
                if not result.text.strip():
                    errors.append(f"{provider.label}: empty answer")
                    break
                breaker.ok()
                return result
            parsed = jsonx.extract(result.text, want=want)
            if parsed is not None:
                breaker.ok()
                result.data = parsed
                return result
            errors.append(f"{provider.label}: answer was not valid JSON")
            attempt_msgs = list(messages) + [
                {"role": "assistant", "content": result.text[:4000]},
                {"role": "user", "content": "That reply was not valid JSON. Send the same answer again as one valid JSON object only."},
            ]
        log.info("AI call %s/%s fell through %s", role, task_role, key)
    raise AIError("; ".join(errors[-4:]) or "All AI models failed.")


async def fim(prefix: str, suffix: str, max_tokens: int = 96) -> tuple[str, str]:
    reg = registry()
    keys = [k for k in demote_offline(["qwen", LOCAL_QWEN]) if k in reg.providers and reg.breakers[k].available()]
    if not keys:
        if "qwen" in reg.providers or LOCAL_QWEN in reg.providers:
            raise AIError("Qwen Coder is paused after repeated failures.")
        raise AIError("Autocomplete needs the local Qwen Coder model, which is not installed on this computer.")
    key = keys[0]
    provider = reg.providers[key]
    if key == LOCAL_QWEN:   # CPU model: a shorter window keeps ghost text responsive
        prefix, suffix = prefix[-1500:], suffix[:400]
    started = time.monotonic()
    try:
        async with reg.slot(key):
            text = await provider.fim(prefix, suffix, max_tokens=max_tokens)
    except ProviderError as exc:
        usage.record(provider=key, model=getattr(provider, "autocomplete_model", provider.model), role="autocomplete",
                     ok=False, elapsed_ms=int((time.monotonic() - started) * 1000), error_kind=exc.kind)
        if exc.retryable:
            reg.breakers[key].fail()
        raise AIError(str(exc)) from exc
    usage.record(provider=key, model=getattr(provider, "autocomplete_model", provider.model), role="autocomplete",
                 ok=True, input_tokens=(len(prefix) + len(suffix)) // 4, output_tokens=len(text) // 4,
                 elapsed_ms=int((time.monotonic() - started) * 1000))
    reg.breakers[key].ok()
    return text, provider.label
