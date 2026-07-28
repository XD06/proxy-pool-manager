"""Auth route module — handles login, logout, and auth status."""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..schemas import LoginRequest

AUTH_MAX_FAILURES = 5
AUTH_LOCK_WINDOW_SECONDS = 60
AUTH_SESSION_TTL_SECONDS = 8 * 3600
# Upper bounds so the in-memory dicts cannot grow without limit under
# brute-force attempts or repeated logins.
AUTH_MAX_SESSIONS = 500


def client_ip(request: Request, *, trust_proxy: bool = False) -> str:
    """Return the best-effort client address for rate limiting."""
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


@dataclass
class AuthContext:
    """Holds auth-related mutable state and config accessors."""

    # token -> unix expiry timestamp
    sessions: dict[str, float] = field(default_factory=dict)
    failures: dict[str, list[float]] = field(default_factory=dict)
    auth_enabled: Callable[[], bool] = lambda: False
    admin_key: Callable[[], str] = lambda: ""
    cookie_name: Callable[[], str] = lambda: "ppm_session"
    cookie_secure: Callable[[], bool] = lambda: False
    trust_proxy: Callable[[], bool] = lambda: False
    session_ttl_seconds: int = AUTH_SESSION_TTL_SECONDS

    def purge_expired_sessions(self, now: float | None = None) -> None:
        current = time.time() if now is None else now
        expired = [token for token, expires_at in self.sessions.items() if expires_at <= current]
        for token in expired:
            self.sessions.pop(token, None)

    def purge_stale_failures(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        for ip in list(self.failures):
            recent = [t for t in self.failures[ip] if current - t < AUTH_LOCK_WINDOW_SECONDS]
            if recent:
                self.failures[ip] = recent
            else:
                self.failures.pop(ip, None)

    def enforce_session_limit(self) -> None:
        overflow = len(self.sessions) - AUTH_MAX_SESSIONS + 1
        if overflow <= 0:
            return
        # Evict the sessions closest to expiry first.
        for token, _ in sorted(self.sessions.items(), key=lambda item: item[1])[:overflow]:
            self.sessions.pop(token, None)

    def is_authenticated(self, request: Request) -> bool:
        if not self.auth_enabled():
            return True
        token = request.cookies.get(self.cookie_name())
        if not token:
            return False
        self.purge_expired_sessions()
        expires_at = self.sessions.get(token)
        if expires_at is None:
            return False
        if expires_at <= time.time():
            self.sessions.pop(token, None)
            return False
        return True

    def payload(self, request: Request) -> dict:
        return {
            "enabled": self.auth_enabled(),
            "authenticated": self.is_authenticated(request),
        }


def create_auth_router(ctx: AuthContext) -> APIRouter:
    """Create and return the auth APIRouter with all auth endpoints."""
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.get("/status")
    async def auth_status(request: Request):
        return ctx.payload(request)

    @router.post("/login")
    async def auth_login(payload: LoginRequest, request: Request):
        if not ctx.auth_enabled():
            return {"enabled": False, "authenticated": True}
        ip = client_ip(request, trust_proxy=ctx.trust_proxy())
        now = time.monotonic()
        ctx.purge_stale_failures(now)
        failures = ctx.failures.get(ip, [])
        if len(failures) >= AUTH_MAX_FAILURES:
            retry_after = int(AUTH_LOCK_WINDOW_SECONDS - (now - failures[0]))
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts. Try again in {max(retry_after, 1)}s.",
            )
        expected = ctx.admin_key()
        if not hmac.compare_digest(payload.key or "", expected):
            failures.append(now)
            ctx.failures[ip] = failures
            remaining = AUTH_MAX_FAILURES - len(failures)
            raise HTTPException(
                status_code=401,
                detail=f"Invalid admin key. {remaining} attempt(s) remaining.",
            )
        ctx.failures.pop(ip, None)
        ctx.purge_expired_sessions()
        ctx.enforce_session_limit()
        token = secrets.token_urlsafe(32)
        ttl = max(60, int(ctx.session_ttl_seconds))
        ctx.sessions[token] = time.time() + ttl
        response = JSONResponse({"enabled": True, "authenticated": True})
        response.set_cookie(
            ctx.cookie_name(),
            token,
            max_age=ttl,
            httponly=True,
            samesite="lax",
            secure=ctx.cookie_secure(),
            path="/",
        )
        return response

    @router.post("/logout")
    async def auth_logout(request: Request):
        token = request.cookies.get(ctx.cookie_name())
        if token:
            ctx.sessions.pop(token, None)
        response = JSONResponse({"enabled": ctx.auth_enabled(), "authenticated": False})
        response.delete_cookie(ctx.cookie_name(), path="/")
        return response

    return router
