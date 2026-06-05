from __future__ import annotations

import asyncio
import json
import socket
import time
from datetime import datetime, timezone

import httpx

from .engine import EngineManager
from .generator import generate_config
from .models import AppState, ExitIpCache, LatencyResult, PortMapping, ProxyNode
from .settings import SING_BOX_TEST_CONFIG_PATH, TEST_START_PORT


EXIT_IP_URLS = [
    "https://ipv4.webshare.io/",
    "https://api.ipify.org?format=text",
    "https://ipv4.icanhazip.com/",
    "https://ifconfig.me/ip",
    "https://checkip.amazonaws.com/",
    "https://ident.me/",
]

DEFAULT_VALIDATION_URLS = [
    "https://ipv4.webshare.io/",
    "https://www.google.com/generate_204",
    "https://www.gstatic.com/generate_204",
    "https://www.cloudflare.com/cdn-cgi/trace",
]


def _fresh(cache: ExitIpCache, ttl_seconds: int = 60) -> bool:
    try:
        checked = datetime.fromisoformat(cache.checked_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - checked).total_seconds() < ttl_seconds


async def query_exit_ip(port: int, state: AppState, engine: EngineManager) -> ExitIpCache:
    cached = state.exit_ip_cache.get(str(port))
    if cached and _fresh(cached):
        return cached
    if not engine.status().running:
        return ExitIpCache(ip=None, error="sing-box is not running")
    proxies = [f"socks5://127.0.0.1:{port}", f"socks5h://127.0.0.1:{port}"]
    last_error = None
    for proxy in proxies:
        for url in EXIT_IP_URLS:
            try:
                async with httpx.AsyncClient(proxy=proxy, timeout=10) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    ip = response.text.strip()
                    return ExitIpCache(ip=ip)
            except Exception as exc:
                last_error = str(exc)
    return ExitIpCache(ip=None, error=last_error or "exit IP query failed")


async def measure_port_latency(port: int) -> LatencyResult:
    started = time.perf_counter()
    proxy = f"socks5://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=8) as client:
            response = await client.get("https://www.gstatic.com/generate_204")
            response.raise_for_status()
        elapsed = int((time.perf_counter() - started) * 1000)
        return LatencyResult(alive=True, delay=max(elapsed, 1))
    except Exception as exc:
        return LatencyResult(alive=False, delay=None, error=str(exc))


async def validate_proxy_targets(port: int, urls: list[str] | None = None) -> list[dict]:
    targets = urls or DEFAULT_VALIDATION_URLS
    proxy = f"http://127.0.0.1:{port}"

    async def fetch_target(client, url: str) -> dict:
        started = time.perf_counter()
        try:
            response = await client.get(url)
            elapsed = int((time.perf_counter() - started) * 1000)
            text = response.text.strip().replace("\r", "")
            return {
                "url": url,
                "ok": 200 <= response.status_code < 400,
                "status_code": response.status_code,
                "elapsed_ms": max(elapsed, 1),
                "body_preview": text[:160],
                "error": None,
            }
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return {
                "url": url,
                "ok": False,
                "status_code": None,
                "elapsed_ms": max(elapsed, 1),
                "body_preview": "",
                "error": str(exc),
            }

    async with httpx.AsyncClient(proxy=proxy, timeout=8, follow_redirects=False) as client:
        return await asyncio.gather(*(fetch_target(client, url) for url in targets))


async def validate_proxy_port(
    port: int,
    target_url: str | None = None,
    *,
    include_exit_ip: bool = False,
) -> LatencyResult:
    proxy = f"http://127.0.0.1:{port}"
    test_url = target_url or "https://www.gstatic.com/generate_204"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=5, follow_redirects=False) as client:
            response = await client.get(test_url)
            elapsed = int((time.perf_counter() - started) * 1000)
            body = response.text.strip().replace("\r", "")[:160]
            alive = 200 <= response.status_code < 400
            exit_ip = None
            exit_error = None
            if alive and include_exit_ip:
                for url in EXIT_IP_URLS:
                    try:
                        candidate = await client.get(url)
                        candidate.raise_for_status()
                        exit_ip = candidate.text.strip()
                        break
                    except Exception as exc:
                        exit_error = str(exc)
            return LatencyResult(
                alive=alive,
                delay=max(elapsed, 1) if alive else None,
                exit_ip=exit_ip,
                test_port=port,
                target_url=target_url,
                status_code=response.status_code,
                body_preview=body if target_url else None,
                error=(exit_error if include_exit_ip and alive and not exit_ip else None)
                if alive
                else f"HTTP {response.status_code}",
            )
    except Exception as exc:
        return LatencyResult(
            alive=False,
            delay=None,
            test_port=port,
            target_url=target_url,
            error=str(exc),
        )


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def allocate_test_ports(count: int, start_port: int = TEST_START_PORT) -> list[int]:
    ports: list[int] = []
    port = start_port
    while len(ports) < count and port <= 65000:
        if _port_is_free(port):
            ports.append(port)
        port += 1
    if len(ports) < count:
        raise RuntimeError(f"Could not allocate {count} free local test ports")
    return ports


async def test_nodes_with_temporary_engine(
    nodes: list[ProxyNode],
    on_result=None,
    target_url: str | None = None,
    include_exit_ip: bool = False,
) -> dict[str, LatencyResult]:
    if not nodes:
        return {}
    ports = allocate_test_ports(len(nodes))
    mappings = {
        str(ports[index]): PortMapping(node_tag=node.tag)
        for index, node in enumerate(nodes)
    }
    config = generate_config(
        nodes,
        mappings,
        include_clash_api=False,
        log_path=SING_BOX_TEST_CONFIG_PATH.with_suffix(".log"),
    )
    SING_BOX_TEST_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SING_BOX_TEST_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    engine = EngineManager()
    try:
        await engine.start(SING_BOX_TEST_CONFIG_PATH, check=False, settle_seconds=0.35)
        async def run_one(tag: str, port: int):
            try:
                timeout = 10 if include_exit_ip else 6
                result = await asyncio.wait_for(
                    validate_proxy_port(port, target_url=target_url, include_exit_ip=include_exit_ip),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                result = LatencyResult(
                    alive=False,
                    delay=None,
                    test_port=port,
                    target_url=target_url,
                    error="node test timed out",
                )
            return tag, result

        semaphore = asyncio.Semaphore(12)

        async def limited_run_one(tag: str, port: int):
            async with semaphore:
                return await run_one(tag, port)

        tasks = [limited_run_one(node.tag, ports[index]) for index, node in enumerate(nodes)]
        results: dict[str, LatencyResult] = {}
        for completed in asyncio.as_completed(tasks):
            tag, result = await completed
            results[tag] = result
            if on_result:
                on_result(tag, result)
        return results
    finally:
        await engine.stop()


def prune_same_exit_ip(nodes: list[ProxyNode], results: dict[str, LatencyResult]) -> tuple[list[ProxyNode], list[str]]:
    node_by_tag = {node.tag: node for node in nodes}
    best_by_ip: dict[str, str] = {}
    removed: list[str] = []

    for tag, result in results.items():
        if not result.alive or not result.exit_ip:
            continue
        current = best_by_ip.get(result.exit_ip)
        if not current:
            best_by_ip[result.exit_ip] = tag
            continue
        current_result = results[current]
        current_delay = current_result.delay if current_result.delay is not None else 10**9
        next_delay = result.delay if result.delay is not None else 10**9
        if next_delay < current_delay:
            removed.append(current)
            best_by_ip[result.exit_ip] = tag
        else:
            removed.append(tag)

    removed_set = set(removed)
    kept = [node for node in nodes if node.tag not in removed_set]
    messages = [
        f"Removed duplicate exit IP {results[tag].exit_ip}: {node_by_tag[tag].name}"
        for tag in removed
        if tag in node_by_tag and tag in results
    ]
    return kept, messages


def sort_nodes_by_test_result(nodes: list[ProxyNode], results: dict[str, LatencyResult]) -> list[ProxyNode]:
    def key(node: ProxyNode):
        result = results.get(node.tag)
        if result and result.alive:
            delay = result.delay if result.delay is not None else 10**9
            return (0, delay, node.name.lower())
        if result:
            return (1, 10**9, node.name.lower())
        return (2, 10**9, node.name.lower())

    return sorted(nodes, key=key)
