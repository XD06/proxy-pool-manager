from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import socket
import subprocess
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .engine import EngineError, EngineManager
from .engine import _can_bind_tcp_port
from .generator import ConfigError, generate_config
from .geoip import geoip_compact_summary, geoip_summary, lookup_geoip
from .models import AppState, ExitIpCache, LatencyResult, PortMapping, utc_now_iso
from .parser import import_nodes
from .proxy_admin import (
    proxy_admin_client_scope,
    proxy_admin_import,
    proxy_admin_list_all,
    proxy_admin_quality_check,
    proxy_admin_remove_ids,
)
from .proxy_check import check_proxy_quality
from .settings import (
    APP_CONFIG_PATH,
    ASSET_VERSION,
    ROOT_DIR,
    SING_BOX_CONFIG_PATH,
    SING_BOX_TEST_CONFIG_PATH,
    STATIC_DIR,
    TEMPLATES_DIR,
    current_clash_api_addr,
    current_performance_settings,
    current_proxy_listen_host,
    current_proxy_public_host,
)
from .store import StateStore
from .tester import (
    DEFAULT_VALIDATION_URLS,
    PRIMARY_TEST_URL,
    measure_port_latency,
    prune_same_exit_ip,
    query_exit_ip,
    sort_nodes_by_test_result,
    test_nodes_with_temporary_engine,
    validate_proxy_targets,
)


class ImportRequest(BaseModel):
    url: str | None = None
    text: str | None = None


class LoginRequest(BaseModel):
    key: str


class SubscriptionConfigRequest(BaseModel):
    url: str | None = None
    refresh_interval_minutes: int = 0


class AssignRequest(BaseModel):
    mappings: dict[str, str]


class DeleteNodesRequest(BaseModel):
    node_tags: list[str] | None = None
    all: bool = False


class TestRequest(BaseModel):
    node_tags: list[str] | None = None
    prune_same_ip: bool = False
    include_geoip: bool = False
    target_url: str | None = None
    target_urls: list[str] | None = None


class PortTestRequest(BaseModel):
    ports: list[int] | None = None
    urls: list[str] | None = None


def default_port_validation_urls(urls: list[str] | None) -> list[str]:
    selected = [url.strip() for url in (urls or []) if str(url).strip()]
    return selected or [PRIMARY_TEST_URL]


class PortAvailabilityRequest(BaseModel):
    ports: list[int]


class PortAllocateRequest(BaseModel):
    start_port: int = 8001
    count: int
    exclude: list[int] = []


class ProxyAdminRequest(BaseModel):
    base_url: str
    token: str
    proxy_host: str | None = None
    replace_from: str | None = None
    replace_to: str | None = None
    proxy_name_prefix: str | None = "代理"
    ports: list[int] | None = None
    concurrency: int = 10
    check_only: bool = False
    proxy_ids_by_port: dict[str, int] | None = None


class ProxyAdminRemoveRequest(BaseModel):
    base_url: str
    token: str
    ids: list[int] | None = None
    unused: bool = False
    concurrency: int = 5


class ProxyAdminConfig(BaseModel):
    base_url: str = ""
    token: str = ""
    proxy_host: str = ""
    replace_from: str = "127.0.0.1"
    replace_to: str = ""
    proxy_name_prefix: str = "代理"
    concurrency: int = 10


class LocalProxyCheckRequest(BaseModel):
    port: int | None = None
    proxy_url: str | None = None
    timeout: int = 30


class LocalProxyCheckStartRequest(BaseModel):
    ports: list[int] | None = None
    timeout: int = 30
    concurrency: int = 3


class GeoIpConfig(BaseModel):
    enabled: bool = True
    cache_ttl_hours: int = 168
    concurrency: int = 4


class DoctorRequest(BaseModel):
    timeout: int = 30


class TestJob(BaseModel):
    id: str
    status: str = "running"
    total: int = 0
    completed: int = 0
    results: dict[str, dict] = {}
    details: dict[str, dict] = {}
    removed: list[str] = []
    error: str | None = None
    touched_at: float = Field(default_factory=time.monotonic, exclude=True)


class PortTestJob(BaseModel):
    id: str
    status: str = "running"
    total: int = 0
    completed: int = 0
    results: dict[str, dict] = {}
    details: dict[str, dict] = {}
    error: str | None = None
    touched_at: float = Field(default_factory=time.monotonic, exclude=True)


class ProxyAdminJob(BaseModel):
    id: str
    status: str = "running"
    total: int = 0
    completed: int = 0
    imported: list[dict] = []
    results: dict[str, dict] = {}
    error: str | None = None
    touched_at: float = Field(default_factory=time.monotonic, exclude=True)


class LocalProxyCheckJob(BaseModel):
    id: str
    status: str = "running"
    total: int = 0
    completed: int = 0
    results: dict[str, dict] = {}
    error: str | None = None
    touched_at: float = Field(default_factory=time.monotonic, exclude=True)


def create_app(store: StateStore | None = None, engine: EngineManager | None = None) -> FastAPI:
    web_started_at = utc_now_iso()
    state_store = store or StateStore()
    app_state = state_store.load()
    state_loaded_mtime_ns = state_store.path.stat().st_mtime_ns if state_store.path.exists() else None
    engine_manager = engine or EngineManager(SING_BOX_CONFIG_PATH)
    performance = current_performance_settings()
    test_jobs: dict[str, TestJob] = {}
    test_job_lock = asyncio.Lock()
    active_test_job: dict[str, str | None] = {"id": None}
    port_test_jobs: dict[str, PortTestJob] = {}
    port_test_job_lock = asyncio.Lock()
    active_port_test_job: dict[str, str | None] = {"id": None}
    proxy_admin_jobs: dict[str, ProxyAdminJob] = {}
    proxy_admin_job_lock = asyncio.Lock()
    active_proxy_admin_job: dict[str, str | None] = {"id": None}
    local_proxy_check_jobs: dict[str, LocalProxyCheckJob] = {}
    local_proxy_check_job_lock = asyncio.Lock()
    active_local_proxy_check_job: dict[str, str | None] = {"id": None}
    canceled_test_jobs: set[str] = set()
    canceled_port_test_jobs: set[str] = set()
    canceled_proxy_admin_jobs: set[str] = set()
    canceled_local_proxy_check_jobs: set[str] = set()
    auth_sessions: set[str] = set()
    geoip_tasks: dict[str, asyncio.Task] = {}
    geoip_semaphore = asyncio.Semaphore(performance.max_geoip_concurrency)
    subscription_refresh_lock = asyncio.Lock()
    next_subscription_refresh_at: dict[str, float | None] = {"value": None}

    monitor_task: asyncio.Task | None = None
    subscription_task: asyncio.Task | None = None
    job_cleanup_task: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal monitor_task, subscription_task, job_cleanup_task
        monitor = getattr(engine_manager, "monitor", None)
        if monitor and not monitor_task:
            monitor_task = asyncio.create_task(monitor(SING_BOX_CONFIG_PATH))
        if not subscription_task:
            subscription_task = asyncio.create_task(subscription_refresh_loop())
        if not job_cleanup_task:
            job_cleanup_task = asyncio.create_task(job_cleanup_loop())
        try:
            yield
        finally:
            if monitor_task:
                monitor_task.cancel()
            if subscription_task:
                subscription_task.cancel()
            if job_cleanup_task:
                job_cleanup_task.cancel()
            await flush_save()

    app = FastAPI(title="Proxy Pool Manager", lifespan=lifespan)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def admin_key() -> str:
        config = load_app_config()
        return os.environ.get("PPM_ADMIN_KEY") or str(config.get("admin_key") or "")

    def auth_enabled() -> bool:
        return bool(admin_key())

    def auth_cookie_name() -> str:
        return "ppm_session"

    def auth_cookie_secure() -> bool:
        value = os.environ.get("PPM_COOKIE_SECURE") or str(load_app_config().get("cookie_secure") or "")
        return value.lower() in {"1", "true", "yes", "on"}

    def is_authenticated(request: Request) -> bool:
        if not auth_enabled():
            return True
        token = request.cookies.get(auth_cookie_name())
        return bool(token and token in auth_sessions)

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        path = request.url.path
        public = (
            path.startswith("/static/")
            or path in {"/", "/api/auth/status", "/api/auth/login", "/favicon.ico"}
        )
        if not public and not is_authenticated(request):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Authentication required"}, status_code=401)
        return await call_next(request)

    pending_save_task: dict[str, asyncio.Task | None] = {"task": None}
    save_dirty: dict[str, bool] = {"value": False}

    def save_now() -> None:
        nonlocal state_loaded_mtime_ns
        state_store.save(app_state)
        state_loaded_mtime_ns = state_store.path.stat().st_mtime_ns if state_store.path.exists() else None

    def save() -> None:
        save_now()

    async def _run_debounced_save() -> None:
        try:
            await asyncio.sleep(performance.state_save_debounce_ms / 1000)
            if save_dirty["value"]:
                save_dirty["value"] = False
                save_now()
        finally:
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if current_task is None or pending_save_task["task"] is current_task:
                pending_save_task["task"] = None

    def save_later() -> None:
        if performance.state_save_debounce_ms <= 0:
            save()
            return
        save_dirty["value"] = True
        task = pending_save_task["task"]
        if task is None or task.done():
            pending_save_task["task"] = asyncio.create_task(_run_debounced_save())

    async def flush_save() -> None:
        task = pending_save_task["task"]
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if save_dirty["value"]:
            save_dirty["value"] = False
            save_now()

    def touch_job(job) -> None:
        job.touched_at = time.monotonic()

    def tail_test_engine_log(lines: int = 40) -> list[str]:
        log_path = SING_BOX_TEST_CONFIG_PATH.with_suffix(".log")
        if not log_path.exists():
            return []
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return []
        return content[-max(1, lines):]

    def cleanup_job_map(jobs: dict[str, BaseModel], active_id: str | None) -> None:
        now = time.monotonic()
        retention_seconds = performance.job_retention_minutes * 60
        inactive_statuses = {"done", "error", "canceled"}
        removable = [
            (job_id, job)
            for job_id, job in jobs.items()
            if job_id != active_id and getattr(job, "status", "") in inactive_statuses
        ]
        for job_id, job in list(removable):
            if now - getattr(job, "touched_at", now) > retention_seconds:
                jobs.pop(job_id, None)
        removable = [
            (job_id, job)
            for job_id, job in jobs.items()
            if job_id != active_id and getattr(job, "status", "") in inactive_statuses
        ]
        removable.sort(key=lambda item: getattr(item[1], "touched_at", 0), reverse=True)
        for job_id, _job in removable[performance.max_jobs_per_type:]:
            jobs.pop(job_id, None)

    def cleanup_all_jobs() -> None:
        cleanup_job_map(test_jobs, active_test_job["id"])
        cleanup_job_map(port_test_jobs, active_port_test_job["id"])
        cleanup_job_map(proxy_admin_jobs, active_proxy_admin_job["id"])
        cleanup_job_map(local_proxy_check_jobs, active_local_proxy_check_job["id"])

    async def job_cleanup_loop() -> None:
        while True:
            await asyncio.sleep(60)
            cleanup_all_jobs()

    def refresh_state_from_disk() -> bool:
        nonlocal state_loaded_mtime_ns
        try:
            mtime_ns = state_store.path.stat().st_mtime_ns
        except OSError:
            return False
        if state_loaded_mtime_ns is not None and mtime_ns <= state_loaded_mtime_ns:
            return False
        fresh = state_store.load()
        for field in AppState.model_fields:
            setattr(app_state, field, getattr(fresh, field))
        state_loaded_mtime_ns = mtime_ns
        return True

    def subscription_payload() -> dict:
        next_at = next_subscription_refresh_at["value"]
        return {
            "url": app_state.subscription_url or "",
            "refresh_interval_minutes": app_state.subscription_refresh_interval_minutes,
            "last_refresh_at": app_state.subscription_last_refresh_at,
            "last_error": app_state.subscription_last_error,
            "last_count": app_state.subscription_last_count,
            "next_refresh_in_seconds": max(0, int(next_at - time.time())) if next_at else None,
        }

    async def refresh_subscription_now() -> dict:
        refresh_state_from_disk()
        url = (app_state.subscription_url or "").strip()
        if not url:
            raise ValueError("Subscription URL is not configured")
        async with subscription_refresh_lock:
            try:
                result = await import_nodes(url=url)
                existing = node_by_tag()
                added = 0
                updated = 0
                for node in result.nodes:
                    if node.tag in existing:
                        updated += 1
                    else:
                        added += 1
                    existing[node.tag] = node
                app_state.nodes = list(existing.values())
                app_state.subscription_last_refresh_at = utc_now_iso()
                app_state.subscription_last_error = None
                app_state.subscription_last_count = result.count
                save()
                return {
                    **subscription_payload(),
                    "imported": result.count,
                    "added": added,
                    "updated": updated,
                    "total_nodes": len(app_state.nodes),
                    "warnings": result.warnings,
                }
            except Exception as exc:
                app_state.subscription_last_refresh_at = utc_now_iso()
                app_state.subscription_last_error = str(exc)
                save()
                raise

    async def subscription_refresh_loop() -> None:
        while True:
            refresh_state_from_disk()
            interval = int(app_state.subscription_refresh_interval_minutes or 0)
            if not app_state.subscription_url or interval <= 0:
                next_subscription_refresh_at["value"] = None
                await asyncio.sleep(30)
                continue
            if next_subscription_refresh_at["value"] is None:
                next_subscription_refresh_at["value"] = time.time() + interval * 60
            delay = next_subscription_refresh_at["value"] - time.time()
            if delay > 0:
                await asyncio.sleep(min(delay, 30))
                continue
            try:
                await refresh_subscription_now()
            except Exception:
                pass
            finally:
                next_subscription_refresh_at["value"] = time.time() + interval * 60

    def load_app_config() -> dict:
        if not APP_CONFIG_PATH.exists():
            return {}
        try:
            return json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_app_config(config: dict) -> None:
        APP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        APP_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def auth_payload(request: Request) -> dict:
        return {
            "enabled": auth_enabled(),
            "authenticated": is_authenticated(request),
        }

    def proxy_admin_config_payload() -> dict:
        config = load_app_config().get("proxy_admin") or {}
        return ProxyAdminConfig.model_validate(config).model_dump()

    def geoip_config() -> GeoIpConfig:
        config = load_app_config().get("geoip") or {}
        return GeoIpConfig.model_validate(config)

    def geoip_payload(ip: str | None) -> dict | None:
        if not ip:
            return None
        result = app_state.geoip_cache.get(ip)
        return result.model_dump() if result else None

    def geoip_payload_with_summaries(ip: str | None) -> dict | None:
        payload = geoip_payload(ip)
        if not payload:
            return None
        payload["summary"] = geoip_summary(payload)
        payload["compact"] = geoip_compact_summary(payload)
        return payload

    def attach_geoip_to_result(result: LatencyResult) -> LatencyResult:
        if result.exit_ip:
            cached = geoip_payload_with_summaries(result.exit_ip)
            if cached:
                result.geoip = cached
        return result

    def enrich_result_dict(result: dict) -> dict:
        exit_ip = result.get("exit_ip")
        if exit_ip and not result.get("geoip"):
            cached = geoip_payload_with_summaries(exit_ip)
            if cached:
                result["geoip"] = cached
        return result

    def schedule_geoip_lookup(ip: str | None) -> None:
        if not ip:
            return
        config = geoip_config()
        if not config.enabled:
            return
        cached = app_state.geoip_cache.get(ip)
        if cached and not cached.error:
            return
        existing = geoip_tasks.get(ip)
        if existing and not existing.done():
            return

        async def runner():
            try:
                async with geoip_semaphore:
                    await lookup_geoip(
                        ip,
                        app_state,
                        ttl_hours=config.cache_ttl_hours,
                        enabled=config.enabled,
                    )
                for latency in app_state.latency_cache.values():
                    if latency.exit_ip == ip:
                        cached_result = geoip_payload_with_summaries(ip)
                        if cached_result:
                            latency.geoip = cached_result
                for cache in app_state.exit_ip_cache.values():
                    if cache.ip == ip:
                        cache.geoip = geoip_payload_with_summaries(ip)
                save_later()
            finally:
                geoip_tasks.pop(ip, None)

        geoip_tasks[ip] = asyncio.create_task(runner())

    async def enrich_geoip_now(ip: str | None) -> dict | None:
        if not ip:
            return None
        config = geoip_config()
        result = await lookup_geoip(
            ip,
            app_state,
            ttl_hours=config.cache_ttl_hours,
            enabled=config.enabled,
        )
        if not result:
            return None
        payload = result.model_dump()
        payload["summary"] = geoip_summary(payload)
        payload["compact"] = geoip_compact_summary(payload)
        return payload

    def node_by_tag():
        return {node.tag: node for node in app_state.nodes}

    def mapped_ports() -> list[int]:
        return sorted(int(port) for port in app_state.port_mappings)

    def configured_ports() -> list[int]:
        if not SING_BOX_CONFIG_PATH.exists():
            return []
        try:
            config = json.loads(SING_BOX_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
        ports = [
            item.get("listen_port")
            for item in config.get("inbounds", [])
            if isinstance(item, dict) and isinstance(item.get("listen_port"), int)
        ]
        return sorted(ports)

    def proxy_connect_host(request: Request | None = None) -> str:
        proxy_public_host = current_proxy_public_host()
        proxy_listen_host = current_proxy_listen_host()
        if proxy_public_host:
            return proxy_public_host
        if proxy_listen_host not in {"0.0.0.0", "::", ""}:
            return proxy_listen_host
        if request and request.url.hostname:
            return request.url.hostname
        return "127.0.0.1"

    def proxy_authority(port: int, request: Request | None = None) -> str:
        host = proxy_connect_host(request)
        formatted_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return f"{formatted_host}:{port}"

    def engine_status_payload(request: Request | None = None) -> dict:
        payload = engine_manager.status().model_dump()
        expected = mapped_ports()
        config_ports = [] if not expected and not payload["running"] else configured_ports()
        listening = _listening_local_ports(expected) if payload["running"] else []
        payload["expected_ports"] = expected
        payload["config_ports"] = config_ports
        payload["config_matches_state"] = config_ports == expected
        payload["listening_ports"] = listening
        payload["missing_ports"] = sorted(set(expected) - set(listening))
        payload["expected_count"] = len(expected)
        payload["listening_count"] = len(listening)
        payload["ready"] = bool(payload["running"]) and len(listening) == len(expected)
        payload["proxy_listen_host"] = current_proxy_listen_host()
        payload["proxy_public_host"] = current_proxy_public_host()
        payload["proxy_connect_host"] = proxy_connect_host(request)
        return payload

    def node_test_running() -> bool:
        return active_test_job["id"] is not None or test_job_lock.locked()

    def include_clash_api_for_next_start() -> bool:
        if engine_manager.status().running:
            return True
        port = configured_clash_api_port()
        if port is None:
            return False
        return _can_bind_tcp_port(port)

    def configured_clash_api_port() -> int | None:
        controller = current_clash_api_addr()
        match = re.search(r":(\d+)$", controller)
        if not match:
            return None
        return int(match.group(1))

    def port_diagnostic(
        port: int,
        *,
        clash_port: int | None = None,
        engine_running: bool | None = None,
        project_listening: set[int] | None = None,
    ) -> dict:
        if port < 1024 or port > 65535:
            return {"available": False, "reason": "invalid", "label": "端口无效"}
        if clash_port is None:
            clash_port = configured_clash_api_port()
        if clash_port == port:
            return {
                "available": False,
                "reason": "reserved-clash-api",
                "label": "Clash API 控制端口",
            }
        if engine_running is None:
            engine_running = engine_manager.status().running
        if project_listening is None:
            project_listening = set(_listening_local_ports([port])) if engine_running else set()
        if engine_running:
            if port in project_listening:
                return {
                    "available": False,
                    "reason": "project-listening",
                    "label": "本项目正在监听",
                }
        available = _can_bind_tcp_port(port)
        return {
            "available": available,
            "reason": "available" if available else "busy",
            "label": "可用" if available else "系统不可绑定",
        }

    async def local_proxy_port_open(port: int) -> bool:
        def check() -> bool:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    return True
            except OSError:
                return False

        return await asyncio.to_thread(check)

    async def run_local_proxy_check(payload: LocalProxyCheckRequest) -> dict:
        proxy_url = (payload.proxy_url or "").strip()
        proxy_id = int(payload.port or 0)
        if not proxy_url:
            if not payload.port:
                raise ValueError("port or proxy_url is required")
            if payload.port < 1 or payload.port > 65535:
                raise ValueError("invalid port")
            if not await local_proxy_port_open(payload.port):
                return {
                    "id": int(payload.port),
                    "proxy_url": f"http://127.0.0.1:{payload.port}/",
                    "exit_ip": "",
                    "country": "",
                    "country_code": "",
                    "score": 0,
                    "grade": "ERR",
                    "summary": "本地代理端口未监听",
                    "items": [
                        {
                            "target": "base_connectivity",
                            "status": "fail",
                            "http_status": None,
                            "latency_ms": None,
                            "message": f"127.0.0.1:{payload.port} 未监听，请先启动或重启引擎",
                        }
                    ],
                }
            proxy_url = f"http://127.0.0.1:{payload.port}/"
        result = await check_proxy_quality(proxy_url, proxy_id, payload.timeout)
        base_failed = any(
            item.get("target") == "base_connectivity" and item.get("status") == "fail"
            for item in result.get("items") or []
        )
        if base_failed and not result.get("exit_ip"):
            result["grade"] = "ERR"
            result["score"] = 0
            result["error"] = next(
                (
                    item.get("message")
                    for item in result.get("items") or []
                    if item.get("target") == "base_connectivity" and item.get("status") == "fail"
                ),
                "base connectivity failed",
            )
        return result

    def allocate_available_ports(start_port: int, count: int, exclude: list[int]) -> dict:
        if count < 0 or count > 1000:
            raise HTTPException(status_code=400, detail="Invalid allocation count")
        if start_port < 1024 or start_port > 65535:
            raise HTTPException(status_code=400, detail=f"Invalid start port: {start_port}")
        excluded = {int(port) for port in exclude}
        clash_port = configured_clash_api_port()
        engine_running = engine_manager.status().running
        project_listening = set(_listening_local_ports(mapped_ports())) if engine_running else set()
        ports: list[int] = []
        skipped: dict[str, dict] = {}
        port = start_port
        while len(ports) < count and port <= 65535:
            if port in excluded:
                port += 1
                continue
            diagnostic = port_diagnostic(
                port,
                clash_port=clash_port,
                engine_running=engine_running,
                project_listening=project_listening,
            )
            if diagnostic["available"]:
                ports.append(port)
            else:
                skipped[str(port)] = diagnostic
            port += 1
        if len(ports) < count:
            raise HTTPException(
                status_code=409,
                detail=f"Could not allocate {count} ports from {start_port}; found {len(ports)} available ports.",
            )
        return {"ports": ports, "skipped": skipped}

    def proxy_urls_for_ports(payload: ProxyAdminRequest, request: Request) -> list[dict]:
        selected_ports = payload.ports or mapped_ports()
        host = (payload.proxy_host or proxy_connect_host(request)).strip()
        replace_from = (payload.replace_from or "").strip()
        replace_to = (payload.replace_to or "").strip()
        name_prefix = (payload.proxy_name_prefix or "代理").strip() or "代理"
        by_tag = node_by_tag()

        def location_label(port: int) -> str:
            mapping = app_state.port_mappings.get(str(port))
            node = by_tag.get(mapping.node_tag) if mapping else None
            latency = app_state.latency_cache.get(node.tag) if node else None
            geoip = latency.geoip if latency else None
            if not geoip:
                exit_cache = app_state.exit_ip_cache.get(str(port))
                geoip = exit_cache.geoip if exit_cache else None
            if not geoip or geoip.get("error"):
                return "none"
            country = str(geoip.get("country_code") or geoip.get("country") or "").strip()
            city = str(geoip.get("city") or geoip.get("region") or "").strip()
            compact = ".".join(part.replace(" ", "") for part in [country, city] if part)
            return compact or "none"

        items = []
        for index, port in enumerate(selected_ports, start=1):
            export_host = replace_to if replace_from and replace_to and host == replace_from else host
            items.append(
                {
                    "url": f"http://{host}:{port}",
                    "host": export_host,
                    "port": int(port),
                    "name": f"{name_prefix}{index}",
                    "name_suffix": location_label(int(port)),
                }
            )
        return items

    async def wait_for_mapped_ports(timeout_seconds: float = 8.0) -> None:
        expected = mapped_ports()
        if not expected:
            return
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            listening = _listening_local_ports(expected)
            if len(listening) == len(expected):
                return
            await asyncio.sleep(0.2)

    def assigned_tags_for_ports(ports: list[int] | None = None) -> list[tuple[str, int]]:
        port_by_tag = {mapping.node_tag: int(port) for port, mapping in app_state.port_mappings.items()}
        requested_ports = set(ports or [])
        if requested_ports:
            return [
                (tag, port)
                for tag, port in port_by_tag.items()
                if port in requested_ports
            ]
        return list(port_by_tag.items())

    async def validate_assigned_tag(tag: str, port: int, urls: list[str] | None):
        targets = default_port_validation_urls(urls)
        if not engine_manager.status().running:
            target_results = [
                {
                    "url": url,
                    "ok": False,
                    "status_code": None,
                    "elapsed_ms": None,
                    "body_preview": "",
                    "error": "sing-box is not running",
                }
                for url in targets
            ]
            return tag, port, {
                "alive": False,
                "error": "sing-box is not running",
                "delay": None,
                "target_url": targets[0] if targets else None,
            }, {
                "node_tag": tag,
                "exit_ip": None,
                "targets": target_results,
            }
        target_results = await validate_proxy_targets(port, targets)
        ok_targets = [item for item in target_results if item.get("ok")]
        first_ok = ok_targets[0] if ok_targets else None
        first_target = first_ok or (target_results[0] if target_results else None)
        exit_ip = _extract_ip_from_targets(target_results)
        if not exit_ip and first_ok:
            try:
                exit_result = await asyncio.wait_for(query_exit_ip(port, app_state, engine_manager), timeout=3.0)
                exit_ip = exit_result.ip
                if exit_ip:
                    app_state.exit_ip_cache[str(port)] = exit_result
            except Exception:
                exit_ip = None
        geoip = geoip_payload_with_summaries(exit_ip) if exit_ip else None
        if exit_ip and not geoip:
            schedule_geoip_lookup(exit_ip)
        result = {
            "alive": bool(first_ok),
            "delay": first_ok.get("elapsed_ms") if first_ok else None,
            "exit_ip": exit_ip,
            "geoip": geoip,
            "test_port": None,
            "target_url": first_target.get("url") if first_target else None,
            "status_code": first_target.get("status_code") if first_target else None,
            "body_preview": first_target.get("body_preview") if first_target else None,
            "error": None if first_ok else _first_target_error(target_results),
        }
        return tag, port, result, {
            "node_tag": tag,
            "exit_ip": exit_ip,
            "geoip": geoip,
            "targets": target_results,
        }

    def store_port_test_result(tag: str, port: int | None, result: dict, detail: dict | None) -> None:
        result = enrich_result_dict(result)
        if tag in node_by_tag():
            app_state.latency_cache[tag] = LatencyResult.model_validate(result)
        if port and detail and detail.get("exit_ip"):
            geoip = geoip_payload_with_summaries(detail["exit_ip"])
            if geoip:
                detail["geoip"] = geoip
            app_state.exit_ip_cache[str(port)] = ExitIpCache(ip=detail["exit_ip"], geoip=geoip, error=None)
            schedule_geoip_lookup(detail["exit_ip"])

    @app.get("/", response_class=HTMLResponse)
    async def index():
        index_path = TEMPLATES_DIR / "index.html"
        if not index_path.exists():
            return HTMLResponse("<h1>Proxy Pool Manager</h1>")
        html = index_path.read_text(encoding="utf-8").replace("__ASSET_VERSION__", ASSET_VERSION)
        return HTMLResponse(html)

    @app.get("/api/auth/status")
    async def auth_status(request: Request):
        return auth_payload(request)

    @app.post("/api/auth/login")
    async def auth_login(payload: LoginRequest):
        if not auth_enabled():
            return {"enabled": False, "authenticated": True}
        expected = admin_key()
        if not hmac.compare_digest(payload.key or "", expected):
            raise HTTPException(status_code=401, detail="Invalid admin key")
        token = secrets.token_urlsafe(32)
        auth_sessions.add(token)
        response = JSONResponse({"enabled": True, "authenticated": True})
        response.set_cookie(
            auth_cookie_name(),
            token,
            httponly=True,
            samesite="lax",
            secure=auth_cookie_secure(),
            path="/",
        )
        return response

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request):
        token = request.cookies.get(auth_cookie_name())
        if token:
            auth_sessions.discard(token)
        response = JSONResponse({"enabled": auth_enabled(), "authenticated": False})
        response.delete_cookie(auth_cookie_name(), path="/")
        return response

    @app.get("/api/status")
    async def status(request: Request):
        refresh_state_from_disk()
        engine_status = engine_status_payload(request)
        return {
            "service": "ok",
            "store_warning": state_store.warning,
            "node_count": len(app_state.nodes),
            "mapping_count": len(app_state.port_mappings),
            "subscription": subscription_payload(),
            "web": {
                "pid": os.getpid(),
                "started_at": web_started_at,
                "asset_version": ASSET_VERSION,
            },
            "performance": performance.__dict__,
            "engine": engine_status,
            **engine_status,
        }

    @app.get("/api/subscription")
    async def get_subscription():
        refresh_state_from_disk()
        return subscription_payload()

    @app.put("/api/subscription")
    async def save_subscription(payload: SubscriptionConfigRequest):
        url = (payload.url or "").strip()
        interval = max(0, min(int(payload.refresh_interval_minutes or 0), 10080))
        app_state.subscription_url = url or None
        app_state.subscription_refresh_interval_minutes = interval
        app_state.subscription_last_error = None
        next_subscription_refresh_at["value"] = time.time() + interval * 60 if url and interval > 0 else None
        save()
        return subscription_payload()

    @app.post("/api/subscription/refresh")
    async def refresh_subscription():
        try:
            return await refresh_subscription_now()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/import")
    async def api_import(payload: ImportRequest):
        try:
            result = await import_nodes(url=payload.url, text=payload.text)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result.count == 0:
            detail = "No supported nodes were imported"
            if result.warnings:
                detail += ": " + "; ".join(result.warnings)
            raise HTTPException(status_code=400, detail=detail)
        existing = node_by_tag()
        for node in result.nodes:
            existing[node.tag] = node
        app_state.nodes = list(existing.values())
        if payload.url:
            app_state.subscription_url = payload.url
            app_state.subscription_last_refresh_at = utc_now_iso()
            app_state.subscription_last_error = None
            app_state.subscription_last_count = result.count
        save()
        return {"ok": True, **result.model_dump()}

    @app.get("/api/nodes")
    async def nodes():
        refresh_state_from_disk()
        return {
            "nodes": [
                {
                    **node.model_dump(exclude={"outbound"}),
                    "latency": attach_geoip_to_result(app_state.latency_cache.get(node.tag)).model_dump()
                    if node.tag in app_state.latency_cache
                    else None,
                }
                for node in app_state.nodes
            ]
        }

    @app.post("/api/nodes/delete")
    async def delete_nodes(payload: DeleteNodesRequest):
        if payload.all:
            remove_tags = {node.tag for node in app_state.nodes}
        else:
            remove_tags = set(payload.node_tags or [])
        if not remove_tags:
            return {"ok": True, "removed": 0}
        before = len(app_state.nodes)
        app_state.nodes = [node for node in app_state.nodes if node.tag not in remove_tags]
        app_state.port_mappings = {
            port: mapping
            for port, mapping in app_state.port_mappings.items()
            if mapping.node_tag not in remove_tags
        }
        app_state.latency_cache = {
            tag: result
            for tag, result in app_state.latency_cache.items()
            if tag not in remove_tags
        }
        assigned_tags = {mapping.node_tag for mapping in app_state.port_mappings.values()}
        app_state.exit_ip_cache = {
            port: cache
            for port, cache in app_state.exit_ip_cache.items()
            if app_state.port_mappings.get(port) and app_state.port_mappings[port].node_tag in assigned_tags
        }
        save()
        return {"ok": True, "removed": before - len(app_state.nodes)}

    @app.post("/api/test")
    async def test_nodes(payload: TestRequest):
        tags = payload.node_tags if payload.node_tags is not None else [node.tag for node in app_state.nodes]
        by_tag = node_by_tag()
        selected = [by_tag[tag] for tag in tags if tag in by_tag]
        missing = [tag for tag in tags if tag not in by_tag]
        results = {tag: {"alive": False, "error": "node not found", "delay": None} for tag in missing}
        try:
            tested = await test_nodes_with_temporary_engine(
                selected,
                target_url=payload.target_url,
                target_urls=payload.target_urls,
                include_exit_ip=payload.prune_same_ip or payload.include_geoip,
                concurrency=performance.max_node_test_concurrency,
                batch_size=performance.node_test_batch_size,
            )
        except EngineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for tag, result in tested.items():
            attach_geoip_to_result(result)
            app_state.latency_cache[tag] = result
            results[tag] = result.model_dump()
            schedule_geoip_lookup(result.exit_ip)
        removed = []
        if payload.prune_same_ip:
            app_state.nodes, removed = prune_same_exit_ip(app_state.nodes, app_state.latency_cache)
            kept_tags = {node.tag for node in app_state.nodes}
            app_state.port_mappings = {
                port: mapping
                for port, mapping in app_state.port_mappings.items()
                if mapping.node_tag in kept_tags
            }
            app_state.latency_cache = {
                tag: value
                for tag, value in app_state.latency_cache.items()
                if tag in kept_tags
            }
        app_state.nodes = sort_nodes_by_test_result(app_state.nodes, app_state.latency_cache)
        save()
        return {"results": results, "removed": removed, "node_count": len(app_state.nodes)}

    @app.post("/api/test/start")
    async def start_test_job(payload: TestRequest):
        if active_test_job["id"] is not None or test_job_lock.locked():
            raise HTTPException(status_code=409, detail="A node test is already running")
        cleanup_all_jobs()
        tags = payload.node_tags if payload.node_tags is not None else [node.tag for node in app_state.nodes]
        by_tag = node_by_tag()
        selected = [by_tag[tag] for tag in tags if tag in by_tag]
        missing = [tag for tag in tags if tag not in by_tag]
        job = TestJob(id=str(uuid.uuid4()), total=len(selected) + len(missing))
        for tag in missing:
            job.completed += 1
            job.results[tag] = {"alive": False, "error": "node not found", "delay": None}
        test_jobs[job.id] = job
        active_test_job["id"] = job.id
        asyncio.create_task(run_test_job(job.id, selected, payload.prune_same_ip, payload.include_geoip, payload.target_url, payload.target_urls))
        return job.model_dump()

    @app.get("/api/test/jobs/{job_id}")
    async def get_test_job(job_id: str):
        job = test_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Test job not found")
        return job.model_dump()

    @app.post("/api/test/jobs/{job_id}/cancel")
    async def cancel_test_job(job_id: str):
        job = test_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Test job not found")
        canceled_test_jobs.add(job_id)
        if job.status == "running":
            job.status = "canceling"
        return job.model_dump()

    async def run_test_job(job_id: str, selected, prune_same_ip: bool, include_geoip: bool, target_url: str | None, target_urls: list[str] | None) -> None:
        job = test_jobs[job_id]
        async with test_job_lock:
            try:
                def update(tag, result, detail: dict | None = None):
                    if job_id in canceled_test_jobs:
                        return
                    attach_geoip_to_result(result)
                    result_payload = result.model_dump()
                    if detail:
                        detail = dict(detail)
                        detail.setdefault("node_tag", tag)
                        if result.test_port is not None:
                            detail.setdefault("test_port", result.test_port)
                        detail.setdefault("engine_log_tail", tail_test_engine_log())
                        detail.setdefault("result", result_payload)
                        job.details[tag] = detail
                    app_state.latency_cache[tag] = result
                    job.results[tag] = result_payload
                    schedule_geoip_lookup(result.exit_ip)
                    job.completed += 1
                    touch_job(job)
                    save_later()

                assigned_port_by_tag = {
                    mapping.node_tag: int(port)
                    for port, mapping in app_state.port_mappings.items()
                }
                remaining = []
                assigned = []
                if engine_manager.status().running:
                    for node in selected:
                        assigned_port = assigned_port_by_tag.get(node.tag)
                        if assigned_port:
                            assigned.append((node, assigned_port))
                        else:
                            remaining.append(node)
                else:
                    remaining = selected

                if assigned and job_id not in canceled_test_jobs:
                    semaphore = asyncio.Semaphore(performance.max_node_test_concurrency)

                    async def validate_assigned_node(node, assigned_port: int):
                        async with semaphore:
                            if job_id in canceled_test_jobs:
                                return
                            _tag, _port, result, _detail = await validate_assigned_tag(
                                node.tag,
                                assigned_port,
                                target_urls or ([target_url] if target_url else None),
                            )
                            update(node.tag, LatencyResult.model_validate(result), _detail)

                    tasks = [
                        asyncio.create_task(validate_assigned_node(node, assigned_port))
                        for node, assigned_port in assigned
                    ]
                    await asyncio.gather(*tasks)
                if remaining and job_id not in canceled_test_jobs:
                    def on_temp_result(tag, result):
                        target_items = result.target_results or []
                        detail = {
                            "node_tag": tag,
                            "test_port": result.test_port,
                            "exit_ip": result.exit_ip,
                            "geoip": result.geoip,
                            "targets": target_items,
                            "engine_log_tail": tail_test_engine_log(),
                            "result": result.model_dump(),
                        }
                        update(tag, result, detail)

                    await test_nodes_with_temporary_engine(
                        remaining,
                        on_result=on_temp_result,
                        target_url=target_url,
                        target_urls=target_urls,
                        include_exit_ip=prune_same_ip or include_geoip,
                        should_cancel=lambda: job_id in canceled_test_jobs,
                        concurrency=performance.max_node_test_concurrency,
                        batch_size=performance.node_test_batch_size,
                    )
                if job_id in canceled_test_jobs:
                    job.status = "canceled"
                    touch_job(job)
                    return
                if include_geoip and performance.profile != "low":
                    ips = sorted({result.exit_ip for result in app_state.latency_cache.values() if result.exit_ip})
                    for ip in ips:
                        geoip = await enrich_geoip_now(ip)
                        if not geoip:
                            continue
                        for tag, result in app_state.latency_cache.items():
                            if result.exit_ip == ip:
                                result.geoip = geoip
                                if tag in job.results:
                                    job.results[tag] = result.model_dump()
                        save_later()
                if prune_same_ip:
                    app_state.nodes, job.removed = prune_same_exit_ip(app_state.nodes, app_state.latency_cache)
                    kept_tags = {node.tag for node in app_state.nodes}
                    app_state.port_mappings = {
                        port: mapping
                        for port, mapping in app_state.port_mappings.items()
                        if mapping.node_tag in kept_tags
                    }
                    app_state.latency_cache = {
                        tag: value
                        for tag, value in app_state.latency_cache.items()
                        if tag in kept_tags
                    }
                app_state.nodes = sort_nodes_by_test_result(app_state.nodes, app_state.latency_cache)
                job.status = "done"
                touch_job(job)
                save()
            except EngineError as exc:
                job.status = "error"
                job.error = str(exc)
                job.details["__engine__"] = {"engine_log_tail": tail_test_engine_log()}
                touch_job(job)
            except Exception as exc:
                job.status = "error"
                job.error = str(exc)
                job.details["__engine__"] = {"engine_log_tail": tail_test_engine_log()}
                touch_job(job)
            finally:
                await flush_save()
                if active_test_job["id"] == job_id:
                    active_test_job["id"] = None
                canceled_test_jobs.discard(job_id)

    @app.post("/api/test-ports")
    async def test_ports(payload: PortTestRequest | None = None):
        payload = payload or PortTestRequest()
        payload.urls = default_port_validation_urls(payload.urls)
        tag_ports = assigned_tags_for_ports(payload.ports)
        semaphore = asyncio.Semaphore(performance.max_port_test_concurrency)

        async def validate_one(tag: str, port: int):
            async with semaphore:
                return await validate_assigned_tag(tag, port, payload.urls)

        validated = await asyncio.gather(*(validate_one(tag, port) for tag, port in tag_ports))
        results = {}
        details = {}
        for tag, port, result, detail in validated:
            results[tag] = result
            if port and detail:
                details[str(port)] = detail
                store_port_test_result(tag, port, result, detail)
        save()
        return {"results": results, "details": details}

    @app.post("/api/test-ports/start")
    async def start_port_test_job(payload: PortTestRequest | None = None):
        payload = payload or PortTestRequest()
        payload.urls = default_port_validation_urls(payload.urls)
        if active_port_test_job["id"] is not None or port_test_job_lock.locked():
            raise HTTPException(status_code=409, detail="A port validation job is already running")
        cleanup_all_jobs()
        tag_ports = assigned_tags_for_ports(payload.ports)
        job = PortTestJob(id=str(uuid.uuid4()), total=len(tag_ports))
        port_test_jobs[job.id] = job
        active_port_test_job["id"] = job.id
        asyncio.create_task(run_port_test_job(job.id, tag_ports, payload.urls))
        return job.model_dump()

    @app.get("/api/test-ports/jobs/{job_id}")
    async def get_port_test_job(job_id: str):
        job = port_test_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Port validation job not found")
        return job.model_dump()

    @app.post("/api/test-ports/jobs/{job_id}/cancel")
    async def cancel_port_test_job(job_id: str):
        job = port_test_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Port validation job not found")
        canceled_port_test_jobs.add(job_id)
        if job.status == "running":
            job.status = "canceling"
        return job.model_dump()

    async def run_port_test_job(job_id: str, tag_ports: list[tuple[str, int]], urls: list[str] | None) -> None:
        job = port_test_jobs[job_id]
        async with port_test_job_lock:
            try:
                idx = 0
                worker_count = max(1, min(performance.max_port_test_concurrency, len(tag_ports) or 1))

                async def worker():
                    nonlocal idx
                    while idx < len(tag_ports):
                        current = idx
                        idx += 1
                        if job_id in canceled_port_test_jobs:
                            return
                        tag, port = tag_ports[current]
                        tag, port, result, detail = await validate_assigned_tag(tag, port, urls)
                        job.results[tag] = result
                        if port and detail:
                            job.details[str(port)] = detail
                            store_port_test_result(tag, port, result, detail)
                        job.completed += 1
                        touch_job(job)
                        save_later()

                await asyncio.gather(*(worker() for _ in range(worker_count)))
                if job_id in canceled_port_test_jobs:
                    job.status = "canceled"
                else:
                    job.status = "done"
                touch_job(job)
            except Exception as exc:
                if job_id in canceled_port_test_jobs:
                    job.status = "canceled"
                else:
                    job.status = "error"
                    job.error = str(exc)
                touch_job(job)
            finally:
                await flush_save()
                if active_port_test_job["id"] == job_id:
                    active_port_test_job["id"] = None
                canceled_port_test_jobs.discard(job_id)

    @app.put("/api/assign")
    async def assign(payload: AssignRequest):
        known = node_by_tag()
        mappings: dict[str, PortMapping] = {}
        for port_text, tag in payload.mappings.items():
            try:
                port = int(port_text)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Invalid port: {port_text}") from exc
            if port < 1024 or port > 65535:
                raise HTTPException(status_code=400, detail=f"Invalid port: {port}")
            if tag not in known:
                raise HTTPException(status_code=400, detail=f"Unknown node tag: {tag}")
            mappings[str(port)] = PortMapping(node_tag=tag)
        previous_mappings = app_state.port_mappings
        app_state.port_mappings = dict(sorted(mappings.items(), key=lambda item: int(item[0])))

        def same_port_node(port: str) -> bool:
            previous = previous_mappings.get(port)
            current = app_state.port_mappings.get(port)
            return bool(previous and current and previous.node_tag == current.node_tag)

        app_state.exit_ip_cache = {
            port: cache
            for port, cache in app_state.exit_ip_cache.items()
            if same_port_node(port)
        }
        app_state.local_proxy_check_results = {
            port: result
            for port, result in app_state.local_proxy_check_results.items()
            if same_port_node(port)
        }
        save()
        restarted = False
        stopped = False
        if engine_manager.status().running:
            if node_test_running():
                raise HTTPException(
                    status_code=409,
                    detail="A node test is running. Wait for it to finish before restarting the engine.",
                )
            if app_state.port_mappings:
                try:
                    config = generate_config(
                        app_state.nodes,
                        app_state.port_mappings,
                        include_clash_api=include_clash_api_for_next_start(),
                    )
                    _write_json_if_changed(SING_BOX_CONFIG_PATH, config)
                    await engine_manager.start(SING_BOX_CONFIG_PATH)
                    await wait_for_mapped_ports()
                    restarted = True
                except (ConfigError, EngineError) as exc:
                    raise HTTPException(status_code=400, detail=f"Port mappings saved, but engine restart failed: {exc}") from exc
            else:
                await engine_manager.stop()
                stopped = True
        return {
            "ok": True,
            "mappings": {key: value.model_dump() for key, value in app_state.port_mappings.items()},
            "engine_restarted": restarted,
            "engine_stopped": stopped,
            "engine": engine_status_payload(),
        }

    @app.get("/api/ports")
    async def ports():
        refresh_state_from_disk()
        by_tag = node_by_tag()
        response = {}
        for port, mapping in app_state.port_mappings.items():
            node = by_tag.get(mapping.node_tag)
            latency = app_state.latency_cache.get(mapping.node_tag)
            exit_cache = app_state.exit_ip_cache.get(port)
            exit_ip = exit_cache.ip if exit_cache else None
            geoip = exit_cache.geoip if exit_cache else None
            if exit_ip and not geoip:
                geoip = geoip_payload_with_summaries(exit_ip)
                if geoip and exit_cache:
                    exit_cache.geoip = geoip
                else:
                    schedule_geoip_lookup(exit_ip)
            response[port] = {
                "node_tag": mapping.node_tag,
                "node_name": node.name if node else None,
                "type": node.type if node else None,
                "exit_ip": exit_ip,
                "geoip": geoip,
                "latency": attach_geoip_to_result(latency).model_dump() if latency else None,
                "local_proxy_check": app_state.local_proxy_check_results.get(port),
            }
        local_checks = {
            port: app_state.local_proxy_check_results.get(port)
            for port in response
            if app_state.local_proxy_check_results.get(port)
        }
        return {"ports": response, "local_proxy_checks": local_checks}

    @app.post("/api/ports/check")
    async def check_ports(payload: PortAvailabilityRequest):
        checked = {}
        clash_port = configured_clash_api_port()
        engine_running = engine_manager.status().running
        project_listening = set(_listening_local_ports(mapped_ports())) if engine_running else set()
        for port in payload.ports:
            checked[str(port)] = port_diagnostic(
                port,
                clash_port=clash_port,
                engine_running=engine_running,
                project_listening=project_listening,
            )
        return {"ports": checked}

    @app.post("/api/ports/allocate")
    async def allocate_ports(payload: PortAllocateRequest):
        return allocate_available_ports(payload.start_port, payload.count, payload.exclude)

    @app.post("/api/proxy-admin/check/start")
    async def start_proxy_admin_check(payload: ProxyAdminRequest, request: Request):
        if active_proxy_admin_job["id"] is not None or proxy_admin_job_lock.locked():
            raise HTTPException(status_code=409, detail="A ProxyAdmin check job is already running")
        cleanup_all_jobs()
        proxy_items = proxy_urls_for_ports(payload, request)
        if payload.check_only:
            id_map = payload.proxy_ids_by_port or {}
            for item in proxy_items:
                item["id"] = int(id_map.get(str(item["port"])) or 0)
            missing = [str(item["port"]) for item in proxy_items if int(item.get("id") or 0) <= 0]
            if missing:
                raise HTTPException(status_code=400, detail=f"Missing ProxyAdmin ids for ports: {', '.join(missing)}")
        job = ProxyAdminJob(id=str(uuid.uuid4()))
        job.total = len(proxy_items)
        proxy_admin_jobs[job.id] = job
        active_proxy_admin_job["id"] = job.id
        asyncio.create_task(run_proxy_admin_job(job.id, payload, proxy_items))
        return job.model_dump()

    @app.get("/api/proxy-admin/config")
    async def get_proxy_admin_config():
        return proxy_admin_config_payload()

    @app.put("/api/proxy-admin/config")
    async def save_proxy_admin_config(payload: ProxyAdminConfig):
        config = load_app_config()
        config["proxy_admin"] = payload.model_dump()
        save_app_config(config)
        return {"ok": True, "config": proxy_admin_config_payload()}

    @app.post("/api/proxy-check")
    async def local_proxy_check(payload: LocalProxyCheckRequest):
        try:
            result = await run_local_proxy_check(payload)
            if payload.port:
                app_state.local_proxy_check_results[str(payload.port)] = result
                save()
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/proxy-check/start")
    async def start_local_proxy_check(payload: LocalProxyCheckStartRequest):
        if active_local_proxy_check_job["id"] is not None or local_proxy_check_job_lock.locked():
            raise HTTPException(status_code=409, detail="A local proxy check job is already running")
        cleanup_all_jobs()
        ports = [int(port) for port in (payload.ports or mapped_ports())]
        ports = sorted(dict.fromkeys(ports))
        if not ports:
            raise HTTPException(status_code=400, detail="No ports to check")
        job = LocalProxyCheckJob(id=str(uuid.uuid4()), total=len(ports))
        local_proxy_check_jobs[job.id] = job
        active_local_proxy_check_job["id"] = job.id
        asyncio.create_task(run_local_proxy_check_job(job.id, ports, payload.timeout, payload.concurrency))
        return job.model_dump()

    @app.get("/api/proxy-check/jobs/{job_id}")
    async def get_local_proxy_check_job(job_id: str):
        job = local_proxy_check_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Local proxy check job not found")
        return job.model_dump()

    @app.post("/api/proxy-check/jobs/{job_id}/cancel")
    async def cancel_local_proxy_check_job(job_id: str):
        job = local_proxy_check_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Local proxy check job not found")
        canceled_local_proxy_check_jobs.add(job_id)
        if job.status == "running":
            job.status = "canceling"
        return job.model_dump()

    async def run_local_proxy_check_job(job_id: str, ports: list[int], timeout: int, concurrency: int) -> None:
        job = local_proxy_check_jobs[job_id]
        async with local_proxy_check_job_lock:
            try:
                idx = 0
                worker_count = max(
                    1,
                    min(int(concurrency or 3), performance.max_proxycheck_concurrency, len(ports)),
                )

                async def worker():
                    nonlocal idx
                    while idx < len(ports):
                        if job_id in canceled_local_proxy_check_jobs:
                            return
                        port = ports[idx]
                        idx += 1
                        try:
                            result = await run_local_proxy_check(
                                LocalProxyCheckRequest(port=port, timeout=timeout)
                            )
                        except Exception as exc:
                            result = {
                                "id": port,
                                "proxy_url": f"http://127.0.0.1:{port}/",
                                "exit_ip": "",
                                "country": "",
                                "country_code": "",
                                "score": 0,
                                "grade": "ERR",
                                "summary": "本地检测失败",
                                "error": str(exc),
                                "items": [
                                    {
                                        "target": "base_connectivity",
                                        "status": "fail",
                                        "message": str(exc),
                                    }
                                ],
                            }
                        job.results[str(port)] = result
                        app_state.local_proxy_check_results[str(port)] = result
                        job.completed += 1
                        touch_job(job)
                        save_later()

                await asyncio.gather(*(worker() for _ in range(worker_count)))
                job.status = "canceled" if job_id in canceled_local_proxy_check_jobs else "done"
                touch_job(job)
            except Exception as exc:
                if job_id in canceled_local_proxy_check_jobs:
                    job.status = "canceled"
                else:
                    job.status = "error"
                    job.error = str(exc)
                touch_job(job)
            finally:
                await flush_save()
                if active_local_proxy_check_job["id"] == job_id:
                    active_local_proxy_check_job["id"] = None
                canceled_local_proxy_check_jobs.discard(job_id)

    @app.get("/api/proxy-admin/jobs/{job_id}")
    async def get_proxy_admin_job(job_id: str):
        job = proxy_admin_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="ProxyAdmin job not found")
        return job.model_dump()

    @app.post("/api/proxy-admin/jobs/{job_id}/cancel")
    async def cancel_proxy_admin_job(job_id: str):
        job = proxy_admin_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="ProxyAdmin job not found")
        canceled_proxy_admin_jobs.add(job_id)
        if job.status == "running":
            job.status = "canceling"
        return job.model_dump()

    async def run_proxy_admin_job(job_id: str, payload: ProxyAdminRequest, proxy_items: list[dict]) -> None:
        job = proxy_admin_jobs[job_id]
        async with proxy_admin_job_lock:
            try:
                effective_concurrency = max(
                    1,
                    min(int(payload.concurrency or 1), performance.max_proxy_admin_concurrency),
                )
                effective_payload = payload.model_copy(update={"concurrency": effective_concurrency})
                imported = proxy_items if effective_payload.check_only else await proxy_admin_import(effective_payload, proxy_items)
                job.imported = imported
                check_items = [item for item in imported if int(item.get("id") or 0) > 0]
                upload_failures = [item for item in imported if int(item.get("id") or 0) <= 0]
                job.total = len(imported)
                for item in upload_failures:
                    if job_id in canceled_proxy_admin_jobs:
                        job.status = "canceled"
                        return
                    result = {
                        "id": item.get("id") or 0,
                        "exit_ip": "",
                        "country": "",
                        "score": 0,
                        "grade": "ERR",
                        "items": [],
                        "error": item.get("upload_error") or "ProxyAdmin upload failed",
                    }
                    job.results[str(result["id"])] = result
                    job.completed += 1
                    touch_job(job)
                if not check_items:
                    job.status = "done"
                    touch_job(job)
                    return
                idx = 0
                concurrency = max(1, min(effective_payload.concurrency, len(check_items)))

                async def worker():
                    nonlocal idx
                    while idx < len(check_items):
                        if job_id in canceled_proxy_admin_jobs:
                            return
                        item = check_items[idx]
                        idx += 1
                        proxy_id = int(item.get("id") or 0)
                        try:
                            result = await proxy_admin_quality_check(effective_payload, proxy_id)
                        except Exception as exc:
                            result = {
                                "id": proxy_id,
                                "exit_ip": "",
                                "country": "",
                                "score": 0,
                                "grade": "ERR",
                                "items": [],
                                "error": str(exc),
                            }
                        job.results[str(proxy_id)] = result
                        job.completed += 1
                        touch_job(job)

                async with proxy_admin_client_scope():
                    await asyncio.gather(*(worker() for _ in range(concurrency)))
                job.status = "canceled" if job_id in canceled_proxy_admin_jobs else "done"
                touch_job(job)
            except Exception as exc:
                if job_id in canceled_proxy_admin_jobs:
                    job.status = "canceled"
                else:
                    job.status = "error"
                    job.error = str(exc)
                touch_job(job)
            finally:
                if active_proxy_admin_job["id"] == job_id:
                    active_proxy_admin_job["id"] = None
                canceled_proxy_admin_jobs.discard(job_id)

    @app.post("/api/proxy-admin/remove")
    async def remove_proxy_admin_items(payload: ProxyAdminRemoveRequest):
        ids = list(payload.ids or [])
        effective_concurrency = max(1, min(int(payload.concurrency or 1), performance.max_proxy_admin_concurrency))
        effective_payload = payload.model_copy(update={"concurrency": effective_concurrency})
        async with proxy_admin_client_scope():
            if payload.unused:
                proxy_map = await proxy_admin_list_all(effective_payload)
                ids = [
                    int(item.get("id"))
                    for item in proxy_map.values()
                    if int(item.get("account_count") or 0) == 0 and item.get("id")
                ]
            if not ids:
                return {"removed": [], "count": 0}
            results = await proxy_admin_remove_ids(effective_payload, ids)
        return {"removed": results, "count": len(results)}

    @app.get("/api/ports/{port}/ip")
    async def port_ip(port: int):
        if str(port) not in app_state.port_mappings:
            raise HTTPException(status_code=404, detail="Port mapping not found")
        result = await query_exit_ip(port, app_state, engine_manager)
        geoip = await enrich_geoip_now(result.ip) if result.ip else None
        result.geoip = geoip
        app_state.exit_ip_cache[str(port)] = result
        save()
        return {
            "port": port,
            "node_tag": app_state.port_mappings[str(port)].node_tag,
            "exit_ip": result.ip,
            "geoip": geoip,
            "geoip_summary": geoip_summary(geoip),
            "geoip_compact": geoip_compact_summary(geoip),
            "error": result.error,
        }

    @app.get("/api/proxy/fastest")
    async def fastest_proxy(
        request: Request,
        scheme: str = "http",
        require_running: bool = True,
        check: bool = False,
        target_url: str | None = None,
    ):
        refreshed = refresh_state_from_disk()
        scheme = scheme.lower().strip()
        if scheme not in {"http", "socks5"}:
            raise HTTPException(status_code=400, detail="scheme must be http or socks5")
        engine_status = engine_status_payload(request)
        if require_running and not engine_status["ready"]:
            raise HTTPException(status_code=409, detail="Engine is not running or mapped ports are not ready")
        by_tag = node_by_tag()
        candidates = []
        for port_text, mapping in app_state.port_mappings.items():
            latency = app_state.latency_cache.get(mapping.node_tag)
            if not latency or not latency.alive:
                continue
            try:
                port = int(port_text)
            except ValueError:
                continue
            success_count = sum(1 for item in latency.target_results or [] if item.get("ok"))
            if not latency.target_results and latency.alive:
                success_count = 1
            delay = latency.delay if latency.delay is not None else 10**9
            candidates.append((success_count, delay, port, mapping, latency))
        candidates = sorted(candidates, key=lambda item: (-item[0], item[1], item[2]))
        if not candidates:
            raise HTTPException(status_code=404, detail="No tested alive proxy mapping is available")
        if check:
            checked_candidates = []
            urls = [target_url] if target_url else None
            for _, _, port, mapping, _ in candidates:
                tag, checked_port, result, detail = await validate_assigned_tag(mapping.node_tag, port, urls)
                store_port_test_result(tag, checked_port, result, detail)
                latency = app_state.latency_cache.get(mapping.node_tag)
                if latency and latency.alive:
                    success_count = sum(1 for item in latency.target_results or [] if item.get("ok"))
                    if not latency.target_results:
                        success_count = 1
                    delay = latency.delay if latency.delay is not None else 10**9
                    checked_candidates.append((success_count, delay, port, mapping, latency))
            save()
            candidates = sorted(checked_candidates, key=lambda item: (-item[0], item[1], item[2]))
            if not candidates:
                raise HTTPException(status_code=404, detail="No live proxy mapping passed real-time validation")
        success_count, delay, port, mapping, latency = candidates[0]
        authority = proxy_authority(port, request)
        node = by_tag.get(mapping.node_tag)
        exit_cache = app_state.exit_ip_cache.get(str(port))
        return {
            "ok": True,
            "scheme": scheme,
            "proxy": f"{scheme}://{authority}",
            "http_proxy": f"http://{authority}",
            "socks5_proxy": f"socks5://{authority}",
            "host": proxy_connect_host(request),
            "port": port,
            "node_tag": mapping.node_tag,
            "node_name": node.name if node else None,
            "type": node.type if node else None,
            "node": node.model_dump(exclude={"outbound"}) if node else None,
            "delay": delay,
            "target_success_count": success_count,
            "real_time_checked": check,
            "check_target_url": target_url,
            "exit_ip": exit_cache.ip if exit_cache else latency.exit_ip,
            "geoip": exit_cache.geoip if exit_cache else latency.geoip,
            "latency": attach_geoip_to_result(latency).model_dump(),
            "engine": engine_status,
            "state_updated_at": app_state.updated_at,
            "state_refreshed": refreshed,
        }

    @app.post("/api/start")
    async def start():
        if not app_state.port_mappings:
            raise HTTPException(status_code=400, detail="No port mappings configured")
        if node_test_running():
            raise HTTPException(
                status_code=409,
                detail="A node test is running. Wait for it to finish before starting the engine.",
            )
        try:
            config = generate_config(
                app_state.nodes,
                app_state.port_mappings,
                include_clash_api=include_clash_api_for_next_start(),
            )
            config_written = _write_json_if_changed(SING_BOX_CONFIG_PATH, config)
            await engine_manager.start(SING_BOX_CONFIG_PATH)
            await wait_for_mapped_ports()
            return {"ok": True, "engine": engine_status_payload(), "config_written": config_written}
        except (ConfigError, EngineError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/stop")
    async def stop():
        await engine_manager.stop()
        return {"ok": True, "engine": engine_manager.status().model_dump()}

    @app.post("/api/doctor")
    async def doctor(payload: DoctorRequest | None = None):
        return await asyncio.to_thread(run_doctor_script, (payload or DoctorRequest()).timeout)

    app.state.proxy_pool_state = app_state
    app.state.proxy_pool_store = state_store
    app.state.proxy_pool_engine = engine_manager
    return app


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


def _write_json_if_changed(path, data: dict) -> bool:
    serialized = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        if path.exists() and path.read_text(encoding="utf-8") == serialized:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")
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


def run_doctor_script(timeout: int = 30) -> dict:
    timeout = max(5, min(int(timeout or 30), 120))
    command = _doctor_command()
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
            "fail": max(parsed["fail"], 1),
            "summary": f"doctor timed out after {timeout}s",
            "exit_code": None,
            "timed_out": True,
            "command": " ".join(command),
        }
    except OSError as exc:
        return {
            "ok": 0,
            "warn": 0,
            "fail": 1,
            "summary": str(exc),
            "lines": [f"[FAIL] doctor script could not run: {exc}"],
            "exit_code": None,
            "timed_out": False,
            "command": " ".join(command),
        }
