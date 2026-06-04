from __future__ import annotations

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

def _load_app_config() -> dict:
    if not APP_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _setting(config: dict, key: str, env_name: str, default: str) -> str:
    return os.environ.get(env_name) or str(config.get(key) or default)


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
DEFAULT_START_PORT = 8001
TEST_START_PORT = 19001


def current_proxy_listen_host() -> str:
    return runtime_setting("proxy_listen_host", "PPM_PROXY_LISTEN_HOST", PROXY_LISTEN_HOST)


def current_proxy_public_host() -> str:
    return runtime_setting("proxy_public_host", "PPM_PROXY_PUBLIC_HOST", PROXY_PUBLIC_HOST)


def current_clash_api_addr() -> str:
    return runtime_setting("clash_api_addr", "PPM_CLASH_API_ADDR", CLASH_API_ADDR)


def current_domain_resolve_strategy() -> str:
    config = _load_app_config()
    fallback = str(config.get("outbound_domain_strategy") or DOMAIN_RESOLVE_STRATEGY)
    return _setting(config, "domain_resolve_strategy", "PPM_DOMAIN_RESOLVE_STRATEGY", fallback)
