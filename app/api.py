from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import re
import socket
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .engine import EngineError, EngineManager
from .generator import ConfigError, generate_config
from .models import AppState, ExitIpCache, LatencyResult, PortMapping
from .parser import import_nodes
from .settings import (
    SING_BOX_CONFIG_PATH,
    STATIC_DIR,
    TEMPLATES_DIR,
    current_proxy_listen_host,
    current_proxy_public_host,
)
from .store import StateStore
from .tester import (
    DEFAULT_VALIDATION_URLS,
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


class AssignRequest(BaseModel):
    mappings: dict[str, str]


class DeleteNodesRequest(BaseModel):
    node_tags: list[str] | None = None
    all: bool = False


class TestRequest(BaseModel):
    node_tags: list[str] | None = None
    prune_same_ip: bool = False
    target_url: str | None = None


class PortTestRequest(BaseModel):
    ports: list[int] | None = None
    urls: list[str] | None = None


class TestJob(BaseModel):
    id: str
    status: str = "running"
    total: int = 0
    completed: int = 0
    results: dict[str, dict] = {}
    removed: list[str] = []
    error: str | None = None


class PortTestJob(BaseModel):
    id: str
    status: str = "running"
    total: int = 0
    completed: int = 0
    results: dict[str, dict] = {}
    details: dict[str, dict] = {}
    error: str | None = None


def create_app(store: StateStore | None = None, engine: EngineManager | None = None) -> FastAPI:
    state_store = store or StateStore()
    app_state = state_store.load()
    engine_manager = engine or EngineManager(SING_BOX_CONFIG_PATH)
    test_jobs: dict[str, TestJob] = {}
    test_job_lock = asyncio.Lock()
    active_test_job: dict[str, str | None] = {"id": None}
    port_test_jobs: dict[str, PortTestJob] = {}
    port_test_job_lock = asyncio.Lock()
    active_port_test_job: dict[str, str | None] = {"id": None}

    monitor_task: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal monitor_task
        monitor = getattr(engine_manager, "monitor", None)
        if monitor and not monitor_task:
            monitor_task = asyncio.create_task(monitor(SING_BOX_CONFIG_PATH))
        try:
            yield
        finally:
            if monitor_task:
                monitor_task.cancel()

    app = FastAPI(title="Proxy Pool Manager", lifespan=lifespan)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def save() -> None:
        state_store.save(app_state)

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

    def engine_status_payload(request: Request | None = None) -> dict:
        payload = engine_manager.status().model_dump()
        expected = mapped_ports()
        config_ports = configured_ports()
        listening = _listening_local_ports(expected) if payload["running"] else []
        payload["expected_ports"] = expected
        payload["config_ports"] = config_ports
        payload["config_matches_state"] = config_ports == expected
        payload["listening_ports"] = listening
        payload["ready"] = bool(payload["running"]) and len(listening) == len(expected)
        payload["proxy_listen_host"] = current_proxy_listen_host()
        payload["proxy_public_host"] = current_proxy_public_host()
        payload["proxy_connect_host"] = proxy_connect_host(request)
        return payload

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
        targets = urls or DEFAULT_VALIDATION_URLS
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
        result = {
            "alive": bool(first_ok),
            "delay": first_ok.get("elapsed_ms") if first_ok else None,
            "exit_ip": exit_ip,
            "test_port": None,
            "target_url": first_target.get("url") if first_target else None,
            "status_code": first_target.get("status_code") if first_target else None,
            "body_preview": first_target.get("body_preview") if first_target else None,
            "error": None if first_ok else _first_target_error(target_results),
        }
        return tag, port, result, {
            "node_tag": tag,
            "exit_ip": exit_ip,
            "targets": target_results,
        }

    def store_port_test_result(tag: str, port: int | None, result: dict, detail: dict | None) -> None:
        if tag in node_by_tag():
            app_state.latency_cache[tag] = LatencyResult.model_validate(result)
        if port and detail and detail.get("exit_ip"):
            app_state.exit_ip_cache[str(port)] = ExitIpCache(ip=detail["exit_ip"], error=None)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        index_path = TEMPLATES_DIR / "index.html"
        if not index_path.exists():
            return HTMLResponse("<h1>Proxy Pool Manager</h1>")
        return HTMLResponse(index_path.read_text(encoding="utf-8"))

    @app.get("/api/status")
    async def status(request: Request):
        engine_status = engine_status_payload(request)
        return {
            "service": "ok",
            "store_warning": state_store.warning,
            "node_count": len(app_state.nodes),
            "mapping_count": len(app_state.port_mappings),
            "engine": engine_status,
            **engine_status,
        }

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
        save()
        return {"ok": True, **result.model_dump()}

    @app.get("/api/nodes")
    async def nodes():
        return {
            "nodes": [
                {
                    **node.model_dump(exclude={"outbound"}),
                    "latency": app_state.latency_cache.get(node.tag).model_dump()
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
            tested = await test_nodes_with_temporary_engine(selected, target_url=payload.target_url)
        except EngineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for tag, result in tested.items():
            app_state.latency_cache[tag] = result
            results[tag] = result.model_dump()
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
        asyncio.create_task(run_test_job(job.id, selected, payload.prune_same_ip, payload.target_url))
        return job.model_dump()

    @app.get("/api/test/jobs/{job_id}")
    async def get_test_job(job_id: str):
        job = test_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Test job not found")
        return job.model_dump()

    async def run_test_job(job_id: str, selected, prune_same_ip: bool, target_url: str | None) -> None:
        job = test_jobs[job_id]
        async with test_job_lock:
            try:
                def update(tag, result):
                    app_state.latency_cache[tag] = result
                    job.results[tag] = result.model_dump()
                    job.completed += 1

                await test_nodes_with_temporary_engine(selected, on_result=update, target_url=target_url)
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
                save()
                job.status = "done"
            except EngineError as exc:
                job.status = "error"
                job.error = str(exc)
            except Exception as exc:
                job.status = "error"
                job.error = str(exc)
            finally:
                if active_test_job["id"] == job_id:
                    active_test_job["id"] = None

    @app.post("/api/test-ports")
    async def test_ports(payload: PortTestRequest | None = None):
        payload = payload or PortTestRequest()
        tag_ports = assigned_tags_for_ports(payload.ports)
        validated = await asyncio.gather(*(validate_assigned_tag(tag, port, payload.urls) for tag, port in tag_ports))
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
        if active_port_test_job["id"] is not None or port_test_job_lock.locked():
            raise HTTPException(status_code=409, detail="A port validation job is already running")
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

    async def run_port_test_job(job_id: str, tag_ports: list[tuple[str, int]], urls: list[str] | None) -> None:
        job = port_test_jobs[job_id]
        async with port_test_job_lock:
            try:
                tasks = [
                    validate_assigned_tag(tag, port, urls)
                    for tag, port in tag_ports
                ]
                for completed in asyncio.as_completed(tasks):
                    tag, port, result, detail = await completed
                    job.results[tag] = result
                    if port and detail:
                        job.details[str(port)] = detail
                        store_port_test_result(tag, port, result, detail)
                    job.completed += 1
                    save()
                job.status = "done"
            except Exception as exc:
                job.status = "error"
                job.error = str(exc)
            finally:
                if active_port_test_job["id"] == job_id:
                    active_port_test_job["id"] = None

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
        app_state.port_mappings = mappings
        app_state.exit_ip_cache = {
            port: cache
            for port, cache in app_state.exit_ip_cache.items()
            if port in app_state.port_mappings
        }
        save()
        restarted = False
        stopped = False
        if engine_manager.status().running:
            if app_state.port_mappings:
                try:
                    config = generate_config(app_state.nodes, app_state.port_mappings)
                    SING_BOX_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                    SING_BOX_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
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
            "mappings": {key: value.model_dump() for key, value in mappings.items()},
            "engine_restarted": restarted,
            "engine_stopped": stopped,
            "engine": engine_status_payload(),
        }

    @app.get("/api/ports")
    async def ports():
        by_tag = node_by_tag()
        response = {}
        for port, mapping in app_state.port_mappings.items():
            node = by_tag.get(mapping.node_tag)
            latency = app_state.latency_cache.get(mapping.node_tag)
            response[port] = {
                "node_tag": mapping.node_tag,
                "node_name": node.name if node else None,
                "type": node.type if node else None,
                "exit_ip": app_state.exit_ip_cache.get(port).ip if port in app_state.exit_ip_cache else None,
                "latency": latency.model_dump() if latency else None,
            }
        return {"ports": response}

    @app.get("/api/ports/{port}/ip")
    async def port_ip(port: int):
        if str(port) not in app_state.port_mappings:
            raise HTTPException(status_code=404, detail="Port mapping not found")
        result = await query_exit_ip(port, app_state, engine_manager)
        app_state.exit_ip_cache[str(port)] = result
        save()
        return {
            "port": port,
            "node_tag": app_state.port_mappings[str(port)].node_tag,
            "exit_ip": result.ip,
            "error": result.error,
        }

    @app.post("/api/start")
    async def start():
        if not app_state.port_mappings:
            raise HTTPException(status_code=400, detail="No port mappings configured")
        try:
            config = generate_config(app_state.nodes, app_state.port_mappings)
            SING_BOX_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            SING_BOX_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            await engine_manager.start(SING_BOX_CONFIG_PATH)
            await wait_for_mapped_ports()
            return {"ok": True, "engine": engine_status_payload()}
        except (ConfigError, EngineError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/stop")
    async def stop():
        await engine_manager.stop()
        return {"ok": True, "engine": engine_manager.status().model_dump()}

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
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.05)
        return sock.connect_ex(("127.0.0.1", port)) == 0
