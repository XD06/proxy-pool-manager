"""Pydantic request/response models and job models extracted from api.py."""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from .models import PoolMember


# ── Request models ──────────────────────────────────────────────────────────


class ImportRequest(BaseModel):
    url: str | None = None
    text: str | None = None


class LoginRequest(BaseModel):
    key: str


class SubscriptionConfigRequest(BaseModel):
    url: str | None = None
    refresh_interval_minutes: int = 0


class SubscriptionSourceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1)
    refresh_interval_minutes: int = Field(default=0, ge=0, le=10_080)


class NodeGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str = "manual"
    node_tags: list[str] = Field(default_factory=list)


class AssignRequest(BaseModel):
    mappings: dict[str, str]


class PoolUpsertRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    listen_port: int = Field(ge=1024, le=65535)
    policy: str = "weighted_round_robin"
    rotation_interval_seconds: int = Field(default=600, ge=10, le=86_400)
    enabled: bool = True
    members: list[PoolMember] = Field(default_factory=list)


class PoolEnabledRequest(BaseModel):
    enabled: bool


class PoolDrainRequest(BaseModel):
    draining: bool


class DeleteNodesRequest(BaseModel):
    node_tags: list[str] | None = None
    all: bool = False


class TestRequest(BaseModel):
    node_tags: list[str] | None = None
    prune_same_ip: bool = False
    include_geoip: bool = False
    target_url: str | None = None
    target_urls: list[str] | None = None


class PortTestRequest(BaseModel):
    ports: list[int] | None = None
    urls: list[str] | None = None


class PortAvailabilityRequest(BaseModel):
    ports: list[int]


class PortAllocateRequest(BaseModel):
    start_port: int = 8001
    count: int
    exclude: list[int] = []


class LocalProxyCheckRequest(BaseModel):
    port: int | None = None
    proxy_url: str | None = None
    timeout: int = 30


class LocalProxyCheckStartRequest(BaseModel):
    ports: list[int] | None = None
    timeout: int = 30
    concurrency: int = 3


class GeoIpConfig(BaseModel):
    enabled: bool = True
    cache_ttl_hours: int = 168
    concurrency: int = 4


class DoctorRequest(BaseModel):
    timeout: int = 30


# ── Job models ──────────────────────────────────────────────────────────────


class TestJob(BaseModel):
    id: str
    status: str = "running"
    total: int = 0
    completed: int = 0
    results: dict[str, dict] = {}
    details: dict[str, dict] = {}
    removed: list[str] = []
    error: str | None = None
    revision: int = 0
    result_revisions: dict[str, int] = Field(default_factory=dict, exclude=True)
    touched_at: float = Field(default_factory=time.monotonic, exclude=True)


class PortTestJob(BaseModel):
    id: str
    status: str = "running"
    total: int = 0
    completed: int = 0
    results: dict[str, dict] = {}
    details: dict[str, dict] = {}
    error: str | None = None
    touched_at: float = Field(default_factory=time.monotonic, exclude=True)


class LocalProxyCheckJob(BaseModel):
    id: str
    status: str = "running"
    total: int = 0
    completed: int = 0
    results: dict[str, dict] = {}
    error: str | None = None
    touched_at: float = Field(default_factory=time.monotonic, exclude=True)
