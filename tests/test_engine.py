import asyncio
from pathlib import Path

from app.engine import EngineManager


def test_status_detects_managed_sing_box_process(monkeypatch):
    manager = EngineManager()
    monkeypatch.setattr(
        manager,
        "_managed_processes",
        lambda config_path=None: [{"ProcessId": 1234, "ExecutablePath": "bin/sing-box.exe"}],
    )

    status = manager.status()

    assert status.running is True
    assert status.pid == 1234
    assert status.last_error == "project sing-box process detected"


def test_stop_cleans_managed_orphans_when_no_tracked_process(monkeypatch):
    manager = EngineManager()
    calls = []
    monkeypatch.setattr(manager, "_kill_managed_orphans", lambda keep_pid=None, config_path=None: calls.append((keep_pid, config_path)))

    asyncio.run(manager.stop())

    assert calls == [(None, None)]


def test_stop_can_target_config_specific_orphans(monkeypatch):
    manager = EngineManager()
    calls = []
    test_config = Path("config/sing-box-test.json")
    monkeypatch.setattr(manager, "_kill_managed_orphans", lambda keep_pid=None, config_path=None: calls.append((keep_pid, config_path)))

    asyncio.run(manager.stop(config_path=test_config))

    assert calls == [(None, test_config)]
