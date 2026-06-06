from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx


async def proxy_admin_api(payload: Any, method: str, path: str, body=None):
    base_url = payload.base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {payload.token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.request(method, f"{base_url}{path}", headers=headers, json=body)
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
                results[current] = {
                    **item,
                    "id": int(proxy.get("id") or 0),
                    "name": proxy.get("name") or item["name"],
                }
            except Exception as exc:
                results[current] = {
                    **item,
                    "id": -int(item["port"]),
                    "upload_error": str(exc),
                }

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return [item for item in results if item is not None]


async def proxy_admin_quality_check(payload: Any, proxy_id: int) -> dict:
    data = await proxy_admin_api(payload, "POST", f"/api/v1/admin/proxies/{proxy_id}/quality-check")
    raw = data.get("data") or {}
    return {
        "id": int(raw.get("proxy_id") or proxy_id),
        "exit_ip": raw.get("exit_ip") or "",
        "country": raw.get("country") or "",
        "score": raw.get("score") or 0,
        "grade": raw.get("grade") or "",
        "items": [
            {
                "target": item.get("target") or "",
                "status": item.get("status") or "",
                "http_status": item.get("http_status"),
                "latency_ms": item.get("latency_ms"),
                "message": item.get("message") or "",
            }
            for item in raw.get("items") or []
        ],
    }


async def proxy_admin_remove_ids(payload: Any, ids: list[int]) -> list[dict]:
    results: list[dict] = []
    idx = 0
    concurrency = max(1, min(payload.concurrency, 20, len(ids) or 1))

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
