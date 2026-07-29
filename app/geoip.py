from __future__ import annotations

import ipaddress
from datetime import datetime, timezone

import httpx

from .httpclient import shared_ssl_context
from .models import AppState, GeoIpResult


DEFAULT_GEOIP_TTL_HOURS = 168
FAILED_GEOIP_TTL_HOURS = 1


def _fresh(checked_at: str, ttl_hours: int = DEFAULT_GEOIP_TTL_HOURS) -> bool:
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - checked).total_seconds() < ttl_hours * 3600


def _cache_ttl_hours(result: GeoIpResult, ttl_hours: int) -> int:
    # ponytail: failed lookups expire fast so transient network/DNS issues recover
    if result.error:
        return min(max(1, int(ttl_hours or DEFAULT_GEOIP_TTL_HOURS)), FAILED_GEOIP_TTL_HOURS)
    return max(1, int(ttl_hours or DEFAULT_GEOIP_TTL_HOURS))


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


def geoip_compact_summary(result: GeoIpResult | dict | None) -> str:
    if not result:
        return ""
    data = result.model_dump() if isinstance(result, GeoIpResult) else result
    if data.get("error"):
        return "未知"
    org = data.get("org") or data.get("isp") or ""
    org = str(org).replace("Corporation", "").replace("Limited", "").replace("Ltd.", "").strip()
    org = " ".join(org.split()[:2])
    parts = [
        data.get("country_code") or data.get("country"),
        data.get("city") or data.get("region"),
        org,
    ]
    return " · ".join(str(part) for part in parts if part)


def _usable(result: GeoIpResult | None) -> bool:
    if not result or result.error:
        return False
    return bool(result.country_code or result.country or result.city or result.region)


def _asn_text(value: object) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.upper().startswith("AS"):
        return text
    if text.isdigit():
        return f"AS{text}"
    return text


async def _fetch_json(client: httpx.AsyncClient, url: str) -> dict:
    response = await client.get(url)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("geoip provider returned non-object JSON")
    return data


async def _from_geojs(client: httpx.AsyncClient, ip: str) -> GeoIpResult:
    data = await _fetch_json(client, f"https://get.geojs.io/v1/ip/geo/{ip}.json")
    if data.get("error") or data.get("message") == "Not Found":
        raise RuntimeError(str(data.get("error") or data.get("message") or "geojs lookup failed"))
    return GeoIpResult(
        ip=str(data.get("ip") or ip),
        country=data.get("country") or None,
        country_code=data.get("country_code") or None,
        region=None,
        city=data.get("city") or None,
        asn=_asn_text(data.get("asn")),
        org=data.get("organization_name") or data.get("organization") or None,
        isp=data.get("organization_name") or data.get("organization") or None,
        source="geojs",
        confidence="reference",
    )


async def _from_ipwho(client: httpx.AsyncClient, ip: str) -> GeoIpResult:
    data = await _fetch_json(client, f"https://ipwho.is/{ip}")
    if data.get("success") is False:
        raise RuntimeError(str(data.get("message") or "ipwho lookup failed"))
    conn = data.get("connection") if isinstance(data.get("connection"), dict) else {}
    return GeoIpResult(
        ip=str(data.get("ip") or ip),
        country=data.get("country") or None,
        country_code=data.get("country_code") or None,
        region=data.get("region") or None,
        city=data.get("city") or None,
        asn=_asn_text(conn.get("asn")),
        org=conn.get("org") or None,
        isp=conn.get("isp") or None,
        source="ipwho",
        confidence="reference",
    )


async def _from_freeipapi(client: httpx.AsyncClient, ip: str) -> GeoIpResult:
    data = await _fetch_json(client, f"https://free.freeipapi.com/api/json/{ip}")
    if not (data.get("countryCode") or data.get("countryName")):
        raise RuntimeError("freeipapi lookup failed")
    return GeoIpResult(
        ip=str(data.get("ipAddress") or ip),
        country=data.get("countryName") or None,
        country_code=data.get("countryCode") or None,
        region=data.get("regionName") or None,
        city=data.get("cityName") or None,
        asn=_asn_text(data.get("asn")),
        org=data.get("asnOrganization") or None,
        isp=data.get("asnOrganization") or None,
        source="freeipapi",
        confidence="reference",
    )


async def _from_ipinfo(client: httpx.AsyncClient, ip: str) -> GeoIpResult:
    data = await _fetch_json(client, f"https://ipinfo.io/{ip}/json")
    if data.get("error") or data.get("bogon"):
        raise RuntimeError(str(data.get("error") or "ipinfo lookup failed"))
    org = data.get("org") or None
    asn = None
    if isinstance(org, str) and org.startswith("AS"):
        parts = org.split(None, 1)
        asn = parts[0]
        org = parts[1] if len(parts) > 1 else org
    return GeoIpResult(
        ip=str(data.get("ip") or ip),
        country=None,
        country_code=data.get("country") or None,
        region=data.get("region") or None,
        city=data.get("city") or None,
        asn=asn,
        org=org,
        isp=org,
        source="ipinfo",
        confidence="reference",
    )


async def _from_ip_api(client: httpx.AsyncClient, ip: str) -> GeoIpResult:
    response = await client.get(
        f"http://ip-api.com/json/{ip}",
        params={
            "fields": "status,message,country,countryCode,regionName,city,isp,org,as,query",
            "lang": "en",
        },
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("ip-api returned non-object JSON")
    if data.get("status") != "success":
        raise RuntimeError(str(data.get("message") or "ip-api lookup failed"))
    return GeoIpResult(
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


_PROVIDERS = (
    ("geojs", _from_geojs),
    ("ipwho", _from_ipwho),
    ("freeipapi", _from_freeipapi),
    ("ipinfo", _from_ipinfo),
    ("ip-api", _from_ip_api),
)


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
    if cached and _fresh(cached.checked_at, _cache_ttl_hours(cached, ttl_hours)):
        return cached
    if not _queryable_public_ip(ip):
        result = GeoIpResult(ip=ip, error="not a public IPv4 address")
        state.geoip_cache[ip] = result
        return result

    errors: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True, verify=shared_ssl_context()) as client:
            for name, provider in _PROVIDERS:
                try:
                    result = await provider(client, ip)
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
                    continue
                if _usable(result):
                    state.geoip_cache[ip] = result
                    return result
                errors.append(f"{name}: empty geo fields")
    except Exception as exc:
        errors.append(str(exc))

    result = GeoIpResult(
        ip=ip,
        error="; ".join(errors[-3:]) if errors else "geoip lookup failed",
        source="multi",
    )
    state.geoip_cache[ip] = result
    return result
