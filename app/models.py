from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

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


class AppState(BaseModel):
    version: int = 1
    updated_at: str = Field(default_factory=utc_now_iso)
    subscription_url: str | None = None
    nodes: list[ProxyNode] = Field(default_factory=list)
    port_mappings: dict[str, PortMapping] = Field(default_factory=dict)
    latency_cache: dict[str, LatencyResult] = Field(default_factory=dict)
    exit_ip_cache: dict[str, ExitIpCache] = Field(default_factory=dict)
    geoip_cache: dict[str, GeoIpResult] = Field(default_factory=dict)


class ImportResult(BaseModel):
    nodes: list[ProxyNode]
    count: int
    warnings: list[str] = Field(default_factory=list)


class EngineStatus(BaseModel):
    running: bool = False
    pid: int | None = None
    uptime_seconds: int | None = None
    fatal: bool = False
    last_error: str | None = None
