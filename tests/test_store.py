import json

import pytest

from app.models import AppState, ProxyNode
from app.store import StateStore, StateStoreError


def test_store_round_trip(tmp_path):
    path = tmp_path / "assignments.json"
    store = StateStore(path)
    state = AppState()
    state.nodes.append(
        ProxyNode(
            tag="node-a",
            name="Node A",
            type="vless",
            server="example.com",
            server_port=443,
            outbound={
                "type": "vless",
                "tag": "node-a",
                "server": "example.com",
                "server_port": 443,
                "uuid": "00000000-0000-0000-0000-000000000000",
            },
        )
    )
    store.save(state)
    loaded = store.load()
    assert loaded.nodes[0].tag == "node-a"


def test_store_migrates_legacy_shadowsocks_plugin_options(tmp_path):
    path = tmp_path / "assignments.json"
    state = AppState(
        nodes=[
            ProxyNode(
                tag="node-ss-a",
                name="SS",
                type="shadowsocks",
                server="ss.example.com",
                server_port=443,
                outbound={
                    "type": "shadowsocks",
                    "server": "ss.example.com",
                    "server_port": 443,
                    "method": "aes-256-gcm",
                    "password": "secret",
                    "plugin": {"type": "v2ray-plugin", "tls": True, "host": "cdn.example.com", "path": "/ws"},
                },
            )
        ]
    )
    StateStore(path).save(state)

    loaded = StateStore(path).load()

    assert loaded.nodes[0].outbound["plugin"] == "v2ray-plugin"
    assert loaded.nodes[0].outbound["plugin_opts"] == "tls;host=cdn.example.com;path=/ws"


def test_store_migrates_legacy_subscription_to_source_and_group(tmp_path):
    path = tmp_path / "assignments.json"
    state = AppState(
        subscription_url="https://example.com/sub",
        subscription_refresh_interval_minutes=60,
        nodes=[
            ProxyNode(
                tag="node-a",
                name="Node A",
                type="vless",
                server="example.com",
                server_port=443,
                outbound={"type": "vless", "tag": "node-a", "server": "example.com", "server_port": 443},
            )
        ],
    )
    StateStore(path).save(state)

    loaded = StateStore(path).load()

    assert len(loaded.subscription_sources) == 1
    assert loaded.subscription_sources[0].url == "https://example.com/sub"
    assert loaded.node_groups[0].kind == "subscription"
    assert loaded.node_groups[0].node_tags == ["node-a"]
    assert loaded.node_groups[0].source_id == loaded.subscription_sources[0].id


def test_store_discards_unverified_fast_test_cache_with_a_backup(tmp_path):
    path = tmp_path / "assignments.json"
    path.write_text(
        json.dumps(
            {
                "latency_cache": {
                    "fast-screen": {
                        "alive": False,
                        "verified": False,
                        "assessment": "transport_unreachable",
                        "error": "TCP preflight failed",
                    },
                    "full-test": {
                        "alive": True,
                        "verified": True,
                        "assessment": "verified",
                        "delay": 120,
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = StateStore(path).load()

    assert set(loaded.latency_cache) == {"full-test"}
    backups = list(tmp_path.glob("assignments.json.pre-speed-test-rollback*"))
    assert len(backups) == 1
    assert "fast-screen" in backups[0].read_text(encoding="utf-8")


def test_store_backs_up_invalid_json_without_replacing_the_original(tmp_path):
    path = tmp_path / "assignments.json"
    original = "{bad json"
    path.write_text(original, encoding="utf-8")
    store = StateStore(path)
    with pytest.raises(StateStoreError):
        store.load()
    assert store.warning
    backups = list(tmp_path.glob("assignments.json.bak-*"))
    assert len(backups) == 1
    assert path.read_text(encoding="utf-8") == original
    assert backups[0].read_text(encoding="utf-8") == original
