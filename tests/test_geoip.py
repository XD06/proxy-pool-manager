import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.geoip import FAILED_GEOIP_TTL_HOURS, geoip_compact_summary, geoip_summary, lookup_geoip
from app.models import AppState, GeoIpResult


def test_lookup_geoip_skips_private_address():
    state = AppState()

    result = asyncio.run(lookup_geoip("127.0.0.1", state))

    assert result is not None
    assert result.error == "not a public IPv4 address"
    assert state.geoip_cache["127.0.0.1"].error == "not a public IPv4 address"


def test_geoip_summary_prefers_compact_fields():
    result = GeoIpResult(
        ip="8.8.8.8",
        country="United States",
        country_code="US",
        city="Mountain View",
        asn="AS15169",
        org="Google",
    )

    assert geoip_summary(result) == "US · Mountain View · AS15169 · Google"
    assert geoip_compact_summary(result) == "US · Mountain View · Google"


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, routes: dict[str, dict | Exception]):
        self.routes = routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, params=None):
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return _FakeResponse(value)
        raise RuntimeError(f"unexpected url: {url}")


def test_lookup_geoip_uses_first_working_provider():
    state = AppState()
    routes = {
        "get.geojs.io": RuntimeError("geojs down"),
        "ipwho.is": {
            "ip": "8.8.8.8",
            "success": True,
            "country": "United States",
            "country_code": "US",
            "region": "California",
            "city": "Mountain View",
            "connection": {"asn": 15169, "org": "Google LLC", "isp": "Google LLC"},
        },
    }

    with patch("app.geoip.httpx.AsyncClient", lambda **kwargs: _FakeClient(routes)):
        result = asyncio.run(lookup_geoip("8.8.8.8", state, timeout_seconds=1))

    assert result is not None
    assert result.error is None
    assert result.country_code == "US"
    assert result.city == "Mountain View"
    assert result.asn == "AS15169"
    assert result.source == "ipwho"
    assert state.geoip_cache["8.8.8.8"].country_code == "US"


def test_lookup_geoip_failed_cache_expires_quickly():
    state = AppState()
    state.geoip_cache["1.1.1.1"] = GeoIpResult(
        ip="1.1.1.1",
        error="old failure",
        checked_at="2000-01-01T00:00:00+00:00",
    )
    routes = {
        "get.geojs.io": {
            "ip": "1.1.1.1",
            "country": "Australia",
            "country_code": "AU",
            "city": "Research",
            "asn": 13335,
            "organization_name": "Cloudflare, Inc.",
        }
    }

    with patch("app.geoip.httpx.AsyncClient", lambda **kwargs: _FakeClient(routes)):
        result = asyncio.run(lookup_geoip("1.1.1.1", state, ttl_hours=168, timeout_seconds=1))

    assert result is not None
    assert result.error is None
    assert result.country_code == "AU"
    assert result.source == "geojs"
    assert FAILED_GEOIP_TTL_HOURS == 1
