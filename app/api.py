from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import logging
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

logger = logging.getLogger("proxy_pool_manager")

from .engine import EngineError, EngineManager
from .jobs import JobManager
from .routes.auth import AUTH_SESSION_TTL_SECONDS, AuthContext, create_auth_router
from .engine import _can_bind_tcp_port
from .generator import ConfigError, generate_config, generate_pool_router_config
from .geoip import geoip_compact_summary, geoip_summary, lookup_geoip
from .models import (
    AppState,
    ExitIpCache,
    LatencyResult,
    NodeGroup,
    PoolMember,
    PortMapping,
    ProxyPool,
    SubscriptionSource,
    utc_now_iso,
)
from .pool_router import PoolRouterError, PoolRouterManager
from .traffic import TrafficStore
from .parser import import_nodes
from .schemas import (
    AssignRequest,
    DoctorRequest,
    DeleteNodesRequest,
    GeoIpConfig,
    ImportRequest,
    LocalProxyCheckJob,
    LocalProxyCheckRequest,
    LocalProxyCheckStartRequest,
    LoginRequest,
    PortAllocateRequest,
    PortAvailabilityRequest,
    PortTestJob,
    PortTestRequest,
    PoolDrainRequest,
    PoolEnabledRequest,
    PoolUpsertRequest,
    SubscriptionConfigRequest,
    SubscriptionSourceRequest,
    NodeGroupRequest,
    TestJob,
    TestRequest,
)
from .utils import (
    PRIMARY_TEST_URL,
    _doctor_command,
    _extract_ip_from_targets,
    _first_target_error,
    _listening_local_ports,
    _write_json_if_changed,
    default_port_validation_urls,
    run_doctor_script,
)
from .proxy_check import check_proxy_quality
from .settings import (
    APP_CONFIG_PATH,
    ASSET_VERSION,
    PORT,
    ROOT_DIR,
    SING_BOX_CONFIG_PATH,
    SING_BOX_TEST_CONFIG_PATH,
    POOL_ROUTER_CONFIG_PATH,
    STATIC_DIR,
    TEMPLATES_DIR,
    TRAFFIC_DB_PATH,
    current_clash_api_addr,
    current_performance_settings,
    current_pool_router_control_addr,
    current_proxy_listen_host,
    current_proxy_public_host,
)
from .store import StateStore, StateStoreError
from .tester import (
    measure_port_latency,
    prune_same_exit_ip,
    query_exit_ip,
    sort_nodes_by_test_result,
    test_nodes_with_temporary_engine,
    validate_proxy_targets,
)


def create_app(store: StateStore | None = None, engine: EngineManager | None = None) -> FastAPI:
    web_started_at = utc_now_iso()
    state_store = store or StateStore()
    app_state = state_store.load()
    state_loaded_mtime_ns = state_store.path.stat().st_mtime_ns if state_store.path.exists() else None
    engine_manager = engine or EngineManager(SING_BOX_CONFIG_PATH)
    pool_router = PoolRouterManager(POOL_ROUTER_CONFIG_PATH)
    traffic_store = TrafficStore(TRAFFIC_DB_PATH)
    performance = current_performance_settings()
    test_mgr = JobManager()
    test_jobs = test_mgr.jobs
    test_job_lock = test_mgr.lock
    active_test_job: dict[str, str | None] = {"id": None}
    canceled_test_jobs = test_mgr.canceled
    port_test_mgr = JobManager()
    port_test_jobs = port_test_mgr.jobs
    port_test_job_lock = port_test_mgr.lock
    active_port_test_job: dict[str, str | None] = {"id": None}
    canceled_port_test_jobs = port_test_mgr.canceled
    local_proxy_check_mgr = JobManager()
    local_proxy_check_jobs = local_proxy_check_mgr.jobs
    local_proxy_check_job_lock = local_proxy_check_mgr.lock
    active_local_proxy_check_job: dict[str, str | None] = {"id": None}
    canceled_local_proxy_check_jobs = local_proxy_check_mgr.canceled
    # token -> unix expiry timestamp
    auth_sessions: dict[str, float] = {}
    auth_failures: dict[str, list[float]] = {}
    geoip_tasks: dict[str, asyncio.Task] = {}
    geoip_semaphore = asyncio.Semaphore(performance.max_geoip_concurrency)
    subscription_refresh_lock = asyncio.Lock()
    next_subscription_refresh_at: dict[str, float | None] = {"value": None}
    subscription_next_refresh_at: dict[str, float] = {}

    monitor_task: asyncio.Task | None = None
    subscription_task: asyncio.Task | None = None
    job_cleanup_task: asyncio.Task | None = None
    traffic_task: asyncio.Task | None = None
    router_runtime_task: asyncio.Task | None = None
    engine_runtime_task: asyncio.Task | None = None
    router_listener_ports: set[int] = set()
    router_failed_listeners: list[dict] = []
    # Background-refreshed listening set so /api/status never does a full port
    # scan on the event loop (that freezes the web UI after many mappings).
    listening_ports_cache: dict[str, object] = {
        "ports": set(),
        "expected": [],
        "checked_at": None,
    }
    router_telemetry = {
        "telemetry_stale": False,
        "telemetry_error": None,
        "telemetry_checked_at": None,
        "telemetry_sampled_at": None,
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal monitor_task, subscription_task, job_cleanup_task, traffic_task, router_runtime_task, engine_runtime_task
        monitor = getattr(engine_manager, "monitor", None)
        if monitor and not monitor_task:
            monitor_task = asyncio.create_task(monitor(SING_BOX_CONFIG_PATH))
        if not subscription_task:
            subscription_task = asyncio.create_task(subscription_refresh_loop())
        if not job_cleanup_task:
            job_cleanup_task = asyncio.create_task(job_cleanup_loop())
        await refresh_engine_runtime()
        await refresh_pool_router_runtime()
        await refresh_listening_ports_cache()
        if not engine_runtime_task:
            engine_runtime_task = asyncio.create_task(engine_runtime_loop())
        if not router_runtime_task:
            router_runtime_task = asyncio.create_task(router_runtime_loop())
        if not traffic_task:
            traffic_task = asyncio.create_task(traffic_sampling_loop())
        try:
            yield
        finally:
            if monitor_task:
                monitor_task.cancel()
            if subscription_task:
                subscription_task.cancel()
            if job_cleanup_task:
                job_cleanup_task.cancel()
            if traffic_task:
                traffic_task.cancel()
            if engine_runtime_task:
                engine_runtime_task.cancel()
            if router_runtime_task:
                router_runtime_task.cancel()
            try:
                await engine_manager.stop(SING_BOX_CONFIG_PATH)
            except TypeError:
                # Test doubles may expose a zero-argument stop().
                await engine_manager.stop()
            except Exception:
                logger.exception("Failed to stop sing-box engine on shutdown")
            try:
                await pool_router.stop()
            except Exception:
                logger.exception("Failed to stop pool router on shutdown")
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

    def auth_trust_proxy() -> bool:
        value = os.environ.get("PPM_TRUST_PROXY") or str(load_app_config().get("trust_proxy") or "")
        return value.lower() in {"1", "true", "yes", "on"}

    def purge_expired_auth_sessions(now: float | None = None) -> None:
        current = time.time() if now is None else now
        expired = [token for token, expires_at in auth_sessions.items() if expires_at <= current]
        for token in expired:
            auth_sessions.pop(token, None)

    def is_authenticated(request: Request) -> bool:
        if not auth_enabled():
            return True
        token = request.cookies.get(auth_cookie_name())
        if not token:
            return False
        purge_expired_auth_sessions()
        expires_at = auth_sessions.get(token)
        if expires_at is None:
            return False
        if expires_at <= time.time():
            auth_sessions.pop(token, None)
            return False
        return True

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

    async def save_async() -> None:
        await asyncio.to_thread(save_now)

    def save() -> None:
        save_now()

    async def _run_debounced_save() -> None:
        try:
            await asyncio.sleep(performance.state_save_debounce_ms / 1000)
            if save_dirty["value"]:
                save_dirty["value"] = False
                try:
                    await asyncio.to_thread(save_now)
                except Exception:
                    # Keep the dirty flag so the next debounce retries the write.
                    save_dirty["value"] = True
                    logger.exception("Debounced state save failed")
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
            try:
                await asyncio.to_thread(save_now)
            except Exception:
                save_dirty["value"] = True
                logger.exception("Flush state save failed")

    def touch_job(job) -> None:
        job.touched_at = time.monotonic()

    def touch_test_job(job: TestJob, result_tag: str | None = None) -> None:
        job.revision += 1
        if result_tag:
            job.result_revisions[result_tag] = job.revision
        job.touched_at = time.monotonic()

    def tail_test_engine_log(lines: int = 40) -> list[str]:
        log_path = SING_BOX_TEST_CONFIG_PATH.with_suffix(".log")
        if not log_path.exists():
            return []
        try:
            # Read only the tail: this runs on the event loop once per test
            # result, and slurping a multi-MB log 100+ times per batch adds up.
            with open(log_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 64 * 1024))
                content = handle.read().decode("utf-8", errors="replace").splitlines()
        except Exception:
            return []
        return content[-max(1, lines):]

    def cleanup_all_jobs() -> None:
        retention = performance.job_retention_minutes * 60
        test_mgr.cleanup(active_test_job["id"], retention, performance.max_jobs_per_type)
        port_test_mgr.cleanup(active_port_test_job["id"], retention, performance.max_jobs_per_type)
        local_proxy_check_mgr.cleanup(active_local_proxy_check_job["id"], retention, performance.max_jobs_per_type)

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
        try:
            fresh = state_store.load()
        except StateStoreError:
            # Keep serving the last known-good state. A failed disk reload must
            # never replace active data with an empty state.
            state_loaded_mtime_ns = mtime_ns
            return False
        for field in AppState.model_fields:
            setattr(app_state, field, getattr(fresh, field))
        state_loaded_mtime_ns = mtime_ns
        return True

    def primary_subscription() -> SubscriptionSource | None:
        return app_state.subscription_sources[0] if app_state.subscription_sources else None

    def source_payload(source: SubscriptionSource) -> dict:
        next_at = subscription_next_refresh_at.get(source.id)
        return {
            **source.model_dump(),
            "next_refresh_in_seconds": max(0, int(next_at - time.time())) if next_at else None,
        }

    def subscription_payload() -> dict:
        source = primary_subscription()
        if source:
            payload = source_payload(source)
            return {
                "url": source.url,
                "refresh_interval_minutes": source.refresh_interval_minutes,
                "last_refresh_at": source.last_refresh_at,
                "last_error": source.last_error,
                "last_count": source.last_count,
                "next_refresh_in_seconds": payload["next_refresh_in_seconds"],
            }
        next_at = next_subscription_refresh_at["value"]
        return {
            "url": app_state.subscription_url or "",
            "refresh_interval_minutes": app_state.subscription_refresh_interval_minutes,
            "last_refresh_at": app_state.subscription_last_refresh_at,
            "last_error": app_state.subscription_last_error,
            "last_count": app_state.subscription_last_count,
            "next_refresh_in_seconds": max(0, int(next_at - time.time())) if next_at else None,
        }

    def source_display_name(url: str, fallback: str = "订阅") -> str:
        match = re.match(r"https?://([^/?#]+)", url, re.IGNORECASE)
        return match.group(1) if match else fallback

    def ensure_subscription_group(source: SubscriptionSource) -> NodeGroup:
        group = next((item for item in app_state.node_groups if item.id == source.group_id), None)
        if group:
            return group
        group = NodeGroup(
            name=source.name,
            kind="subscription",
            source_id=source.id,
        )
        source.group_id = group.id
        app_state.node_groups.append(group)
        return group

    def sync_legacy_subscription(source: SubscriptionSource | None = None) -> None:
        source = source or primary_subscription()
        if not source:
            app_state.subscription_url = None
            app_state.subscription_refresh_interval_minutes = 0
            app_state.subscription_last_refresh_at = None
            app_state.subscription_last_error = None
            app_state.subscription_last_count = 0
            next_subscription_refresh_at["value"] = None
            return
        app_state.subscription_url = source.url
        app_state.subscription_refresh_interval_minutes = source.refresh_interval_minutes
        app_state.subscription_last_refresh_at = source.last_refresh_at
        app_state.subscription_last_error = source.last_error
        app_state.subscription_last_count = source.last_count
        next_subscription_refresh_at["value"] = subscription_next_refresh_at.get(source.id)

    async def refresh_subscription_source(source: SubscriptionSource) -> dict:
        async with subscription_refresh_lock:
            try:
                result = await import_nodes(url=source.url)
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
                group = ensure_subscription_group(source)
                group.name = source.name
                group.node_tags = [node.tag for node in result.nodes]
                group.updated_at = utc_now_iso()
                source.last_refresh_at = utc_now_iso()
                source.last_error = None
                source.last_count = result.count
                source.updated_at = utc_now_iso()
                sync_legacy_subscription()
                await save_async()
                return {
                    **source_payload(source),
                    "imported": result.count,
                    "added": added,
                    "updated": updated,
                    "total_nodes": len(app_state.nodes),
                    "warnings": result.warnings,
                }
            except Exception as exc:
                source.last_refresh_at = utc_now_iso()
                source.last_error = str(exc)
                source.updated_at = utc_now_iso()
                sync_legacy_subscription()
                await save_async()
                raise

    async def refresh_subscription_now() -> dict:
        refresh_state_from_disk()
        source = primary_subscription()
        if not source:
            raise ValueError("Subscription URL is not configured")
        return await refresh_subscription_source(source)

    async def subscription_refresh_loop() -> None:
        while True:
            try:
                await _subscription_refresh_tick()
            except Exception:
                logger.exception("Subscription refresh loop iteration failed")
            await asyncio.sleep(15)

    async def _subscription_refresh_tick() -> None:
        refresh_state_from_disk()
        active_ids = {source.id for source in app_state.subscription_sources}
        for source_id in list(subscription_next_refresh_at):
            if source_id not in active_ids:
                subscription_next_refresh_at.pop(source_id, None)
        now = time.time()
        for source in app_state.subscription_sources:
            interval = int(source.refresh_interval_minutes or 0)
            if interval <= 0:
                subscription_next_refresh_at.pop(source.id, None)
                continue
            due_at = subscription_next_refresh_at.setdefault(source.id, now + interval * 60)
            if due_at > now:
                continue
            try:
                await refresh_subscription_source(source)
            except Exception:
                logger.warning("Auto refresh failed for subscription %s", source.id, exc_info=True)
            finally:
                subscription_next_refresh_at[source.id] = time.time() + interval * 60
        sync_legacy_subscription()

    # (path, mtime)-keyed cache so hot paths (auth middleware, status) do not
    # re-read and re-parse app.json from disk on every request.
    _app_config_cache: dict[str, object] = {"path": None, "mtime": None, "config": {}}

    def load_app_config() -> dict:
        path = APP_CONFIG_PATH
        if not path.exists():
            _app_config_cache.update(path=str(path), mtime=None, config={})
            return {}
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if (
            mtime is not None
            and _app_config_cache["path"] == str(path)
            and _app_config_cache["mtime"] == mtime
        ):
            return dict(_app_config_cache["config"])  # type: ignore[arg-type]
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(config, dict):
            config = {}
        _app_config_cache.update(path=str(path), mtime=mtime, config=dict(config))
        return dict(config)

    def save_app_config(config: dict) -> None:
        APP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(config, ensure_ascii=False, indent=2)
        tmp_path = APP_CONFIG_PATH.with_name(APP_CONFIG_PATH.name + ".tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, APP_CONFIG_PATH)
        _app_config_cache.update(path=None, mtime=None, config={})

    def auth_payload(request: Request) -> dict:
        return {
            "enabled": auth_enabled(),
            "authenticated": is_authenticated(request),
        }

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

    def runtime_ports() -> list[int]:
        return sorted({*mapped_ports(), *(pool.listen_port for pool in app_state.pools if pool.enabled)})

    def router_mode() -> bool:
        return pool_router.available()

    def write_runtime_configs() -> tuple[bool, bool]:
        use_router = router_mode()
        config = generate_config(
            app_state.nodes,
            app_state.port_mappings,
            pools=app_state.pools,
            router_mode=use_router,
            include_clash_api=include_clash_api_for_next_start(),
        )
        config_written = _write_json_if_changed(SING_BOX_CONFIG_PATH, config)
        router_written = False
        if use_router:
            router_written = _write_json_if_changed(
                POOL_ROUTER_CONFIG_PATH,
                generate_pool_router_config(
                    app_state.port_mappings,
                    app_state.pools,
                    unhealthy_node_tags={
                        tag
                        for tag, result in app_state.latency_cache.items()
                        if not result.alive
                    },
                ),
            )
        return config_written, router_written

    # Serialize full runtime restarts; concurrent restarts would race the
    # engine/router stop-start sequence and can strand orphan processes.
    runtime_restart_lock = asyncio.Lock()

    async def restart_runtime() -> tuple[bool, bool]:
        async with runtime_restart_lock:
            config_written, router_written = write_runtime_configs()
            await pool_router.stop()
            await engine_manager.start(SING_BOX_CONFIG_PATH)
            if router_mode():
                await pool_router.start(POOL_ROUTER_CONFIG_PATH)
                await refresh_router_listener_ports()
            await wait_for_runtime_ports()
            return config_written, router_written

    _configured_ports_cache: dict[str, object] = {"mtime": None, "ports": []}

    def configured_ports() -> list[int]:
        if not SING_BOX_CONFIG_PATH.exists():
            _configured_ports_cache["mtime"] = None
            _configured_ports_cache["ports"] = []
            return []
        try:
            mtime = SING_BOX_CONFIG_PATH.stat().st_mtime_ns
        except OSError:
            return list(_configured_ports_cache["ports"])  # type: ignore[arg-type]
        if _configured_ports_cache["mtime"] == mtime:
            return list(_configured_ports_cache["ports"])  # type: ignore[arg-type]
        try:
            config = json.loads(SING_BOX_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return list(_configured_ports_cache["ports"])  # type: ignore[arg-type]
        ports = sorted(
            item.get("listen_port")
            for item in config.get("inbounds", [])
            if isinstance(item, dict) and isinstance(item.get("listen_port"), int)
        )
        _configured_ports_cache["mtime"] = mtime
        _configured_ports_cache["ports"] = ports
        return list(ports)

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

    def cached_listening_ports(expected: list[int]) -> list[int]:
        """Return listening ports from the background cache (non-blocking)."""
        cached_ports = listening_ports_cache.get("ports") or set()
        if not isinstance(cached_ports, set):
            cached_ports = set(cached_ports)  # type: ignore[arg-type]
        return sorted(set(expected) & cached_ports)

    def engine_status_payload(request: Request | None = None) -> dict:
        payload = engine_manager.status().model_dump()
        payload["runtime_checked_at"] = getattr(engine_manager, "runtime_checked_at", None)
        expected = runtime_ports()
        config_ports = [] if not expected and not payload["running"] else configured_ports()
        router = pool_router.payload()
        if payload["running"] and router["running"]:
            listening = sorted(set(expected) & router_listener_ports)
        elif payload["running"] and router_mode():
            # Router mode with the router down: no public port is served, even
            # when a foreign process answers on one of them (a blind connect
            # probe once counted iCloud on 8081 as "1/314 listening").
            listening = []
        elif payload["running"]:
            # Never scan sockets on the request path; the runtime loop refreshes
            # listening_ports_cache every few seconds.
            listening = cached_listening_ports(expected)
        else:
            listening = []
        payload["expected_ports"] = expected
        payload["config_ports"] = config_ports
        payload["config_matches_state"] = bool(router_mode() and router["running"]) or config_ports == expected
        payload["listening_ports"] = listening
        payload["missing_ports"] = sorted(set(expected) - set(listening))
        payload["expected_count"] = len(expected)
        payload["listening_count"] = len(listening)
        payload["ready"] = bool(payload["running"]) and len(listening) == len(expected)
        payload["listening_checked_at"] = listening_ports_cache.get("checked_at")
        payload["failed_listeners"] = router_failed_listeners if (router_mode() and router["running"]) else []
        payload["proxy_listen_host"] = current_proxy_listen_host()
        payload["proxy_public_host"] = current_proxy_public_host()
        payload["proxy_connect_host"] = proxy_connect_host(request)
        payload["pool_router"] = router
        return payload

    async def refresh_listening_ports_cache() -> list[int]:
        """Probe expected ports off the event loop and publish to the cache."""
        expected = runtime_ports()
        if not expected or not engine_manager.status().running:
            listening_ports_cache["ports"] = set()
            listening_ports_cache["expected"] = expected
            listening_ports_cache["checked_at"] = time.time()
            return []
        if router_mode():
            if pool_router.payload()["running"]:
                await refresh_router_listener_ports()
                listening = set(expected) & router_listener_ports
            else:
                # Router down: a blind connect probe would attribute foreign
                # listeners on our public ports to this project.
                listening = set()
        else:
            listening = set(await asyncio.to_thread(_listening_local_ports, expected))
        listening_ports_cache["ports"] = listening
        listening_ports_cache["expected"] = list(expected)
        listening_ports_cache["checked_at"] = time.time()
        return sorted(listening)

    async def refresh_engine_runtime():
        refresh = getattr(engine_manager, "refresh_runtime_status", None)
        if not callable(refresh):
            return engine_manager.status()
        try:
            return await refresh()
        except Exception as exc:
            engine_manager.last_error = f"sing-box process discovery unavailable: {exc}"
            return engine_manager.status()

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

    def configured_web_port() -> int:
        config = load_app_config()
        raw = os.environ.get("PPM_PORT") or config.get("port") or PORT
        try:
            return int(raw)
        except (TypeError, ValueError):
            return int(PORT)

    def configured_pool_router_control_port() -> int | None:
        controller = current_pool_router_control_addr()
        match = re.search(r":(\d+)$", controller)
        if not match:
            return None
        return int(match.group(1))

    def reserved_ports() -> dict[int, tuple[str, str]]:
        """Return project-owned ports that must not be assigned as proxy ports.

        Values are (reason, label) pairs used by diagnostics and assign/pool validation.
        """
        reserved: dict[int, tuple[str, str]] = {
            configured_web_port(): ("reserved-web", "Web 管理台端口"),
        }
        clash_port = configured_clash_api_port()
        if clash_port is not None:
            reserved[clash_port] = ("reserved-clash-api", "Clash API 控制端口")
        control_port = configured_pool_router_control_port()
        if control_port is not None:
            reserved[control_port] = ("reserved-pool-router", "Pool Router 控制端口")
        return reserved

    def port_diagnostic(
        port: int,
        *,
        clash_port: int | None = None,
        engine_running: bool | None = None,
        project_listening: set[int] | None = None,
        reserved: dict[int, tuple[str, str]] | None = None,
    ) -> dict:
        if port < 1024 or port > 65535:
            return {"available": False, "reason": "invalid", "label": "端口无效"}
        if reserved is None:
            reserved = reserved_ports()
        # Keep the clash_port kwarg for older call sites/tests; merge into reserved map.
        if clash_port is not None and clash_port not in reserved:
            reserved = {
                **reserved,
                clash_port: ("reserved-clash-api", "Clash API 控制端口"),
            }
        if port in reserved:
            reason, label = reserved[port]
            return {"available": False, "reason": reason, "label": label}
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
        # Caller-provided excludes (UI drafts) plus every port this app already owns.
        # Ownership must be skipped even when the engine is stopped, otherwise auto
        # assign reuses mapped/pool ports that are only present in state, not DOM.
        excluded = {int(port) for port in exclude}
        owned_ports = set(runtime_ports())
        reserved = reserved_ports()
        engine_running = engine_manager.status().running
        project_listening = (
            set(cached_listening_ports(mapped_ports()))
            if engine_running
            else set()
        )
        ports: list[int] = []
        skipped: dict[str, dict] = {}
        port = start_port
        scanned = 0
        # Cap scan window so a pathological start port cannot walk the whole range.
        max_scan = max(count * 20, 512)
        while len(ports) < count and port <= 65535 and scanned < max_scan:
            scanned += 1
            if port in excluded:
                skipped.setdefault(
                    str(port),
                    {"available": False, "reason": "excluded", "label": "已排除"},
                )
                port += 1
                continue
            if port in owned_ports:
                skipped[str(port)] = {
                    "available": False,
                    "reason": "project-mapped",
                    "label": "已分配/池端口",
                }
                port += 1
                continue
            diagnostic = port_diagnostic(
                port,
                engine_running=engine_running,
                project_listening=project_listening,
                reserved=reserved,
            )
            if diagnostic["available"]:
                ports.append(port)
            else:
                skipped[str(port)] = diagnostic
            port += 1
        if len(ports) < count:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Could not allocate {count} ports from {start_port}; "
                    f"found {len(ports)} available ports after scanning {scanned}."
                ),
            )
        return {"ports": ports, "skipped": skipped}

    async def wait_for_runtime_ports(timeout_seconds: float = 8.0) -> None:
        expected = runtime_ports()
        if not expected:
            return
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if pool_router.payload()["running"]:
                await refresh_router_listener_ports()
                listening = sorted(set(expected) & router_listener_ports)
            elif router_mode():
                listening = []
            else:
                listening = await asyncio.to_thread(_listening_local_ports, expected)
            listening_ports_cache["ports"] = set(listening)
            listening_ports_cache["expected"] = list(expected)
            listening_ports_cache["checked_at"] = time.time()
            if len(listening) == len(expected):
                return
            await asyncio.sleep(0.2)

    async def refresh_router_listener_ports() -> dict:
        nonlocal router_listener_ports, router_failed_listeners
        status = await pool_router.status()
        listening: set[int] = set()
        for listener in status.get("listeners") or []:
            address = str(listener.get("listen") or "")
            try:
                listening.add(int(address.rsplit(":", 1)[-1]))
            except ValueError:
                continue
        router_listener_ports = listening
        router_failed_listeners = status.get("failed_listeners") or []
        return status

    async def refresh_pool_router_runtime() -> dict:
        try:
            return await pool_router.refresh_runtime_payload()
        except Exception as exc:
            pool_router.last_error = f"Pool router process discovery unavailable: {exc}"
            return pool_router.payload()

    def traffic_router_payload() -> dict:
        return {**pool_router.payload(), **router_telemetry}

    async def sample_router_traffic() -> None:
        if not pool_router.payload()["running"]:
            router_telemetry.update({
                "telemetry_stale": False,
                "telemetry_error": None,
                "telemetry_checked_at": int(time.time()),
            })
            return
        try:
            status = await refresh_router_listener_ports()
        except PoolRouterError as exc:
            router_telemetry.update({
                "telemetry_stale": True,
                "telemetry_error": str(exc),
                "telemetry_checked_at": int(time.time()),
            })
            return
        now = int(time.time())
        for listener in status.get("listeners") or []:
            listener_id = str(listener.get("id") or "")
            if listener_id.startswith("port-"):
                entity_type, entity_id = "port", listener_id.removeprefix("port-")
            elif listener_id.startswith("pool-"):
                entity_type, entity_id = "pool", listener_id.removeprefix("pool-")
            else:
                continue
            await asyncio.to_thread(
                traffic_store.sample,
                source_key=f"listener:{listener_id}", entity_type=entity_type, entity_id=entity_id,
                upload=int(listener.get("upload") or 0), download=int(listener.get("download") or 0),
                active=int(listener.get("active") or 0), selections=int(listener.get("selections") or 0), now=now,
            )
            for backend in listener.get("backends") or []:
                node_tag = str(backend.get("id") or "")
                if not node_tag:
                    continue
                await asyncio.to_thread(
                    traffic_store.sample,
                    source_key=f"backend:{listener_id}:{node_tag}", entity_type="node", entity_id=node_tag,
                    upload=int(backend.get("upload") or 0), download=int(backend.get("download") or 0),
                active=int(backend.get("active") or 0), selections=int(backend.get("selections") or 0), now=now,
            )

        router_telemetry.update({
            "telemetry_stale": False,
            "telemetry_error": None,
            "telemetry_checked_at": now,
            "telemetry_sampled_at": now,
        })

    router_monitor_failures = 0

    async def router_runtime_tick() -> None:
        nonlocal router_monitor_failures
        await refresh_pool_router_runtime()
        # The router must serve whenever the engine runs in router mode with
        # mapped ports. The previous poll-window check (`was_running and
        # process is None`) missed deaths between iterations and never healed.
        should_run = (
            router_mode()
            and engine_manager.status().running
            and bool(runtime_ports())
        )
        if not should_run or pool_router.payload()["running"]:
            router_monitor_failures = 0
            return
        if runtime_restart_lock.locked():
            # A full restart is already rewriting configs and restarting.
            return
        router_monitor_failures += 1
        if router_monitor_failures >= 3:
            # Slow down instead of giving up forever so the router can
            # still self-heal after transient crash storms.
            pool_router.last_error = "Pool router crashed repeatedly; retrying every 60s"
            logger.error("Pool router crashed %d times in a row; retry slowed to 60s", router_monitor_failures)
            await asyncio.sleep(55)
        try:
            # Regenerate configs from current state first so a stale or
            # corrupted pool-router.json on disk cannot resurrect the router
            # with the wrong listener set.
            write_runtime_configs()
            await pool_router.start(POOL_ROUTER_CONFIG_PATH)
            await refresh_router_listener_ports()
            router_monitor_failures = 0
        except (ConfigError, PoolRouterError):
            logger.warning("Pool router auto-restart failed", exc_info=True)

    async def router_runtime_loop() -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await router_runtime_tick()
            except Exception:
                logger.exception("Router runtime loop iteration failed")

    async def engine_runtime_loop() -> None:
        while True:
            try:
                await refresh_engine_runtime()
                await refresh_listening_ports_cache()
            except Exception:
                # Keep the loop alive; a single probe failure must not stop status updates.
                logger.warning("Engine runtime probe failed", exc_info=True)
            await asyncio.sleep(5)

    async def traffic_sampling_loop() -> None:
        while True:
            try:
                await sample_router_traffic()
            except PoolRouterError:
                pass
            except Exception:
                logger.warning("Traffic sampling failed", exc_info=True)
            await asyncio.sleep(15)

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
        asset_paths = [STATIC_DIR / "style.css", STATIC_DIR / "helpers.js", STATIC_DIR / "app.js"]
        asset_stamp = max((path.stat().st_mtime_ns for path in asset_paths if path.exists()), default=0)
        html = index_path.read_text(encoding="utf-8").replace("__ASSET_VERSION__", f"{ASSET_VERSION}-{asset_stamp}")
        return HTMLResponse(html)

    auth_ctx = AuthContext(
        sessions=auth_sessions,
        failures=auth_failures,
        auth_enabled=auth_enabled,
        admin_key=admin_key,
        cookie_name=auth_cookie_name,
        cookie_secure=auth_cookie_secure,
        trust_proxy=auth_trust_proxy,
        session_ttl_seconds=AUTH_SESSION_TTL_SECONDS,
    )
    app.include_router(create_auth_router(auth_ctx))

    @app.get("/api/status")
    async def status(request: Request):
        refresh_state_from_disk()
        engine_status = engine_status_payload(request)
        return {
            "service": "ok",
            "store_warning": state_store.warning,
            "node_count": len(app_state.nodes),
            "mapping_count": len(app_state.port_mappings),
            "pool_count": len(app_state.pools),
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

    @app.get("/api/engine/version")
    async def engine_version(check_latest: bool = False):
        try:
            return await engine_manager.binary_info(check_latest=check_latest)
        except EngineError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/engine/update")
    async def update_engine_binary():
        if node_test_running():
            raise HTTPException(status_code=409, detail="A node test is running")
        try:
            return await engine_manager.update_binary()
        except EngineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/engine/rollback")
    async def rollback_engine_binary():
        if node_test_running():
            raise HTTPException(status_code=409, detail="A node test is running")
        try:
            return await engine_manager.rollback_binary()
        except EngineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


    @app.get("/api/subscription")
    async def get_subscription():
        refresh_state_from_disk()
        return subscription_payload()

    @app.put("/api/subscription")
    async def save_subscription(payload: SubscriptionConfigRequest):
        url = (payload.url or "").strip()
        interval = max(0, min(int(payload.refresh_interval_minutes or 0), 10080))
        source = primary_subscription()
        if not url:
            if source:
                app_state.subscription_sources = [item for item in app_state.subscription_sources if item.id != source.id]
                app_state.node_groups = [item for item in app_state.node_groups if item.id != source.group_id]
                subscription_next_refresh_at.pop(source.id, None)
            sync_legacy_subscription()
            await save_async()
            return subscription_payload()
        if source is None:
            source = SubscriptionSource(name=source_display_name(url), url=url)
            app_state.subscription_sources.append(source)
            ensure_subscription_group(source)
        source.url = url
        source.refresh_interval_minutes = interval
        source.last_error = None
        source.updated_at = utc_now_iso()
        if interval > 0:
            subscription_next_refresh_at[source.id] = time.time() + interval * 60
        else:
            subscription_next_refresh_at.pop(source.id, None)
        sync_legacy_subscription()
        await save_async()
        return subscription_payload()

    @app.post("/api/subscription/refresh")
    async def refresh_subscription():
        try:
            return await refresh_subscription_now()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/subscriptions")
    async def list_subscription_sources():
        refresh_state_from_disk()
        return {"sources": [source_payload(source) for source in app_state.subscription_sources]}

    @app.post("/api/subscriptions")
    async def create_subscription_source(payload: SubscriptionSourceRequest):
        url = payload.url.strip()
        if any(item.url == url for item in app_state.subscription_sources):
            raise HTTPException(status_code=409, detail="This subscription URL already exists")
        source = SubscriptionSource(
            name=payload.name.strip(),
            url=url,
            refresh_interval_minutes=payload.refresh_interval_minutes,
        )
        app_state.subscription_sources.append(source)
        ensure_subscription_group(source)
        if source.refresh_interval_minutes:
            subscription_next_refresh_at[source.id] = time.time() + source.refresh_interval_minutes * 60
        sync_legacy_subscription()
        await save_async()
        return {"ok": True, "source": source_payload(source)}

    @app.put("/api/subscriptions/{source_id}")
    async def update_subscription_source(source_id: str, payload: SubscriptionSourceRequest):
        source = next((item for item in app_state.subscription_sources if item.id == source_id), None)
        if not source:
            raise HTTPException(status_code=404, detail="Subscription source not found")
        url = payload.url.strip()
        if any(item.id != source_id and item.url == url for item in app_state.subscription_sources):
            raise HTTPException(status_code=409, detail="This subscription URL already exists")
        source.name = payload.name.strip()
        source.url = url
        source.refresh_interval_minutes = payload.refresh_interval_minutes
        source.updated_at = utc_now_iso()
        group = ensure_subscription_group(source)
        group.name = source.name
        group.updated_at = utc_now_iso()
        if source.refresh_interval_minutes:
            subscription_next_refresh_at[source.id] = time.time() + source.refresh_interval_minutes * 60
        else:
            subscription_next_refresh_at.pop(source.id, None)
        sync_legacy_subscription()
        await save_async()
        return {"ok": True, "source": source_payload(source)}

    @app.delete("/api/subscriptions/{source_id}")
    async def delete_subscription_source(source_id: str):
        source = next((item for item in app_state.subscription_sources if item.id == source_id), None)
        if not source:
            raise HTTPException(status_code=404, detail="Subscription source not found")
        app_state.subscription_sources = [item for item in app_state.subscription_sources if item.id != source_id]
        app_state.node_groups = [item for item in app_state.node_groups if item.id != source.group_id]
        subscription_next_refresh_at.pop(source_id, None)
        sync_legacy_subscription()
        await save_async()
        return {"ok": True}

    @app.post("/api/subscriptions/{source_id}/refresh")
    async def refresh_subscription_source_api(source_id: str):
        source = next((item for item in app_state.subscription_sources if item.id == source_id), None)
        if not source:
            raise HTTPException(status_code=404, detail="Subscription source not found")
        try:
            return await refresh_subscription_source(source)
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
            source = next((item for item in app_state.subscription_sources if item.url == payload.url), None)
            if source is None:
                source = SubscriptionSource(name=source_display_name(payload.url), url=payload.url)
                app_state.subscription_sources.append(source)
            group = ensure_subscription_group(source)
            group.node_tags = [node.tag for node in result.nodes]
            group.updated_at = utc_now_iso()
            source.last_refresh_at = utc_now_iso()
            source.last_error = None
            source.last_count = result.count
            source.updated_at = utc_now_iso()
            sync_legacy_subscription()
        else:
            group = NodeGroup(
                name=f"粘贴导入 {time.strftime('%m/%d %H:%M')}",
                kind="import_batch",
                node_tags=[node.tag for node in result.nodes],
            )
            app_state.node_groups.append(group)
        await save_async()
        return {"ok": True, "group_id": group.id, **result.model_dump()}

    def group_payload(group: NodeGroup) -> dict:
        known_tags = {node.tag for node in app_state.nodes}
        tags = [tag for tag in group.node_tags if tag in known_tags]
        results = [app_state.latency_cache.get(tag) for tag in tags]
        healthy = [result for result in results if result and result.alive]
        failed = [result for result in results if result and not result.alive]
        delays = [result.delay for result in healthy if result.delay is not None]
        pool_count = sum(bool({member.node_tag for member in pool.members} & set(tags)) for pool in app_state.pools)
        return {
            **group.model_dump(),
            "node_tags": tags,
            "node_count": len(tags),
            "healthy_count": len(healthy),
            "failed_count": len(failed),
            "untested_count": len(tags) - len(healthy) - len(failed),
            "average_delay": round(sum(delays) / len(delays)) if delays else None,
            "pool_count": pool_count,
        }

    @app.get("/api/groups")
    async def list_node_groups():
        refresh_state_from_disk()
        return {"groups": [group_payload(group) for group in app_state.node_groups]}

    @app.post("/api/groups")
    async def create_node_group(payload: NodeGroupRequest):
        kind = payload.kind if payload.kind in {"manual", "quality_snapshot"} else "manual"
        tags = list(dict.fromkeys(payload.node_tags))
        missing = sorted(set(tags) - set(node_by_tag()))
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown group node: {', '.join(missing)}")
        group = NodeGroup(name=payload.name.strip(), kind=kind, node_tags=tags)
        app_state.node_groups.append(group)
        await save_async()
        return {"ok": True, "group": group_payload(group)}

    @app.put("/api/groups/{group_id}")
    async def update_node_group(group_id: str, payload: NodeGroupRequest):
        group = next((item for item in app_state.node_groups if item.id == group_id), None)
        if not group:
            raise HTTPException(status_code=404, detail="Node group not found")
        tags = list(dict.fromkeys(payload.node_tags))
        missing = sorted(set(tags) - set(node_by_tag()))
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown group node: {', '.join(missing)}")
        group.name = payload.name.strip()
        group.node_tags = tags
        group.updated_at = utc_now_iso()
        await save_async()
        return {"ok": True, "group": group_payload(group)}

    @app.delete("/api/groups/{group_id}")
    async def delete_node_group(group_id: str):
        group = next((item for item in app_state.node_groups if item.id == group_id), None)
        if not group:
            raise HTTPException(status_code=404, detail="Node group not found")
        if group.kind == "subscription":
            source = next((item for item in app_state.subscription_sources if item.id == group.source_id), None)
            if source:
                source.group_id = None
        app_state.node_groups = [item for item in app_state.node_groups if item.id != group_id]
        await save_async()
        return {"ok": True}

    @app.get("/api/nodes")
    async def nodes(
        page: int | None = None,
        page_size: int = 50,
        group_id: str | None = None,
        status: str | None = None,
        query: str | None = None,
        sort: str = "quality",
    ):
        refresh_state_from_disk()
        values = list(app_state.nodes)
        if group_id:
            group = next((item for item in app_state.node_groups if item.id == group_id), None)
            allowed = set(group.node_tags) if group else set()
            values = [node for node in values if node.tag in allowed]
        if status in {"alive", "failed", "untested"}:
            def status_matches(node):
                result = app_state.latency_cache.get(node.tag)
                return (
                    (status == "alive" and bool(result and result.alive))
                    or (status == "failed" and bool(result and not result.alive))
                    or (status == "untested" and result is None)
                )
            values = [node for node in values if status_matches(node)]
        normalized_query = (query or "").strip().lower()
        if normalized_query:
            values = [node for node in values if normalized_query in f"{node.name} {node.tag} {node.type} {node.server} {node.server_port}".lower()]
        if page is not None:
            if sort == "name":
                values.sort(key=lambda node: node.name.lower())
            else:
                values = sort_nodes_by_test_result(values, app_state.latency_cache)
        total = len(values)
        if page is not None:
            page = max(1, page)
            page_size = max(10, min(page_size, 100))
            start = (page - 1) * page_size
            values = values[start:start + page_size]
        return {
            "nodes": [
                {
                    **node.model_dump(exclude={"outbound"}),
                    "latency": attach_geoip_to_result(app_state.latency_cache.get(node.tag)).model_dump()
                    if node.tag in app_state.latency_cache
                    else None,
                }
                for node in values
            ],
            "pagination": {
                "page": page or 1,
                "page_size": page_size if page is not None else total,
                "total": total,
                "total_pages": max(1, (total + max(1, page_size) - 1) // max(1, page_size)) if page is not None else 1,
            },
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
        for pool in app_state.pools:
            pool.members = [member for member in pool.members if member.node_tag not in remove_tags]
            if not any(member.enabled and not member.draining for member in pool.members):
                pool.enabled = False
                pool.updated_at = utc_now_iso()
        for group in app_state.node_groups:
            group.node_tags = [tag for tag in group.node_tags if tag not in remove_tags]
            group.updated_at = utc_now_iso()
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
        await save_async()
        return {"ok": True, "removed": before - len(app_state.nodes)}

    @app.post("/api/test")
    async def test_nodes(payload: TestRequest):
        # Share the async job lock so sync and progressive tests never race on
        # the same temporary sing-box config path.
        if node_test_running():
            raise HTTPException(status_code=409, detail="A node test is already running")
        async with test_job_lock:
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
                for pool in app_state.pools:
                    pool.members = [member for member in pool.members if member.node_tag in kept_tags]
                    if not any(member.enabled and not member.draining for member in pool.members):
                        pool.enabled = False
                        pool.updated_at = utc_now_iso()
                for group in app_state.node_groups:
                    group.node_tags = [tag for tag in group.node_tags if tag in kept_tags]
                    group.updated_at = utc_now_iso()
            app_state.nodes = sort_nodes_by_test_result(app_state.nodes, app_state.latency_cache)
            await save_async()
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
            touch_test_job(job, tag)
        test_jobs[job.id] = job
        active_test_job["id"] = job.id
        asyncio.create_task(run_test_job(job.id, selected, payload.prune_same_ip, payload.include_geoip, payload.target_url, payload.target_urls))
        return job.model_dump()

    @app.get("/api/test/jobs/{job_id}")
    async def get_test_job(job_id: str, since: int = 0):
        job = test_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Test job not found")
        payload = job.model_dump()
        if since > 0:
            payload["results"] = {
                tag: result
                for tag, result in job.results.items()
                if job.result_revisions.get(tag, 0) > since
            }
            payload["details"] = {
                tag: detail
                for tag, detail in job.details.items()
                if job.result_revisions.get(tag, 0) > since
            }
        return payload

    @app.post("/api/test/jobs/{job_id}/cancel")
    async def cancel_test_job(job_id: str):
        job = test_mgr.request_cancel(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Test job not found")
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
                    touch_test_job(job, tag)
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
                    touch_test_job(job)
                    return
                if include_geoip and performance.profile != "low":
                    ips = sorted({result.exit_ip for result in app_state.latency_cache.values() if result.exit_ip})

                    # Look up IPs concurrently (bounded by the shared GeoIP
                    # semaphore); a serial loop once added minutes for large
                    # node sets with many distinct exit IPs.
                    async def enrich_one(ip: str) -> tuple[str, dict | None]:
                        async with geoip_semaphore:
                            return ip, await enrich_geoip_now(ip)

                    for ip, geoip in await asyncio.gather(*(enrich_one(ip) for ip in ips)):
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
                    for pool in app_state.pools:
                        pool.members = [member for member in pool.members if member.node_tag in kept_tags]
                        if not any(member.enabled and not member.draining for member in pool.members):
                            pool.enabled = False
                            pool.updated_at = utc_now_iso()
                    for group in app_state.node_groups:
                        group.node_tags = [tag for tag in group.node_tags if tag in kept_tags]
                        group.updated_at = utc_now_iso()
                app_state.nodes = sort_nodes_by_test_result(app_state.nodes, app_state.latency_cache)
                job.status = "done"
                touch_test_job(job)
                await save_async()
                if engine_manager.status().running and app_state.pools:
                    await restart_runtime()
                    await asyncio.to_thread(traffic_store.event, "pool_health_refreshed", "Applied latest node health to node pools")
            except EngineError as exc:
                job.status = "error"
                job.error = str(exc)
                job.details["__engine__"] = {"engine_log_tail": tail_test_engine_log()}
                touch_test_job(job)
            except Exception as exc:
                job.status = "error"
                job.error = str(exc)
                job.details["__engine__"] = {"engine_log_tail": tail_test_engine_log()}
                touch_test_job(job)
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
        await save_async()
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
        job = port_test_mgr.request_cancel(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Port validation job not found")
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
        reserved = reserved_ports()
        unavailable: list[str] = []
        for port_text in mappings:
            if port_text in previous_mappings:
                continue
            port = int(port_text)
            if port in reserved:
                unavailable.append(f"{port}（{reserved[port][1]}）")
                continue
            if not _can_bind_tcp_port(port):
                unavailable.append(f"{port}（系统不可绑定）")
        if unavailable:
            raise HTTPException(
                status_code=409,
                detail=f"以下端口不可用，无法分配：{', '.join(unavailable)}。请更换端口后重试。",
            )

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
        await save_async()
        restarted = False
        stopped = False
        if engine_manager.status().running:
            if node_test_running():
                raise HTTPException(
                    status_code=409,
                    detail="A node test is running. Wait for it to finish before restarting the engine.",
                )
            if runtime_ports():
                try:
                    await restart_runtime()
                    restarted = True
                except (ConfigError, EngineError, PoolRouterError) as exc:
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

    def pool_payload(pool: ProxyPool, request: Request | None = None) -> dict:
        by_tag = node_by_tag()
        members = []
        for member in pool.members:
            node = by_tag.get(member.node_tag)
            latency = app_state.latency_cache.get(member.node_tag)
            members.append(
                {
                    **member.model_dump(),
                    "node_name": node.name if node else None,
                    "node_type": node.type if node else None,
                    "alive": latency.alive if latency else None,
                    "delay": latency.delay if latency else None,
                }
            )
        authority = proxy_authority(pool.listen_port, request)
        return {
            **pool.model_dump(exclude={"members"}),
            "members": members,
            "http_proxy": f"http://{authority}",
            "socks5_proxy": f"socks5://{authority}",
        }

    def validate_pool_request(payload: PoolUpsertRequest, *, pool_id: str | None = None) -> ProxyPool:
        if payload.policy not in {"round_robin", "weighted_round_robin", "time_window"}:
            raise HTTPException(status_code=400, detail="Unsupported pool policy")
        known = node_by_tag()
        tags = [member.node_tag for member in payload.members]
        missing = sorted(set(tags) - set(known))
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown pool node: {', '.join(missing)}")
        if len(tags) != len(set(tags)):
            raise HTTPException(status_code=400, detail="A pool cannot include the same node twice")
        if payload.enabled and not any(member.enabled and not member.draining for member in payload.members):
            raise HTTPException(status_code=400, detail="An enabled pool requires at least one active member")
        occupied = {int(port) for port in app_state.port_mappings}
        occupied.update(pool.listen_port for pool in app_state.pools if pool.id != pool_id)
        if payload.listen_port in occupied:
            raise HTTPException(status_code=409, detail=f"Port {payload.listen_port} is already assigned")
        reserved = reserved_ports()
        if payload.listen_port in reserved:
            raise HTTPException(
                status_code=409,
                detail=f"Port {payload.listen_port} is reserved ({reserved[payload.listen_port][1]})",
            )
        now = utc_now_iso()
        old = next((pool for pool in app_state.pools if pool.id == pool_id), None)
        return ProxyPool(
            id=pool_id or ProxyPool(name=payload.name, listen_port=payload.listen_port).id,
            name=payload.name.strip(), listen_port=payload.listen_port, policy=payload.policy,
            rotation_interval_seconds=payload.rotation_interval_seconds,
            enabled=payload.enabled, members=payload.members,
            created_at=old.created_at if old else now, updated_at=now,
        )

    async def restart_for_pool_change() -> None:
        if not engine_manager.status().running:
            return
        if node_test_running():
            raise HTTPException(status_code=409, detail="A node test is running. Wait for it to finish before restarting the engine.")
        if runtime_ports():
            await restart_runtime()
        else:
            await pool_router.stop()
            await engine_manager.stop()

    def require_pool_router() -> None:
        if not pool_router.available():
            raise HTTPException(status_code=409, detail="Pool Router is not installed. Run the install script after installing Go.")

    @app.get("/api/pools")
    async def pools(request: Request):
        refresh_state_from_disk()
        return {"router": pool_router.payload(), "pools": [pool_payload(pool, request) for pool in app_state.pools]}

    @app.post("/api/pools")
    async def create_pool(payload: PoolUpsertRequest, request: Request):
        require_pool_router()
        pool = validate_pool_request(payload)
        app_state.pools.append(pool)
        await save_async()
        try:
            await restart_for_pool_change()
        except (ConfigError, EngineError, PoolRouterError) as exc:
            raise HTTPException(status_code=400, detail=f"Pool saved, but runtime restart failed: {exc}") from exc
        await asyncio.to_thread(traffic_store.event, "pool_created", f"Created pool {pool.name}", entity_type="pool", entity_id=pool.id)
        return {"ok": True, "pool": pool_payload(pool, request)}

    @app.put("/api/pools/{pool_id}")
    async def update_pool(pool_id: str, payload: PoolUpsertRequest, request: Request):
        require_pool_router()
        index = next((i for i, pool in enumerate(app_state.pools) if pool.id == pool_id), None)
        if index is None:
            raise HTTPException(status_code=404, detail="Pool not found")
        pool = validate_pool_request(payload, pool_id=pool_id)
        app_state.pools[index] = pool
        await save_async()
        try:
            await restart_for_pool_change()
        except (ConfigError, EngineError, PoolRouterError) as exc:
            raise HTTPException(status_code=400, detail=f"Pool saved, but runtime restart failed: {exc}") from exc
        await asyncio.to_thread(traffic_store.event, "pool_updated", f"Updated pool {pool.name}", entity_type="pool", entity_id=pool.id)
        return {"ok": True, "pool": pool_payload(pool, request)}

    @app.patch("/api/pools/{pool_id}/enabled")
    async def set_pool_enabled(pool_id: str, payload: PoolEnabledRequest, request: Request):
        require_pool_router()
        pool = next((item for item in app_state.pools if item.id == pool_id), None)
        if not pool:
            raise HTTPException(status_code=404, detail="Pool not found")
        if payload.enabled and not any(member.enabled and not member.draining for member in pool.members):
            raise HTTPException(status_code=400, detail="An enabled pool requires at least one active member")
        if pool.enabled == payload.enabled:
            return {"ok": True, "pool": pool_payload(pool, request)}
        pool.enabled = payload.enabled
        pool.updated_at = utc_now_iso()
        await save_async()
        try:
            await restart_for_pool_change()
        except (ConfigError, EngineError, PoolRouterError) as exc:
            raise HTTPException(status_code=400, detail=f"Pool state saved, but runtime update failed: {exc}") from exc
        action = "pool_enabled" if pool.enabled else "pool_disabled"
        message = f"Enabled pool {pool.name}" if pool.enabled else f"Disabled pool {pool.name}"
        await asyncio.to_thread(traffic_store.event, action, message, entity_type="pool", entity_id=pool.id)
        return {"ok": True, "pool": pool_payload(pool, request)}

    @app.post("/api/pools/{pool_id}/advance")
    async def advance_pool(pool_id: str):
        pool = next((item for item in app_state.pools if item.id == pool_id), None)
        if not pool:
            raise HTTPException(status_code=404, detail="Pool not found")
        try:
            result = await pool_router.advance(f"pool-{pool_id}")
        except PoolRouterError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await asyncio.to_thread(traffic_store.event, "pool_advanced", f"Advanced next selection for {pool.name}", entity_type="pool", entity_id=pool.id)
        return result

    @app.post("/api/pools/{pool_id}/members/{node_tag}/drain")
    async def drain_pool_member(pool_id: str, node_tag: str, payload: PoolDrainRequest):
        require_pool_router()
        pool = next((item for item in app_state.pools if item.id == pool_id), None)
        if not pool:
            raise HTTPException(status_code=404, detail="Pool not found")
        member = next((item for item in pool.members if item.node_tag == node_tag), None)
        if not member:
            raise HTTPException(status_code=404, detail="Pool member not found")
        member.draining = payload.draining
        pool.updated_at = utc_now_iso()
        if pool.enabled and not any(item.enabled and not item.draining for item in pool.members):
            raise HTTPException(status_code=400, detail="A pool requires at least one active member")
        await save_async()
        try:
            await restart_for_pool_change()
        except (ConfigError, EngineError, PoolRouterError) as exc:
            raise HTTPException(status_code=400, detail=f"Member updated, but runtime restart failed: {exc}") from exc
        action = "drained" if payload.draining else "reactivated"
        await asyncio.to_thread(traffic_store.event, "pool_member_state", f"{action}: {node_tag}", entity_type="pool", entity_id=pool.id)
        return {"ok": True, "pool": pool_payload(pool)}

    @app.delete("/api/pools/{pool_id}")
    async def delete_pool(pool_id: str):
        pool = next((item for item in app_state.pools if item.id == pool_id), None)
        if not pool:
            raise HTTPException(status_code=404, detail="Pool not found")
        app_state.pools = [item for item in app_state.pools if item.id != pool_id]
        await save_async()
        try:
            await restart_for_pool_change()
        except (ConfigError, EngineError, PoolRouterError) as exc:
            raise HTTPException(status_code=400, detail=f"Pool removed, but runtime restart failed: {exc}") from exc
        await asyncio.to_thread(traffic_store.event, "pool_deleted", f"Deleted pool {pool.name}", entity_type="pool", entity_id=pool.id)
        return {"ok": True}

    def traffic_seconds(value: str) -> int:
        ranges = {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}
        if value not in ranges:
            raise HTTPException(status_code=400, detail="Invalid traffic range")
        return ranges[value]

    @app.get("/api/traffic/overview")
    async def traffic_overview(range: str = "24h"):
        seconds = traffic_seconds(range)
        overview = await asyncio.to_thread(traffic_store.overview, seconds)
        recent = await asyncio.to_thread(traffic_store.overview, 120)
        overview.update({"range": range, "upload_rate": recent["upload"] / 120, "download_rate": recent["download"] / 120, "router": traffic_router_payload()})
        return overview

    @app.get("/api/traffic/entities")
    async def traffic_entities(type: str = "port", range: str = "24h"):
        if type not in {"port", "pool", "node"}:
            raise HTTPException(status_code=400, detail="Invalid traffic entity type")
        values = await asyncio.to_thread(traffic_store.entities, type, traffic_seconds(range))
        names = {node.tag: node.name for node in app_state.nodes}
        pools_by_id = {pool.id: pool for pool in app_state.pools}
        for item in values:
            if type == "port":
                mapping = app_state.port_mappings.get(item["entity_id"])
                item["name"] = names.get(mapping.node_tag) if mapping else item["entity_id"]
            elif type == "pool":
                item["name"] = pools_by_id.get(item["entity_id"]).name if item["entity_id"] in pools_by_id else item["entity_id"]
            else:
                item["name"] = names.get(item["entity_id"], item["entity_id"])
        return {"type": type, "range": range, "items": values}

    @app.get("/api/traffic/entities/{entity_type}/{entity_id}")
    async def traffic_entity(entity_type: str, entity_id: str, range: str = "24h"):
        if entity_type not in {"port", "pool", "node"}:
            raise HTTPException(status_code=400, detail="Invalid traffic entity type")
        return await asyncio.to_thread(traffic_store.detail, entity_type, entity_id, traffic_seconds(range))

    @app.get("/api/traffic/events")
    async def traffic_events(limit: int = 100, entity_type: str | None = None, entity_id: str | None = None):
        return {"items": await asyncio.to_thread(traffic_store.events, limit, entity_type, entity_id)}

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
        def run_check() -> dict:
            checked = {}
            reserved = reserved_ports()
            engine_running = engine_manager.status().running
            if not engine_running:
                project_listening: set[int] = set()
            elif router_mode():
                # Public ports are served by the router; a blind connect probe
                # would attribute foreign listeners to this project.
                project_listening = (
                    set(router_listener_ports) if pool_router.payload()["running"] else set()
                )
            else:
                # Fresh probe for explicit check requests; keep it off the event loop.
                project_listening = set(_listening_local_ports(mapped_ports()))
            owned = set(runtime_ports())
            for port in payload.ports:
                if port in owned and str(port) in app_state.port_mappings:
                    # Already mapped ports are "in use by this project" even when
                    # the engine is stopped (bind would otherwise look free).
                    if not engine_running or port not in project_listening:
                        if engine_running and not _can_bind_tcp_port(port):
                            # Mapped but not served and not bindable: a foreign
                            # process holds the port, so the runtime skipped it.
                            checked[str(port)] = {
                                "available": False,
                                "reason": "project-mapped-conflict",
                                "label": "已分配但被其他程序占用",
                            }
                        else:
                            checked[str(port)] = {
                                "available": False,
                                "reason": "project-mapped",
                                "label": "已分配端口",
                            }
                        continue
                checked[str(port)] = port_diagnostic(
                    port,
                    engine_running=engine_running,
                    project_listening=project_listening,
                    reserved=reserved,
                )
            return checked

        return {"ports": await asyncio.to_thread(run_check)}

    @app.post("/api/ports/allocate")
    async def allocate_ports(payload: PortAllocateRequest):
        return await asyncio.to_thread(
            allocate_available_ports,
            payload.start_port,
            payload.count,
            payload.exclude,
        )

    @app.post("/api/proxy-check")
    async def local_proxy_check(payload: LocalProxyCheckRequest):
        try:
            result = await run_local_proxy_check(payload)
            if payload.port:
                app_state.local_proxy_check_results[str(payload.port)] = result
                await save_async()
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
        job = local_proxy_check_mgr.request_cancel(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Local proxy check job not found")
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

    @app.get("/api/ports/{port}/ip")
    async def port_ip(port: int):
        if str(port) not in app_state.port_mappings:
            raise HTTPException(status_code=404, detail="Port mapping not found")
        result = await query_exit_ip(port, app_state, engine_manager)
        geoip = await enrich_geoip_now(result.ip) if result.ip else None
        result.geoip = geoip
        app_state.exit_ip_cache[str(port)] = result
        await save_async()
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
            await save_async()
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
        if not runtime_ports():
            detail = (
                "No active port mappings or pools configured"
                if not app_state.pools
                else "No active public port: enable a node pool or add a port mapping"
            )
            raise HTTPException(status_code=400, detail=detail)
        if node_test_running():
            raise HTTPException(
                status_code=409,
                detail="A node test is running. Wait for it to finish before starting the engine.",
        )
        try:
            config_written, router_written = await restart_runtime()
            return {"ok": True, "engine": engine_status_payload(), "config_written": config_written, "router_config_written": router_written}
        except (ConfigError, EngineError, PoolRouterError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/stop")
    async def stop():
        await pool_router.stop()
        await engine_manager.stop()
        return {"ok": True, "engine": engine_manager.status().model_dump()}

    @app.post("/api/doctor")
    async def doctor(payload: DoctorRequest | None = None):
        return await asyncio.to_thread(run_doctor_script, (payload or DoctorRequest()).timeout)

    app.state.proxy_pool_state = app_state
    app.state.proxy_pool_store = state_store
    app.state.proxy_pool_engine = engine_manager
    app.state.proxy_pool_router_tick = router_runtime_tick
    return app


def run_doctor_script(timeout: int = 30) -> dict:
    """Wrapper that delegates to utils.run_doctor_script but uses this module's
    _doctor_command so test monkeypatching of api_module._doctor_command works."""
    from .utils import run_doctor_script as _impl
    return _impl(timeout, command_fn=_doctor_command)
