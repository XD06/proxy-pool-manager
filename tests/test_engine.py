import asyncio
import io
import json
import platform
from pathlib import Path

import pytest

from app.engine import EngineError, EngineManager, _config_listen_ports
from app.models import EngineStatus


def test_status_uses_cached_managed_sing_box_process(monkeypatch):
    manager = EngineManager()
    calls = []

    def discover(config_path=None):
        calls.append(config_path)
        return [{"ProcessId": 1234, "ExecutablePath": "bin/sing-box.exe"}]

    monkeypatch.setattr(
        manager,
        "_managed_processes",
        discover,
    )

    assert manager.status().running is False
    assert calls == []

    asyncio.run(manager.refresh_runtime_status())
    status = manager.status()

    assert status.running is True
    assert status.pid == 1234
    assert status.last_error is None
    assert calls == [None]


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


def test_concurrent_start_stop_does_not_corrupt_process(monkeypatch):
    manager = EngineManager()
    test_config = Path("config/sing-box.json")

    class FakeProc:
        pid = 4321

        def __init__(self):
            self.returncode = None
            self.stderr = io.BytesIO()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = 0

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    async def run():
        monkeypatch.setattr(manager, "ensure_binary", lambda: asyncio.sleep(0, result=Path("sing-box.exe")))
        monkeypatch.setattr(manager, "_kill_managed_orphans", lambda keep_pid=None, config_path=None: None)
        monkeypatch.setattr("app.engine._unavailable_ports", lambda ports: [])
        monkeypatch.setattr("app.engine.subprocess.Popen", lambda *args, **kwargs: FakeProc())
        await asyncio.gather(
            manager.start(test_config, check=False, settle_seconds=0.01),
            manager.stop(config_path=test_config),
        )

    asyncio.run(run())
    assert manager.process is None


def test_config_listen_ports_includes_inbounds_and_clash_api(tmp_path):
    config_path = tmp_path / "sing-box.json"
    config_path.write_text(
        json.dumps(
            {
                "inbounds": [
                    {"type": "mixed", "listen_port": 8001},
                    {"type": "mixed", "listen_port": 8002},
                ],
                "experimental": {
                    "clash_api": {
                        "external_controller": "127.0.0.1:10000",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert _config_listen_ports(config_path) == [8001, 8002, 10000]


def test_start_fails_before_popen_when_config_ports_are_unavailable(monkeypatch, tmp_path):
    manager = EngineManager()
    config_path = tmp_path / "sing-box.json"
    config_path.write_text(json.dumps({"inbounds": [{"listen_port": 8001}]}), encoding="utf-8")
    popen_called = False

    def fake_popen(*args, **kwargs):
        nonlocal popen_called
        popen_called = True
        raise AssertionError("Popen should not be called when ports are unavailable")

    async def run():
        monkeypatch.setattr(manager, "ensure_binary", lambda: asyncio.sleep(0, result=Path("sing-box.exe")))
        monkeypatch.setattr(manager, "_kill_managed_orphans", lambda keep_pid=None, config_path=None: None)
        monkeypatch.setattr("app.engine._unavailable_ports", lambda ports: ports)
        monkeypatch.setattr("app.engine.subprocess.Popen", fake_popen)
        with pytest.raises(EngineError, match="Ports are not available: 8001"):
            await manager.start(config_path, check=False, settle_seconds=0, port_wait_timeout=0)

    asyncio.run(run())
    assert not popen_called


def test_managed_processes_parses_linux_ps_output(monkeypatch):
    config_path = Path("/tmp/sing-box-test.json")
    manager = EngineManager(config_path)
    config_text = str(config_path)

    class Completed:
        returncode = 0
        stdout = f"1234 /usr/bin/sing-box run -c {config_text}\n5678 other-process\n"

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr("app.engine.subprocess.run", lambda *args, **kwargs: Completed())

    managed = manager._managed_processes(config_path)

    assert managed == [
        {
            "ProcessId": 1234,
            "ExecutablePath": None,
            "CommandLine": f"/usr/bin/sing-box run -c {config_text}",
        }
    ]


def test_release_asset_detects_platform_and_architecture(monkeypatch):
    manager = EngineManager()
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    release = {
        "tag_name": "v1.13.14",
        "assets": [
            {
                "name": "sing-box-1.13.14-linux-arm64.tar.gz",
                "browser_download_url": "https://example.test/sing-box.tar.gz",
            }
        ],
    }

    asset, version = manager._release_asset(release)

    assert version == "1.13.14"
    assert asset["name"] == "sing-box-1.13.14-linux-arm64.tar.gz"


def test_update_and_rollback_managed_binary(monkeypatch, tmp_path):
    manager = EngineManager(tmp_path / "sing-box.json")
    monkeypatch.setattr("app.engine.BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr("app.engine.SING_BOX_CONFIG_PATH", tmp_path / "sing-box.json")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    target = manager.binary_path()
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")

    monkeypatch.setattr(manager, "_binary_version", lambda path: "1.13.13" if path.read_text(encoding="utf-8") == "old" else "1.13.14")
    monkeypatch.setattr(
        manager,
        "_latest_release",
        lambda: asyncio.sleep(0, result={
            "tag_name": "v1.13.14",
            "assets": [{
                "name": "sing-box-1.13.14-windows-amd64.zip",
                "browser_download_url": "https://example.test/sing-box.zip",
            }],
        }),
    )

    async def fake_download(release, destination):
        destination.write_text("new", encoding="utf-8")
        return "1.13.14"

    monkeypatch.setattr(manager, "_download_release_binary", fake_download)
    monkeypatch.setattr(manager, "status", lambda: EngineStatus(running=False))

    updated = asyncio.run(manager.update_binary())

    assert updated["updated"] is True
    assert updated["current_version"] == "1.13.14"
    assert target.read_text(encoding="utf-8") == "new"
    assert manager.backup_path().read_text(encoding="utf-8") == "old"

    rolled_back = asyncio.run(manager.rollback_binary())

    assert rolled_back["rolled_back"] is True
    assert rolled_back["current_version"] == "1.13.13"
    assert target.read_text(encoding="utf-8") == "old"
    assert manager.backup_path().read_text(encoding="utf-8") == "new"
