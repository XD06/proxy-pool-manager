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
import tempfile
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
        self._update_lock = asyncio.Lock()
        self._monitor_failures = 0
        self._orphan_processes: list[dict[str, Any]] = []
        self.runtime_checked_at: float | None = None

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

    def _platform_spec(self) -> tuple[str, str, str]:
        system = {
            "windows": "windows",
            "linux": "linux",
            "darwin": "darwin",
        }.get(platform.system().lower())
        machine = {
            "amd64": "amd64",
            "x86_64": "amd64",
            "arm64": "arm64",
            "aarch64": "arm64",
            "x86": "386",
            "i386": "386",
            "i686": "386",
            "armv7l": "armv7",
        }.get(platform.machine().lower())
        if not system or not machine:
            raise EngineError(
                f"Unsupported platform for sing-box download: "
                f"{platform.system().lower()}/{platform.machine().lower()}"
            )
        return system, machine, ".zip" if system == "windows" else ".tar.gz"

    def backup_path(self) -> Path:
        binary = self.binary_path()
        return binary.with_name(f"{binary.stem}.backup{binary.suffix}")

    def _binary_version(self, binary: Path) -> str:
        try:
            completed = subprocess.run(
                [str(binary), "version"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system().lower() == "windows" else 0,
            )
        except Exception as exc:
            raise EngineError(f"Could not run sing-box binary: {exc}") from exc
        output = f"{completed.stdout}\n{completed.stderr}"
        match = re.search(r"sing-box version ([^\s]+)", output)
        if completed.returncode != 0 or not match:
            raise EngineError(f"Could not detect sing-box version: {output.strip()[-1000:]}")
        return match.group(1).lstrip("v")

    @staticmethod
    def _version_key(version: str | None) -> tuple[int, ...]:
        return tuple(int(item) for item in re.findall(r"\d+", version or ""))

    async def _latest_release(self) -> dict[str, Any]:
        api_url = "https://api.github.com/repos/SagerNet/sing-box/releases/latest"
        last_error: httpx.HTTPError | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    response = await client.get(api_url, headers={"User-Agent": "ProxyPoolManager"})
                    response.raise_for_status()
                    return response.json()
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(attempt + 1)
        assert last_error is not None
        raise EngineError(
            f"Could not check sing-box releases: {type(last_error).__name__}: {last_error}"
        ) from last_error

    def _release_asset(self, release: dict[str, Any]) -> tuple[dict[str, Any], str]:
        system, machine, extension = self._platform_spec()
        version = str(release.get("tag_name") or "").lstrip("v")
        expected = f"sing-box-{version}-{system}-{machine}{extension}"
        for asset in release.get("assets", []):
            if asset.get("name") == expected:
                return asset, version
        raise EngineError(f"No sing-box release asset found for {system}/{machine}: {expected}")

    async def _download_release_binary(self, release: dict[str, Any], destination: Path) -> str:
        asset, version = self._release_asset(release)
        system, _, _ = self._platform_spec()
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="sing-box-update-", dir=BIN_DIR) as temp_dir:
                archive_path = Path(temp_dir) / str(asset["name"])
                last_error: httpx.HTTPError | None = None
                for attempt in range(3):
                    try:
                        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                            response = await client.get(asset["browser_download_url"])
                            response.raise_for_status()
                            archive_path.write_bytes(response.content)
                        break
                    except httpx.HTTPError as exc:
                        last_error = exc
                        if attempt < 2:
                            await asyncio.sleep(attempt + 1)
                else:
                    assert last_error is not None
                    raise last_error
                extracted = self._extract_binary(archive_path, system)
                shutil.copy2(extracted, destination)
        except httpx.HTTPError as exc:
            raise EngineError(
                f"Could not download sing-box {version}: {type(exc).__name__}: {exc}"
            ) from exc
        if system != "windows":
            destination.chmod(0o755)
        detected = await asyncio.to_thread(self._binary_version, destination)
        if detected != version:
            destination.unlink(missing_ok=True)
            raise EngineError(f"Downloaded sing-box version mismatch: expected {version}, got {detected}")
        return version

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
        await self._download_latest(binary)
        return binary

    async def _download_latest(self, target: Path, system: str | None = None) -> None:
        release = await self._latest_release()
        await self._download_release_binary(release, target)

    def _extract_binary(self, archive_path: Path, system: str) -> Path:
        expected = "sing-box.exe" if system == "windows" else "sing-box"
        extract_dir = archive_path.parent / "extracted"
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

    async def _check_config_with_binary(self, binary: Path, config_path: Path) -> None:
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
            hint = _failing_outbound_hint(config_path, output)
            raise EngineError(f"sing-box check failed{hint}: {output}")

    async def binary_info(self, *, check_latest: bool = False) -> dict[str, Any]:
        configured = os.environ.get("SING_BOX_PATH")
        managed_path = self.binary_path()
        if configured:
            path = Path(configured)
            managed = False
        elif managed_path.exists():
            path = managed_path
            managed = True
        else:
            system_binary = shutil.which("sing-box")
            path = Path(system_binary) if system_binary else managed_path
            managed = False
        current_version = await asyncio.to_thread(self._binary_version, path) if path.exists() else None
        backup = self.backup_path()
        backup_version = None
        if backup.exists():
            try:
                backup_version = await asyncio.to_thread(self._binary_version, backup)
            except EngineError:
                backup_version = None
        latest_version = None
        if check_latest:
            _, latest_version = self._release_asset(await self._latest_release())
        return {
            "path": str(path),
            "managed": managed,
            "platform": platform.system().lower(),
            "architecture": platform.machine().lower(),
            "current_version": current_version,
            "latest_version": latest_version,
            "update_available": bool(
                latest_version
                and (not current_version or self._version_key(latest_version) > self._version_key(current_version))
            ),
            "rollback_available": managed and backup_version is not None,
            "backup_version": backup_version,
        }

    async def update_binary(self) -> dict[str, Any]:
        async with self._update_lock:
            if os.environ.get("SING_BOX_PATH"):
                raise EngineError("Cannot update a binary configured through SING_BOX_PATH")
            target = self.binary_path()
            if not target.exists():
                await self.ensure_binary()
            if not target.exists():
                raise EngineError("Automatic update requires a managed binary in bin/")
            current_version = await asyncio.to_thread(self._binary_version, target)
            release = await self._latest_release()
            _, latest_version = self._release_asset(release)
            if self._version_key(latest_version) <= self._version_key(current_version):
                info = await self.binary_info()
                return {**info, "updated": False, "previous_version": current_version, "restarted": False}

            candidate = target.with_name(f"{target.stem}.download{target.suffix}")
            candidate.unlink(missing_ok=True)
            config_path = self.managed_config_path or SING_BOX_CONFIG_PATH
            try:
                await self._download_release_binary(release, candidate)
                if config_path.exists():
                    await self._check_config_with_binary(candidate, config_path)
            except Exception:
                candidate.unlink(missing_ok=True)
                raise
            was_running = self.status().running
            if was_running:
                await self.stop(config_path=config_path)
            backup = self.backup_path()
            shutil.copy2(target, backup)
            replaced = False
            try:
                os.replace(candidate, target)
                replaced = True
                if platform.system().lower() != "windows":
                    target.chmod(0o755)
                if was_running:
                    await self.start(config_path)
            except Exception as exc:
                if replaced:
                    shutil.copy2(backup, target)
                    if platform.system().lower() != "windows":
                        target.chmod(0o755)
                if was_running:
                    try:
                        await self.start(config_path)
                    except Exception:
                        pass
                raise EngineError(f"sing-box update failed and was rolled back: {exc}") from exc
            finally:
                candidate.unlink(missing_ok=True)
            info = await self.binary_info()
            return {**info, "updated": True, "previous_version": current_version, "restarted": was_running}

    async def rollback_binary(self) -> dict[str, Any]:
        async with self._update_lock:
            if os.environ.get("SING_BOX_PATH"):
                raise EngineError("Cannot roll back a binary configured through SING_BOX_PATH")
            target = self.binary_path()
            backup = self.backup_path()
            if not target.exists() or not backup.exists():
                raise EngineError("No sing-box backup is available")
            config_path = self.managed_config_path or SING_BOX_CONFIG_PATH
            if config_path.exists():
                await self._check_config_with_binary(backup, config_path)
            previous_version = await asyncio.to_thread(self._binary_version, target)
            rollback_version = await asyncio.to_thread(self._binary_version, backup)
            was_running = self.status().running
            if was_running:
                await self.stop(config_path=config_path)
            swap = target.with_name(f"{target.stem}.swap{target.suffix}")
            swap.unlink(missing_ok=True)
            swapped = False
            try:
                os.replace(target, swap)
                os.replace(backup, target)
                os.replace(swap, backup)
                swapped = True
                if platform.system().lower() != "windows":
                    target.chmod(0o755)
                    backup.chmod(0o755)
                if was_running:
                    await self.start(config_path)
            except Exception as exc:
                if swapped:
                    os.replace(target, swap)
                    os.replace(backup, target)
                    os.replace(swap, backup)
                elif swap.exists():
                    if target.exists():
                        os.replace(target, backup)
                    os.replace(swap, target)
                if was_running:
                    try:
                        await self.start(config_path)
                    except Exception:
                        pass
                raise EngineError(f"sing-box rollback failed: {exc}") from exc
            info = await self.binary_info()
            return {
                **info,
                "rolled_back": True,
                "previous_version": previous_version,
                "current_version": rollback_version,
                "restarted": was_running,
            }

    async def check_config(self, config_path: Path) -> None:
        binary = await self.ensure_binary()
        await self._check_config_with_binary(binary, config_path)

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
            # Keep the child off pipes: an unread PIPE fills up (~64KB) and
            # blocks sing-box mid-run. stderr goes to a file for diagnostics.
            stderr_log = _stderr_log_path(config_path)
            with open(stderr_log, "wb") as stderr_file:
                proc = subprocess.Popen(
                    [str(binary), "run", "-c", str(config_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    cwd=str(config_path.parent.parent),
                    creationflags=subprocess.CREATE_NO_WINDOW if platform.system().lower() == "windows" else 0,
                )
            self.process = proc
            self.started_at = time.time()
            await asyncio.sleep(settle_seconds)
            if proc.poll() is not None:
                err = _tail_text(stderr_log)
                self.last_error = err or "sing-box exited immediately"
                if self.process is proc:
                    self.process = None
                raise EngineError(self.last_error)
            if self.process is proc:
                self.fatal = False
                self.last_error = None
                self._monitor_failures = 0

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
            if self._orphan_processes:
                return EngineStatus(
                    running=True,
                    pid=self._orphan_processes[0].get("ProcessId"),
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

    async def refresh_runtime_status(self, config_path: Path | None = None) -> EngineStatus:
        """Refresh orphan-process state away from API request paths."""
        running = self.process is not None and self.process.poll() is None
        if running:
            self._orphan_processes = []
        else:
            self._orphan_processes = await asyncio.to_thread(self._managed_processes, config_path)
        self.runtime_checked_at = time.time()
        return self.status()

    async def monitor(self, config_path: Path) -> None:
        while True:
            await asyncio.sleep(5)
            if not self.process:
                continue
            if self.process.poll() is None:
                if not self.fatal and self._monitor_failures > 0:
                    self._monitor_failures = 0
                continue
            self._monitor_failures += 1
            self.last_error = "sing-box process exited"
            if self._monitor_failures >= 3:
                self.fatal = True
                self.process = None
                continue
            try:
                await self.start(config_path)
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
    """Return True when the port can be bound for both wildcard and loopback.

    Checking only 0.0.0.0 misses exclusive 127.0.0.1 listeners on some stacks,
    and checking only loopback misses foreign wildcard binds. Require both.

    Do not enable SO_REUSEADDR here: on Windows it can make an occupied port
    look free, which defeats allocation skip-busy behaviour.
    """
    for host in ("0.0.0.0", "127.0.0.1"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                return False
    return True


def _stderr_log_path(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.stem}.stderr.log")


def _tail_text(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if not data:
        return ""
    return data[-limit:].decode(errors="replace").strip()


def _failing_outbound_hint(config_path: Path, output: str) -> str:
    """Best-effort hint naming the outbound that made sing-box reject the config."""
    match = re.search(r"outbounds\[(\d+)\]", output)
    if not match:
        return ""
    index = int(match.group(1))
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        outbound = (config.get("outbounds") or [])[index]
    except Exception:
        return f" (outbounds[{index}])"
    if not isinstance(outbound, dict):
        return f" (outbounds[{index}])"
    tag = outbound.get("tag")
    server = outbound.get("server")
    if tag and server:
        return f" (outbound {tag} @ {server})"
    if tag:
        return f" (outbound {tag})"
    return f" (outbounds[{index}])"
