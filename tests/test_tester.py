from app.models import LatencyResult, ProxyNode
import socket

import asyncio
import pytest

from app import tester as tester_module
from app.tester import (
    DEFAULT_NODE_TEST_URLS,
    DEFAULT_VALIDATION_URLS,
    PRIMARY_TEST_URL,
    _fetch_first_test_url,
    allocate_test_ports,
    extract_public_ipv4,
    prune_same_exit_ip,
    sort_nodes_by_test_result,
)

def _node(tag, name):
    return ProxyNode(
        tag=tag,
        name=name,
        type="vless",
        server=f"{tag}.example.com",
        server_port=443,
        outbound={
            "type": "vless",
            "tag": tag,
            "server": f"{tag}.example.com",
            "server_port": 443,
            "uuid": "00000000-0000-0000-0000-000000000000",
        },
    )


def test_prune_same_exit_ip_keeps_fastest_alive_node():
    nodes = [_node("node-a", "A"), _node("node-b", "B"), _node("node-c", "C")]
    results = {
        "node-a": LatencyResult(alive=True, delay=300, exit_ip="203.0.113.1"),
        "node-b": LatencyResult(alive=True, delay=120, exit_ip="203.0.113.1"),
        "node-c": LatencyResult(alive=True, delay=200, exit_ip="198.51.100.2"),
    }

    kept, removed = prune_same_exit_ip(nodes, results)

    assert [node.tag for node in kept] == ["node-b", "node-c"]
    assert removed
    assert "A" in removed[0]


def test_prune_same_exit_ip_ignores_unusable_nodes():
    nodes = [_node("node-a", "A"), _node("node-b", "B")]
    results = {
        "node-a": LatencyResult(alive=False, error="timeout"),
        "node-b": LatencyResult(alive=True, delay=120, exit_ip="203.0.113.1"),
    }

    kept, removed = prune_same_exit_ip(nodes, results)

    assert [node.tag for node in kept] == ["node-a", "node-b"]
    assert removed == []


def test_sort_nodes_by_test_result_orders_alive_fastest_first():
    nodes = [_node("node-a", "A"), _node("node-b", "B"), _node("node-c", "C"), _node("node-d", "D")]
    results = {
        "node-a": LatencyResult(alive=True, delay=300, exit_ip="203.0.113.1"),
        "node-b": LatencyResult(alive=False, error="timeout"),
        "node-c": LatencyResult(alive=True, delay=80, exit_ip="198.51.100.2"),
    }

    sorted_nodes = sort_nodes_by_test_result(nodes, results)

    assert [node.tag for node in sorted_nodes] == ["node-c", "node-a", "node-b", "node-d"]


def test_sort_nodes_by_multi_target_success_then_average_delay():
    nodes = [_node("node-a", "A"), _node("node-b", "B"), _node("node-c", "C")]
    results = {
        "node-a": LatencyResult(
            alive=True,
            delay=100,
            target_results=[
                {"url": "u1", "ok": True, "elapsed_ms": 100},
                {"url": "u2", "ok": False, "elapsed_ms": 300},
            ],
        ),
        "node-b": LatencyResult(
            alive=True,
            delay=180,
            target_results=[
                {"url": "u1", "ok": True, "elapsed_ms": 160},
                {"url": "u2", "ok": True, "elapsed_ms": 200},
            ],
        ),
        "node-c": LatencyResult(
            alive=True,
            delay=220,
            target_results=[
                {"url": "u1", "ok": True, "elapsed_ms": 220},
                {"url": "u2", "ok": True, "elapsed_ms": 220},
            ],
        ),
    }

    sorted_nodes = sort_nodes_by_test_result(nodes, results)

    assert [node.tag for node in sorted_nodes] == ["node-b", "node-c", "node-a"]


def test_allocate_test_ports_skips_occupied_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 19001))
        sock.listen()
        ports = allocate_test_ports(2, start_port=19001)

    assert 19001 not in ports
    assert len(ports) == 2


def test_test_nodes_with_temporary_engine_batches_nodes(monkeypatch):
    batches = []
    stops = []

    class FakeEngine:
        def __init__(self, *args, **kwargs):
            pass

        async def stop(self, *args, **kwargs):
            stops.append(args)

    async def fake_batch(nodes, **kwargs):
        batches.append(
            {
                "tags": [node.tag for node in nodes],
                "concurrency": kwargs["concurrency"],
            }
        )
        return {node.tag: LatencyResult(alive=True, delay=10) for node in nodes}

    monkeypatch.setattr(tester_module, "EngineManager", FakeEngine)
    monkeypatch.setattr(tester_module, "_test_node_batch_with_temporary_engine", fake_batch)
    nodes = [_node(f"node-{index}", f"Node {index}") for index in range(5)]

    results = asyncio.run(
        tester_module.test_nodes_with_temporary_engine(
            nodes,
            concurrency=3,
            batch_size=2,
        )
    )

    assert [batch["tags"] for batch in batches] == [
        ["node-0", "node-1"],
        ["node-2", "node-3"],
        ["node-4"],
    ]
    assert {batch["concurrency"] for batch in batches} == {3}
    assert sorted(results) == [node.tag for node in nodes]
    assert stops


def test_test_nodes_with_temporary_engine_uses_small_default_batches(monkeypatch):
    batches = []

    class FakeEngine:
        def __init__(self, *args, **kwargs):
            pass

        async def stop(self, *args, **kwargs):
            return None

    async def fake_batch(nodes, **kwargs):
        batches.append([node.tag for node in nodes])
        return {node.tag: LatencyResult(alive=True, delay=10) for node in nodes}

    monkeypatch.setattr(tester_module, "EngineManager", FakeEngine)
    monkeypatch.setattr(tester_module, "_test_node_batch_with_temporary_engine", fake_batch)
    nodes = [_node(f"node-{index}", f"Node {index}") for index in range(8)]

    asyncio.run(tester_module.test_nodes_with_temporary_engine(nodes, concurrency=4))

    assert batches == [
        ["node-0", "node-1", "node-2", "node-3"],
        ["node-4", "node-5", "node-6", "node-7"],
    ]


def test_default_validation_urls_include_exit_ip_and_google_targets():
    assert DEFAULT_VALIDATION_URLS[0] == PRIMARY_TEST_URL
    assert "http://cp.cloudflare.com/generate_204" in DEFAULT_VALIDATION_URLS
    assert "https://ipv4.webshare.io/" in DEFAULT_VALIDATION_URLS
    assert "https://www.google.com/generate_204" in DEFAULT_VALIDATION_URLS
    assert "https://www.gstatic.com/generate_204" in DEFAULT_VALIDATION_URLS


def test_default_node_test_urls_only_use_primary_target():
    assert DEFAULT_NODE_TEST_URLS == [PRIMARY_TEST_URL]


def test_query_exit_ip_with_budget_returns_last_error():
    class FakeClient:
        async def get(self, url, timeout=None):
            raise RuntimeError(f"boom:{url}")

    ip, error = asyncio.run(tester_module._query_exit_ip_with_budget(FakeClient(), timeout_seconds=0.2))

    assert ip is None
    assert error is not None


def test_extract_public_ipv4_ignores_empty_or_private_responses():
    assert extract_public_ipv4("Found") is None
    assert extract_public_ipv4("<html>127.0.0.1</html>") is None
    assert extract_public_ipv4("ip=8.8.8.8\n") == "8.8.8.8"


@pytest.mark.parametrize("primary_status", [500, 404])
def test_fetch_first_test_url_falls_back(primary_status):
    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.text = ""

    class FakeClient:
        def __init__(self):
            self.urls = []

        async def get(self, url):
            self.urls.append(url)
            return FakeResponse(primary_status if len(self.urls) == 1 else 204)

    client = FakeClient()
    result = asyncio.run(_fetch_first_test_url(client, ["https://bad.example", PRIMARY_TEST_URL]))

    assert result["url"] == PRIMARY_TEST_URL
    assert result["status_code"] == 204
    assert "switched" in result["fallback_notice"]
