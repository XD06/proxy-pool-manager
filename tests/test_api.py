import asyncio
import json
from fastapi.testclient import TestClient
import pytest
import socket
import time

from app import api as api_module
from app.api import create_app
from app.models import AppState, EngineStatus, ExitIpCache, GeoIpResult, ImportResult, LatencyResult, PortMapping, ProxyNode
from app.settings import ASSET_VERSION, PerformanceSettings
from app.store import StateStore


# Path/process isolation lives in tests/conftest.py. This file additionally
# guarantees no test ever spawns the real Go router binary (tests here often
# flip available() to True, which once made an assign call fork the binary
# and clobber the production pool-router.json).
@pytest.fixture(autouse=True)
def never_spawn_pool_router(monkeypatch):
    async def _no_spawn_start(self, config_path=None):
        return None

    monkeypatch.setattr(api_module.PoolRouterManager, "start", _no_spawn_start)


class RunningEngine:
    def status(self):
        return EngineStatus(running=True, pid=1234)

    async def start(self, config_path):
        return None

    async def stop(self):
        return None


class StoppedEngine:
    def status(self):
        return EngineStatus(running=False)

    async def start(self, config_path):
        return None

    async def stop(self):
        return None


class RestartingEngine:
    def __init__(self):
        self.running = True
        self.started = []
        self.stopped = 0

    def status(self):
        return EngineStatus(running=self.running, pid=1234 if self.running else None)

    async def start(self, config_path):
        self.started.append(config_path)
        self.running = True

    async def stop(self):
        self.stopped += 1
        self.running = False


def test_api_import_assign_and_status(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    link = (
        "vless://00000000-0000-0000-0000-000000000000@example.com:443"
        "?security=tls#HK"
    )
    imported = client.post("/api/import", json={"text": link})

    assert imported.status_code == 200
    tag = imported.json()["nodes"][0]["tag"]

    nodes = client.get("/api/nodes")
    assert nodes.status_code == 200
    assert nodes.json()["nodes"][0]["tag"] == tag

    assigned = client.put("/api/assign", json={"mappings": {"8001": tag}})
    assert assigned.status_code == 200

    ports = client.get("/api/ports")
    assert ports.status_code == 200
    assert ports.json()["ports"]["8001"]["node_tag"] == tag

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["engine"]["running"] is False
    assert status.json()["node_count"] == 1
    assert status.json()["mapping_count"] == 1
    assert status.json()["web"]["asset_version"] == ASSET_VERSION
    assert status.json()["web"]["pid"] > 0
    assert status.json()["web"]["started_at"]
    assert status.json()["performance"]["profile"] in {"normal", "low"}


def test_api_keeps_last_known_good_state_when_disk_reload_fails(tmp_path):
    path = tmp_path / "assignments.json"
    store = StateStore(path)
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)
    imported = client.post(
        "/api/import",
        json={"text": "vless://00000000-0000-0000-0000-000000000000@example.com:443?security=tls#HK"},
    )
    assert imported.status_code == 200

    invalid = "{bad json"
    path.write_text(invalid, encoding="utf-8")

    first = client.get("/api/status")
    second = client.get("/api/status")

    assert first.status_code == 200
    assert first.json()["node_count"] == 1
    assert first.json()["store_warning"]
    assert second.status_code == 200
    assert second.json()["node_count"] == 1
    assert path.read_text(encoding="utf-8") == invalid
    assert len(list(tmp_path.glob("assignments.json.bak-*"))) == 1


def test_api_admin_auth_gate_and_login(tmp_path, monkeypatch):
    monkeypatch.setenv("PPM_ADMIN_KEY", "secret-key")
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    index = client.get("/")
    assert index.status_code == 200
    assert "authGate" in index.text

    blocked = client.get("/api/status")
    assert blocked.status_code == 401

    login = client.post("/api/auth/login", json={"key": "secret-key"})
    assert login.status_code == 200
    assert login.json()["authenticated"] is True
    set_cookie = login.headers.get("set-cookie", "")
    assert "Max-Age=" in set_cookie or "max-age=" in set_cookie.lower()

    allowed = client.get("/api/status")
    assert allowed.status_code == 200
    assert allowed.json()["engine"]["running"] is False

    logout = client.post("/api/auth/logout")
    assert logout.status_code == 200
    assert logout.json()["authenticated"] is False


def test_api_auth_rate_limit_uses_forwarded_for_when_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("PPM_ADMIN_KEY", "secret-key")
    monkeypatch.setenv("PPM_TRUST_PROXY", "1")
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    for _ in range(5):
        response = client.post(
            "/api/auth/login",
            json={"key": "wrong"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        assert response.status_code == 401

    locked = client.post(
        "/api/auth/login",
        json={"key": "wrong"},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert locked.status_code == 429

    # A different forwarded client must not share the lock bucket.
    other = client.post(
        "/api/auth/login",
        json={"key": "secret-key"},
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    assert other.status_code == 200


def test_index_injects_asset_version(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert f"style.css?v={ASSET_VERSION}-" in response.text
    assert f"app.js?v={ASSET_VERSION}-" in response.text
    assert "__ASSET_VERSION__" not in response.text


def test_api_delete_nodes_cleans_mappings(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store)
    client = TestClient(app)

    link = (
        "vless://00000000-0000-0000-0000-000000000000@example.com:443"
        "?security=tls#HK"
    )
    imported = client.post("/api/import", json={"text": link})
    tag = imported.json()["nodes"][0]["tag"]
    client.put("/api/assign", json={"mappings": {"8001": tag}})

    deleted = client.post("/api/nodes/delete", json={"node_tags": [tag]})

    assert deleted.status_code == 200
    assert deleted.json()["removed"] == 1
    assert client.get("/api/nodes").json()["nodes"] == []
    assert client.get("/api/ports").json()["ports"] == {}


def test_api_pool_crud_and_node_deletion_disables_empty_pool(tmp_path):
    # Pool CRUD is available once the installer has built the Go router.
    original_available = api_module.PoolRouterManager.available
    api_module.PoolRouterManager.available = lambda self: True
    store = StateStore(tmp_path / "assignments.json")
    try:
        app = create_app(store=store, engine=StoppedEngine())
        client = TestClient(app)
        imported = client.post(
            "/api/import",
            json={"text": "vless://00000000-0000-0000-0000-000000000000@example.com:443?security=tls#HK"},
        )
        tag = imported.json()["nodes"][0]["tag"]

        created = client.post(
            "/api/pools",
            json={"name": "轮询池", "listen_port": 8201, "policy": "weighted_round_robin", "members": [{"node_tag": tag, "weight": 2}]},
        )

        assert created.status_code == 200
        pool_id = created.json()["pool"]["id"]
        assert client.get("/api/status").json()["pool_count"] == 1
        assert client.get("/api/pools").json()["pools"][0]["http_proxy"].endswith(":8201")
        assert client.post(f"/api/pools/{pool_id}/members/{tag}/drain", json={"draining": True}).status_code == 400

        deleted = client.post("/api/nodes/delete", json={"node_tags": [tag]})
        assert deleted.status_code == 200
        assert store.load().pools[0].enabled is False
    finally:
        api_module.PoolRouterManager.available = original_available


def test_api_groups_and_paginated_nodes_keep_nodes_independent(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)
    imported = client.post(
        "/api/import",
        json={
            "text": "\n".join(
                [
                    "vless://00000000-0000-0000-0000-000000000001@one.example:443?security=tls#One",
                    "vless://00000000-0000-0000-0000-000000000002@two.example:443?security=tls#Two",
                ]
            )
        },
    )
    assert imported.status_code == 200
    tags = [item["tag"] for item in imported.json()["nodes"]]
    assert imported.json()["group_id"]

    created = client.post(
        "/api/groups",
        json={"name": "优选节点", "kind": "quality_snapshot", "node_tags": [tags[0]]},
    )
    assert created.status_code == 200
    group_id = created.json()["group"]["id"]
    assert created.json()["group"]["node_count"] == 1

    paged = client.get(f"/api/nodes?page=1&page_size=25&group_id={group_id}")
    assert paged.status_code == 200
    assert [item["tag"] for item in paged.json()["nodes"]] == [tags[0]]
    assert paged.json()["pagination"] == {"page": 1, "page_size": 25, "total": 1, "total_pages": 1}

    assert client.delete(f"/api/groups/{group_id}").status_code == 200
    assert len(client.get("/api/nodes").json()["nodes"]) == 2


def test_api_subscription_source_creates_source_group(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.post(
        "/api/subscriptions",
        json={"name": "备用订阅", "url": "https://example.com/sub", "refresh_interval_minutes": 30},
    )

    assert response.status_code == 200
    source = response.json()["source"]
    assert source["group_id"]
    assert source["next_refresh_in_seconds"] is not None
    groups = client.get("/api/groups").json()["groups"]
    assert groups[0]["kind"] == "subscription"
    assert groups[0]["source_id"] == source["id"]


def test_api_time_window_pool_policy_is_preserved(tmp_path):
    original_available = api_module.PoolRouterManager.available
    api_module.PoolRouterManager.available = lambda self: True
    try:
        store = StateStore(tmp_path / "assignments.json")
        app = create_app(store=store, engine=StoppedEngine())
        client = TestClient(app)
        imported = client.post(
            "/api/import",
            json={"text": "vless://00000000-0000-0000-0000-000000000000@example.com:443?security=tls#HK"},
        )
        tag = imported.json()["nodes"][0]["tag"]

        created = client.post(
            "/api/pools",
            json={
                "name": "固定出口池",
                "listen_port": 8202,
                "policy": "time_window",
                "rotation_interval_seconds": 900,
                "members": [{"node_tag": tag}],
            },
        )

        assert created.status_code == 200
        assert created.json()["pool"]["policy"] == "time_window"
        assert created.json()["pool"]["rotation_interval_seconds"] == 900
    finally:
        api_module.PoolRouterManager.available = original_available


def test_api_assign_can_clear_mappings(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    link = (
        "vless://00000000-0000-0000-0000-000000000000@example.com:443"
        "?security=tls#HK"
    )
    imported = client.post("/api/import", json={"text": link})
    tag = imported.json()["nodes"][0]["tag"]
    client.put("/api/assign", json={"mappings": {"8001": tag}})

    cleared = client.put("/api/assign", json={"mappings": {}})

    assert cleared.status_code == 200
    assert cleared.json()["mappings"] == {}
    assert client.get("/api/ports").json()["ports"] == {}


def test_api_assign_orders_mappings_by_port(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    imported = client.post(
        "/api/import",
        json={
            "text": "\n".join(
                [
                    "vless://00000000-0000-0000-0000-000000000001@example-a.com:443?security=tls#A",
                    "vless://00000000-0000-0000-0000-000000000002@example-b.com:443?security=tls#B",
                ]
            )
        },
    )
    tags = [node["tag"] for node in imported.json()["nodes"]]

    response = client.put("/api/assign", json={"mappings": {"8010": tags[0], "8001": tags[1]}})

    assert response.status_code == 200
    assert list(response.json()["mappings"].keys()) == ["8001", "8010"]
    assert list(store.load().port_mappings.keys()) == ["8001", "8010"]


def test_api_assign_clears_port_caches_when_node_changes(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    nodes = [
        ProxyNode(
            tag="node-a",
            name="A",
            type="vless",
            server="a.example.com",
            server_port=443,
            outbound={"type": "vless", "tag": "node-a", "server": "a.example.com", "server_port": 443},
        ),
        ProxyNode(
            tag="node-b",
            name="B",
            type="vless",
            server="b.example.com",
            server_port=443,
            outbound={"type": "vless", "tag": "node-b", "server": "b.example.com", "server_port": 443},
        ),
    ]
    store.save(
        AppState(
            nodes=nodes,
            port_mappings={
                "8001": PortMapping(node_tag="node-a"),
                "8003": PortMapping(node_tag="node-b"),
            },
            exit_ip_cache={
                "8001": ExitIpCache(ip="203.0.113.1"),
                "8003": ExitIpCache(ip="203.0.113.3"),
            },
            local_proxy_check_results={
                "8001": {"id": 8001, "grade": "A"},
                "8003": {"id": 8003, "grade": "B"},
            },
        )
    )
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.put("/api/assign", json={"mappings": {"8001": "node-a", "8002": "node-b"}})

    assert response.status_code == 200
    state = store.load()
    assert set(state.exit_ip_cache) == {"8001"}
    assert state.exit_ip_cache["8001"].ip == "203.0.113.1"
    assert state.local_proxy_check_results == {"8001": {"id": 8001, "grade": "A"}}


def test_api_import_zero_nodes_returns_error(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store)
    client = TestClient(app)

    response = client.post("/api/import", json={"text": "ssr://unsupported"})

    assert response.status_code == 400
    assert "No supported nodes" in response.json()["detail"]


def test_api_import_returns_structured_parser_diagnostics(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.post(
        "/api/import",
        json={
            "text": "\n".join([
                "vless://00000000-0000-0000-0000-000000000000@example.com:443?security=tls&type=xhttp#Unsupported",
                "vless://00000000-0000-0000-0000-000000000001@example.com:443?security=tls#Supported",
            ])
        },
    )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["diagnostics"] == [
        {
            "kind": "unsupported_transport",
            "scheme": "vless",
            "message": "Unsupported V2Ray transport: xhttp",
        }
    ]


def test_api_progressive_test_job(tmp_path, monkeypatch):
    seen = []

    async def fake_test_nodes(nodes, on_result=None, target_url=None, target_urls=None, include_exit_ip=False, **kwargs):
        seen.append(include_exit_ip)
        for node in nodes:
            result = LatencyResult(
                alive=True,
                delay=123,
                exit_ip="203.0.113.10" if include_exit_ip else None,
                test_port=19001,
                target_url=target_url,
                status_code=200 if target_url else None,
                body_preview="ok" if target_url else None,
            )
            if on_result:
                on_result(node.tag, result)
        return {}

    monkeypatch.setattr(api_module, "test_nodes_with_temporary_engine", fake_test_nodes)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store)
    client = TestClient(app)

    link = (
        "vless://00000000-0000-0000-0000-000000000000@example.com:443"
        "?security=tls#HK"
    )
    imported = client.post("/api/import", json={"text": link})
    tag = imported.json()["nodes"][0]["tag"]

    started = client.post("/api/test/start", json={"node_tags": [tag], "prune_same_ip": False})
    assert started.status_code == 200
    job_id = started.json()["id"]

    for _ in range(20):
        job = client.get(f"/api/test/jobs/{job_id}")
        if job.json()["status"] == "done":
            break
        time.sleep(0.05)
    assert job.status_code == 200
    assert job.json()["status"] == "done"
    assert job.json()["results"][tag]["alive"] is True
    assert job.json()["results"][tag]["exit_ip"] is None
    assert job.json()["details"][tag]["node_tag"] == tag
    assert "result" in job.json()["details"][tag]
    assert seen == [False]

    revision = job.json()["revision"]
    delta = client.get(f"/api/test/jobs/{job_id}?since={revision}")
    assert delta.status_code == 200
    assert delta.json()["revision"] == revision
    assert delta.json()["results"] == {}

def test_api_node_test_uses_regular_defaults(tmp_path, monkeypatch):
    seen = {}

    async def fake_test_nodes(nodes, **kwargs):
        seen.update(kwargs)
        return {node.tag: LatencyResult(alive=True, delay=123) for node in nodes}

    monkeypatch.setattr(api_module, "test_nodes_with_temporary_engine", fake_test_nodes)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store)
    client = TestClient(app)
    imported = client.post(
        "/api/import",
        json={"text": "vless://00000000-0000-0000-0000-000000000000@example.com:443?security=tls#HK"},
    )

    response = client.post("/api/test", json={"node_tags": [imported.json()["nodes"][0]["tag"]]})

    assert response.status_code == 200
    assert seen["target_url"] is None
    assert seen["target_urls"] is None
    assert seen["include_exit_ip"] is False
    assert "fast_transport_preflight" not in seen
    assert "fallback_on_failure" not in seen


def test_api_prune_node_test_includes_exit_ip(tmp_path, monkeypatch):
    seen = []

    async def fake_test_nodes(nodes, on_result=None, target_url=None, target_urls=None, include_exit_ip=False, **kwargs):
        seen.append(include_exit_ip)
        for node in nodes:
            result = LatencyResult(alive=True, delay=123, exit_ip="203.0.113.10", test_port=19001)
            if on_result:
                on_result(node.tag, result)
        return {}

    monkeypatch.setattr(api_module, "test_nodes_with_temporary_engine", fake_test_nodes)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store)
    client = TestClient(app)

    link = (
        "vless://00000000-0000-0000-0000-000000000000@example.com:443"
        "?security=tls#HK"
    )
    imported = client.post("/api/import", json={"text": link})
    tag = imported.json()["nodes"][0]["tag"]

    started = client.post("/api/test/start", json={"node_tags": [tag], "prune_same_ip": True})
    assert started.status_code == 200
    job_id = started.json()["id"]

    for _ in range(20):
        job = client.get(f"/api/test/jobs/{job_id}")
        if job.json()["status"] == "done":
            break
        time.sleep(0.05)
    assert job.json()["status"] == "done"
    assert seen == [True]


def test_api_node_test_include_geoip_requests_exit_ip(tmp_path, monkeypatch):
    seen = []

    async def fake_test_nodes(nodes, on_result=None, target_url=None, target_urls=None, include_exit_ip=False, **kwargs):
        seen.append(include_exit_ip)
        for node in nodes:
            result = LatencyResult(alive=True, delay=123, exit_ip="8.8.8.8" if include_exit_ip else None)
            if on_result:
                on_result(node.tag, result)
        return {}

    async def fake_lookup_geoip(ip, state, **kwargs):
        result = GeoIpResult(ip=ip, country="United States", country_code="US", city="Mountain View", asn="AS15169", org="Google")
        state.geoip_cache[ip] = result
        return result

    monkeypatch.setattr(api_module, "test_nodes_with_temporary_engine", fake_test_nodes)
    monkeypatch.setattr(api_module, "lookup_geoip", fake_lookup_geoip)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store)
    client = TestClient(app)

    imported = client.post(
        "/api/import",
        json={
            "text": (
                "vless://00000000-0000-0000-0000-000000000000@example.com:443"
                "?security=tls#HK"
            )
        },
    )
    tag = imported.json()["nodes"][0]["tag"]

    started = client.post("/api/test/start", json={"node_tags": [tag], "include_geoip": True})
    job_id = started.json()["id"]
    for _ in range(20):
        job = client.get(f"/api/test/jobs/{job_id}")
        if job.json()["status"] == "done":
            break
        time.sleep(0.05)

    assert job.json()["status"] == "done"
    assert seen == [True]
    assert job.json()["results"][tag]["exit_ip"] == "8.8.8.8"
    assert job.json()["results"][tag]["geoip"]["country_code"] == "US"


def test_api_node_test_passes_multiple_target_urls(tmp_path, monkeypatch):
    seen = []

    async def fake_test_nodes(nodes, on_result=None, target_url=None, target_urls=None, include_exit_ip=False, **kwargs):
        seen.append(target_urls)
        for node in nodes:
            result = LatencyResult(
                alive=True,
                delay=200,
                target_url=",".join(target_urls or []),
                target_results=[
                    {"url": target_urls[0], "ok": True, "status_code": 204, "elapsed_ms": 120},
                    {"url": target_urls[1], "ok": True, "status_code": 204, "elapsed_ms": 280},
                ],
            )
            if on_result:
                on_result(node.tag, result)
        return {}

    monkeypatch.setattr(api_module, "test_nodes_with_temporary_engine", fake_test_nodes)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store)
    client = TestClient(app)

    imported = client.post(
        "/api/import",
        json={
            "text": (
                "vless://00000000-0000-0000-0000-000000000000@example.com:443"
                "?security=tls#HK"
            )
        },
    )
    tag = imported.json()["nodes"][0]["tag"]
    targets = ["http://cp.cloudflare.com/generate_204", "https://www.gstatic.com/generate_204"]

    started = client.post("/api/test/start", json={"node_tags": [tag], "target_urls": targets})
    job_id = started.json()["id"]
    for _ in range(20):
        job = client.get(f"/api/test/jobs/{job_id}")
        if job.json()["status"] == "done":
            break
        time.sleep(0.05)

    assert job.json()["status"] == "done"
    assert seen == [targets]
    assert job.json()["results"][tag]["target_results"][1]["elapsed_ms"] == 280


def test_api_node_test_attaches_geoip_cache(tmp_path, monkeypatch):
    async def fake_test_nodes(nodes, on_result=None, target_url=None, target_urls=None, include_exit_ip=False, **kwargs):
        for node in nodes:
            result = LatencyResult(alive=True, delay=123, exit_ip="8.8.8.8", test_port=19001)
            if on_result:
                on_result(node.tag, result)
        return {}

    async def fake_lookup_geoip(ip, state, **kwargs):
        result = GeoIpResult(ip=ip, country="United States", country_code="US", city="Mountain View", asn="AS15169", org="Google")
        state.geoip_cache[ip] = result
        return result

    monkeypatch.setattr(api_module, "test_nodes_with_temporary_engine", fake_test_nodes)
    monkeypatch.setattr(api_module, "lookup_geoip", fake_lookup_geoip)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store)
    client = TestClient(app)

    imported = client.post(
        "/api/import",
        json={
            "text": (
                "vless://00000000-0000-0000-0000-000000000000@example.com:443"
                "?security=tls#US"
            )
        },
    )
    tag = imported.json()["nodes"][0]["tag"]

    started = client.post("/api/test/start", json={"node_tags": [tag], "prune_same_ip": True})
    job_id = started.json()["id"]
    for _ in range(20):
        job = client.get(f"/api/test/jobs/{job_id}")
        if job.json()["status"] == "done":
            break
        time.sleep(0.05)

    for _ in range(20):
        nodes = client.get("/api/nodes").json()["nodes"]
        geoip = nodes[0]["latency"].get("geoip")
        if geoip:
            break
        time.sleep(0.05)
    assert geoip["country_code"] == "US"
    assert geoip["asn"] == "AS15169"


def test_api_port_ip_returns_geoip(tmp_path, monkeypatch):
    async def fake_query_exit_ip(port, state, engine):
        return ExitIpCache(ip="8.8.4.4")

    async def fake_lookup_geoip(ip, state, **kwargs):
        result = GeoIpResult(ip=ip, country="United States", country_code="US", city="Mountain View", asn="AS15169", org="Google")
        state.geoip_cache[ip] = result
        return result

    monkeypatch.setattr(api_module, "query_exit_ip", fake_query_exit_ip)
    monkeypatch.setattr(api_module, "lookup_geoip", fake_lookup_geoip)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=RunningEngine())
    client = TestClient(app)

    imported = client.post(
        "/api/import",
        json={
            "text": (
                "vless://00000000-0000-0000-0000-000000000000@example.com:443"
                "?security=tls#US"
            )
        },
    )
    tag = imported.json()["nodes"][0]["tag"]
    client.put("/api/assign", json={"mappings": {"8001": tag}})

    response = client.get("/api/ports/8001/ip")

    assert response.status_code == 200
    payload = response.json()
    assert payload["exit_ip"] == "8.8.4.4"
    assert payload["geoip"]["country_code"] == "US"
    assert "AS15169" in payload["geoip_summary"]


def test_api_fastest_proxy_returns_best_alive_mapping(tmp_path, monkeypatch):
    # proxy_authority falls back to the request host only when the proxy binds a
    # wildcard address; pin that intent here so the assertion does not depend on
    # whatever proxy_listen_host the repo's config/app.json happens to hold.
    monkeypatch.setattr(api_module, "current_proxy_listen_host", lambda: "0.0.0.0")
    monkeypatch.setattr(api_module, "current_proxy_public_host", lambda: "")
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app, base_url="http://proxy.example.com:9000")

    imported = client.post(
        "/api/import",
        json={
            "text": "\n".join(
                [
                    "vless://00000000-0000-0000-0000-000000000001@a.example.com:443?security=tls#A",
                    "vless://00000000-0000-0000-0000-000000000002@b.example.com:443?security=tls#B",
                ]
            )
        },
    )
    tags = [node["tag"] for node in imported.json()["nodes"]]
    client.put("/api/assign", json={"mappings": {"8001": tags[0], "8002": tags[1]}})
    state = app.state.proxy_pool_state
    state.latency_cache[tags[0]] = LatencyResult(
        alive=True,
        delay=50,
        target_results=[
            {"url": "u1", "ok": True, "elapsed_ms": 50},
            {"url": "u2", "ok": False, "elapsed_ms": 100},
        ],
    )
    state.latency_cache[tags[1]] = LatencyResult(
        alive=True,
        delay=180,
        target_results=[
            {"url": "u1", "ok": True, "elapsed_ms": 160},
            {"url": "u2", "ok": True, "elapsed_ms": 200},
        ],
    )

    response = client.get("/api/proxy/fastest?require_running=false&scheme=socks5")

    assert response.status_code == 200
    payload = response.json()
    assert payload["port"] == 8002
    assert payload["scheme"] == "socks5"
    assert payload["proxy"] == "socks5://proxy.example.com:8002"
    assert payload["target_success_count"] == 2
    assert payload["engine"]["running"] is False
    assert payload["node"]["name"] == "B"


def test_api_start_reports_when_config_file_is_unchanged(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    imported = client.post(
        "/api/import",
        json={
            "text": (
                "vless://00000000-0000-0000-0000-000000000000@example.com:443"
                "?security=tls#HK"
            )
        },
    )
    tag = imported.json()["nodes"][0]["tag"]
    client.put("/api/assign", json={"mappings": {"8001": tag}})

    first = client.post("/api/start")
    second = client.post("/api/start")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["config_written"] is True
    assert second.json()["config_written"] is False


def test_api_fastest_proxy_refreshes_latest_state_from_disk(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app, base_url="http://proxy.example.com:9000")
    node = ProxyNode(
        tag="node-vless-latest",
        name="latest-node",
        type="vless",
        server="latest.example.com",
        server_port=443,
        outbound={"type": "vless", "tag": "node-vless-latest", "server": "latest.example.com", "server_port": 443},
    )
    time.sleep(0.01)
    store.save(
        AppState(
            nodes=[node],
            port_mappings={"8008": PortMapping(node_tag=node.tag)},
            latency_cache={node.tag: LatencyResult(alive=True, delay=88)},
        )
    )

    response = client.get("/api/proxy/fastest?require_running=false")

    assert response.status_code == 200
    payload = response.json()
    assert payload["port"] == 8008
    assert payload["node_name"] == "latest-node"
    assert payload["state_refreshed"] is True


def test_api_fastest_proxy_requires_ready_engine_by_default(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.get("/api/proxy/fastest")

    assert response.status_code == 409


def test_api_fastest_proxy_realtime_check_skips_failed_candidate(tmp_path, monkeypatch):
    async def fake_validate_proxy_targets(port, urls=None):
        if port == 8001:
            return [{"url": urls[0], "ok": False, "elapsed_ms": 20, "error": "failed"}]
        return [{"url": urls[0], "ok": True, "elapsed_ms": 77, "status_code": 204, "body_preview": "", "error": None}]

    monkeypatch.setattr(api_module, "validate_proxy_targets", fake_validate_proxy_targets)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=RunningEngine())
    client = TestClient(app, base_url="http://proxy.example.com:9000")

    imported = client.post(
        "/api/import",
        json={
            "text": "\n".join(
                [
                    "vless://00000000-0000-0000-0000-000000000001@a.example.com:443?security=tls#A",
                    "vless://00000000-0000-0000-0000-000000000002@b.example.com:443?security=tls#B",
                ]
            )
        },
    )
    tags = [node["tag"] for node in imported.json()["nodes"]]
    client.put("/api/assign", json={"mappings": {"8001": tags[0], "8002": tags[1]}})
    state = app.state.proxy_pool_state
    state.latency_cache[tags[0]] = LatencyResult(alive=True, delay=10)
    state.latency_cache[tags[1]] = LatencyResult(alive=True, delay=100)

    response = client.get("/api/proxy/fastest?require_running=false&check=true&target_url=https://example.com/ping")

    assert response.status_code == 200
    payload = response.json()
    assert payload["port"] == 8002
    assert payload["real_time_checked"] is True
    assert payload["latency"]["delay"] == 77


def test_api_port_check_reports_availability(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "_can_bind_tcp_port", lambda port: port == 18001)
    monkeypatch.setattr(api_module, "current_clash_api_addr", lambda: "127.0.0.1:10000")
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.post("/api/ports/check", json={"ports": [8001, 18001, 70000]})

    assert response.status_code == 200
    payload = response.json()["ports"]
    assert payload["8001"]["available"] is False
    assert payload["8001"]["reason"] == "busy"
    assert payload["18001"]["available"] is True
    assert payload["18001"]["reason"] == "available"
    assert payload["70000"]["available"] is False
    assert payload["70000"]["reason"] == "invalid"


def test_api_port_allocate_skips_busy_and_clash_ports(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "current_clash_api_addr", lambda: "127.0.0.1:10000")
    monkeypatch.setattr(api_module, "current_pool_router_control_addr", lambda: "127.0.0.1:9091")
    monkeypatch.setattr(api_module, "_can_bind_tcp_port", lambda port: port in {10002, 10003})
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.post(
        "/api/ports/allocate",
        json={"start_port": 10000, "count": 2, "exclude": [10001]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ports"] == [10002, 10003]
    assert payload["skipped"]["10000"]["reason"] == "reserved-clash-api"


def test_api_port_allocate_skips_already_mapped_ports_when_engine_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "current_clash_api_addr", lambda: "127.0.0.1:10000")
    monkeypatch.setattr(api_module, "current_pool_router_control_addr", lambda: "127.0.0.1:9091")
    monkeypatch.setattr(api_module, "_can_bind_tcp_port", lambda port: True)
    store = StateStore(tmp_path / "assignments.json")
    store.save(
        AppState(
            nodes=[
                ProxyNode(
                    tag="node-a",
                    name="A",
                    type="vless",
                    server="a.example.com",
                    server_port=443,
                    outbound={"type": "vless", "server": "a.example.com", "server_port": 443, "uuid": "u", "tag": "node-a"},
                ),
                ProxyNode(
                    tag="node-b",
                    name="B",
                    type="vless",
                    server="b.example.com",
                    server_port=443,
                    outbound={"type": "vless", "server": "b.example.com", "server_port": 443, "uuid": "u", "tag": "node-b"},
                ),
            ],
            port_mappings={"8001": PortMapping(node_tag="node-a")},
        )
    )
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.post(
        "/api/ports/allocate",
        json={"start_port": 8001, "count": 1, "exclude": []},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ports"] == [8002]
    assert payload["skipped"]["8001"]["reason"] == "project-mapped"


def test_api_status_does_not_block_on_port_scan(tmp_path, monkeypatch):
    """/api/status must stay fast even with many mapped ports (uses cache)."""
    calls = {"n": 0}

    def counting_listen(ports):
        calls["n"] += 1
        return list(ports)

    monkeypatch.setattr(api_module, "_listening_local_ports", counting_listen)
    store = StateStore(tmp_path / "assignments.json")
    mappings = {
        str(8000 + i): PortMapping(node_tag="node-a")
        for i in range(40)
    }
    store.save(
        AppState(
            nodes=[
                ProxyNode(
                    tag="node-a",
                    name="A",
                    type="vless",
                    server="a.example.com",
                    server_port=443,
                    outbound={"type": "vless", "server": "a.example.com", "server_port": 443, "uuid": "u", "tag": "node-a"},
                )
            ],
            port_mappings=mappings,
        )
    )
    app = create_app(store=store, engine=RunningEngine())
    client = TestClient(app)

    first = client.get("/api/status")
    assert first.status_code == 200
    # Request path must not call the socket scanner; cache starts empty until
    # the runtime loop / explicit refresh populates it.
    assert calls["n"] == 0
    assert first.json()["listening_ports"] == []


def test_api_assign_rejects_reserved_web_and_router_ports(tmp_path, monkeypatch):
    monkeypatch.setenv("PPM_PORT", "9000")
    monkeypatch.setattr(api_module, "current_clash_api_addr", lambda: "127.0.0.1:10000")
    monkeypatch.setattr(api_module, "current_pool_router_control_addr", lambda: "127.0.0.1:9091")
    monkeypatch.setattr(api_module, "_can_bind_tcp_port", lambda port: True)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    link = "vless://00000000-0000-0000-0000-000000000000@example.com:443?security=tls#HK"
    imported = client.post("/api/import", json={"text": link})
    tag = imported.json()["nodes"][0]["tag"]

    for port in (9000, 10000, 9091):
        response = client.put("/api/assign", json={"mappings": {str(port): tag}})
        assert response.status_code == 409, port
        detail = response.json()["detail"]
        assert "不可用" in detail or "reserved" in detail.lower() or "端口" in detail


def test_api_local_proxy_check_uses_proxycheck_adapter(tmp_path, monkeypatch):
    called = {}

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(api_module.socket, "create_connection", lambda *args, **kwargs: FakeConnection())

    async def fake_check_proxy_quality(proxy_url, proxy_id, timeout_seconds=30):
        called["args"] = (proxy_url, proxy_id, timeout_seconds)
        return {
            "id": proxy_id,
            "proxy_url": proxy_url,
            "exit_ip": "203.0.113.9",
            "country": "TEST",
            "score": 100,
            "grade": "A",
            "items": [{"target": "base_connectivity", "status": "pass", "latency_ms": 10}],
        }

    monkeypatch.setattr(api_module, "check_proxy_quality", fake_check_proxy_quality)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.post("/api/proxy-check", json={"port": 18007, "timeout": 20})

    assert response.status_code == 200
    assert response.json()["grade"] == "A"
    assert called["args"] == ("http://127.0.0.1:18007/", 18007, 20)
    assert store.load().local_proxy_check_results["18007"]["grade"] == "A"


def test_api_ports_returns_persisted_local_proxy_check(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    state = AppState(
        nodes=[
            ProxyNode(
                tag="node-a",
                name="A",
                type="vless",
                server="example.com",
                server_port=443,
                outbound={},
            )
        ],
        port_mappings={"18007": PortMapping(node_tag="node-a")},
        local_proxy_check_results={"18007": {"id": 18007, "grade": "A", "score": 100}},
    )
    store.save(state)
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.get("/api/ports")

    assert response.status_code == 200
    payload = response.json()
    assert payload["local_proxy_checks"]["18007"]["grade"] == "A"
    assert payload["ports"]["18007"]["local_proxy_check"]["score"] == 100


def test_api_local_proxy_check_reports_closed_port(tmp_path, monkeypatch):
    def fake_create_connection(*args, **kwargs):
        raise OSError("connection refused")

    async def fake_check_proxy_quality(proxy_url, proxy_id, timeout_seconds=30):
        raise AssertionError("closed local port should not run proxycheck")

    monkeypatch.setattr(api_module.socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(api_module, "check_proxy_quality", fake_check_proxy_quality)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.post("/api/proxy-check", json={"port": 18007, "timeout": 20})

    assert response.status_code == 200
    payload = response.json()
    assert payload["grade"] == "ERR"
    assert payload["score"] == 0
    assert payload["items"][0]["status"] == "fail"
    assert "未监听" in payload["items"][0]["message"]


def test_api_local_proxy_check_job_streams_results(tmp_path, monkeypatch):
    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(api_module.socket, "create_connection", lambda *args, **kwargs: FakeConnection())

    async def fake_check_proxy_quality(proxy_url, proxy_id, timeout_seconds=30):
        await asyncio.sleep(0)
        return {
            "id": proxy_id,
            "proxy_url": proxy_url,
            "exit_ip": f"203.0.113.{proxy_id - 18000}",
            "country": "TEST",
            "score": 100,
            "grade": "A",
            "items": [{"target": "base_connectivity", "status": "pass", "latency_ms": 10}],
        }

    monkeypatch.setattr(api_module, "check_proxy_quality", fake_check_proxy_quality)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())

    with TestClient(app) as client:
        started = client.post(
            "/api/proxy-check/start",
            json={"ports": [18002, 18001], "timeout": 20, "concurrency": 2},
        )
        assert started.status_code == 200
        job_id = started.json()["id"]
        for _ in range(20):
            job = client.get(f"/api/proxy-check/jobs/{job_id}")
            if job.json()["status"] == "done":
                break
            time.sleep(0.05)

        payload = job.json()
        assert payload["status"] == "done"
        assert payload["total"] == 2
        assert payload["completed"] == 2
        assert payload["results"]["18001"]["grade"] == "A"
        assert payload["results"]["18002"]["exit_ip"] == "203.0.113.2"


def test_api_local_proxy_check_job_can_be_canceled(tmp_path, monkeypatch):
    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(api_module.socket, "create_connection", lambda *args, **kwargs: FakeConnection())

    async def fake_check_proxy_quality(proxy_url, proxy_id, timeout_seconds=30):
        await asyncio.sleep(0.05)
        return {
            "id": proxy_id,
            "proxy_url": proxy_url,
            "exit_ip": "",
            "country": "",
            "score": 100,
            "grade": "A",
            "items": [{"target": "base_connectivity", "status": "pass"}],
        }

    monkeypatch.setattr(api_module, "check_proxy_quality", fake_check_proxy_quality)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())

    with TestClient(app) as client:
        started = client.post(
            "/api/proxy-check/start",
            json={"ports": [18001, 18002, 18003], "timeout": 20, "concurrency": 1},
        )
        job_id = started.json()["id"]
        canceled = client.post(f"/api/proxy-check/jobs/{job_id}/cancel")
        assert canceled.status_code == 200
        assert canceled.json()["status"] in {"running", "canceling"}

        for _ in range(20):
            job = client.get(f"/api/proxy-check/jobs/{job_id}")
            if job.json()["status"] == "canceled":
                break
            time.sleep(0.05)

        payload = job.json()
    assert payload["status"] == "canceled"
    assert payload["completed"] < payload["total"]


def test_local_proxy_check_job_debounces_state_saves(tmp_path, monkeypatch):
    class CountingStore(StateStore):
        def __init__(self, path):
            super().__init__(path)
            self.save_count = 0

        def save(self, state):
            self.save_count += 1
            super().save(state)

    monkeypatch.setattr(
        api_module,
        "current_performance_settings",
        lambda: PerformanceSettings(
            profile="low",
            max_node_test_concurrency=8,
            node_test_batch_size=50,
            max_port_test_concurrency=8,
            max_proxycheck_concurrency=3,
            max_geoip_concurrency=2,
            state_save_debounce_ms=1000,
            job_retention_minutes=60,
            max_jobs_per_type=20,
        ),
    )

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(api_module.socket, "create_connection", lambda *args, **kwargs: FakeConnection())

    async def fake_check_proxy_quality(proxy_url, proxy_id, timeout_seconds=30):
        await asyncio.sleep(0)
        return {"id": proxy_id, "grade": "A", "items": []}

    monkeypatch.setattr(api_module, "check_proxy_quality", fake_check_proxy_quality)
    store = CountingStore(tmp_path / "assignments.json")
    store.save(AppState())
    store.save_count = 0
    app = create_app(store=store, engine=RunningEngine())

    with TestClient(app) as client:
        started = client.post(
            "/api/proxy-check/start",
            json={"ports": [18001, 18002, 18003], "concurrency": 3},
        )
        job_id = started.json()["id"]
        for _ in range(20):
            job = client.get(f"/api/proxy-check/jobs/{job_id}")
            if job.json()["status"] == "done":
                break
            time.sleep(0.05)

    assert job.json()["completed"] == 3
    assert store.save_count == 1


def test_local_proxy_check_jobs_are_retained_by_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "current_performance_settings",
        lambda: PerformanceSettings(
            profile="normal",
            max_node_test_concurrency=12,
            node_test_batch_size=1000,
            max_port_test_concurrency=32,
            max_proxycheck_concurrency=1,
            max_geoip_concurrency=4,
            state_save_debounce_ms=0,
            job_retention_minutes=60,
            max_jobs_per_type=1,
        ),
    )

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(api_module.socket, "create_connection", lambda *args, **kwargs: FakeConnection())

    async def fake_check_proxy_quality(proxy_url, proxy_id, timeout_seconds=30):
        return {"id": proxy_id, "grade": "A", "items": []}

    monkeypatch.setattr(api_module, "check_proxy_quality", fake_check_proxy_quality)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=RunningEngine())

    def run_job(client, port):
        started = client.post("/api/proxy-check/start", json={"ports": [port], "concurrency": 1})
        job_id = started.json()["id"]
        for _ in range(20):
            job = client.get(f"/api/proxy-check/jobs/{job_id}")
            if job.json()["status"] == "done":
                break
            time.sleep(0.05)
        return job_id

    with TestClient(app) as client:
        first_id = run_job(client, 18001)
        second_id = run_job(client, 18002)
        third_id = run_job(client, 18003)

        assert client.get(f"/api/proxy-check/jobs/{first_id}").status_code == 404
        assert client.get(f"/api/proxy-check/jobs/{second_id}").status_code == 200
        assert client.get(f"/api/proxy-check/jobs/{third_id}").status_code == 200


def test_api_test_ports_uses_only_custom_urls(tmp_path, monkeypatch):
    seen = []

    async def fake_validate_proxy_targets(port, urls=None):
        seen.append((port, list(urls or [])))
        return [
            {
                "url": urls[0],
                "ok": True,
                "status_code": 204,
                "elapsed_ms": 88,
                "body_preview": "",
                "error": None,
            }
        ]

    monkeypatch.setattr(api_module, "validate_proxy_targets", fake_validate_proxy_targets)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=RunningEngine())
    client = TestClient(app)

    link = (
        "vless://00000000-0000-0000-0000-000000000000@example.com:443"
        "?security=tls#HK"
    )
    imported = client.post("/api/import", json={"text": link})
    tag = imported.json()["nodes"][0]["tag"]
    client.put("/api/assign", json={"mappings": {"8001": tag}})

    response = client.post(
        "/api/test-ports",
        json={"urls": ["https://custom.example.com/check"]},
    )

    assert response.status_code == 200
    assert seen == [(8001, ["https://custom.example.com/check"])]
    detail = response.json()["details"]["8001"]
    assert detail["targets"][0]["url"] == "https://custom.example.com/check"

    ports = client.get("/api/ports").json()["ports"]
    assert ports["8001"]["latency"]["target_url"] == "https://custom.example.com/check"
    assert ports["8001"]["latency"]["delay"] == 88


def test_api_test_ports_respects_configured_concurrency(tmp_path, monkeypatch):
    active = 0
    max_active = 0
    seen_ports = []

    monkeypatch.setattr(
        api_module,
        "current_performance_settings",
        lambda: PerformanceSettings(
            profile="normal",
            max_node_test_concurrency=12,
            node_test_batch_size=1000,
            max_port_test_concurrency=2,
            max_proxycheck_concurrency=10,
            max_geoip_concurrency=4,
            state_save_debounce_ms=0,
            job_retention_minutes=60,
            max_jobs_per_type=20,
        ),
    )

    async def fake_validate_proxy_targets(port, urls=None):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            seen_ports.append(port)
            return [
                {
                    "url": (urls or ["https://example.com"])[0],
                    "ok": True,
                    "status_code": 204,
                    "elapsed_ms": 10,
                    "body_preview": "",
                    "error": None,
                }
            ]
        finally:
            active -= 1

    async def fake_query_exit_ip(port, state, engine):
        return ExitIpCache(ip=None)

    monkeypatch.setattr(api_module, "validate_proxy_targets", fake_validate_proxy_targets)
    monkeypatch.setattr(api_module, "query_exit_ip", fake_query_exit_ip)
    store = StateStore(tmp_path / "assignments.json")
    nodes = [
        ProxyNode(
            tag=f"node-{index}",
            name=f"Node {index}",
            type="vless",
            server="example.com",
            server_port=443,
            outbound={"type": "vless", "tag": f"node-{index}"},
        )
        for index in range(5)
    ]
    store.save(
        AppState(
            nodes=nodes,
            port_mappings={
                str(8100 + index): PortMapping(node_tag=node.tag)
                for index, node in enumerate(nodes)
            },
        )
    )
    app = create_app(store=store, engine=RunningEngine())
    client = TestClient(app)

    response = client.post("/api/test-ports")

    assert response.status_code == 200
    assert sorted(seen_ports) == [8100, 8101, 8102, 8103, 8104]
    assert max_active <= 2


def test_api_test_ports_falls_back_to_exit_ip_for_geoip(tmp_path, monkeypatch):
    async def fake_validate_proxy_targets(port, urls=None):
        return [
            {
                "url": urls[0],
                "ok": True,
                "status_code": 204,
                "elapsed_ms": 88,
                "body_preview": "",
                "error": None,
            }
        ]

    async def fake_query_exit_ip(port, state, engine):
        return ExitIpCache(ip="8.8.8.8")

    async def fake_lookup_geoip(ip, state, **kwargs):
        result = GeoIpResult(ip=ip, country="United States", country_code="US", city="Mountain View", asn="AS15169", org="Google")
        state.geoip_cache[ip] = result
        return result

    monkeypatch.setattr(api_module, "validate_proxy_targets", fake_validate_proxy_targets)
    monkeypatch.setattr(api_module, "query_exit_ip", fake_query_exit_ip)
    monkeypatch.setattr(api_module, "lookup_geoip", fake_lookup_geoip)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=RunningEngine())
    client = TestClient(app)

    imported = client.post(
        "/api/import",
        json={
            "text": (
                "vless://00000000-0000-0000-0000-000000000000@example.com:443"
                "?security=tls#US"
            )
        },
    )
    tag = imported.json()["nodes"][0]["tag"]
    client.put("/api/assign", json={"mappings": {"8001": tag}})

    response = client.post("/api/test-ports", json={"urls": ["https://custom.example.com/check"]})

    assert response.status_code == 200
    detail = response.json()["details"]["8001"]
    assert detail["exit_ip"] == "8.8.8.8"
    assert detail["geoip"]["country_code"] == "US"
    ports = client.get("/api/ports").json()["ports"]
    assert ports["8001"]["geoip"]["compact"] == "US · Mountain View · Google"


def test_api_assign_restarts_running_engine(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    engine = RestartingEngine()
    app = create_app(store=store, engine=engine)
    client = TestClient(app)

    link = (
        "vless://00000000-0000-0000-0000-000000000000@example.com:443"
        "?security=tls#HK"
    )
    imported = client.post("/api/import", json={"text": link})
    tag = imported.json()["nodes"][0]["tag"]

    response = client.put("/api/assign", json={"mappings": {"8002": tag}})

    assert response.status_code == 200
    assert response.json()["engine_restarted"] is True
    assert engine.started


def test_api_start_rejects_while_node_test_job_is_running(tmp_path, monkeypatch):
    async def slow_test_nodes(nodes, on_result=None, target_url=None, target_urls=None, include_exit_ip=False, **kwargs):
        await asyncio.sleep(0.2)
        return {}

    monkeypatch.setattr(api_module, "test_nodes_with_temporary_engine", slow_test_nodes)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    with TestClient(app) as client:
        link = (
            "vless://00000000-0000-0000-0000-000000000000@example.com:443"
            "?security=tls#HK"
        )
        imported = client.post("/api/import", json={"text": link})
        tag = imported.json()["nodes"][0]["tag"]
        client.put("/api/assign", json={"mappings": {"8001": tag}})

        started = client.post("/api/test/start", json={"node_tags": [tag], "prune_same_ip": False})
        assert started.status_code == 200

        blocked = client.post("/api/start")

        assert blocked.status_code == 409
        assert "node test is running" in blocked.json()["detail"]


def test_api_start_omits_clash_api_when_controller_port_is_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "_can_bind_tcp_port", lambda port: port != 10000)
    monkeypatch.setattr(api_module, "current_clash_api_addr", lambda: "127.0.0.1:10000")
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    link = (
        "vless://00000000-0000-0000-0000-000000000000@example.com:443"
        "?security=tls#HK"
    )
    imported = client.post("/api/import", json={"text": link})
    tag = imported.json()["nodes"][0]["tag"]
    client.put("/api/assign", json={"mappings": {"18001": tag}})

    started = client.post("/api/start")

    assert started.status_code == 200
    config = json.loads(api_module.SING_BOX_CONFIG_PATH.read_text(encoding="utf-8"))
    assert "experimental" not in config


def _listening_ports_cache_from_app(app):
    """Extract the create_app listening cache via engine_status_payload's closure."""
    status_route = next(route for route in app.routes if getattr(route, "path", None) == "/api/status")
    endpoint = status_route.endpoint
    while hasattr(endpoint, "__wrapped__"):
        endpoint = endpoint.__wrapped__
    engine_status_payload = None
    for cell in endpoint.__closure__ or ():
        value = cell.cell_contents
        if callable(value) and getattr(value, "__name__", "") == "engine_status_payload":
            engine_status_payload = value
            break
    if engine_status_payload is None:
        raise AssertionError("engine_status_payload not found on /api/status closure")
    for cell in engine_status_payload.__closure__ or ():
        value = cell.cell_contents
        if isinstance(value, dict) and "ports" in value and "expected" in value and "checked_at" in value:
            return value
    raise AssertionError("listening_ports_cache not found on engine_status_payload closure")


def test_api_status_reports_engine_port_readiness(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "_can_bind_tcp_port", lambda port: True)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=RunningEngine())
    client = TestClient(app)

    link = (
        "vless://00000000-0000-0000-0000-000000000000@example.com:443"
        "?security=tls#HK"
    )
    imported = client.post("/api/import", json={"text": link})
    tag = imported.json()["nodes"][0]["tag"]
    assigned = client.put("/api/assign", json={"mappings": {"8001": tag}})
    assert assigned.status_code == 200

    # Status no longer probes sockets inline; drive readiness via the cache the
    # background runtime loop would publish.
    listening_ports_cache = _listening_ports_cache_from_app(app)

    listening_ports_cache["ports"] = set()
    listening_ports_cache["expected"] = [8001]
    listening_ports_cache["checked_at"] = time.time()
    not_ready = client.get("/api/status").json()
    assert not_ready["engine"]["running"] is True
    assert not_ready["engine"]["ready"] is False
    assert not_ready["engine"]["expected_ports"] == [8001]
    assert not_ready["engine"]["listening_ports"] == []
    assert not_ready["engine"]["missing_ports"] == [8001]
    assert not_ready["engine"]["expected_count"] == 1
    assert not_ready["engine"]["listening_count"] == 0

    listening_ports_cache["ports"] = {8001}
    ready = client.get("/api/status").json()
    assert ready["engine"]["ready"] is True
    assert ready["engine"]["listening_ports"] == [8001]
    assert ready["engine"]["missing_ports"] == []


def test_api_router_status_uses_control_plane_without_proxy_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module.PoolRouterManager, "available", lambda self: True)
    monkeypatch.setattr(api_module.PoolRouterManager, "payload", lambda self: {"available": True, "running": True, "pid": 123, "last_error": None})

    async def refresh_runtime_payload(self):
        return self.payload()

    async def fake_router_status(self):
        return {
            "listeners": [
                {"id": "port-8001", "listen": "0.0.0.0:8001", "upload": 0, "download": 0, "active": 0, "selections": 0, "backends": []}
            ]
        }

    monkeypatch.setattr(api_module.PoolRouterManager, "status", fake_router_status)
    monkeypatch.setattr(api_module.PoolRouterManager, "refresh_runtime_payload", refresh_runtime_payload)
    monkeypatch.setattr(api_module, "_listening_local_ports", lambda ports: (_ for _ in ()).throw(AssertionError("router ports must not be socket-probed")))
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=RunningEngine())
    with TestClient(app) as client:
        imported = client.post(
            "/api/import",
            json={"text": "vless://00000000-0000-0000-0000-000000000000@example.com:443?security=tls#HK"},
        )
        client.put("/api/assign", json={"mappings": {"8001": imported.json()["nodes"][0]["tag"]}})

        assert client.get("/api/traffic/overview").status_code == 200
        status = client.get("/api/status").json()["engine"]

    assert status["ready"] is True
    assert status["listening_ports"] == [8001]


def test_api_status_router_down_ignores_foreign_listeners(tmp_path, monkeypatch):
    # Router mode with the router dead: a foreign process answering on a
    # mapped port (iCloud on 8081) must not count toward "listening".
    monkeypatch.setattr(api_module.PoolRouterManager, "available", lambda self: True)
    store = StateStore(tmp_path / "assignments.json")
    store.save(
        AppState(
            nodes=[
                ProxyNode(
                    tag="node-a",
                    name="A",
                    type="vless",
                    server="a.example.com",
                    server_port=443,
                    outbound={"type": "vless", "server": "a.example.com", "server_port": 443, "uuid": "u", "tag": "node-a"},
                )
            ],
            port_mappings={"8081": PortMapping(node_tag="node-a")},
        )
    )
    app = create_app(store=store, engine=RunningEngine())
    client = TestClient(app)

    # Poison the blind-probe cache as if a foreign listener answered on 8081.
    listening_ports_cache = _listening_ports_cache_from_app(app)
    listening_ports_cache["ports"] = {8081}
    listening_ports_cache["expected"] = [8081]
    listening_ports_cache["checked_at"] = time.time()

    status = client.get("/api/status").json()["engine"]

    assert status["running"] is True
    assert status["listening_ports"] == []
    assert status["missing_ports"] == [8081]
    assert status["ready"] is False


def test_router_runtime_tick_restarts_router_and_regenerates_config(tmp_path, monkeypatch):
    # The runtime tick must restart a dead router and rebuild pool-router.json
    # from current state so a corrupted file on disk cannot come back to life.
    monkeypatch.setattr(api_module.PoolRouterManager, "available", lambda self: True)
    started = []

    async def record_start(self, config_path=None):
        started.append(config_path)

    async def fake_status(self):
        return {"listeners": [{"id": "port-8001", "listen": "127.0.0.1:8001", "backends": []}]}

    monkeypatch.setattr(api_module.PoolRouterManager, "start", record_start)
    monkeypatch.setattr(api_module.PoolRouterManager, "status", fake_status)
    api_module.POOL_ROUTER_CONFIG_PATH.write_text(
        json.dumps({"listeners": [{"id": "port-9999", "listen": "127.0.0.1:9999"}]}),
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "assignments.json")
    store.save(
        AppState(
            nodes=[
                ProxyNode(
                    tag="node-a",
                    name="A",
                    type="vless",
                    server="a.example.com",
                    server_port=443,
                    outbound={"type": "vless", "server": "a.example.com", "server_port": 443, "uuid": "u", "tag": "node-a"},
                )
            ],
            port_mappings={"8001": PortMapping(node_tag="node-a")},
        )
    )
    app = create_app(store=store, engine=RunningEngine())

    asyncio.run(app.state.proxy_pool_router_tick())

    assert started == [api_module.POOL_ROUTER_CONFIG_PATH]
    config = json.loads(api_module.POOL_ROUTER_CONFIG_PATH.read_text(encoding="utf-8"))
    listens = [str(listener.get("listen", "")) for listener in config["listeners"]]
    assert any(listen.endswith(":8001") for listen in listens)
    assert not any(listen.endswith(":9999") for listen in listens)


def test_api_ports_check_flags_mapped_port_held_by_foreign_process(tmp_path, monkeypatch):
    # A mapped port the runtime skipped because a foreign process owns it must
    # be reported as a conflict, not as a plain "already assigned" port.
    monkeypatch.setattr(api_module.PoolRouterManager, "available", lambda self: True)
    monkeypatch.setattr(api_module, "_can_bind_tcp_port", lambda port: False)
    store = StateStore(tmp_path / "assignments.json")
    store.save(
        AppState(
            nodes=[
                ProxyNode(
                    tag="node-a",
                    name="A",
                    type="vless",
                    server="a.example.com",
                    server_port=443,
                    outbound={"type": "vless", "server": "a.example.com", "server_port": 443, "uuid": "u", "tag": "node-a"},
                )
            ],
            port_mappings={"8081": PortMapping(node_tag="node-a")},
        )
    )
    app = create_app(store=store, engine=RunningEngine())
    client = TestClient(app)

    response = client.post("/api/ports/check", json={"ports": [8081]})

    assert response.status_code == 200
    checked = response.json()["ports"]["8081"]
    assert checked["available"] is False
    assert checked["reason"] == "project-mapped-conflict"
    assert checked["label"] == "已分配但被其他程序占用"


def test_api_traffic_returns_stale_snapshot_when_router_control_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module.PoolRouterManager, "available", lambda self: True)
    monkeypatch.setattr(
        api_module.PoolRouterManager,
        "payload",
        lambda self: {"available": True, "running": True, "pid": 123, "last_error": None},
    )

    async def unavailable_router_status(self):
        raise api_module.PoolRouterError("control endpoint timed out")

    async def refresh_runtime_payload(self):
        return self.payload()

    monkeypatch.setattr(api_module.PoolRouterManager, "status", unavailable_router_status)
    monkeypatch.setattr(api_module.PoolRouterManager, "refresh_runtime_payload", refresh_runtime_payload)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=RunningEngine())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/traffic/overview")

    assert response.status_code == 200
    assert response.json()["router"]["telemetry_stale"] is True
    assert "timed out" in response.json()["router"]["telemetry_error"]


def test_api_status_reports_proxy_connect_host(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "current_proxy_listen_host", lambda: "0.0.0.0")
    monkeypatch.setattr(api_module, "current_proxy_public_host", lambda: "")
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app, base_url="http://203.0.113.7:9000")

    response = client.get("/api/status").json()

    assert response["proxy_listen_host"] == "0.0.0.0"
    assert response["proxy_connect_host"] == "203.0.113.7"

    monkeypatch.setattr(api_module, "current_proxy_public_host", lambda: "proxy.example.com")
    response = client.get("/api/status").json()

    assert response["proxy_public_host"] == "proxy.example.com"
    assert response["proxy_connect_host"] == "proxy.example.com"


def test_api_doctor_returns_structured_script_result(tmp_path, monkeypatch):
    def fake_run_doctor_script(timeout=30):
        return {
            "ok": 2,
            "warn": 1,
            "fail": 0,
            "summary": "Summary: ok=2 warn=1 fail=0",
            "lines": ["[OK] python", "[WARN] web service not running", "=== Summary: ok=2 warn=1 fail=0 ==="],
            "exit_code": 0,
            "timed_out": False,
            "command": "doctor",
        }

    monkeypatch.setattr(api_module, "run_doctor_script", fake_run_doctor_script)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.post("/api/doctor", json={"timeout": 12})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] == 2
    assert body["warn"] == 1
    assert body["fail"] == 0
    assert body["exit_code"] == 0
    assert body["lines"][1].startswith("[WARN]")


def test_api_subscription_config_and_manual_refresh(tmp_path, monkeypatch):
    async def fake_import_nodes(url=None, text=None):
        assert url == "https://example.com/sub"
        return ImportResult(
            nodes=[
                ProxyNode(
                    tag="node-1",
                    name="JP",
                    type="vless",
                    server="example.com",
                    server_port=443,
                    outbound={"type": "vless", "server": "example.com", "server_port": 443},
                )
            ],
            count=1,
            warnings=["kept existing stale nodes"],
        )

    monkeypatch.setattr(api_module, "import_nodes", fake_import_nodes)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    saved = client.put(
        "/api/subscription",
        json={"url": "https://example.com/sub", "refresh_interval_minutes": 15},
    )
    assert saved.status_code == 200
    assert saved.json()["url"] == "https://example.com/sub"
    assert saved.json()["refresh_interval_minutes"] == 15

    refreshed = client.post("/api/subscription/refresh")
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["imported"] == 1
    assert body["added"] == 1
    assert body["updated"] == 0
    assert body["total_nodes"] == 1
    assert body["last_error"] is None

    state = store.load()
    assert state.subscription_url == "https://example.com/sub"
    assert state.subscription_refresh_interval_minutes == 15
    assert state.subscription_last_count == 1
    assert state.subscription_last_refresh_at
    assert state.nodes[0].tag == "node-1"


def test_api_subscription_refresh_requires_url(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.post("/api/subscription/refresh")

    assert response.status_code == 400
    assert "Subscription URL is not configured" in response.json()["detail"]


def test_run_doctor_script_parses_failed_output(monkeypatch):
    class FakeCompleted:
        returncode = 1
        stdout = "[OK] python\n[FAIL] requirements.txt missing\n=== Summary: ok=1 warn=0 fail=1 ===\n"
        stderr = ""

    monkeypatch.setattr(api_module.subprocess, "run", lambda *args, **kwargs: FakeCompleted())
    monkeypatch.setattr(api_module, "_doctor_command", lambda: ["doctor"])

    result = api_module.run_doctor_script(30)

    assert result["exit_code"] == 1
    assert result["ok"] == 1
    assert result["fail"] == 1
    assert result["summary"] == "Summary: ok=1 warn=0 fail=1"


def test_listening_local_ports_uses_socket_probe():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert api_module._listening_local_ports([port, port + 1]) == [port]
    finally:
        listener.close()


def test_api_progressive_port_test_job(tmp_path, monkeypatch):
    async def fake_validate_proxy_targets(port, urls=None):
        return [
            {
                "url": urls[0],
                "ok": True,
                "status_code": 200,
                "elapsed_ms": 55,
                "body_preview": "ok",
                "error": None,
            }
        ]

    async def fake_query_exit_ip(port, state, engine):
        return ExitIpCache(ip=None, error="not checked")

    monkeypatch.setattr(api_module, "validate_proxy_targets", fake_validate_proxy_targets)
    monkeypatch.setattr(api_module, "query_exit_ip", fake_query_exit_ip)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=RunningEngine())
    client = TestClient(app)

    link = (
        "vless://00000000-0000-0000-0000-000000000000@example.com:443"
        "?security=tls#HK"
    )
    imported = client.post("/api/import", json={"text": link})
    tag = imported.json()["nodes"][0]["tag"]
    client.put("/api/assign", json={"mappings": {"8001": tag}})

    started = client.post(
        "/api/test-ports/start",
        json={"urls": ["https://target.example.com/"]},
    )
    assert started.status_code == 200
    job_id = started.json()["id"]

    for _ in range(20):
        job = client.get(f"/api/test-ports/jobs/{job_id}")
        if job.json()["status"] == "done":
            break
        time.sleep(0.05)

    assert job.status_code == 200
    assert job.json()["status"] == "done"
    assert job.json()["completed"] == 1
    assert job.json()["details"]["8001"]["targets"][0]["url"] == "https://target.example.com/"

    ports = client.get("/api/ports").json()["ports"]
    assert ports["8001"]["latency"]["delay"] == 55
    assert ports["8001"]["latency"]["target_url"] == "https://target.example.com/"


def test_api_port_test_job_reports_details_when_engine_stopped(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    link = (
        "vless://00000000-0000-0000-0000-000000000000@example.com:443"
        "?security=tls#HK"
    )
    imported = client.post("/api/import", json={"text": link})
    tag = imported.json()["nodes"][0]["tag"]
    client.put("/api/assign", json={"mappings": {"8001": tag}})

    started = client.post(
        "/api/test-ports/start",
        json={"urls": ["https://rawchat.cn"]},
    )
    assert started.status_code == 200
    job_id = started.json()["id"]

    for _ in range(20):
        job = client.get(f"/api/test-ports/jobs/{job_id}")
        if job.json()["status"] == "done":
            break
        time.sleep(0.05)

    body = job.json()
    assert body["status"] == "done"
    assert body["total"] == 1
    assert body["completed"] == 1
    assert body["details"]["8001"]["targets"][0]["url"] == "https://rawchat.cn"
    assert body["details"]["8001"]["targets"][0]["error"] == "sing-box is not running"



def test_api_test_ports_defaults_to_primary_target_only(tmp_path, monkeypatch):
    seen = []

    async def fake_validate_proxy_targets(port, urls=None):
        seen.append((port, list(urls or [])))
        return [
            {
                "url": urls[0],
                "ok": True,
                "status_code": 204,
                "elapsed_ms": 25,
                "body_preview": "",
                "error": None,
            }
        ]

    async def fake_query_exit_ip(port, state, engine):
        return ExitIpCache(ip=None)

    monkeypatch.setattr(api_module, "validate_proxy_targets", fake_validate_proxy_targets)
    monkeypatch.setattr(api_module, "query_exit_ip", fake_query_exit_ip)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=RunningEngine())
    client = TestClient(app)

    imported = client.post(
        "/api/import",
        json={
            "text": (
                "vless://00000000-0000-0000-0000-000000000000@example.com:443"
                "?security=tls#HK"
            )
        },
    )
    tag = imported.json()["nodes"][0]["tag"]
    client.put("/api/assign", json={"mappings": {"8001": tag}})

    response = client.post("/api/test-ports")

    assert response.status_code == 200
    assert seen == [(8001, [api_module.PRIMARY_TEST_URL])]


def test_current_performance_settings_prefers_lower_batch_defaults(monkeypatch):
    monkeypatch.delenv("PPM_MAX_NODE_TEST_CONCURRENCY", raising=False)
    monkeypatch.delenv("PPM_MAX_PORT_TEST_CONCURRENCY", raising=False)
    monkeypatch.delenv("PPM_STATE_SAVE_DEBOUNCE_MS", raising=False)
    monkeypatch.setattr("app.settings._APP_CONFIG", {})
    monkeypatch.setattr("app.settings._load_app_config", lambda: {})

    settings = api_module.current_performance_settings()

    assert settings.max_node_test_concurrency == 64
    assert settings.max_port_test_concurrency == 32
    assert settings.state_save_debounce_ms == 500


def test_api_sing_box_version_update_and_rollback(tmp_path):
    class VersionEngine(StoppedEngine):
        async def binary_info(self, check_latest=False):
            return {
                "managed": True,
                "current_version": "1.13.13",
                "latest_version": "1.13.14" if check_latest else None,
                "update_available": check_latest,
                "rollback_available": True,
                "backup_version": "1.13.12",
                "path": "bin/sing-box.exe",
                "platform": "windows",
                "architecture": "amd64",
            }

        async def update_binary(self):
            return {"updated": True, "previous_version": "1.13.13", "current_version": "1.13.14"}

        async def rollback_binary(self):
            return {"rolled_back": True, "previous_version": "1.13.14", "current_version": "1.13.13"}

    app = create_app(store=StateStore(tmp_path / "assignments.json"), engine=VersionEngine())
    client = TestClient(app)

    info = client.get("/api/engine/version?check_latest=true")
    updated = client.post("/api/engine/update")
    rolled_back = client.post("/api/engine/rollback")

    assert info.status_code == 200
    assert info.json()["update_available"] is True
    assert updated.json()["current_version"] == "1.13.14"
    assert rolled_back.json()["current_version"] == "1.13.13"
