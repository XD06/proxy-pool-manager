import asyncio
import json
from fastapi.testclient import TestClient
import pytest
import socket
import time

from app import api as api_module
from app import proxy_admin as proxy_admin_module
from app.api import create_app
from app.models import AppState, EngineStatus, ExitIpCache, GeoIpResult, LatencyResult, PortMapping, ProxyNode
from app.store import StateStore


@pytest.fixture(autouse=True)
def isolate_sing_box_config(monkeypatch, tmp_path):
    monkeypatch.setattr(api_module, "SING_BOX_CONFIG_PATH", tmp_path / "sing-box.json")
    monkeypatch.setattr(api_module, "APP_CONFIG_PATH", tmp_path / "app.json")


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


def test_api_import_zero_nodes_returns_error(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store)
    client = TestClient(app)

    response = client.post("/api/import", json={"text": "ssr://unsupported"})

    assert response.status_code == 400
    assert "No supported nodes" in response.json()["detail"]


def test_api_progressive_test_job(tmp_path, monkeypatch):
    seen = []

    async def fake_test_nodes(nodes, on_result=None, target_url=None, target_urls=None, include_exit_ip=False):
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
    assert seen == [False]


def test_api_prune_node_test_includes_exit_ip(tmp_path, monkeypatch):
    seen = []

    async def fake_test_nodes(nodes, on_result=None, target_url=None, target_urls=None, include_exit_ip=False):
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

    async def fake_test_nodes(nodes, on_result=None, target_url=None, target_urls=None, include_exit_ip=False):
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

    async def fake_test_nodes(nodes, on_result=None, target_url=None, target_urls=None, include_exit_ip=False):
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
    async def fake_test_nodes(nodes, on_result=None, target_url=None, target_urls=None, include_exit_ip=False):
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


def test_api_fastest_proxy_returns_best_alive_mapping(tmp_path):
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


def test_api_proxy_admin_check_job_streams_results(tmp_path, monkeypatch):
    created = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, headers=None, json=None):
            if method == "POST" and url.endswith("/api/v1/admin/proxies"):
                created.append(json)
                proxy_id = 500 + len(created)
                return FakeResponse(
                    {
                        "code": 0,
                        "message": "ok",
                        "data": {
                            "id": proxy_id,
                            "name": json["name"],
                            "protocol": json["protocol"],
                            "host": json["host"],
                            "port": json["port"],
                        },
                    }
                )
            if "/api/v1/admin/proxies?page=" in url:
                return FakeResponse(
                    {
                        "code": 0,
                        "message": "ok",
                        "data": {
                            "items": [
                                {"id": 499, "name": "代理1", "host": "127.0.0.1", "port": 17999, "account_count": 0},
                            ],
                            "pages": 1,
                        },
                    }
                )
            if url.endswith("/quality-check"):
                proxy_id = int(url.split("/")[-2])
                return FakeResponse(
                    {
                        "code": 0,
                        "message": "ok",
                        "data": {
                            "proxy_id": proxy_id,
                            "exit_ip": f"203.0.113.{proxy_id - 500}",
                            "country": "TEST",
                            "score": 90,
                            "grade": "A",
                            "items": [{"target": "openai", "status": "pass", "latency_ms": 123, "message": "ok"}],
                        },
                    }
                )
            raise AssertionError(url)

    monkeypatch.setattr(proxy_admin_module.httpx, "AsyncClient", FakeClient)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())

    with TestClient(app) as client:
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
        assigned = client.put("/api/assign", json={"mappings": {"18001": tags[0], "18002": tags[1]}})
        assert assigned.status_code == 200
        state = app.state.proxy_pool_state
        state.latency_cache[tags[0]] = LatencyResult(alive=True, delay=100, geoip={"country_code": "JP", "city": "Tokyo"})
        state.latency_cache[tags[1]] = LatencyResult(alive=True, delay=120, geoip={"country_code": "US", "city": "Los Angeles"})

        started = client.post(
            "/api/proxy-admin/check/start",
            json={
                "base_url": "http://127.0.0.1:8081",
                "token": "token",
                "proxy_host": "127.0.0.1",
                "ports": [18001, 18002],
                "concurrency": 2,
            },
        )
        assert started.status_code == 200
        job_id = started.json()["id"]

        for _ in range(20):
            job = client.get(f"/api/proxy-admin/jobs/{job_id}")
            if job.json()["status"] == "done":
                break
            time.sleep(0.05)

        payload = job.json()
        assert payload["status"] == "done"
        assert payload["completed"] == 2
        assert payload["results"]["501"]["items"][0]["status"] == "pass"
        assert {item["name"] for item in created} == {"代理2-JP.Tokyo", "代理3-US.LosAngeles"}


def test_api_proxy_admin_upload_failure_does_not_stop_batch(tmp_path, monkeypatch):
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, headers=None, json=None):
            if "/api/v1/admin/proxies?page=" in url:
                return FakeResponse({"code": 0, "message": "ok", "data": {"items": [], "pages": 1}})
            if method == "POST" and url.endswith("/api/v1/admin/proxies"):
                if json["port"] == 18001:
                    raise RuntimeError("upload failed")
                return FakeResponse({"code": 0, "message": "ok", "data": {"id": 502, "name": json["name"], "port": json["port"]}})
            if url.endswith("/quality-check"):
                return FakeResponse(
                    {
                        "code": 0,
                        "message": "ok",
                        "data": {
                            "proxy_id": 502,
                            "exit_ip": "203.0.113.2",
                            "country": "TEST",
                            "score": 80,
                            "grade": "B",
                            "items": [{"target": "openai", "status": "pass", "latency_ms": 123, "message": "ok"}],
                        },
                    }
                )
            raise AssertionError(url)

    monkeypatch.setattr(proxy_admin_module.httpx, "AsyncClient", FakeClient)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())

    with TestClient(app) as client:
        started = client.post(
            "/api/proxy-admin/check/start",
            json={
                "base_url": "http://127.0.0.1:8081",
                "token": "token",
                "proxy_host": "127.0.0.1",
                "ports": [18001, 18002],
                "concurrency": 2,
            },
        )
        job_id = started.json()["id"]
        for _ in range(20):
            job = client.get(f"/api/proxy-admin/jobs/{job_id}")
            if job.json()["status"] == "done":
                break
            time.sleep(0.05)

        payload = job.json()
        assert payload["status"] == "done"
        assert payload["completed"] == 2
        assert payload["results"]["-18001"]["grade"] == "ERR"
        assert payload["results"]["502"]["grade"] == "B"


def test_api_proxy_admin_retry_check_only_does_not_upload(tmp_path, monkeypatch):
    checked_ids = []

    async def fail_import(payload, proxy_items):
        raise AssertionError("check_only retry should not upload proxies")

    async def fake_proxy_admin_quality_check(payload, proxy_id):
        checked_ids.append(proxy_id)
        return {
            "id": proxy_id,
            "exit_ip": "203.0.113.7",
            "country": "TEST",
            "score": 100,
            "grade": "A",
            "items": [{"target": "openai", "status": "pass", "latency_ms": 100, "message": "ok"}],
        }

    monkeypatch.setattr(api_module, "proxy_admin_import", fail_import)
    monkeypatch.setattr(api_module, "proxy_admin_quality_check", fake_proxy_admin_quality_check)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())

    with TestClient(app) as client:
        started = client.post(
            "/api/proxy-admin/check/start",
            json={
                "base_url": "http://127.0.0.1:8081",
                "token": "token",
                "proxy_host": "127.0.0.1",
                "ports": [18007],
                "check_only": True,
                "proxy_ids_by_port": {"18007": 507},
            },
        )
        assert started.status_code == 200
        job_id = started.json()["id"]
        for _ in range(20):
            job = client.get(f"/api/proxy-admin/jobs/{job_id}")
            if job.json()["status"] == "done":
                break
            time.sleep(0.05)

        payload = job.json()
        assert payload["status"] == "done"
        assert payload["imported"][0]["id"] == 507
        assert payload["results"]["507"]["grade"] == "A"
        assert checked_ids == [507]


def test_api_proxy_admin_check_only_requires_ids(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.post(
        "/api/proxy-admin/check/start",
        json={
            "base_url": "http://127.0.0.1:8081",
            "token": "token",
            "proxy_host": "127.0.0.1",
            "ports": [18007],
            "check_only": True,
            "proxy_ids_by_port": {},
        },
    )

    assert response.status_code == 400
    assert "Missing ProxyAdmin ids for ports: 18007" in response.json()["detail"]


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


def test_api_proxy_admin_remove_unused(tmp_path, monkeypatch):
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, headers=None, json=None):
            if "/api/v1/admin/proxies?page=" in url:
                return FakeResponse(
                    {
                        "code": 0,
                        "message": "ok",
                        "data": {
                            "items": [
                                {"id": 501, "host": "127.0.0.1", "port": 18001, "account_count": 0},
                                {"id": 502, "host": "127.0.0.1", "port": 18002, "account_count": 2},
                            ],
                            "pages": 1,
                        },
                    }
                )
            if method == "DELETE":
                return FakeResponse({"code": 0, "message": "ok", "data": {}})
            raise AssertionError(url)

    monkeypatch.setattr(proxy_admin_module.httpx, "AsyncClient", FakeClient)
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    response = client.post(
        "/api/proxy-admin/remove",
        json={"base_url": "http://127.0.0.1:8081", "token": "token", "unused": True},
    )

    assert response.status_code == 200
    assert response.json()["removed"] == [{"id": 501, "success": True}]


def test_api_proxy_admin_config_persists_without_dropping_app_settings(tmp_path):
    api_module.APP_CONFIG_PATH.write_text(
        json.dumps({"host": "0.0.0.0", "port": 9000}),
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store, engine=StoppedEngine())
    client = TestClient(app)

    saved = client.put(
        "/api/proxy-admin/config",
        json={
            "base_url": "http://127.0.0.1:8081",
            "token": "secret",
            "proxy_host": "43.156.235.29",
            "replace_from": "127.0.0.1",
            "replace_to": "172.17.0.1",
            "proxy_name_prefix": "测试代理",
            "concurrency": 12,
        },
    )
    loaded = client.get("/api/proxy-admin/config")

    assert saved.status_code == 200
    assert loaded.json()["token"] == "secret"
    config = json.loads(api_module.APP_CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["host"] == "0.0.0.0"
    assert config["proxy_admin"]["concurrency"] == 12
    assert config["proxy_admin"]["proxy_name_prefix"] == "测试代理"


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
    async def slow_test_nodes(nodes, on_result=None, target_url=None, target_urls=None, include_exit_ip=False):
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


def test_api_status_reports_engine_port_readiness(tmp_path, monkeypatch):
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

    monkeypatch.setattr(api_module, "_listening_local_ports", lambda ports: [])
    not_ready = client.get("/api/status").json()
    assert not_ready["engine"]["running"] is True
    assert not_ready["engine"]["ready"] is False
    assert not_ready["engine"]["expected_ports"] == [8001]
    assert not_ready["engine"]["listening_ports"] == []

    monkeypatch.setattr(api_module, "_listening_local_ports", lambda ports: [8001])
    ready = client.get("/api/status").json()
    assert ready["engine"]["ready"] is True
    assert ready["engine"]["listening_ports"] == [8001]


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
