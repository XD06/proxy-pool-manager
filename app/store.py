from __future__ import annotations

import json
import shutil
from pathlib import Path

from .models import AppState, NodeGroup, SubscriptionSource, utc_now_iso
from .settings import CONFIG_DIR, STATE_PATH


class StateStoreError(RuntimeError):
    """The persisted state cannot be read without risking data loss."""


def _migrate_shadowsocks_plugin(outbound: dict) -> bool:
    plugin = outbound.get("plugin")
    if not isinstance(plugin, dict):
        return False
    plugin_name = str(plugin.get("type") or "").strip()
    if not plugin_name:
        outbound.pop("plugin", None)
        outbound.pop("plugin_opts", None)
        return True

    options = plugin.get("options")
    if isinstance(options, list):
        plugin_options = [str(option) for option in options if str(option)]
    elif isinstance(options, str):
        plugin_options = [option for option in options.split(";") if option]
    else:
        plugin_options = []

    if plugin_name == "v2ray-plugin":
        if plugin.get("tls"):
            plugin_options.append("tls")
        if plugin.get("host"):
            plugin_options.append(f"host={plugin['host']}")
        if plugin.get("path"):
            plugin_options.append(f"path={plugin['path']}")
    elif plugin_name in {"obfs-local", "simple-obfs"}:
        if plugin.get("mode"):
            plugin_options.append(f"obfs={plugin['mode']}")
        if plugin.get("host"):
            plugin_options.append(f"obfs-host={plugin['host']}")

    outbound["plugin"] = plugin_name
    if plugin_options:
        outbound["plugin_opts"] = ";".join(dict.fromkeys(plugin_options))
    else:
        outbound.pop("plugin_opts", None)
    return True


def _strip_reality_spider_x(outbound: dict) -> bool:
    """Drop the Xray-only reality field that makes sing-box reject the config."""
    tls = outbound.get("tls")
    if not isinstance(tls, dict):
        return False
    reality = tls.get("reality")
    if not isinstance(reality, dict) or "spider_x" not in reality:
        return False
    reality.pop("spider_x", None)
    return True


def _discard_unverified_fast_test_cache(data: dict) -> bool:
    """Remove stale TCP-preflight results before they can affect pool health.

    The removed fast-screen results never completed a proxy handshake. Keeping
    them after returning to full sing-box validation would incorrectly mark
    nodes as failed. The caller snapshots the original state before saving the
    migrated cache.
    """
    cache = data.get("latency_cache")
    if not isinstance(cache, dict):
        return False
    stale_tags = [
        tag
        for tag, result in cache.items()
        if isinstance(result, dict) and result.get("verified") is False
    ]
    for tag in stale_tags:
        cache.pop(tag, None)
    return bool(stale_tags)


def _migrate_state(state: AppState) -> bool:
    changed = False
    for node in state.nodes:
        if node.outbound.get("type") == "shadowsocks":
            changed = _migrate_shadowsocks_plugin(node.outbound) or changed
        changed = _strip_reality_spider_x(node.outbound) or changed
    # Preserve old single-subscription installations while making the new
    # source/group model immediately useful after upgrade.
    if state.subscription_url and not state.subscription_sources:
        source = SubscriptionSource(
            name="默认订阅",
            url=state.subscription_url,
            refresh_interval_minutes=state.subscription_refresh_interval_minutes,
            last_refresh_at=state.subscription_last_refresh_at,
            last_error=state.subscription_last_error,
            last_count=state.subscription_last_count,
        )
        group = NodeGroup(
            name="默认订阅",
            kind="subscription",
            source_id=source.id,
            node_tags=[node.tag for node in state.nodes],
        )
        source.group_id = group.id
        state.subscription_sources.append(source)
        state.node_groups.append(group)
        changed = True
    return changed


class StateStore:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.warning: str | None = None
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    def load(self) -> AppState:
        self.warning = None
        if not self.path.exists():
            state = AppState()
            self.save(state)
            return state
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            dropped_fast_test_cache = _discard_unverified_fast_test_cache(data)
            if dropped_fast_test_cache:
                backup = self.path.with_name(f"{self.path.name}.pre-speed-test-rollback")
                if not backup.exists():
                    shutil.copy2(self.path, backup)
            state = AppState.model_validate(data)
            if dropped_fast_test_cache or _migrate_state(state):
                self.save(state)
            return state
        except Exception as exc:
            backup = self.path.with_name(f"{self.path.name}.bak-{utc_now_iso().replace(':', '-')}")
            try:
                shutil.copy2(self.path, backup)
                self.warning = f"State file could not be loaded; the original was preserved in {backup.name}: {exc}"
            except Exception:
                self.warning = f"State file could not be loaded and could not be backed up: {exc}"
            raise StateStoreError(self.warning) from exc

    def save(self, state: AppState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state.updated_at = utc_now_iso()
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(state.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)
