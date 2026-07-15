from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from .settings import ROOT_DIR, current_pool_router_control_addr


class PoolRouterError(RuntimeError):
    pass


class PoolRouterManager:
    """Lifecycle and local-control client for the Go edge router."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.process: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.last_error: str | None = None

    def binary_path(self) -> Path:
        configured = os.environ.get("PPM_POOL_ROUTER_PATH")
        if configured:
            return Path(configured)
        suffix = ".exe" if platform.system().lower() == "windows" else ""
        return ROOT_DIR / "proxycheck-api" / f"pool-router{suffix}"

    def available(self) -> bool:
        return self.binary_path().exists()

    def control_url(self) -> str:
        return f"http://{current_pool_router_control_addr()}"

    async def start(self, config_path: Path | None = None) -> None:
        path = config_path or self.config_path
        if not path.exists():
            raise PoolRouterError(f"Pool router config does not exist: {path}")
        binary = self.binary_path()
        if not binary.exists():
            raise PoolRouterError(
                f"Pool router binary not found: {binary}. Build it with: cd proxycheck-api && go build -o {binary.name} ./cmd/pool-router"
            )
        await self.stop()
        flags = subprocess.CREATE_NO_WINDOW if platform.system().lower() == "windows" else 0
        try:
            self.process = subprocess.Popen(
                [str(binary), "-config", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except OSError as exc:
            raise PoolRouterError(f"Could not start pool router: {exc}") from exc
        self.started_at = time.monotonic()
        self.last_error = None
        for _ in range(20):
            await asyncio.sleep(0.1)
            if self.process.poll() is not None:
                self.last_error = "Pool router exited immediately; check the port configuration"
                raise PoolRouterError(self.last_error)
            try:
                await self.status()
                return
            except PoolRouterError:
                continue
        await self.stop()
        raise PoolRouterError("Pool router did not expose its control endpoint")

    async def stop(self) -> None:
        process = self.process
        self.process = None
        self.started_at = None
        if not process or process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 3)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait, 3)

    async def status(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                response = await client.get(f"{self.control_url()}/status")
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PoolRouterError(f"Pool router status unavailable: {exc}") from exc

    async def advance(self, listener_id: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.post(f"{self.control_url()}/listeners/{listener_id}/advance")
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PoolRouterError(f"Could not advance pool: {exc}") from exc

    def payload(self) -> dict[str, Any]:
        running = bool(self.process and self.process.poll() is None)
        return {
            "available": self.available(),
            "running": running,
            "pid": self.process.pid if running and self.process else None,
            "last_error": self.last_error,
        }
