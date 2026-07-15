import time

from app.traffic import TrafficStore


def test_traffic_store_aggregates_counter_deltas_and_never_records_negative_values(tmp_path):
    store = TrafficStore(tmp_path / "traffic.db")
    now = int(time.time())
    store.sample(source_key="listener:port-8001", entity_type="port", entity_id="8001", upload=100, download=200, active=2, now=now)
    store.sample(source_key="listener:port-8001", entity_type="port", entity_id="8001", upload=160, download=260, active=3, now=now + 20)
    store.sample(source_key="listener:port-8001", entity_type="port", entity_id="8001", upload=10, download=20, active=1, now=now + 30)

    detail = store.detail("port", "8001", 10_000_000)

    assert detail["upload"] == 60
    assert detail["download"] == 60
    assert max(item["active_peak"] for item in detail["series"]) == 3


def test_traffic_store_filters_events_without_storing_request_data(tmp_path):
    store = TrafficStore(tmp_path / "traffic.db")
    store.event("pool_advanced", "Advanced next selection", entity_type="pool", entity_id="pool-a", now=1_700_000_001)
    store.event("pool_created", "Created pool", entity_type="pool", entity_id="pool-b", now=1_700_000_002)

    events = store.events(entity_type="pool", entity_id="pool-a")

    assert [item["event_type"] for item in events] == ["pool_advanced"]
