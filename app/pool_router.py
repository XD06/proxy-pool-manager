from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import signal
import socket
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
        self._orphan_pids: list[int] = []
        self._runtime_checked_at: float | None = None

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

    def _managed_processes(self) -> list[int]:
        if platform.system().lower() != "windows":
            try:
                completed = subprocess.run(
                    ["ps", "-eo", "pid=,args="],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
            except Exception:
                return []
            if completed.returncode != 0:
                return []
            pids: list[int] = []
            config = str(self.config_path)
            for line in completed.stdout.splitlines():
                parts = line.strip().split(maxsplit=1)
                if len(parts) != 2 or "pool-router" not in parts[1] or config not in parts[1]:
                    continue
                try:
                    pids.append(int(parts[0]))
                except ValueError:
                    continue
            return pids
        config = str(self.config_path).replace("'", "''")
        script = (
            f"$config = '{config}'; "
            "Get-CimInstance Win32_Process -Filter \"name = 'pool-router.exe'\" | "
            "Where-Object { $_.CommandLine -like \"*$config*\" } | "
            "Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
        )
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if completed.returncode != 0 or not completed.stdout.strip():
                return []
            data = json.loads(completed.stdout)
            values = data if isinstance(data, list) else [data]
            return [int(value) for value in values if value]
        except Exception:
            return []

    def _stop_managed_orphans(self, keep_pid: int | None = None) -> None:
        for pid in self._managed_processes():
            if pid == keep_pid:
                continue
            if platform.system().lower() != "windows":
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
                except Exception:
                    continue
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    except Exception:
                        break
                    time.sleep(0.05)
                else:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass
                continue
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception:
                continue

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
        await self._wait_for_listener_ports_available(path)
        flags = subprocess.CREATE_NO_WINDOW if platform.system().lower() == "windows" else 0
        # Avoid unread PIPE deadlocks; keep stderr on disk for startup diagnostics.
        stderr_log = path.with_name(f"{path.stem}.stderr.log")
        try:
            stderr_file = open(stderr_log, "wb")
        except OSError as exc:
            raise PoolRouterError(f"Could not open pool router log: {exc}") from exc
        try:
            process = subprocess.Popen(
                [str(binary), "-config", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                creationflags=flags,
            )
        except OSError as exc:
            raise PoolRouterError(f"Could not start pool router: {exc}") from exc
        finally:
            # Child keeps the fd; parent only needs to close its copy once.
            stderr_file.close()
        self.process = process
        self.started_at = time.monotonic()
        self.last_error = None
        for _ in range(20):
            await asyncio.sleep(0.1)
            if self.process is not process:
                raise PoolRouterError("Pool router start was interrupted")
            if process.poll() is not None:
                stderr_output = _tail_text(stderr_log)
                if self.process is process:
                    self.process = None
                    self.started_at = None
                self.last_error = (
                    stderr_output
                    if stderr_output
                    else "Pool router exited immediately; check the port configuration"
                )
                raise PoolRouterError(self.last_error)
            try:
                await self.status()
                return
            except PoolRouterError:
                continue
        if self.process is process:
            await self.stop()
        raise PoolRouterError("Pool router did not expose its control endpoint")

    async def _wait_for_listener_ports_available(
        self, config_path: Path, timeout_seconds: float = 3.0
    ) -> None:
        """Pre-check listener ports before launching the binary.

        The Go binary now skips occupied ports and continues with the rest,
        so this pre-check only logs a warning instead of blocking startup.
        It still waits briefly in case a previous process is releasing ports.
        """
        listeners = _parse_router_listeners(config_path)
        if not listeners:
            return
        deadline = time.monotonic() + timeout_seconds
        while True:
            unavailable = _unavailable_router_listeners(listeners)
            if not unavailable:
                return
            if time.monotonic() >= deadline:
                details = "; ".join(
                    f"{item['id']} on {item['listen']}" for item in unavailable
                )
                import logging
                logging.getLogger("pool_router").warning(
                    "Pool router listener ports unavailable (will be skipped): %s", details
                )
                return
            await asyncio.sleep(0.2)

    async def stop(self) -> None:
        process = self.process
        self.process = None
        self.started_at = None
        if not process or process.poll() is not None:
            await asyncio.to_thread(self._stop_managed_orphans)
            return
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 3)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait, 3)
        await asyncio.to_thread(self._stop_managed_orphans, keep_pid=process.pid)

    async def status(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                response = await client.get(f"{self.control_url()}/status")
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PoolRouterError(f"Pool router status unavailable: {exc}") from exc

    async def refresh_runtime_payload(self) -> dict[str, Any]:
        """Refresh orphan-process state away from request handling paths."""
        running = bool(self.process and self.process.poll() is None)
        if running:
            self._orphan_pids = []
        else:
            self._orphan_pids = await asyncio.to_thread(self._managed_processes)
        self._runtime_checked_at = time.time()
        return self.payload()

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
        pid = self.process.pid if running and self.process else None
        if not running:
            running = bool(self._orphan_pids)
            pid = self._orphan_pids[0] if self._orphan_pids else None
        return {
            "available": self.available(),
            "running": running,
            "pid": pid,
            "last_error": self.last_error,
            "runtime_checked_at": self._runtime_checked_at,
        }


def _tail_text(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if not data:
        return ""
    return data[-limit:].decode(errors="replace").strip()


def _parse_router_listeners(config_path: Path) -> list[dict[str, str]]:
    """Extract (id, listen) pairs from a pool-router JSON config."""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    result: list[dict[str, str]] = []
    for item in config.get("listeners", []):
        if not isinstance(item, dict):
            continue
        listener_id = str(item.get("id") or "")
        listen = str(item.get("listen") or "")
        if listener_id and listen:
            result.append({"id": listener_id, "listen": listen})
    return result


def _unavailable_router_listeners(
    listeners: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return listeners whose TCP port cannot be bound right now."""
    unavailable: list[dict[str, str]] = []
    for item in listeners:
        host, port = _parse_listen_addr(item["listen"])
        if port is None:
            continue
        if not _can_bind_tcp_port(host, port):
            unavailable.append(item)
    return unavailable


def _parse_listen_addr(addr: str) -> tuple[str, int | None]:
    """Parse a host:port listen address, returning (host, port)."""
    match = re.match(r"^(.+):([0-9]+)$", addr)
    if not match:
        return addr, None
    return match.group(1), int(match.group(2))


def _can_bind_tcp_port(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True
