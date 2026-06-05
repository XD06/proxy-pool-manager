from __future__ import annotations

import ipaddress
from datetime import datetime, timezone

import httpx

from .models import AppState, GeoIpResult


DEFAULT_GEOIP_TTL_HOURS = 168


def _fresh(checked_at: str, ttl_hours: int = DEFAULT_GEOIP_TTL_HOURS) -> bool:
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - checked).total_seconds() < ttl_hours * 3600


def _queryable_public_ip(ip: str) -> bool:
    try:
        parsed = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return parsed.version == 4 and parsed.is_global


def geoip_summary(result: GeoIpResult | dict | None) -> str:
    if not result:
        return ""
    data = result.model_dump() if isinstance(result, GeoIpResult) else result
    if data.get("error"):
        return "地区未知"
    parts = [
        data.get("country_code") or data.get("country"),
        data.get("city") or data.get("region"),
        data.get("asn"),
        data.get("org") or data.get("isp"),
    ]
    return " · ".join(str(part) for part in parts if part)


async def lookup_geoip(
    ip: str | None,
    state: AppState,
    *,
    ttl_hours: int = DEFAULT_GEOIP_TTL_HOURS,
    enabled: bool = True,
    timeout_seconds: float = 4.0,
) -> GeoIpResult | None:
    if not enabled or not ip:
        return None
    ip = ip.strip()
    cached = state.geoip_cache.get(ip)
    if cached and _fresh(cached.checked_at, ttl_hours):
        return cached
    if not _queryable_public_ip(ip):
        result = GeoIpResult(ip=ip, error="not a public IPv4 address")
        state.geoip_cache[ip] = result
        return result

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(
                f"http://ip-api.com/json/{ip}",
                params={
                    "fields": "status,message,country,countryCode,regionName,city,isp,org,as,query",
                    "lang": "en",
                },
            )
            response.raise_for_status()
            data = response.json()
        if data.get("status") != "success":
            result = GeoIpResult(ip=ip, error=data.get("message") or "geoip lookup failed")
        else:
            result = GeoIpResult(
                ip=data.get("query") or ip,
                country=data.get("country") or None,
                country_code=data.get("countryCode") or None,
                region=data.get("regionName") or None,
                city=data.get("city") or None,
                asn=data.get("as") or None,
                org=data.get("org") or None,
                isp=data.get("isp") or None,
                source="ip-api",
                confidence="reference",
            )
    except Exception as exc:
        result = GeoIpResult(ip=ip, error=str(exc))
    state.geoip_cache[ip] = result
    return result
