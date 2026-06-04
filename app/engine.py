from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
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

    def _binary_name(self) -> str:
        return "sing-box.exe" if platform.system().lower() == "windows" else "sing-box"

    def binary_path(self) -> Path:
        return BIN_DIR / self._binary_name()

    def _managed_processes(self, config_path: Path | None = None) -> list[dict[str, Any]]:
        if platform.system().lower() != "windows":
            return []
        target_config = config_path or self.managed_config_path
        if not target_config:
            return []
        config = str(target_config)
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
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system().lower() == "windows" else 0,
            )

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

    async def start(self, config_path: Path, *, check: bool = True, settle_seconds: float = 1.0) -> None:
        if check:
            await self.check_config(config_path)
        await self.stop(config_path=config_path)
        self.managed_config_path = config_path
        binary = await self.ensure_binary()
        self.process = subprocess.Popen(
            [str(binary), "run", "-c", str(config_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(config_path.parent.parent),
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system().lower() == "windows" else 0,
        )
        self.started_at = time.time()
        await asyncio.sleep(settle_seconds)
        if self.process.poll() is not None:
            err = ""
            if self.process.stderr:
                err = self.process.stderr.read().decode(errors="replace")[-4000:]
            self.last_error = err or "sing-box exited immediately"
            self.process = None
            raise EngineError(self.last_error)
        self.fatal = False
        self.last_error = None

    async def stop(self, config_path: Path | None = None) -> None:
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
                    last_error="project sing-box process detected",
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
