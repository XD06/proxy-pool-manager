from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import signal
import shutil
import socket
import subprocess
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from .models import EngineStatus
from .settings import BIN_DIR, SING_BOX_CONFIG_PATH


class EngineError(RuntimeError):
    pass


class EngineManager:
    def __init__(self, managed_config_path: Path | None = None) -> None:
        self.process: subprocess.Popen | None = None
        self.managed_config_path = managed_config_path
        self.started_at: float | None = None
        self.last_error: str | None = None
        self.fatal = False
        self._lock = asyncio.Lock()

    def _binary_name(self) -> str:
        return "sing-box.exe" if platform.system().lower() == "windows" else "sing-box"

    def binary_path(self) -> Path:
        return BIN_DIR / self._binary_name()

    def _managed_processes(self, config_path: Path | None = None) -> list[dict[str, Any]]:
        target_config = config_path or self.managed_config_path
        if not target_config:
            return []
        config = str(target_config)
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
            if completed.returncode != 0 or not completed.stdout.strip():
                return []
            managed: list[dict[str, Any]] = []
            for line in completed.stdout.splitlines():
                stripped = line.strip()
                if not stripped or config not in stripped or "sing-box" not in stripped:
                    continue
                match = re.match(r"^(\d+)\s+(.*)$", stripped)
                if not match:
                    continue
                managed.append(
                    {
                        "ProcessId": int(match.group(1)),
                        "ExecutablePath": None,
                        "CommandLine": match.group(2),
                    }
                )
            return managed

        script = (
            "$config = " + _ps_quote(config) + "; "
            "Get-CimInstance Win32_Process -Filter \"name = 'sing-box.exe'\" | "
            "Where-Object { $_.CommandLine -like \"*$config*\" } | "
            "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            return []
        if completed.returncode != 0 or not completed.stdout.strip():
            return []
        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    def _kill_managed_orphans(self, keep_pid: int | None = None, config_path: Path | None = None) -> None:
        for item in self._managed_processes(config_path):
            pid = item.get("ProcessId")
            if not pid or pid == keep_pid:
                continue
            if platform.system().lower() == "windows":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except Exception:
                continue
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                except Exception:
                    break
                time.sleep(0.1)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass

    async def ensure_binary(self) -> Path:
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        configured = os.environ.get("SING_BOX_PATH")
        if configured:
            path = Path(configured)
            if path.exists():
                return path
            raise EngineError(f"SING_BOX_PATH does not exist: {path}")

        binary = self.binary_path()
        if binary.exists():
            return binary
        system_binary = shutil.which("sing-box")
        if system_binary:
            return Path(system_binary)
        system = platform.system().lower()
        machine = platform.machine().lower()
        if system not in {"windows", "linux"} or machine not in {"amd64", "x86_64"}:
            raise EngineError(f"Unsupported platform for auto-download: {system}/{machine}")
        await self._download_latest(binary, system)
        return binary

    async def _download_latest(self, target: Path, system: str) -> None:
        api_url = "https://api.github.com/repos/SagerNet/sing-box/releases/latest"
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                release = (await client.get(api_url)).raise_for_status()
                data = release.json()
                wanted = "windows-amd64" if system == "windows" else "linux-amd64"
                asset = None
                for item in data.get("assets", []):
                    name = item.get("name", "")
                    if wanted in name and (name.endswith(".zip") or name.endswith(".tar.gz")):
                        asset = item
                        break
                if not asset:
                    raise EngineError(f"No sing-box release asset found for {wanted}")
                archive_path = BIN_DIR / asset["name"]
                response = await client.get(asset["browser_download_url"])
                response.raise_for_status()
                archive_path.write_bytes(response.content)
        except httpx.HTTPStatusError as exc:
            raise EngineError(
                "Could not download sing-box from GitHub. "
                f"GitHub returned {exc.response.status_code}. "
                "Set SING_BOX_PATH or place sing-box.exe in bin/."
            ) from exc
        except httpx.HTTPError as exc:
            raise EngineError(
                "Could not download sing-box from GitHub. "
                "Set SING_BOX_PATH or place sing-box.exe in bin/. "
                f"Network error: {exc}"
            ) from exc
        extracted = self._extract_binary(archive_path, system)
        shutil.copy2(extracted, target)
        if system != "windows":
            target.chmod(0o755)

    def _extract_binary(self, archive_path: Path, system: str) -> Path:
        expected = "sing-box.exe" if system == "windows" else "sing-box"
        extract_dir = archive_path.with_suffix("")
        extract_dir.mkdir(parents=True, exist_ok=True)
        if archive_path.name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(extract_dir)
        elif archive_path.name.endswith(".tar.gz"):
            with tarfile.open(archive_path) as archive:
                archive.extractall(extract_dir)
        else:
            raise EngineError(f"Unsupported archive: {archive_path.name}")
        for path in extract_dir.rglob(expected):
            return path
        raise EngineError("Downloaded archive did not contain sing-box binary")

    async def check_config(self, config_path: Path) -> None:
        binary = await self.ensure_binary()
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "check",
            "-c",
            str(config_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            output = (stderr or stdout).decode(errors="replace")[-4000:]
            raise EngineError(f"sing-box check failed: {output}")

    async def start(
        self,
        config_path: Path,
        *,
        check: bool = True,
        settle_seconds: float = 1.0,
        port_wait_timeout: float = 6.0,
    ) -> None:
        async with self._lock:
            if check:
                await self.check_config(config_path)
            await self._stop_unlocked(config_path=config_path)
            await self._wait_for_config_ports_available(config_path, timeout_seconds=port_wait_timeout)
            self.managed_config_path = config_path
            binary = await self.ensure_binary()
            proc = subprocess.Popen(
                [str(binary), "run", "-c", str(config_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(config_path.parent.parent),
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system().lower() == "windows" else 0,
            )
            self.process = proc
            self.started_at = time.time()
            await asyncio.sleep(settle_seconds)
            if proc.poll() is not None:
                err = ""
                if proc.stderr:
                    err = proc.stderr.read().decode(errors="replace")[-4000:]
                self.last_error = err or "sing-box exited immediately"
                if self.process is proc:
                    self.process = None
                raise EngineError(self.last_error)
            if self.process is proc:
                self.fatal = False
                self.last_error = None

    async def stop(self, config_path: Path | None = None) -> None:
        async with self._lock:
            await self._stop_unlocked(config_path=config_path)

    async def _stop_unlocked(self, config_path: Path | None = None) -> None:
        if not self.process:
            self._kill_managed_orphans(config_path=config_path)
            return
        proc = self.process
        self.process = None
        if proc.poll() is not None:
            self._kill_managed_orphans(config_path=config_path)
            return
        proc.terminate()
        try:
            await asyncio.to_thread(proc.wait, 1)
        except subprocess.TimeoutExpired:
            proc.kill()
            await asyncio.to_thread(proc.wait, 1)
        self._kill_managed_orphans(config_path=config_path)

    async def _wait_for_config_ports_available(self, config_path: Path, timeout_seconds: float = 6.0) -> None:
        ports = _config_listen_ports(config_path)
        if not ports:
            return
        deadline = time.monotonic() + timeout_seconds
        unavailable = _unavailable_ports(ports)
        while unavailable and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            unavailable = _unavailable_ports(ports)
        if unavailable:
            joined = ", ".join(str(port) for port in unavailable)
            raise EngineError(
                f"Ports are not available: {joined}. "
                "Stop the process using them, wait a few seconds, or change the port mapping."
            )

    def status(self) -> EngineStatus:
        running = self.process is not None and self.process.poll() is None
        if not running:
            managed = self._managed_processes()
            if managed:
                return EngineStatus(
                    running=True,
                    pid=managed[0].get("ProcessId"),
                    uptime_seconds=None,
                    fatal=self.fatal,
                    last_error=None,
                )
        return EngineStatus(
            running=running,
            pid=self.process.pid if running and self.process else None,
            uptime_seconds=int(time.time() - self.started_at) if running and self.started_at else None,
            fatal=self.fatal,
            last_error=self.last_error,
        )

    async def monitor(self, config_path: Path) -> None:
        failures = 0
        while True:
            await asyncio.sleep(5)
            if not self.process:
                continue
            if self.process.poll() is None:
                continue
            failures += 1
            self.last_error = "sing-box process exited"
            if failures >= 3:
                self.fatal = True
                self.process = None
                continue
            try:
                await self.start(config_path)
                failures = 0
            except Exception as exc:
                self.last_error = str(exc)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _config_listen_ports(config_path: Path) -> list[int]:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    ports: set[int] = set()
    for inbound in config.get("inbounds", []):
        if isinstance(inbound, dict) and isinstance(inbound.get("listen_port"), int):
            ports.add(inbound["listen_port"])
    clash_api = config.get("experimental", {}).get("clash_api", {})
    if isinstance(clash_api, dict):
        controller = str(clash_api.get("external_controller") or "")
        match = re.search(r":(\d+)$", controller)
        if match:
            ports.add(int(match.group(1)))
    return sorted(ports)


def _unavailable_ports(ports: list[int]) -> list[int]:
    return [port for port in ports if not _can_bind_tcp_port(port)]


def _can_bind_tcp_port(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
        return True
