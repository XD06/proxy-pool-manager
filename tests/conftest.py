import pytest

from app import api as api_module


@pytest.fixture(autouse=True)
def isolate_runtime_paths(monkeypatch, tmp_path):
    """Keep every test away from the real config/ files and real processes.

    A missing POOL_ROUTER_CONFIG_PATH override once let the API test suite
    overwrite the production pool-router.json and kill the live router.
    """
    monkeypatch.setattr(api_module, "SING_BOX_CONFIG_PATH", tmp_path / "sing-box.json")
    monkeypatch.setattr(api_module, "SING_BOX_TEST_CONFIG_PATH", tmp_path / "sing-box-test.json")
    monkeypatch.setattr(api_module, "APP_CONFIG_PATH", tmp_path / "app.json")
    monkeypatch.setattr(api_module, "TRAFFIC_DB_PATH", tmp_path / "traffic.db")
    monkeypatch.setattr(api_module, "POOL_ROUTER_CONFIG_PATH", tmp_path / "pool-router.json")
    # Never let tests touch real processes: the router binary exists on dev
    # machines, and orphan cleanup filters by config path via taskkill.
    monkeypatch.setattr(api_module.PoolRouterManager, "available", lambda self: False)
    monkeypatch.setattr(api_module.PoolRouterManager, "_managed_processes", lambda self: [])
    monkeypatch.setattr(
        api_module.PoolRouterManager, "_stop_managed_orphans", lambda self, keep_pid=None: None
    )
    # Assign/pool validation does a real TCP bind check, so the suite would
    # otherwise depend on which ports happen to be free on the dev machine --
    # the live service commonly holds 8001-8003. Default every port to bindable;
    # tests that need busy/reserved ports override this with their own setattr,
    # which wins because the test body runs after this autouse fixture.
    monkeypatch.setattr(api_module, "_can_bind_tcp_port", lambda port: True)
