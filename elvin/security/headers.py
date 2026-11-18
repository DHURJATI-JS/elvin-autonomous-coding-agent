"""Talisman-style security headers for FastAPI, as a pure ASGI middleware
(safe for streaming/SSE responses). Each request gets a fresh CSP nonce in
request.state.csp_nonce; templates put it on every <script>/<style> tag."""
from __future__ import annotations

import secrets

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def build_csp(nonce: str) -> str:
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            f"style-src 'self' 'nonce-{nonce}'",
            "img-src 'self' data: blob:",
            "font-src 'self'",
            "connect-src 'self'",
            "media-src 'self'",
            "object-src 'none'",
            "frame-src 'none'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "worker-src 'self' blob:",
            "manifest-src 'self'",
        ]
    )


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, force_https: bool = False) -> None:
        self.app = app
        self.force_https = force_https

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        nonce = secrets.token_urlsafe(18)
        scope.setdefault("state", {})["csp_nonce"] = nonce
        path: str = scope.get("path", "")
        host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1").split(":")[0].strip("[]")
        trustworthy = scope.get("scheme") == "https" or host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "content-security-policy" not in headers:
                    headers["Content-Security-Policy"] = build_csp(nonce)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                headers["Permissions-Policy"] = (
                    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
                    "magnetometer=(), gyroscope=(), accelerometer=(), interest-cohort=()"
                )
                if trustworthy:  # browsers ignore (and complain about) COOP on plain-HTTP LAN origins
                    headers["Cross-Origin-Opener-Policy"] = "same-origin"
                headers["Cross-Origin-Resource-Policy"] = "same-origin"
                headers["X-Permitted-Cross-Domain-Policies"] = "none"
                if self.force_https:
                    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
                if not path.startswith("/static/"):
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_wrapper)
