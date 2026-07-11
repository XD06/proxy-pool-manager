from __future__ import annotations

from typing import Iterable

from .models import PortMapping, ProxyNode
from pathlib import Path

from .settings import (
    SING_BOX_LOG_PATH,
    current_clash_api_addr,
    current_domain_resolve_strategy,
    current_proxy_listen_host,
)


class ConfigError(ValueError):
    pass


def _validate_port(port: int) -> None:
    if port < 1024 or port > 65535:
        raise ConfigError(f"Invalid port {port}; expected 1024-65535")


def generate_config(
    nodes: Iterable[ProxyNode],
    mappings: dict[str, PortMapping],
    *,
    include_clash_api: bool = True,
    log_path: Path | None = None,
    listen_host: str | None = None,
) -> dict:
    node_by_tag = {node.tag: node for node in nodes}
    inbounds: list[dict] = []
    outbounds: list[dict] = []
    rules: list[dict] = []
    used_tags: set[str] = set()
    proxy_listen_host = listen_host or current_proxy_listen_host()
    domain_resolve_strategy = current_domain_resolve_strategy()

    for port_text, mapping in sorted(mappings.items(), key=lambda item: int(item[0])):
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ConfigError(f"Invalid port {port_text}") from exc
        _validate_port(port)
        node = node_by_tag.get(mapping.node_tag)
        if not node:
            raise ConfigError(f"Node tag not found: {mapping.node_tag}")

        inbound_tag = f"port-{port}"
        inbounds.append(
            {
                "type": "mixed",
                "tag": inbound_tag,
                "listen": proxy_listen_host,
                "listen_port": port,
            }
        )
        rules.append(
            {
                "inbound": [inbound_tag],
                "action": "sniff",
            }
        )
        if domain_resolve_strategy:
            rules.append(
                {
                    "inbound": [inbound_tag],
                    "action": "resolve",
                    "strategy": domain_resolve_strategy,
                }
            )
        rules.append(
            {
                "inbound": [inbound_tag],
                "action": "route",
                "outbound": node.tag,
            }
        )
        if node.tag not in used_tags:
            outbound = dict(node.outbound)
            outbound["tag"] = node.tag
            outbound["tcp_fast_open"] = True
            outbounds.append(outbound)
            used_tags.add(node.tag)

    outbounds.append({"type": "direct", "tag": "direct"})
    outbounds.append({"type": "block", "tag": "block"})

    config = {
        "log": {
            "level": "warn",
            "output": str(log_path or SING_BOX_LOG_PATH),
            "disabled": False,
        },
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": {
            "rules": rules,
            "final": "direct",
            "auto_detect_interface": True,
        },
    }

    if domain_resolve_strategy:
        config["dns"] = {
            "servers": [
                {
                    "tag": "dns_direct",
                    "type": "local"
                }
            ],
            "strategy": domain_resolve_strategy,
        }
        config["route"]["default_domain_resolver"] = "dns_direct"
    if include_clash_api:
        config["experimental"] = {
            "clash_api": {
                "external_controller": current_clash_api_addr(),
                "secret": "",
            },
            "cache_file": {
                "enabled": True,
                "path": "config/cache.db",
            },
        }
    return config
