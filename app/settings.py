from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
BIN_DIR = ROOT_DIR / "bin"
TEMPLATES_DIR = ROOT_DIR / "templates"
STATIC_DIR = ROOT_DIR / "static"

STATE_PATH = CONFIG_DIR / "assignments.json"
APP_CONFIG_PATH = CONFIG_DIR / "app.json"
SING_BOX_CONFIG_PATH = CONFIG_DIR / "sing-box.json"
SING_BOX_TEST_CONFIG_PATH = CONFIG_DIR / "sing-box-test.json"
SING_BOX_LOG_PATH = CONFIG_DIR / "sing-box.log"
POOL_ROUTER_CONFIG_PATH = CONFIG_DIR / "pool-router.json"
TRAFFIC_DB_PATH = CONFIG_DIR / "traffic.db"

def _load_app_config() -> dict:
    if not APP_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _setting(config: dict, key: str, env_name: str, default: str) -> str:
    return os.environ.get(env_name) or str(config.get(key) or default)


def _int_setting(
    config: dict,
    key: str,
    env_name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = 10_000,
) -> int:
    raw = os.environ.get(env_name)
    if raw is None:
        raw = config.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def runtime_setting(key: str, env_name: str, default: str) -> str:
    config = _load_app_config()
    return _setting(config, key, env_name, default)


_APP_CONFIG = _load_app_config()

HOST = _setting(_APP_CONFIG, "host", "PPM_HOST", "127.0.0.1")
PORT = int(_setting(_APP_CONFIG, "port", "PPM_PORT", "9000"))
PROXY_LISTEN_HOST = _setting(_APP_CONFIG, "proxy_listen_host", "PPM_PROXY_LISTEN_HOST", "127.0.0.1")
PROXY_PUBLIC_HOST = os.environ.get("PPM_PROXY_PUBLIC_HOST") or str(_APP_CONFIG.get("proxy_public_host") or "")
CLASH_API_ADDR = _setting(_APP_CONFIG, "clash_api_addr", "PPM_CLASH_API_ADDR", "127.0.0.1:9090")
DOMAIN_RESOLVE_STRATEGY = _setting(
    _APP_CONFIG,
    "domain_resolve_strategy",
    "PPM_DOMAIN_RESOLVE_STRATEGY",
    str(_APP_CONFIG.get("outbound_domain_strategy") or ""),
)
ASSET_VERSION = _setting(_APP_CONFIG, "asset_version", "PPM_ASSET_VERSION", "20260716-traffic-pools")
DEFAULT_START_PORT = 8001
TEST_START_PORT = 19001
POOL_ROUTER_CONTROL_ADDR = _setting(_APP_CONFIG, "pool_router_control_addr", "PPM_POOL_ROUTER_CONTROL_ADDR", "127.0.0.1:9091")


def current_proxy_listen_host() -> str:
    return runtime_setting("proxy_listen_host", "PPM_PROXY_LISTEN_HOST", PROXY_LISTEN_HOST)


def current_proxy_public_host() -> str:
    return runtime_setting("proxy_public_host", "PPM_PROXY_PUBLIC_HOST", PROXY_PUBLIC_HOST)


def current_clash_api_addr() -> str:
    return runtime_setting("clash_api_addr", "PPM_CLASH_API_ADDR", CLASH_API_ADDR)


def current_pool_router_control_addr() -> str:
    return runtime_setting("pool_router_control_addr", "PPM_POOL_ROUTER_CONTROL_ADDR", POOL_ROUTER_CONTROL_ADDR)


def current_domain_resolve_strategy() -> str:
    config = _load_app_config()
    fallback = str(config.get("outbound_domain_strategy") or DOMAIN_RESOLVE_STRATEGY)
    return _setting(config, "domain_resolve_strategy", "PPM_DOMAIN_RESOLVE_STRATEGY", fallback)


@dataclass(frozen=True)
class PerformanceSettings:
    profile: str
    max_node_test_concurrency: int
    node_test_batch_size: int
    max_port_test_concurrency: int
    max_proxycheck_concurrency: int
    max_geoip_concurrency: int
    max_proxy_admin_concurrency: int
    state_save_debounce_ms: int
    job_retention_minutes: int
    max_jobs_per_type: int


def current_performance_settings() -> PerformanceSettings:
    config = _load_app_config()
    profile = _setting(config, "performance_profile", "PPM_PERFORMANCE_PROFILE", "normal").lower()
    low = profile == "low"
    return PerformanceSettings(
        profile=profile,
        max_node_test_concurrency=_int_setting(
            config,
            "max_node_test_concurrency",
            "PPM_MAX_NODE_TEST_CONCURRENCY",
            6 if low else 6,
            minimum=1,
            maximum=64,
        ),
        node_test_batch_size=_int_setting(
            config,
            "node_test_batch_size",
            "PPM_NODE_TEST_BATCH_SIZE",
            50 if low else 100,
            minimum=1,
            maximum=1000,
        ),
        max_port_test_concurrency=_int_setting(
            config,
            "max_port_test_concurrency",
            "PPM_MAX_PORT_TEST_CONCURRENCY",
            4 if low else 8,
            minimum=1,
            maximum=128,
        ),
        max_proxycheck_concurrency=_int_setting(
            config,
            "max_proxycheck_concurrency",
            "PPM_MAX_PROXYCHECK_CONCURRENCY",
            2 if low else 10,
            minimum=1,
            maximum=32,
        ),
        max_geoip_concurrency=_int_setting(
            config,
            "max_geoip_concurrency",
            "PPM_MAX_GEOIP_CONCURRENCY",
            2 if low else 4,
            minimum=1,
            maximum=32,
        ),
        max_proxy_admin_concurrency=_int_setting(
            config,
            "max_proxy_admin_concurrency",
            "PPM_MAX_PROXY_ADMIN_CONCURRENCY",
            8 if low else 30,
            minimum=1,
            maximum=64,
        ),
        state_save_debounce_ms=_int_setting(
            config,
            "state_save_debounce_ms",
            "PPM_STATE_SAVE_DEBOUNCE_MS",
            1000 if low else 500,
            minimum=0,
            maximum=60_000,
        ),
        job_retention_minutes=_int_setting(
            config,
            "job_retention_minutes",
            "PPM_JOB_RETENTION_MINUTES",
            60,
            minimum=1,
            maximum=10_080,
        ),
        max_jobs_per_type=_int_setting(
            config,
            "max_jobs_per_type",
            "PPM_MAX_JOBS_PER_TYPE",
            20,
            minimum=1,
            maximum=1000,
        ),
    )
