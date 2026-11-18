"""Model providers.

* Gemma 3 - the user's OpenAI-compatible gateway (llama.cpp behind it).
* Qwen Coder - local: vLLM (NVIDIA/AMD, OpenAI-compatible API) or Ollama
  (Apple-Silicon Metal / AMD on Windows, native API so num_ctx can be set).
* Claude - official `anthropic` SDK (Opus for coding, Haiku for bug hunting).
* ChatGPT - OpenAI Chat Completions API.

Every provider returns a ChatResult or raises ProviderError(retryable=...)
so the router can fail over to the next model in the chain."""
from __future__ import annotations

import base64
import datetime as dt
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("elvin.ai")

# Elvin effort level -> provider-specific knobs
CLAUDE_EFFORT = {"low": "low", "medium": "medium", "high": "high", "max": "xhigh"}
OPENAI_EFFORT = {"low": "low", "medium": "medium", "high": "high", "max": "high"}
CLAUDE_REFUSAL_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}

FIM_STOPS = ["<|endoftext|>", "<|fim_prefix|>", "<|fim_suffix|>", "<|fim_middle|>", "<|file_sep|>", "<|im_end|>", "\n\n\n"]


@dataclass
class ImageInput:
    data: bytes
    mime: str = "image/png"

    def b64(self) -> str:
        return base64.b64encode(self.data).decode()


@dataclass
class ChatResult:
    text: str
    provider: str
    model: str
    finish: str = "stop"
    usage: dict = field(default_factory=dict)
    data: Any = None
    elapsed: float = 0.0


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = True, kind: str = "error") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.kind = kind


_http: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            follow_redirects=False,
            headers={"User-Agent": "Elvin/1.0"},
        )
    return _http


async def close_http() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
    _http = None


def _status_error(name: str, resp: httpx.Response) -> ProviderError:
    code = resp.status_code
    snippet = resp.text[:300].replace("\n", " ")
    if code in (401, 403):
        return ProviderError(f"{name} rejected the credentials (HTTP {code}).", retryable=False, kind="auth")
    if code == 404:
        return ProviderError(f"{name}: model or endpoint not found (HTTP 404). {snippet}", retryable=False, kind="not_found")
    if code == 429:
        return ProviderError(f"{name} is rate-limiting requests (HTTP 429).", retryable=True, kind="rate_limit")
    if code >= 500:
        return ProviderError(f"{name} had a server error (HTTP {code}).", retryable=True, kind="server")
    return ProviderError(f"{name} refused the request (HTTP {code}): {snippet}", retryable=False, kind="bad_request")


class Provider:
    key = ""
    label = ""
    family = ""  # gemma | qwen | claude | chatgpt | fake
    model = ""
    vision = False
    context = 8192

    async def chat(self, messages: list[dict], *, system: str | None = None, max_tokens: int = 2048,
                   json_mode: bool = False, images: list[ImageInput] | None = None, temperature: float = 0.2,
                   role: str = "", effort: str | None = None, model: str | None = None) -> ChatResult:
        raise NotImplementedError

    async def fim(self, prefix: str, suffix: str, max_tokens: int = 96) -> str:
        raise ProviderError(f"{self.label} does not support autocomplete.", retryable=False, kind="unsupported")

    async def health(self) -> tuple[bool, str]:
        return True, "configured"


def _openai_messages(messages: list[dict], system: str | None, images: list[ImageInput] | None) -> list[dict]:
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    out.extend({"role": m["role"], "content": m["content"]} for m in messages)
    if images:
        for idx in range(len(out) - 1, -1, -1):
            if out[idx]["role"] == "user":
                text = out[idx]["content"]
                parts: list[dict] = [{"type": "text", "text": text}]
                parts += [{"type": "image_url", "image_url": {"url": f"data:{im.mime};base64,{im.b64()}"}} for im in images]
                out[idx] = {"role": "user", "content": parts}
                break
    return out


class OpenAICompatProvider(Provider):
    """Gateway (Gemma), vLLM (Qwen) and OpenAI (ChatGPT) all speak this API."""

    def __init__(self, key: str, label: str, family: str, base_url: str, api_key: str, model: str, *,
                 vision: bool, context: int, is_openai: bool = False, read_timeout: float = 300.0,
                 reasoning_effort: str | None = None) -> None:
        self.key, self.label, self.family = key, label, family
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.vision = vision
        self.context = context
        self.is_openai = is_openai
        self.read_timeout = read_timeout
        self.reasoning_effort = reasoning_effort
        self._json_supported = True

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def chat(self, messages: list[dict], *, system: str | None = None, max_tokens: int = 2048,
                   json_mode: bool = False, images: list[ImageInput] | None = None, temperature: float = 0.2,
                   role: str = "", effort: str | None = None, model: str | None = None) -> ChatResult:
        use_model = model or self.model
        body: dict[str, Any] = {"model": use_model, "messages": _openai_messages(messages, system, images)}
        if self.is_openai:
            body["max_completion_tokens"] = max_tokens + 4000  # room for hidden reasoning tokens
            reasoning = OPENAI_EFFORT.get(effort or "", self.reasoning_effort)
            if reasoning and use_model.startswith(("gpt-5", "o")):
                body["reasoning_effort"] = reasoning
        else:
            body["max_tokens"] = max_tokens
            body["temperature"] = temperature
        if json_mode and self._json_supported:
            body["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        timeout = httpx.Timeout(connect=10.0, read=self.read_timeout, write=60.0, pool=30.0)
        try:
            resp = await http().post(f"{self.base_url}/chat/completions", json=body, headers=self._headers(), timeout=timeout)
            if resp.status_code == 400 and ("response_format" in body or "reasoning_effort" in body):
                body.pop("response_format", None)
                body.pop("reasoning_effort", None)
                self._json_supported = False
                resp = await http().post(f"{self.base_url}/chat/completions", json=body, headers=self._headers(), timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.label} timed out.", retryable=True, kind="timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.label} is unreachable ({exc.__class__.__name__}).", retryable=True, kind="network") from exc
        if resp.status_code != 200:
            raise _status_error(self.label, resp)
        try:
            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.label} sent an unreadable reply.", retryable=True, kind="bad_reply") from exc
        finish = "length" if choice.get("finish_reason") == "length" else "stop"
        return ChatResult(text=content, provider=self.key, model=data.get("model", use_model), finish=finish,
                          usage=data.get("usage") or {}, elapsed=time.monotonic() - started)

    async def fim(self, prefix: str, suffix: str, max_tokens: int = 96) -> str:
        body = {"model": self.model, "prompt": f"<|fim_prefix|>{prefix}<|fim_suffix|>{suffix}<|fim_middle|>",
                "max_tokens": max_tokens, "temperature": 0.1, "stop": FIM_STOPS}
        try:
            resp = await http().post(f"{self.base_url}/completions", json=body, headers=self._headers(),
                                     timeout=httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0))
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.label} autocomplete unavailable.", retryable=True, kind="network") from exc
        if resp.status_code != 200:
            raise _status_error(self.label, resp)
        return resp.json()["choices"][0].get("text", "")

    async def health(self) -> tuple[bool, str]:
        try:
            resp = await http().get(f"{self.base_url}/models", headers=self._headers(),
                                    timeout=httpx.Timeout(connect=4.0, read=8.0, write=5.0, pool=5.0))
        except httpx.HTTPError as exc:
            return False, f"unreachable ({exc.__class__.__name__})"
        if resp.status_code in (401, 403):
            return False, "credentials rejected"
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        return True, "online"


class OllamaProvider(Provider):
    """Ollama's native API (lets Elvin set num_ctx and use raw FIM prompts)."""

    def __init__(self, key: str, label: str, base_url: str, model: str, *, context: int,
                 autocomplete_model: str = "", family: str = "qwen", vision: bool = False, threads: int = 0) -> None:
        self.key, self.label, self.family = key, label, family
        self.base_url = base_url.rstrip("/").removesuffix("/v1")
        self.model = model
        self.context = context
        self.vision = vision
        self.autocomplete_model = autocomplete_model or model
        self.threads = max(0, threads)   # CPU-only models: hybrid CPUs run fastest on their performance cores only

    async def chat(self, messages: list[dict], *, system: str | None = None, max_tokens: int = 2048,
                   json_mode: bool = False, images: list[ImageInput] | None = None, temperature: float = 0.2,
                   role: str = "", effort: str | None = None, model: str | None = None,
                   keep_alive: str | None = None) -> ChatResult:
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": m["role"], "content": m["content"]} for m in messages
        ]
        if images:
            for idx in range(len(msgs) - 1, -1, -1):
                if msgs[idx]["role"] == "user":
                    msgs[idx]["images"] = [im.b64() for im in images]
                    break
        body: dict[str, Any] = {
            "model": self.model, "messages": msgs, "stream": False, "keep_alive": keep_alive or "30m",
            "options": {"num_ctx": self.context, "temperature": temperature, "num_predict": max_tokens,
                        **({"num_thread": self.threads} if self.threads else {})},
        }
        if json_mode:
            body["format"] = "json"
        started = time.monotonic()
        try:
            resp = await http().post(f"{self.base_url}/api/chat", json=body,
                                     timeout=httpx.Timeout(connect=10.0, read=420.0, write=60.0, pool=30.0))
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.label} timed out.", retryable=True, kind="timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.label} is unreachable ({exc.__class__.__name__}).", retryable=True, kind="network") from exc
        if resp.status_code != 200:
            raise _status_error(self.label, resp)
        try:
            data = resp.json()
            content = data["message"]["content"] or ""
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError(f"{self.label} sent an unreadable reply.", retryable=True, kind="bad_reply") from exc
        usage = {"prompt_tokens": data.get("prompt_eval_count", 0), "completion_tokens": data.get("eval_count", 0)}
        return ChatResult(text=content, provider=self.key, model=self.model,
                          finish="length" if data.get("done_reason") == "length" else "stop", usage=usage,
                          elapsed=time.monotonic() - started)

    async def loaded(self) -> float | None:
        """Seconds until Ollama unloads this model, or None when it is not loaded right now."""
        try:
            resp = await http().get(f"{self.base_url}/api/ps", timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0))
            for m in resp.json().get("models", []):
                if self.model in (m.get("name"), m.get("model")):
                    exp = re.sub(r"(\.\d{6})\d+", r"\1", str(m.get("expires_at") or "")).replace("Z", "+00:00")
                    if not exp:
                        return 60.0
                    left = (dt.datetime.fromisoformat(exp) - dt.datetime.now(dt.timezone.utc)).total_seconds()
                    return max(0.0, left)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return None
        return None

    async def fim(self, prefix: str, suffix: str, max_tokens: int = 96) -> str:
        body = {
            "model": self.autocomplete_model, "prompt": f"<|fim_prefix|>{prefix}<|fim_suffix|>{suffix}<|fim_middle|>",
            "raw": True, "stream": False, "keep_alive": "30m",
            # same num_ctx/num_thread as chat when one model does both, or Ollama reloads it on every switch
            "options": {"num_predict": max_tokens, "temperature": 0.1, "stop": FIM_STOPS,
                        "num_ctx": self.context if self.autocomplete_model == self.model else 4096,
                        **({"num_thread": self.threads} if self.threads else {})},
        }
        try:
            resp = await http().post(f"{self.base_url}/api/generate", json=body,
                                     timeout=httpx.Timeout(connect=5.0, read=25.0, write=10.0, pool=5.0))
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.label} autocomplete unavailable.", retryable=True, kind="network") from exc
        if resp.status_code != 200:
            raise _status_error(self.label, resp)
        return resp.json().get("response", "")

    async def health(self) -> tuple[bool, str]:
        try:
            resp = await http().get(f"{self.base_url}/api/tags", timeout=httpx.Timeout(connect=3.0, read=6.0, write=5.0, pool=5.0))
        except httpx.HTTPError as exc:
            return False, f"unreachable ({exc.__class__.__name__})"
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        names = {m.get("name", "") for m in resp.json().get("models", [])}
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if wanted not in names:
            return False, f"model {self.model} not downloaded yet"
        return True, "online"


class AnthropicProvider(Provider):
    """Claude through the official SDK. Opus is the coding fallback (with the
    server-side refusal fallback enabled); Haiku handles bug hunting."""

    def __init__(self, key: str, label: str, model: str, api_key: str, *, effort: str, refusal_fallback: bool) -> None:
        import anthropic

        self._sdk = anthropic
        self.key, self.label, self.family = key, label, "claude"
        self.model = model
        self.vision = True
        self.context = 200_000
        self.effort = effort
        self.refusal_fallback = refusal_fallback
        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2, timeout=600.0)

    @staticmethod
    def _messages(messages: list[dict], images: list[ImageInput] | None) -> list[dict]:
        out = [{"role": m["role"], "content": m["content"]} for m in messages]
        if images:
            for idx in range(len(out) - 1, -1, -1):
                if out[idx]["role"] == "user":
                    blocks: list[dict] = [
                        {"type": "image", "source": {"type": "base64", "media_type": im.mime, "data": im.b64()}}
                        for im in images
                    ]
                    blocks.append({"type": "text", "text": out[idx]["content"]})
                    out[idx] = {"role": "user", "content": blocks}
                    break
        return out

    async def chat(self, messages: list[dict], *, system: str | None = None, max_tokens: int = 2048,
                   json_mode: bool = False, images: list[ImageInput] | None = None, temperature: float = 0.2,
                   role: str = "", effort: str | None = None, model: str | None = None) -> ChatResult:
        sdk = self._sdk
        sys_prompt = system or ""
        if json_mode:
            sys_prompt += "\n\nReply with one valid JSON object only - no prose before or after it and no code fences."
        use_model = model or self.model
        kwargs: dict[str, Any] = {
            "model": use_model,
            # adaptive thinking shares this budget, so leave headroom above the answer size
            "max_tokens": min(max(max_tokens * 2, 8000), 64000),
            "messages": self._messages(messages, images),
            "output_config": {"effort": CLAUDE_EFFORT.get(effort or "", self.effort)},
        }
        if sys_prompt:
            kwargs["system"] = sys_prompt
        started = time.monotonic()
        try:
            if self.refusal_fallback and use_model in CLAUDE_REFUSAL_FALLBACK_MODELS:
                api = self.client.beta.messages
                kwargs["betas"] = ["server-side-fallback-2026-07-01"]
                kwargs["fallbacks"] = "default"
            else:
                api = self.client.messages
            if kwargs["max_tokens"] > 16000:
                async with api.stream(**kwargs) as stream:
                    resp = await stream.get_final_message()
            else:
                resp = await api.create(**kwargs)
        except sdk.AuthenticationError as exc:
            raise ProviderError("Claude rejected the API key.", retryable=False, kind="auth") from exc
        except sdk.PermissionDeniedError as exc:
            raise ProviderError("This Claude API key may not use that model.", retryable=False, kind="auth") from exc
        except sdk.NotFoundError as exc:
            raise ProviderError(f"Claude model {use_model} was not found.", retryable=False, kind="not_found") from exc
        except sdk.RateLimitError as exc:
            raise ProviderError("Claude is rate-limiting requests.", retryable=True, kind="rate_limit") from exc
        except sdk.BadRequestError as exc:
            raise ProviderError(f"Claude refused the request: {exc.message}", retryable=False, kind="bad_request") from exc
        except sdk.APIStatusError as exc:
            raise ProviderError(f"Claude API error ({exc.status_code}).", retryable=exc.status_code >= 500, kind="server") from exc
        except (sdk.APITimeoutError, sdk.APIConnectionError) as exc:
            raise ProviderError("Claude is unreachable (network).", retryable=True, kind="network") from exc
        if resp.stop_reason == "refusal":
            raise ProviderError("Claude declined this request.", retryable=False, kind="refusal")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        usage = {"prompt_tokens": getattr(resp.usage, "input_tokens", 0), "completion_tokens": getattr(resp.usage, "output_tokens", 0)}
        return ChatResult(text=text, provider=self.key, model=getattr(resp, "model", use_model),
                          finish="length" if resp.stop_reason == "max_tokens" else "stop", usage=usage,
                          elapsed=time.monotonic() - started)

    async def health(self) -> tuple[bool, str]:
        sdk = self._sdk
        try:
            await self.client.with_options(timeout=10.0, max_retries=0).models.retrieve(self.model)
            return True, "API reachable"
        except sdk.AuthenticationError:
            return False, "API key rejected"
        except sdk.NotFoundError:
            return False, f"model {self.model} not available"
        except (sdk.APIConnectionError, sdk.APITimeoutError):
            return False, "unreachable"
        except sdk.APIStatusError as exc:
            return False, f"HTTP {exc.status_code}"
