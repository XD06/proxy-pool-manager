import json

from app import settings


def test_current_performance_settings_low_profile_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "APP_CONFIG_PATH", tmp_path / "app.json")
    monkeypatch.delenv("PPM_PERFORMANCE_PROFILE", raising=False)
    monkeypatch.delenv("PPM_MAX_PORT_TEST_CONCURRENCY", raising=False)
    settings.APP_CONFIG_PATH.write_text(
        json.dumps({"performance_profile": "low"}),
        encoding="utf-8",
    )

    perf = settings.current_performance_settings()

    assert perf.profile == "low"
    assert perf.max_node_test_concurrency == 6
    assert perf.node_test_batch_size == 50
    assert perf.max_port_test_concurrency == 4
    assert perf.max_proxycheck_concurrency == 2
    assert perf.max_geoip_concurrency == 2
    assert perf.state_save_debounce_ms == 1000


def test_current_performance_settings_env_overrides_are_clamped(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "APP_CONFIG_PATH", tmp_path / "app.json")
    monkeypatch.setenv("PPM_PERFORMANCE_PROFILE", "low")
    monkeypatch.setenv("PPM_MAX_PORT_TEST_CONCURRENCY", "9999")
    monkeypatch.setenv("PPM_STATE_SAVE_DEBOUNCE_MS", "-50")

    perf = settings.current_performance_settings()

    assert perf.profile == "low"
    assert perf.max_port_test_concurrency == 128
    assert perf.state_save_debounce_ms == 0
