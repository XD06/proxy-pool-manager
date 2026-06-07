from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROXYCHECK_DIR = PROJECT_ROOT / "proxycheck-api"


def proxycheck_binary_path() -> Path:
    override = os.environ.get("PROXYCHECK_BIN", "").strip()
    if override:
        path = Path(override)
        if path.exists():
            return path
        raise FileNotFoundError(f"PROXYCHECK_BIN does not exist: {path}")

    if sys.platform.startswith("win"):
        candidates = [PROXYCHECK_DIR / "proxycheck.exe", PROXYCHECK_DIR / "proxycheck"]
    else:
        candidates = [PROXYCHECK_DIR / "proxycheck"]

    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(
        "proxycheck binary not found. Build it with `go build -o proxycheck ./cmd/proxycheck` "
        "inside proxycheck-api, or set PROXYCHECK_BIN."
    )


def normalize_proxycheck_result(raw: dict, proxy_id: int) -> dict:
    return {
        "id": int(raw.get("proxy_id") or proxy_id),
        "proxy_url": raw.get("proxy_url") or "",
        "exit_ip": raw.get("exit_ip") or "",
        "country": raw.get("country") or raw.get("country_code") or "",
        "country_code": raw.get("country_code") or "",
        "score": int(raw.get("score") or 0),
        "grade": raw.get("grade") or "",
        "summary": raw.get("summary") or "",
        "base_latency_ms": raw.get("base_latency_ms"),
        "passed_count": int(raw.get("passed_count") or 0),
        "warn_count": int(raw.get("warn_count") or 0),
        "failed_count": int(raw.get("failed_count") or 0),
        "challenge_count": int(raw.get("challenge_count") or 0),
        "items": [
            {
                "target": item.get("target") or "",
                "status": item.get("status") or "",
                "http_status": item.get("http_status"),
                "latency_ms": item.get("latency_ms"),
                "message": item.get("message") or "",
                "cf_ray": item.get("cf_ray") or "",
            }
            for item in raw.get("items") or []
            if isinstance(item, dict)
        ],
    }


async def check_proxy_quality(proxy_url: str, proxy_id: int, timeout_seconds: int = 30) -> dict:
    binary = proxycheck_binary_path()
    timeout_seconds = max(5, min(int(timeout_seconds or 30), 120))
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "-proxy",
        proxy_url,
        "-json",
        "-timeout",
        str(timeout_seconds),
        cwd=str(PROXYCHECK_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds + 5)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise TimeoutError(f"proxycheck timed out after {timeout_seconds}s")

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        raise RuntimeError(stderr_text or stdout_text or f"proxycheck exited with {process.returncode}")
    try:
        raw = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        message = stderr_text or stdout_text[:500]
        raise RuntimeError(f"proxycheck returned invalid JSON: {message}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("proxycheck returned non-object JSON")
    return normalize_proxycheck_result(raw, proxy_id)
