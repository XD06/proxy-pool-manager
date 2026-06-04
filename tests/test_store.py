from app.models import AppState, ProxyNode
from app.store import StateStore


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


def test_store_backs_up_invalid_json(tmp_path):
    path = tmp_path / "assignments.json"
    path.write_text("{bad json", encoding="utf-8")
    store = StateStore(path)
    state = store.load()
    assert state.nodes == []
    assert store.warning
    assert list(tmp_path.glob("assignments.json.bak-*"))
