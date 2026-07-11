"""Auth route module — handles login, logout, and auth status."""

from __future__ import annotations

import hashlib
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


@dataclass
class AuthContext:
    """Holds auth-related mutable state and config accessors."""

    sessions: set[str] = field(default_factory=set)
    failures: dict[str, list[float]] = field(default_factory=dict)
    auth_enabled: Callable[[], bool] = lambda: False
    admin_key: Callable[[], str] = lambda: ""
    cookie_name: Callable[[], str] = lambda: "ppm_session"
    cookie_secure: Callable[[], bool] = lambda: False

    def is_authenticated(self, request: Request) -> bool:
        if not self.auth_enabled():
            return True
        token = request.cookies.get(self.cookie_name())
        return bool(token and token in self.sessions)

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
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        failures = ctx.failures.get(client_ip, [])
        failures = [t for t in failures if now - t < AUTH_LOCK_WINDOW_SECONDS]
        if len(failures) >= AUTH_MAX_FAILURES:
            retry_after = int(AUTH_LOCK_WINDOW_SECONDS - (now - failures[0]))
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts. Try again in {max(retry_after, 1)}s.",
            )
        expected = ctx.admin_key()
        if not hmac.compare_digest(payload.key or "", expected):
            failures.append(now)
            ctx.failures[client_ip] = failures
            remaining = AUTH_MAX_FAILURES - len(failures)
            raise HTTPException(
                status_code=401,
                detail=f"Invalid admin key. {remaining} attempt(s) remaining.",
            )
        ctx.failures.pop(client_ip, None)
        token = secrets.token_urlsafe(32)
        ctx.sessions.add(token)
        response = JSONResponse({"enabled": True, "authenticated": True})
        response.set_cookie(
            ctx.cookie_name(),
            token,
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
            ctx.sessions.discard(token)
        response = JSONResponse({"enabled": ctx.auth_enabled(), "authenticated": False})
        response.delete_cookie(ctx.cookie_name(), path="/")
        return response

    return router
