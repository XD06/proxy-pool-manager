from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from datetime import datetime, timezone

import httpx

from .engine import EngineManager, _can_bind_tcp_port
from .generator import generate_config
from .models import AppState, ExitIpCache, LatencyResult, PortMapping, ProxyNode
from .settings import SING_BOX_TEST_CONFIG_PATH, TEST_START_PORT


PRIMARY_TEST_URL = "http://cp.cloudflare.com/generate_204"
FALLBACK_TEST_URLS = [
    PRIMARY_TEST_URL,
    "https://www.gstatic.com/generate_204",
    "https://www.google.com/generate_204",
]

EXIT_IP_URLS = [
    "https://ipv4.webshare.io/",
    "https://api.ipify.org?format=text",
    "https://ipv4.icanhazip.com/",
    "https://ifconfig.me/ip",
    "https://checkip.amazonaws.com/",
    "https://ident.me/",
]

IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

DEFAULT_VALIDATION_URLS = [
    PRIMARY_TEST_URL,
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


def extract_public_ipv4(text: str) -> str | None:
    for match in IPV4_PATTERN.findall(text or ""):
        try:
            ip = ipaddress.ip_address(match)
        except ValueError:
            continue
        if ip.version == 4 and not ip.is_private and not ip.is_loopback and not ip.is_reserved:
            return match
    return None


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
                    ip = extract_public_ipv4(response.text)
                    if ip:
                        return ExitIpCache(ip=ip)
                    last_error = f"{url}: no public IPv4 in response"
            except Exception as exc:
                last_error = str(exc)
    return ExitIpCache(ip=None, error=last_error or "exit IP query failed")


async def measure_port_latency(port: int) -> LatencyResult:
    proxy = f"socks5://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=8) as client:
            result = await _fetch_first_test_url(client, FALLBACK_TEST_URLS)
        return LatencyResult(
            alive=True,
            delay=result["elapsed_ms"],
            target_url=result["url"],
            status_code=result["status_code"],
            body_preview=result["body_preview"],
            error=result.get("fallback_notice"),
        )
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
    target_urls: list[str] | None = None,
    *,
    include_exit_ip: bool = False,
) -> LatencyResult:
    proxy = f"http://127.0.0.1:{port}"
    selected_urls = [url for url in (target_urls or []) if url]
    test_urls = selected_urls or ([target_url] if target_url else FALLBACK_TEST_URLS)
    use_fallback = not selected_urls and not target_url
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=5, follow_redirects=False) as client:
            if len(test_urls) > 1 and not use_fallback:
                target_results = []
                for url in test_urls:
                    target_results.append(await _fetch_one_test_url(client, url))
                ok_results = [item for item in target_results if item["ok"]]
                if not ok_results:
                    raise RuntimeError("; ".join(item.get("error") or f"{item['url']}: HTTP {item.get('status_code')}" for item in target_results))
                result = {
                    "url": ",".join(test_urls),
                    "status_code": ok_results[0]["status_code"],
                    "elapsed_ms": max(int(sum(item["elapsed_ms"] for item in ok_results) / len(ok_results)), 1),
                    "body_preview": "",
                    "fallback_notice": None,
                    "target_results": target_results,
                }
            else:
                result = await _fetch_first_test_url(client, test_urls)
            exit_ip = None
            exit_error = None
            if include_exit_ip:
                for url in EXIT_IP_URLS:
                    try:
                        candidate = await client.get(url)
                        candidate.raise_for_status()
                        exit_ip = extract_public_ipv4(candidate.text)
                        if exit_ip:
                            break
                        exit_error = f"{url}: no public IPv4 in response"
                    except Exception as exc:
                        exit_error = str(exc)
            return LatencyResult(
                alive=True,
                delay=result["elapsed_ms"],
                exit_ip=exit_ip,
                target_results=result.get("target_results") or [
                    {
                        "url": result["url"],
                        "ok": True,
                        "status_code": result["status_code"],
                        "elapsed_ms": result["elapsed_ms"],
                        "error": None,
                    }
                ],
                test_port=port,
                target_url=result["url"],
                status_code=result["status_code"],
                body_preview=result["body_preview"] if target_url else None,
                error=(exit_error if include_exit_ip and not exit_ip else None)
                or result.get("fallback_notice"),
            )
    except Exception as exc:
        return LatencyResult(
            alive=False,
            delay=None,
            test_port=port,
            target_url=target_url,
            error=str(exc),
        )


async def _fetch_one_test_url(client, url: str) -> dict:
    started = time.perf_counter()
    try:
        response = await client.get(url)
        elapsed = int((time.perf_counter() - started) * 1000)
        body = response.text.strip().replace("\r", "")[:160]
        ok = 200 <= response.status_code < 400
        return {
            "url": url,
            "ok": ok,
            "status_code": response.status_code,
            "elapsed_ms": max(elapsed, 1),
            "body_preview": body,
            "error": None if ok else f"HTTP {response.status_code}",
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


async def _fetch_first_test_url(client, urls: list[str]) -> dict:
    errors: list[str] = []
    for index, url in enumerate(urls):
        started = time.perf_counter()
        try:
            response = await client.get(url)
            elapsed = int((time.perf_counter() - started) * 1000)
            body = response.text.strip().replace("\r", "")[:160]
            if 200 <= response.status_code < 400:
                notice = None
                if index > 0:
                    notice = f"primary test URL failed; switched to {url}"
                    print(f"[测速] {notice}")
                return {
                    "url": url,
                    "status_code": response.status_code,
                    "elapsed_ms": max(elapsed, 1),
                    "body_preview": body,
                    "fallback_notice": notice,
                    "target_results": [
                        {
                            "url": url,
                            "ok": True,
                            "status_code": response.status_code,
                            "elapsed_ms": max(elapsed, 1),
                            "error": None,
                        }
                    ],
                }
            errors.append(f"{url}: HTTP {response.status_code}")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(errors) or "all test URLs failed")


def _port_is_free(port: int) -> bool:
    if not _can_bind_tcp_port(port):
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
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
    target_urls: list[str] | None = None,
    include_exit_ip: bool = False,
    should_cancel=None,
) -> dict[str, LatencyResult]:
    if not nodes:
        return {}
    cleanup_engine = EngineManager(SING_BOX_TEST_CONFIG_PATH)
    await cleanup_engine.stop(SING_BOX_TEST_CONFIG_PATH)
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

    engine = EngineManager(SING_BOX_TEST_CONFIG_PATH)
    try:
        await engine.start(SING_BOX_TEST_CONFIG_PATH, check=False, settle_seconds=0.35)
        async def run_one(tag: str, port: int):
            try:
                url_count = len(target_urls or ([target_url] if target_url else FALLBACK_TEST_URLS))
                timeout = (10 if include_exit_ip else 6) + max(0, url_count - 1) * 5
                result = await asyncio.wait_for(
                    validate_proxy_port(port, target_url=target_url, target_urls=target_urls, include_exit_ip=include_exit_ip),
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

        tasks = [asyncio.create_task(limited_run_one(node.tag, ports[index])) for index, node in enumerate(nodes)]
        results: dict[str, LatencyResult] = {}
        try:
            for completed in asyncio.as_completed(tasks):
                if should_cancel and should_cancel():
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    break
                tag, result = await completed
                results[tag] = result
                if on_result:
                    on_result(tag, result)
        finally:
            if should_cancel and should_cancel():
                await asyncio.gather(*tasks, return_exceptions=True)
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
    def target_success_count(result: LatencyResult) -> int:
        if not result.target_results:
            return 1 if result.alive else 0
        return sum(1 for item in result.target_results if item.get("ok"))

    def key(node: ProxyNode):
        result = results.get(node.tag)
        if result and result.alive:
            delay = result.delay if result.delay is not None else 10**9
            return (0, -target_success_count(result), delay, node.name.lower())
        if result:
            return (1, 0, 10**9, node.name.lower())
        return (2, 0, 10**9, node.name.lower())

    return sorted(nodes, key=key)
