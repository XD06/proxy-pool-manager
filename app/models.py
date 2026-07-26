from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ProxyNode(BaseModel):
    tag: str
    name: str
    type: str
    server: str
    server_port: int
    outbound: dict[str, Any]


class PortMapping(BaseModel):
    node_tag: str


class PoolMember(BaseModel):
    node_tag: str
    weight: int = Field(default=1, ge=1, le=100)
    enabled: bool = True
    draining: bool = False


class ProxyPool(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    name: str
    listen_port: int = Field(ge=1024, le=65535)
    policy: Literal["round_robin", "weighted_round_robin", "time_window"] = "weighted_round_robin"
    rotation_interval_seconds: int = Field(default=600, ge=10, le=86_400)
    enabled: bool = True
    members: list[PoolMember] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class LatencyResult(BaseModel):
    alive: bool
    delay: int | None = None
    exit_ip: str | None = None
    geoip: dict[str, Any] | None = None
    target_results: list[dict[str, Any]] | None = None
    test_port: int | None = None
    target_url: str | None = None
    status_code: int | None = None
    body_preview: str | None = None
    error: str | None = None
    checked_at: str = Field(default_factory=utc_now_iso)

class ExitIpCache(BaseModel):
    ip: str | None = None
    geoip: dict[str, Any] | None = None
    error: str | None = None
    checked_at: str = Field(default_factory=utc_now_iso)


class GeoIpResult(BaseModel):
    ip: str
    country: str | None = None
    country_code: str | None = None
    region: str | None = None
    city: str | None = None
    asn: str | None = None
    org: str | None = None
    isp: str | None = None
    source: str = "ip-api"
    confidence: str = "reference"
    error: str | None = None
    checked_at: str = Field(default_factory=utc_now_iso)


class NodeGroup(BaseModel):
    """A durable, user-visible collection of nodes.

    Groups deliberately store tags rather than own nodes. A node may therefore
    appear in its import source and in one or more hand-curated quality groups.
    """

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["subscription", "import_batch", "manual", "quality_snapshot"] = "manual"
    node_tags: list[str] = Field(default_factory=list)
    source_id: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class SubscriptionSource(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    name: str = Field(min_length=1, max_length=80)
    url: str
    refresh_interval_minutes: int = Field(default=0, ge=0, le=10_080)
    last_refresh_at: str | None = None
    last_error: str | None = None
    last_count: int = 0
    group_id: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class AppState(BaseModel):
    version: int = 1
    updated_at: str = Field(default_factory=utc_now_iso)
    subscription_url: str | None = None
    subscription_refresh_interval_minutes: int = 0
    subscription_last_refresh_at: str | None = None
    subscription_last_error: str | None = None
    subscription_last_count: int = 0
    subscription_sources: list[SubscriptionSource] = Field(default_factory=list)
    node_groups: list[NodeGroup] = Field(default_factory=list)
    nodes: list[ProxyNode] = Field(default_factory=list)
    port_mappings: dict[str, PortMapping] = Field(default_factory=dict)
    pools: list[ProxyPool] = Field(default_factory=list)
    latency_cache: dict[str, LatencyResult] = Field(default_factory=dict)
    exit_ip_cache: dict[str, ExitIpCache] = Field(default_factory=dict)
    geoip_cache: dict[str, GeoIpResult] = Field(default_factory=dict)
    local_proxy_check_results: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ImportDiagnostic(BaseModel):
    kind: str
    message: str
    scheme: str | None = None


class ImportResult(BaseModel):
    nodes: list[ProxyNode]
    count: int
    warnings: list[str] = Field(default_factory=list)
    diagnostics: list[ImportDiagnostic] = Field(default_factory=list)


class EngineStatus(BaseModel):
    running: bool = False
    pid: int | None = None
    uptime_seconds: int | None = None
    fatal: bool = False
    last_error: str | None = None
