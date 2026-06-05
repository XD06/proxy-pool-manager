import asyncio

from app.geoip import geoip_summary, lookup_geoip
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
