from fastapi.testclient import TestClient
import pytest
import socket
import time

from app import api as api_module
from app.api import create_app
from app.models import EngineStatus, LatencyResult
from app.store import StateStore


@pytest.fixture(autouse=True)
def isolate_sing_box_config(monkeypatch, tmp_path):
    monkeypatch.setattr(api_module, "SING_BOX_CONFIG_PATH", tmp_path / "sing-box.json")


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


def test_api_import_zero_nodes_returns_error(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store)
    client = TestClient(app)

    response = client.post("/api/import", json={"text": "ssr://unsupported"})

    assert response.status_code == 400
    assert "No supported nodes" in response.json()["detail"]


def test_api_progressive_test_job(tmp_path, monkeypatch):
    async def fake_test_nodes(nodes, on_result=None, target_url=None):
        for node in nodes:
            result = LatencyResult(
                alive=True,
                delay=123,
                exit_ip=None if target_url else "203.0.113.10",
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
    assert job.json()["results"][tag]["exit_ip"] == "203.0.113.10"


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
