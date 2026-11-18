"""Runtime configuration. Every value comes from environment variables (the
installer writes them to .env, docker compose passes them in)."""
from __future__ import annotations

import functools
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    app_name: str = "Elvin"
    environment: str = "production"          # "production" | "development" | "test"
    secret_key: str = Field(default="", min_length=0)
    base_url: str = "http://localhost:8765"
    cookie_secure: bool = False              # set true when served over HTTPS
    trusted_origins: str = ""                # extra comma-separated origins (LAN access)

    database_url: str = "postgresql+asyncpg://elvin:elvin@db:5432/elvin"
    redis_url: str = "redis://redis:6379/0"

    # Where projects and local copies of Elvin's data live (bind-mounted ~/Elvin).
    elvin_home: str = "/elvin"
    host_home_hint: str = "~/Elvin"          # how the folder is called on the user's computer

    # Platform facts written by the installer (the container itself is Linux).
    host_os: str = "unknown"                 # macos | windows | linux
    host_arch: str = ""
    host_gpu: str = "none"                   # nvidia | amd | apple | none
    host_gpu_name: str = ""
    host_ram_gb: float = 0
    host_vram_gb: float = 0
    host_compatible: bool = True             # can this machine run a local Qwen model?

    # --- AI: Gemma 3 through the user's OpenAI-compatible gateway ---
    gateway_url: str = ""                    # your OpenAI-compatible Gemma gateway, e.g. http://192.168.1.50:8000/v1
    gateway_api_key: str = ""
    gateway_model: str = "gemma"
    gateway_concurrency: int = 4
    gateway_context: int = 8192

    # --- AI: local Gemma 3 in Docker - automatic fallback when the gateway is down ---
    local_gemma_url: str = ""                # http://gemma:11434 (the "gemma" compose service)
    local_gemma_model: str = "gemma3:4b"
    local_gemma_context: int = 8192
    local_gemma_effort_cap: str = "medium"   # never run the local CPU Gemma at high/max effort
    local_qwen_model: str = ""               # e.g. qwen2.5-coder:3b in the same Docker Ollama: the local coding fallback
    local_gemma_threads: int = 0             # CPU threads (0 = Ollama default); Apple Silicon: the performance cores
    gemma_source: str = "auto"               # "auto": gateway first, local Docker Gemma when it is down; "local": local only
    status_check_interval_s: int = 60        # System Status: how often AI latency is measured

    # --- AI: local Qwen Coder (vLLM on NVIDIA/AMD, Ollama/Metal on Apple Silicon) ---
    local_llm_backend: str = "none"          # vllm | ollama | none
    local_llm_url: str = ""                  # vllm: http://vllm:8000/v1   ollama: http://host.docker.internal:11435
    qwen_model: str = "qwen2.5-coder:7b"
    qwen_autocomplete_model: str = ""        # small FIM model; empty = reuse qwen_model
    qwen_concurrency: int = 2
    qwen_context: int = 16384

    # --- AI: cloud fallbacks ---
    anthropic_api_key: str = ""
    claude_coder_model: str = "claude-opus-5-5"
    claude_fast_model: str = "claude-haiku-5-5"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5"
    openai_fast_model: str = "gpt-5-mini"
    cloud_concurrency: int = 4

    # --- services ---
    clamav_host: str = "clamav"
    clamav_port: int = 3310
    clamav_required: bool = True
    runner_url: str = "http://runner:9000"
    runner_token: str = ""
    osv_url: str = "https://api.osv.dev"
    pypi_url: str = "https://pypi.org"
    npm_registry_url: str = "https://registry.npmjs.org"
    loadtest_allow_public: bool = False
    rate_limit_multiplier: int = 1           # >1 only for throwaway stress-test stacks

    # Password-reset mail. Without SMTP the link is written to the server log
    # and to <elvin_home>/data/outbox/ so the person running Elvin can pass it on.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "elvin@localhost"
    smtp_starttls: bool = True

    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    @property
    def home(self) -> Path:
        return Path(self.elvin_home)

    @property
    def workspace(self) -> Path:
        return self.home / "workspace"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def has_claude(self) -> bool:
        return bool(self.anthropic_api_key.strip())

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key.strip())

    @property
    def has_local_qwen(self) -> bool:
        return self.local_llm_backend in ("vllm", "ollama") and bool(self.local_llm_url)

    def startup_problems(self) -> list[str]:
        """Conditions under which Elvin refuses to start: a computer too weak for local models needs a cloud key."""
        problems: list[str] = []
        if len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be set to at least 32 random characters (the installer generates one).")
        if not (self.has_claude or self.has_openai):
            if not self.host_compatible or not self.has_local_qwen:
                problems.append(
                    "This computer has no GPU that can run Qwen Coder (integrated GPUs and the CPU only get the slower "
                    "Docker models), so an ANTHROPIC_API_KEY (Claude) or an OPENAI_API_KEY (ChatGPT) is required. "
                    "Re-run the installer and enter one."
                )
            elif tiny_model(self.qwen_model):
                problems.append(
                    f"This computer's GPU only fits a Qwen Coder smaller than 3B ({self.qwen_model}), so an "
                    "ANTHROPIC_API_KEY (Claude) or an OPENAI_API_KEY (ChatGPT) is required. Re-run the installer and enter one."
                )
        return problems

    def gemma_problems(self, gateway_url: str) -> list[str]:
        """Checked once the gateway address saved in the app is known: planning and reading images need the
        gateway, a local Gemma that can read images (4B or larger), or a cloud key."""
        if self.has_claude or self.has_openai or gateway_url:
            return []
        if self.local_gemma_url and not tiny_model(self.local_gemma_model):
            return []
        return ["There is no Gemma gateway and Docker has too little memory for a local Gemma that can read images "
                "(the 1B model can't), so an ANTHROPIC_API_KEY or OPENAI_API_KEY is required - or set GATEWAY_URL. "
                "Re-run the installer."]


def tiny_model(name: str) -> bool:
    """Models under 3B parameters are too small to rely on for coding, planning or images."""
    n = (name or "").lower()
    return any(t in n for t in ("0.5b", "1.5b", ":1b", "-1b"))


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()
