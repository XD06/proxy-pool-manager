from __future__ import annotations

from typing import Iterable

from .models import PortMapping, ProxyNode, ProxyPool
from pathlib import Path

from .settings import (
    SING_BOX_LOG_PATH,
    current_clash_api_addr,
    current_domain_resolve_strategy,
    current_pool_router_control_addr,
    current_proxy_listen_host,
)


class ConfigError(ValueError):
    pass


def _validate_port(port: int) -> None:
    if port < 1024 or port > 65535:
        raise ConfigError(f"Invalid port {port}; expected 1024-65535")


def _runtime_inbound_plan(
    mappings: dict[str, PortMapping],
    pools: list[ProxyPool],
    *,
    router_mode: bool,
) -> list[dict]:
    """Return deterministic sing-box inbound records used by both runtimes."""
    public_ports: set[int] = set()
    plan: list[dict] = []
    for port_text, mapping in sorted(mappings.items(), key=lambda item: int(item[0])):
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ConfigError(f"Invalid port {port_text}") from exc
        _validate_port(port)
        if port in public_ports:
            raise ConfigError(f"Duplicate public port: {port}")
        public_ports.add(port)
        plan.append({"kind": "port", "id": str(port), "public_port": port, "node_tag": mapping.node_tag})
    for pool in sorted(pools, key=lambda item: item.id):
        _validate_port(pool.listen_port)
        if pool.listen_port in public_ports:
            raise ConfigError(f"Pool {pool.name} uses an already assigned port: {pool.listen_port}")
        public_ports.add(pool.listen_port)
        # A disabled pool owns its public port in persistent state but must not
        # create internal sing-box listeners or outbound routes at runtime.
        if not pool.enabled:
            continue
        if pool.enabled and not any(member.enabled and not member.draining for member in pool.members):
            raise ConfigError(f"Pool {pool.name} has no active members")
        for member in sorted(pool.members, key=lambda item: item.node_tag):
            if not member.enabled:
                continue
            plan.append(
                {
                    "kind": "pool_member",
                    "id": pool.id,
                    "public_port": pool.listen_port,
                    "node_tag": member.node_tag,
                    "member": member,
                }
            )
    if pools and not router_mode:
        raise ConfigError("Node pools require the pool-router binary")
    if not router_mode:
        for item in plan:
            item["listen_port"] = item["public_port"]
        return plan

    used = set(public_ports)
    next_internal = 18000
    for item in plan:
        while next_internal in used:
            next_internal += 1
        if next_internal > 65535:
            raise ConfigError("No internal port available for pool router")
        item["listen_port"] = next_internal
        used.add(next_internal)
        next_internal += 1
    return plan


def generate_config(
    nodes: Iterable[ProxyNode],
    mappings: dict[str, PortMapping],
    *,
    pools: list[ProxyPool] | None = None,
    router_mode: bool = False,
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

    plan = _runtime_inbound_plan(mappings, pools or [], router_mode=router_mode)
    for item in plan:
        node = node_by_tag.get(item["node_tag"])
        if not node:
            raise ConfigError(f"Node tag not found: {item['node_tag']}")

        if item["kind"] == "port":
            inbound_tag = f"port-{item['id']}"
        else:
            inbound_tag = f"pool-member-{item['id']}-{node.tag}"
        inbounds.append(
            {
                "type": "mixed",
                "tag": inbound_tag,
                "listen": "127.0.0.1" if router_mode else proxy_listen_host,
                "listen_port": item["listen_port"],
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
            if outbound.get("type") == "anytls":
                outbound.pop("tcp_fast_open", None)
            else:
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


def generate_pool_router_config(
    mappings: dict[str, PortMapping],
    pools: list[ProxyPool],
    *,
    listen_host: str | None = None,
    unhealthy_node_tags: set[str] | None = None,
) -> dict:
    """Generate the Go edge-router configuration for public proxy ports."""
    plan = _runtime_inbound_plan(mappings, pools, router_mode=True)
    unhealthy = unhealthy_node_tags or set()
    fixed = [item for item in plan if item["kind"] == "port"]
    members = [item for item in plan if item["kind"] == "pool_member"]
    listeners: list[dict] = [
        {
            "id": f"port-{item['id']}",
            "listen": f"{listen_host or current_proxy_listen_host()}:{item['public_port']}",
            "policy": "round_robin",
            "backends": [{"id": item["node_tag"], "address": f"127.0.0.1:{item['listen_port']}", "weight": 1, "enabled": True}],
        }
        for item in fixed
    ]
    for pool in sorted(pools, key=lambda item: item.id):
        pool_members = [item for item in members if item["id"] == pool.id]
        if not pool.enabled:
            continue
        listeners.append(
            {
                "id": f"pool-{pool.id}",
                "listen": f"{listen_host or current_proxy_listen_host()}:{pool.listen_port}",
                "policy": pool.policy,
                "rotation_interval_seconds": pool.rotation_interval_seconds,
                "backends": [
                    {
                        "id": item["node_tag"],
                        "address": f"127.0.0.1:{item['listen_port']}",
                        "weight": item["member"].weight,
                        "enabled": item["member"].enabled and not item["member"].draining and item["node_tag"] not in unhealthy,
                        "draining": item["member"].draining,
                    }
                    for item in pool_members
                ],
            }
        )
    return {"control_listen": current_pool_router_control_addr(), "listeners": listeners}
