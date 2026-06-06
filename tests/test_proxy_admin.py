import asyncio
from types import SimpleNamespace

from app import proxy_admin as proxy_admin_module
from app.proxy_admin import allocate_proxy_names, proxy_admin_import


def test_allocate_proxy_names_fills_gaps_and_avoids_existing_names():
    names = allocate_proxy_names(
        "代理",
        4,
        {"代理1-jp.Tokyo", "代理2-US.LosAngeles", "代理4-none", "代理13", "其他1", "代理abc"},
        ["jp.Osaka", "US.NewYork", "none", "HK"],
    )

    assert names == ["代理3-jp.Osaka", "代理5-US.NewYork", "代理6-none", "代理7-HK"]


def test_allocate_proxy_names_uses_custom_prefix_only():
    names = allocate_proxy_names("测试", 3, {"代理1", "测试1-US", "测试3-none"})

    assert names == ["测试2-none", "测试4-none", "测试5-none"]


def test_proxy_admin_import_updates_default_remote_name(monkeypatch):
    calls = []

    async def fake_api(payload, method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return {"code": 0, "data": {"items": [], "pages": 1}}
        if method == "POST":
            return {"code": 0, "data": {"id": 10, "name": "default", "host": body["host"], "port": body["port"]}}
        if method == "PUT":
            return {"code": 0, "data": {"id": 10, "name": body["name"], "host": body["host"], "port": body["port"]}}
        raise AssertionError((method, path))

    monkeypatch.setattr(proxy_admin_module, "proxy_admin_api", fake_api)
    payload = SimpleNamespace(base_url="http://127.0.0.1:8081", token="token", concurrency=1, proxy_name_prefix="代理")

    imported = asyncio.run(proxy_admin_import(payload, [{"host": "127.0.0.1", "port": 8001, "name_suffix": "JP.Tokyo"}]))

    assert imported[0]["expected_name"] == "代理1-JP.Tokyo"
    assert imported[0]["remote_name"] == "代理1-JP.Tokyo"
    assert imported[0]["name"] == "代理1-JP.Tokyo"
    assert any(method == "PUT" and body["name"] == "代理1-JP.Tokyo" for method, _, body in calls)
