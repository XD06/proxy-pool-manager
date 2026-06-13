from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import httpx


_PROXY_ADMIN_CLIENT: ContextVar[httpx.AsyncClient | None] = ContextVar(
    "proxy_admin_client",
    default=None,
)


@asynccontextmanager
async def proxy_admin_client_scope():
    existing = _PROXY_ADMIN_CLIENT.get()
    if existing is not None:
        yield
        return
    async with httpx.AsyncClient(timeout=60) as client:
        token = _PROXY_ADMIN_CLIENT.set(client)
        try:
            yield
        finally:
            _PROXY_ADMIN_CLIENT.reset(token)


async def proxy_admin_api(payload: Any, method: str, path: str, body=None):
    base_url = payload.base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {payload.token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }
    client = _PROXY_ADMIN_CLIENT.get()
    if client is not None:
        return await _proxy_admin_request(client, method, f"{base_url}{path}", headers, body)
    async with httpx.AsyncClient(timeout=60) as scoped_client:
        return await _proxy_admin_request(scoped_client, method, f"{base_url}{path}", headers, body)


async def _proxy_admin_request(client: httpx.AsyncClient, method: str, url: str, headers: dict, body=None):
    response = await client.request(method, url, headers=headers, json=body)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("code") not in (None, 0):
        raise RuntimeError(data.get("message") or f"ProxyAdmin returned code {data.get('code')}")
    return data


async def proxy_admin_list_all(payload: Any) -> dict[str, dict]:
    page = 1
    page_size = 200
    result = {}
    while True:
        data = await proxy_admin_api(
            payload,
            "GET",
            f"/api/v1/admin/proxies?page={page}&page_size={page_size}&sort_by=id&sort_order=desc",
        )
        page_data = data.get("data") or {}
        for item in page_data.get("items") or []:
            result[f"{item.get('host')}:{item.get('port')}"] = item
        if page >= int(page_data.get("pages") or 1):
            break
        page += 1
    return result


def allocate_proxy_names(
    prefix: str | None,
    count: int,
    existing_names: set[str],
    suffixes: list[str] | None = None,
) -> list[str]:
    normalized = (prefix or "代理").strip() or "代理"
    pattern = re.compile(rf"^{re.escape(normalized)}(\d+)(?:-.+)?$")
    suffixes = suffixes or ["none"] * count
    used = set()
    for name in existing_names:
        match = pattern.match(str(name or ""))
        if match:
            used.add(int(match.group(1)))
    names = []
    candidate = 1
    while len(names) < count:
        if candidate not in used:
            suffix = str(suffixes[len(names)] if len(names) < len(suffixes) else "none").strip() or "none"
            names.append(f"{normalized}{candidate}-{suffix}")
            used.add(candidate)
        candidate += 1
    return names


async def proxy_admin_import(payload: Any, items: list[dict]) -> list[dict]:
    if not items:
        return []
    async with proxy_admin_client_scope():
        proxy_map = await proxy_admin_list_all(payload)
        names = allocate_proxy_names(
            getattr(payload, "proxy_name_prefix", "代理"),
            len(items),
            {str(item.get("name") or "") for item in proxy_map.values()},
            [str(item.get("name_suffix") or "none") for item in items],
        )
        for index, item in enumerate(items):
            item["name"] = names[index]
        results: list[dict | None] = [None] * len(items)
        idx = 0
        concurrency = max(1, min(payload.concurrency, 10, len(items)))

        async def worker():
            nonlocal idx
            while idx < len(items):
                current = idx
                idx += 1
                item = items[current]
                try:
                    data = await proxy_admin_api(
                        payload,
                        "POST",
                        "/api/v1/admin/proxies",
                        {
                            "name": item["name"],
                            "protocol": "http",
                            "host": item["host"],
                            "port": item["port"],
                            "username": "",
                            "password": "",
                        },
                    )
                    proxy = data.get("data") or {}
                    expected_name = item["name"]
                    remote_name = proxy.get("name") or ""
                    name_update_error = None
                    if proxy.get("id") and remote_name != expected_name:
                        try:
                            updated = await proxy_admin_update_name(payload, int(proxy["id"]), item)
                            proxy = updated or proxy
                            remote_name = proxy.get("name") or remote_name
                        except Exception as exc:
                            name_update_error = str(exc)
                    results[current] = {
                        **item,
                        "id": int(proxy.get("id") or 0),
                        "name": remote_name or expected_name,
                        "expected_name": expected_name,
                        "remote_name": remote_name,
                        "name_update_error": name_update_error,
                    }
                except Exception as exc:
                    results[current] = {
                        **item,
                        "id": -int(item["port"]),
                        "upload_error": str(exc),
                    }

        await asyncio.gather(*(worker() for _ in range(concurrency)))
        return [item for item in results if item is not None]


async def proxy_admin_update_name(payload: Any, proxy_id: int, item: dict) -> dict | None:
    body = {
        "name": item["name"],
        "protocol": "http",
        "host": item["host"],
        "port": item["port"],
        "username": "",
        "password": "",
    }
    last_error: Exception | None = None
    for method in ("PUT", "PATCH"):
        try:
            data = await proxy_admin_api(payload, method, f"/api/v1/admin/proxies/{proxy_id}", body)
            return data.get("data") or {}
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    return None


async def proxy_admin_quality_check(payload: Any, proxy_id: int) -> dict:
    data = await proxy_admin_api(payload, "POST", f"/api/v1/admin/proxies/{proxy_id}/quality-check")
    raw = data.get("data") or {}
    return {
        "id": int(raw.get("proxy_id") or proxy_id),
        "exit_ip": raw.get("exit_ip") or "",
        "country": raw.get("country") or "",
        "country_code": raw.get("country_code") or "",
        "score": raw.get("score") or 0,
        "grade": raw.get("grade") or "",
        "summary": raw.get("summary") or "",
        "base_latency_ms": raw.get("base_latency_ms"),
        "items": [
            {
                "target": item.get("target") or "",
                "status": item.get("status") or "",
                "http_status": item.get("http_status"),
                "latency_ms": item.get("latency_ms"),
                "message": item.get("message") or "",
                "cf_ray": item.get("cf_ray") or "",
            }
            for item in raw.get("items") or []
        ],
    }


async def proxy_admin_remove_ids(payload: Any, ids: list[int]) -> list[dict]:
    results: list[dict] = []
    idx = 0
    concurrency = max(1, min(payload.concurrency, 20, len(ids) or 1))

    async with proxy_admin_client_scope():
        async def worker():
            nonlocal idx
            while idx < len(ids):
                proxy_id = ids[idx]
                idx += 1
                try:
                    data = await proxy_admin_api(payload, "DELETE", f"/api/v1/admin/proxies/{proxy_id}")
                    success = data.get("code") == 0
                    item = {"id": proxy_id, "success": success}
                    if not success:
                        item["message"] = data.get("message")
                    results.append(item)
                except Exception as exc:
                    results.append({"id": proxy_id, "success": False, "message": str(exc)})

        await asyncio.gather(*(worker() for _ in range(concurrency)))
    return results
