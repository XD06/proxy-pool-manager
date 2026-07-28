"""Module-level utility functions extracted from api.py.

These functions don't depend on create_app() closures and can live at module level.
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .settings import ROOT_DIR
from .tester import PRIMARY_TEST_URL

__all__ = [
    "PRIMARY_TEST_URL",
    "default_port_validation_urls",
    "_extract_ip_from_targets",
    "_first_target_error",
    "_listening_local_ports",
    "_is_local_port_listening",
    "_proc_tcp_port_listening",
    "_write_json_if_changed",
    "_doctor_command",
    "_parse_doctor_output",
    "run_doctor_script",
]


def default_port_validation_urls(urls: list[str] | None) -> list[str]:
    selected = [url.strip() for url in (urls or []) if str(url).strip()]
    return selected or [PRIMARY_TEST_URL]


def _extract_ip_from_targets(targets: list[dict]) -> str | None:
    for item in targets:
        lines = (item.get("body_preview") or "").strip().splitlines()
        if not lines:
            continue
        candidate = lines[0].strip()
        if re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", candidate):
            return candidate
    return None


def _first_target_error(targets: list[dict]) -> str | None:
    for item in targets:
        if item.get("error"):
            return item["error"]
    return "all validation targets failed"


def _listening_local_ports(ports: list[int]) -> list[int]:
    if not ports:
        return []
    with ThreadPoolExecutor(max_workers=min(32, len(ports))) as executor:
        results = executor.map(_is_local_port_listening, ports)
    return sorted(port for port, is_listening in zip(ports, results) if is_listening)


def _is_local_port_listening(port: int) -> bool:
    if platform.system().lower() != "windows" and _proc_tcp_port_listening(port):
        return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _proc_tcp_port_listening(port: int) -> bool:
    port_hex = f"{int(port):04X}"
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            columns = line.split()
            if len(columns) < 4 or columns[3] != "0A":
                continue
            local_address = columns[1]
            if local_address.rsplit(":", 1)[-1].upper() == port_hex:
                return True
    return False


def _write_json_if_changed(path: Path, data: dict) -> bool:
    serialized = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        if path.exists() and path.read_text(encoding="utf-8") == serialized:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace so a crash mid-write cannot leave a truncated config that
    # sing-box / pool-router would then refuse to start with.
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(serialized, encoding="utf-8")
    os.replace(tmp_path, path)
    return True


def _doctor_command() -> list[str]:
    if platform.system().lower() == "windows":
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT_DIR / "scripts" / "doctor.ps1"),
        ]
    return ["bash", str(ROOT_DIR / "scripts" / "doctor.sh")]


def _parse_doctor_output(output: str) -> dict:
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    ok = sum(1 for line in lines if line.startswith("[OK]"))
    warn = sum(1 for line in lines if line.startswith("[WARN]"))
    fail = sum(1 for line in lines if line.startswith("[FAIL]"))
    summary = ""
    for line in reversed(lines):
        if line.startswith("=== Summary:"):
            summary = line.strip("= ").strip()
            break
    return {
        "ok": ok,
        "warn": warn,
        "fail": fail,
        "summary": summary or f"ok={ok} warn={warn} fail={fail}",
        "lines": lines,
    }


def run_doctor_script(timeout: int = 30, *, command_fn=None) -> dict:
    timeout = max(5, min(int(timeout or 30), 120))
    command = (command_fn or _doctor_command)()
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
        parsed = _parse_doctor_output(output)
        return {
            **parsed,
            "exit_code": completed.returncode,
            "timed_out": False,
            "command": " ".join(command),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        parsed = _parse_doctor_output("\n".join(part for part in [stdout, stderr] if part).strip())
        return {
            **parsed,
            "exit_code": None,
            "timed_out": True,
            "command": " ".join(command),
        }
