import asyncio

import pytest

from app import pool_router as pool_router_module
from app.pool_router import PoolRouterError, PoolRouterManager


class FakeProcess:
    def __init__(self):
        self.pid = 4567
        self.returncode = None
        self.stderr = None
        self.stdout = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_pool_router_start_handles_a_concurrent_stop(tmp_path, monkeypatch):
    config = tmp_path / "pool-router.json"
    binary = tmp_path / "pool-router.exe"
    config.write_text("{}", encoding="utf-8")
    binary.write_bytes(b"")
    manager = PoolRouterManager(config)
    process = FakeProcess()

    async def immediate_sleep(_delay):
        return None

    async def interrupted_status():
        await manager.stop()
        raise PoolRouterError("control endpoint is not ready")

    captured = {}

    def fake_popen(*args, **kwargs):
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(manager, "binary_path", lambda: binary)
    monkeypatch.setattr(manager, "_stop_managed_orphans", lambda keep_pid=None: None)
    monkeypatch.setattr(manager, "status", interrupted_status)
    monkeypatch.setattr(pool_router_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(pool_router_module.asyncio, "sleep", immediate_sleep)

    with pytest.raises(PoolRouterError, match="start was interrupted"):
        asyncio.run(manager.start())

    assert manager.process is None
    assert captured["kwargs"]["stdout"] is pool_router_module.subprocess.DEVNULL
    # stderr is a file handle opened by start(); parent closes it after spawn.
    assert captured["kwargs"]["stderr"] is not None
    assert captured["kwargs"]["stderr"] is not pool_router_module.subprocess.PIPE


def test_pool_router_payload_uses_cached_orphan_discovery(tmp_path, monkeypatch):
    manager = PoolRouterManager(tmp_path / "pool-router.json")
    calls = []

    def discover():
        calls.append("discover")
        return [9876]

    monkeypatch.setattr(manager, "_managed_processes", discover)

    assert manager.payload()["running"] is False
    assert calls == []

    refreshed = asyncio.run(manager.refresh_runtime_payload())

    assert refreshed["running"] is True
    assert refreshed["pid"] == 9876
    assert calls == ["discover"]
    assert manager.payload()["pid"] == 9876
    assert calls == ["discover"]
