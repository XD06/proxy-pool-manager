from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .engine import EngineError, EngineManager, _can_bind_tcp_port
from .generator import generate_config
from .httpclient import shared_ssl_context
from .models import AppState, ExitIpCache, LatencyResult, PortMapping, ProxyNode
from .settings import SING_BOX_TEST_CONFIG_PATH, TEST_START_PORT


PRIMARY_TEST_URL = "https://www.google.com/generate_204"
FALLBACK_TEST_URLS = [
    PRIMARY_TEST_URL,
    "http://cp.cloudflare.com/generate_204",
    "https://www.gstatic.com/generate_204",
]

DEFAULT_NODE_TEST_URLS = [
    PRIMARY_TEST_URL,
    "http://cp.cloudflare.com/generate_204",
    "https://www.gstatic.com/generate_204",
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
    "http://cp.cloudflare.com/generate_204",
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
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=10, verify=shared_ssl_context()) as client:
                for url in EXIT_IP_URLS:
                    try:
                        response = await client.get(url)
                        response.raise_for_status()
                        ip = extract_public_ipv4(response.text)
                        if ip:
                            return ExitIpCache(ip=ip)
                        last_error = f"{url}: no public IPv4 in response"
                    except Exception as exc:
                        last_error = str(exc)
        except Exception as exc:
            last_error = str(exc)
    return ExitIpCache(ip=None, error=last_error or "exit IP query failed")


async def _query_exit_ip_with_budget(client: httpx.AsyncClient, *, timeout_seconds: float = 3.0) -> tuple[str | None, str | None]:
    deadline = time.monotonic() + max(timeout_seconds, 0.1)
    last_error = None
    for url in EXIT_IP_URLS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = await client.get(url, timeout=min(remaining, 2.0))
            response.raise_for_status()
            ip = extract_public_ipv4(response.text)
            if ip:
                return ip, None
            last_error = f"{url}: no public IPv4 in response"
        except Exception as exc:
            last_error = str(exc)
    return None, last_error or "exit IP query timed out"


async def measure_port_latency(port: int) -> LatencyResult:
    proxy = f"socks5://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=8, verify=shared_ssl_context()) as client:
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
    proxy = f"socks5h://127.0.0.1:{port}"

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

    # Relays commonly need 8-12s for a cold handshake (verified in the test
    # engine log); 5s killed nodes that pass when tested alone.
    async with httpx.AsyncClient(proxy=proxy, timeout=8, follow_redirects=False, verify=shared_ssl_context()) as client:
        return await asyncio.gather(*(fetch_target(client, url) for url in targets))


async def validate_proxy_port(
    port: int,
    target_url: str | None = None,
    target_urls: list[str] | None = None,
    *,
    include_exit_ip: bool = False,
) -> LatencyResult:
    proxy = f"socks5h://127.0.0.1:{port}"
    selected_urls = [url for url in (target_urls or []) if url]
    test_urls = selected_urls or ([target_url] if target_url else DEFAULT_NODE_TEST_URLS)
    use_fallback = not selected_urls and not target_url
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=8, follow_redirects=False, verify=shared_ssl_context()) as client:
            if len(test_urls) > 1 and not use_fallback:
                # Explicit multi-target checks fetch concurrently; gather keeps
                # the request order for the per-URL report.
                target_results = list(
                    await asyncio.gather(*(_fetch_one_test_url(client, url) for url in test_urls))
                )
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
                exit_ip, exit_error = await _query_exit_ip_with_budget(client)
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


_TEST_URL_STAGGER_SECONDS = 2.0

# Max simultaneous probes against a single upstream server. Real node sets
# concentrate dozens of siblings on one relay host, and each dead sibling
# holds its slot for the full timeout, so a tight cap collapses throughput
# (2 turned a 120-node batch into ~10 minutes). With staggered URLs a node
# opens one connection, so 16 stays under the relay 503 threshold that the
# old 3-way URL racing tripped while keeping a 60-sibling host draining.
_PER_HOST_TEST_CONCURRENCY = 16


async def _fetch_first_test_url(client, urls: list[str]) -> dict:
    """Try candidate URLs with staggered starts, returning the first success.

    Racing every URL at once opened 3 upstream connections per node; at batch
    concurrency that overloaded relays (503s, 8-12s cold handshakes) and
    mass-timed-out nodes that pass when tested alone. Staggering keeps a
    healthy node at one connection while slow primaries still get fallbacks.
    """
    tasks: list[asyncio.Task] = []
    pending: set[asyncio.Task] = set()
    queue = list(urls)
    failures: dict[str, str] = {}
    winner: dict | None = None
    try:
        while winner is None and (queue or pending):
            if queue:
                task = asyncio.create_task(_fetch_one_test_url(client, queue.pop(0)))
                tasks.append(task)
                pending.add(task)
            # Launch the next candidate only after the stagger window passes
            # (or a candidate fails outright); never cancel a slow leader.
            timeout = _TEST_URL_STAGGER_SECONDS if queue else None
            done, pending = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                item = finished.result()
                if item["ok"]:
                    winner = item
                    break
                failures[item["url"]] = item["error"] or "request failed"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if winner is None:
        errors = [f"{url}: {failures[url]}" for url in urls if url in failures]
        raise RuntimeError("; ".join(errors) or "all test URLs failed")
    notice = None
    if winner["url"] != urls[0] and urls[0] in failures:
        notice = f"primary test URL failed; switched to {winner['url']}"
        print(f"[测速] {notice}")
    return {
        "url": winner["url"],
        "status_code": winner["status_code"],
        "elapsed_ms": winner["elapsed_ms"],
        "body_preview": winner["body_preview"],
        "fallback_notice": notice,
        "target_results": [
            {
                "url": winner["url"],
                "ok": True,
                "status_code": winner["status_code"],
                "elapsed_ms": winner["elapsed_ms"],
                "error": None,
            }
        ],
    }


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
    # Stay below 49152 so test listeners never collide with the Windows
    # ephemeral (dynamic) port range used by outbound connections.
    while len(ports) < count and port <= 49000:
        if _port_is_free(port):
            ports.append(port)
        port += 1
    if len(ports) < count:
        raise RuntimeError(f"Could not allocate {count} free local test ports")
    return ports


async def preflight_nodes(nodes: list[ProxyNode]) -> dict[str, str]:
    """Identify sing-box-incompatible nodes before opening test listeners.

    A failed batch is bisected so a single unsupported outbound does not mark
    every neighbouring node as unusable.
    """
    if not nodes:
        return {}

    async def check_subset(subset: list[ProxyNode]) -> dict[str, str]:
        with tempfile.TemporaryDirectory(prefix="ppm-preflight-", dir=SING_BOX_TEST_CONFIG_PATH.parent) as directory:
            config_path = Path(directory) / "sing-box-preflight.json"
            mappings = {
                str(30_000 + index): PortMapping(node_tag=node.tag)
                for index, node in enumerate(subset)
            }
            config = generate_config(
                subset,
                mappings,
                include_clash_api=False,
                log_path=config_path.with_suffix(".log"),
                listen_host="127.0.0.1",
            )
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            engine = EngineManager(config_path)
            try:
                await engine.check_config(config_path)
                return {}
            except EngineError as exc:
                message = f"configuration incompatible: {str(exc).strip()}"
                if len(subset) == 1:
                    return {subset[0].tag: message}

        middle = len(subset) // 2
        left, right = await asyncio.gather(check_subset(subset[:middle]), check_subset(subset[middle:]))
        return {**left, **right}

    return await check_subset(nodes)


# Global guard: two concurrent temporary test engines would kill each other's
# sing-box instance (both use SING_BOX_TEST_CONFIG_PATH) and race test ports.
_TEST_ENGINE_LOCK = asyncio.Lock()


async def test_nodes_with_temporary_engine(
    nodes: list[ProxyNode],
    on_result=None,
    target_url: str | None = None,
    target_urls: list[str] | None = None,
    include_exit_ip: bool = False,
    should_cancel=None,
    concurrency: int = 12,
    batch_size: int | None = None,
) -> dict[str, LatencyResult]:
    async with _TEST_ENGINE_LOCK:
        return await _test_nodes_with_temporary_engine_unlocked(
            nodes,
            on_result=on_result,
            target_url=target_url,
            target_urls=target_urls,
            include_exit_ip=include_exit_ip,
            should_cancel=should_cancel,
            concurrency=concurrency,
            batch_size=batch_size,
        )


async def _test_nodes_with_temporary_engine_unlocked(
    nodes: list[ProxyNode],
    on_result=None,
    target_url: str | None = None,
    target_urls: list[str] | None = None,
    include_exit_ip: bool = False,
    should_cancel=None,
    concurrency: int = 12,
    batch_size: int | None = None,
) -> dict[str, LatencyResult]:
    if not nodes:
        return {}
    cleanup_engine = EngineManager(SING_BOX_TEST_CONFIG_PATH)
    await cleanup_engine.stop(SING_BOX_TEST_CONFIG_PATH)
    concurrency = max(1, int(concurrency or 12))
    if batch_size is None:
        env_value = os.environ.get("NODE_TEST_BATCH_SIZE")
        if env_value:
            try:
                batch_size = int(env_value)
            except ValueError:
                batch_size = None
    batch_size = max(1, min(len(nodes), int(batch_size or len(nodes))))
    results: dict[str, LatencyResult] = {}
    for offset in range(0, len(nodes), batch_size):
        if should_cancel and should_cancel():
            break
        batch = nodes[offset:offset + batch_size]
        batch_results = await _test_node_batch_with_temporary_engine(
            batch,
            on_result=on_result,
            target_url=target_url,
            target_urls=target_urls,
            include_exit_ip=include_exit_ip,
            should_cancel=should_cancel,
            concurrency=concurrency,
        )
        results.update(batch_results)
    return results


async def _test_node_batch_with_temporary_engine(
    nodes: list[ProxyNode],
    on_result=None,
    target_url: str | None = None,
    target_urls: list[str] | None = None,
    include_exit_ip: bool = False,
    should_cancel=None,
    concurrency: int = 12,
) -> dict[str, LatencyResult]:
    preflight_errors = await preflight_nodes(nodes)
    results: dict[str, LatencyResult] = {
        node.tag: LatencyResult(alive=False, delay=None, error=preflight_errors[node.tag])
        for node in nodes
        if node.tag in preflight_errors
    }
    for tag, result in results.items():
        if on_result:
            on_result(tag, result)

    valid_nodes = [node for node in nodes if node.tag not in preflight_errors]
    if not valid_nodes:
        return results

    ports = allocate_test_ports(len(valid_nodes))
    mappings = {
        str(ports[index]): PortMapping(node_tag=node.tag)
        for index, node in enumerate(valid_nodes)
    }
    config = generate_config(
        valid_nodes,
        mappings,
        include_clash_api=False,
        log_path=SING_BOX_TEST_CONFIG_PATH.with_suffix(".log"),
        listen_host="127.0.0.1",
    )
    SING_BOX_TEST_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SING_BOX_TEST_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    engine = EngineManager(SING_BOX_TEST_CONFIG_PATH)
    try:
        settle_seconds = 0.8 if os.name != "nt" else 0.45
        await engine.start(SING_BOX_TEST_CONFIG_PATH, check=False, settle_seconds=settle_seconds)
        async def run_one(tag: str, port: int):
            try:
                # Budget: staggered fallbacks start up to 4s late plus an 8s
                # request timeout; cold relay handshakes alone take 8-12s.
                timeout = 15 if include_exit_ip else 12
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

        semaphore = asyncio.Semaphore(max(1, int(concurrency or 12)))

        # Relays rate-limit sibling nodes probed at once (503s and stalled
        # handshakes in the test log killed nodes that pass when tested
        # alone), so also cap in-flight probes per upstream server.
        node_hosts = {node.tag: (node.server or "").lower() or node.tag for node in valid_nodes}
        host_semaphores: dict[str, asyncio.Semaphore] = {}

        def host_semaphore(tag: str) -> asyncio.Semaphore:
            host = node_hosts.get(tag) or tag
            if host not in host_semaphores:
                host_semaphores[host] = asyncio.Semaphore(_PER_HOST_TEST_CONCURRENCY)
            return host_semaphores[host]

        async def limited_run_one(tag: str, port: int):
            # Acquire the host slot first so queued siblings do not pin
            # global slots while they wait.
            async with host_semaphore(tag):
                async with semaphore:
                    return await run_one(tag, port)

        tasks = [asyncio.create_task(limited_run_one(node.tag, ports[index])) for index, node in enumerate(valid_nodes)]
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
