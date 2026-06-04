from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import yaml

from .models import ImportResult, ProxyNode


SUPPORTED_LINK_SCHEMES = {"vless", "vmess", "ss", "trojan", "hysteria2", "hy2"}
BASE64_SUBSCRIPTION_RE = re.compile(r"[A-Za-z0-9+/_-]{80,}={0,2}")
SUBSCRIPTION_HEADERS = {
    "User-Agent": "Clash.Meta/1.18.0 ProxyPoolManager/1.0",
    "Accept": "text/plain, application/octet-stream, application/yaml, text/yaml, */*",
}


def _decode_base64(value: str) -> str:
    compact = re.sub(r"\s+", "", value)
    padding = "=" * (-len(compact) % 4)
    raw = base64.urlsafe_b64decode((compact + padding).encode("utf-8"))
    return raw.decode("utf-8", errors="replace")


def _short_hash(*parts: object) -> str:
    text = "|".join(str(part) for part in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _clean_name(name: str | None, fallback: str) -> str:
    if not name:
        return fallback
    decoded = unquote(name).strip()
    return decoded or fallback


def _node_tag(protocol: str, outbound: dict[str, Any]) -> str:
    credential = outbound.get("uuid") or outbound.get("password") or outbound.get("method") or ""
    return f"node-{protocol}-{_short_hash(outbound.get('server'), outbound.get('server_port'), credential)}"


def _query(parsed) -> dict[str, str]:
    return {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}


def _tls_from_query(params: dict[str, str]) -> dict[str, Any] | None:
    security = params.get("security") or params.get("tls")
    enabled = security in {"tls", "reality"} or params.get("sni") or params.get("fp")
    if not enabled:
        return None
    tls: dict[str, Any] = {"enabled": True}
    sni = params.get("sni") or params.get("peer")
    if sni:
        tls["server_name"] = sni
    fp = params.get("fp")
    if fp:
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    insecure = params.get("allowInsecure") or params.get("skip-cert-verify")
    if insecure in {"1", "true", "True"}:
        tls["insecure"] = True
    alpn = params.get("alpn")
    if alpn:
        tls["alpn"] = [item for item in alpn.split(",") if item]
    return tls


def _transport_from_query(params: dict[str, str]) -> dict[str, Any] | None:
    transport_type = params.get("type") or params.get("net")
    if not transport_type or transport_type in {"tcp", "none"}:
        return None
    if transport_type == "ws":
        transport: dict[str, Any] = {"type": "ws"}
        path = params.get("path")
        if path:
            transport["path"] = unquote(path)
        host = params.get("host")
        if host:
            transport["headers"] = {"Host": host}
        return transport
    if transport_type == "grpc":
        transport = {"type": "grpc"}
        service_name = params.get("serviceName") or params.get("service_name")
        if service_name:
            transport["service_name"] = service_name
        return transport
    if transport_type == "http" or transport_type == "h2":
        transport = {"type": "http"}
        path = params.get("path")
        if path:
            transport["path"] = unquote(path)
        host = params.get("host")
        if host:
            transport["host"] = [host]
        return transport
    return {"type": transport_type}


def _build_node(name: str, protocol: str, outbound: dict[str, Any]) -> ProxyNode:
    tag = outbound.get("tag") or _node_tag(protocol, outbound)
    outbound["tag"] = tag
    return ProxyNode(
        tag=tag,
        name=name,
        type=outbound["type"],
        server=outbound["server"],
        server_port=int(outbound["server_port"]),
        outbound=outbound,
    )


def parse_vless(link: str) -> ProxyNode:
    parsed = urlparse(link)
    params = _query(parsed)
    outbound: dict[str, Any] = {
        "type": "vless",
        "server": parsed.hostname or "",
        "server_port": int(parsed.port or 443),
        "uuid": unquote(parsed.username or ""),
    }
    flow = params.get("flow")
    if flow:
        outbound["flow"] = flow
    tls = _tls_from_query(params)
    if tls:
        outbound["tls"] = tls
    transport = _transport_from_query(params)
    if transport:
        outbound["transport"] = transport
    return _build_node(_clean_name(parsed.fragment, parsed.hostname or "vless"), "vless", outbound)


def parse_trojan(link: str) -> ProxyNode:
    parsed = urlparse(link)
    params = _query(parsed)
    outbound: dict[str, Any] = {
        "type": "trojan",
        "server": parsed.hostname or "",
        "server_port": int(parsed.port or 443),
        "password": unquote(parsed.username or ""),
    }
    tls = _tls_from_query(params)
    if tls:
        outbound["tls"] = tls
    elif params.get("security") != "none":
        outbound["tls"] = {"enabled": True, "server_name": params.get("sni") or parsed.hostname}
    transport = _transport_from_query(params)
    if transport:
        outbound["transport"] = transport
    return _build_node(_clean_name(parsed.fragment, parsed.hostname or "trojan"), "trojan", outbound)


def parse_hysteria2(link: str) -> ProxyNode:
    parsed = urlparse(link)
    params = _query(parsed)
    outbound: dict[str, Any] = {
        "type": "hysteria2",
        "server": parsed.hostname or "",
        "server_port": int(parsed.port or 443),
        "password": unquote(parsed.username or ""),
        "tls": {
            "enabled": True,
            "server_name": params.get("sni") or parsed.hostname,
        },
    }
    mport = params.get("mport")
    if mport:
        outbound["server_ports"] = [mport.replace("-", ":")]
    insecure = params.get("insecure") or params.get("allowInsecure")
    if insecure in {"1", "true", "True"}:
        outbound["tls"]["insecure"] = True
    obfs = params.get("obfs")
    obfs_password = params.get("obfs-password") or params.get("obfs_password")
    if obfs:
        outbound["obfs"] = {"type": obfs}
        if obfs_password:
            outbound["obfs"]["password"] = obfs_password
    return _build_node(_clean_name(parsed.fragment, parsed.hostname or "hysteria2"), "hysteria2", outbound)


def parse_vmess(link: str) -> ProxyNode:
    payload = link[len("vmess://") :]
    data = json.loads(_decode_base64(payload))
    outbound: dict[str, Any] = {
        "type": "vmess",
        "server": data["add"],
        "server_port": int(data["port"]),
        "uuid": data["id"],
        "security": data.get("scy") or "auto",
        "alter_id": int(data.get("aid") or 0),
    }
    if data.get("tls") == "tls":
        tls = {"enabled": True}
        if data.get("sni"):
            tls["server_name"] = data["sni"]
        if data.get("fp"):
            tls["utls"] = {"enabled": True, "fingerprint": data["fp"]}
        outbound["tls"] = tls
    net = data.get("net")
    if net and net not in {"tcp", "none"}:
        params = {
            "type": net,
            "path": data.get("path") or "",
            "host": data.get("host") or "",
        }
        transport = _transport_from_query(params)
        if transport:
            outbound["transport"] = transport
    return _build_node(_clean_name(data.get("ps"), data.get("add", "vmess")), "vmess", outbound)


def parse_ss(link: str) -> ProxyNode:
    parsed = urlparse(link)
    fragment = parsed.fragment
    raw_user = parsed.username or ""
    method = ""
    password = ""
    host = parsed.hostname
    port = parsed.port

    if host and port:
        userinfo = unquote(raw_user)
        if ":" in userinfo:
            method, password = userinfo.split(":", 1)
        else:
            decoded = _decode_base64(userinfo)
            method, password = decoded.split(":", 1)
    else:
        decoded = _decode_base64(link[len("ss://") :].split("#", 1)[0].split("?", 1)[0])
        parsed_decoded = urlparse(f"ss://{decoded}")
        host = parsed_decoded.hostname
        port = parsed_decoded.port
        method, password = unquote(parsed_decoded.username or "").split(":", 1)

    outbound: dict[str, Any] = {
        "type": "shadowsocks",
        "server": host or "",
        "server_port": int(port or 0),
        "method": method,
        "password": password,
    }
    return _build_node(_clean_name(fragment, host or "ss"), "ss", outbound)


def parse_link(link: str) -> ProxyNode:
    scheme = link.split("://", 1)[0].lower()
    if scheme == "vless":
        return parse_vless(link)
    if scheme == "vmess":
        return parse_vmess(link)
    if scheme == "ss":
        return parse_ss(link)
    if scheme == "trojan":
        return parse_trojan(link)
    if scheme in {"hysteria2", "hy2"}:
        return parse_hysteria2(link)
    raise ValueError(f"Unsupported link scheme: {scheme}")


def _clash_tls(proxy: dict[str, Any]) -> dict[str, Any] | None:
    if not proxy.get("tls") and not proxy.get("servername") and not proxy.get("sni"):
        return None
    tls: dict[str, Any] = {"enabled": bool(proxy.get("tls", True))}
    server_name = proxy.get("servername") or proxy.get("sni")
    if server_name:
        tls["server_name"] = server_name
    if proxy.get("skip-cert-verify"):
        tls["insecure"] = True
    if proxy.get("client-fingerprint"):
        tls["utls"] = {"enabled": True, "fingerprint": proxy["client-fingerprint"]}
    return tls


def parse_clash_yaml(text: str) -> tuple[list[ProxyNode], list[str]]:
    data = yaml.safe_load(text) or {}
    proxies = data.get("proxies") or []
    nodes: list[ProxyNode] = []
    warnings: list[str] = []
    for index, proxy in enumerate(proxies, start=1):
        try:
            ptype = proxy.get("type")
            if ptype == "ss":
                outbound = {
                    "type": "shadowsocks",
                    "server": proxy["server"],
                    "server_port": int(proxy["port"]),
                    "method": proxy["cipher"],
                    "password": str(proxy["password"]),
                }
            elif ptype in {"vmess", "vless"}:
                outbound = {
                    "type": ptype,
                    "server": proxy["server"],
                    "server_port": int(proxy["port"]),
                    "uuid": proxy["uuid"],
                }
                if ptype == "vmess":
                    outbound["security"] = proxy.get("cipher") or "auto"
                    outbound["alter_id"] = int(proxy.get("alterId") or proxy.get("alter-id") or 0)
                tls = _clash_tls(proxy)
                if tls:
                    outbound["tls"] = tls
                if proxy.get("network") == "ws":
                    ws_opts = proxy.get("ws-opts") or {}
                    transport: dict[str, Any] = {"type": "ws"}
                    if ws_opts.get("path"):
                        transport["path"] = ws_opts["path"]
                    headers = ws_opts.get("headers") or {}
                    host = headers.get("Host") or headers.get("host")
                    if host:
                        transport["headers"] = {"Host": host}
                    outbound["transport"] = transport
            elif ptype == "trojan":
                outbound = {
                    "type": "trojan",
                    "server": proxy["server"],
                    "server_port": int(proxy["port"]),
                    "password": str(proxy["password"]),
                }
                tls = _clash_tls(proxy) or {"enabled": True, "server_name": proxy.get("sni") or proxy["server"]}
                outbound["tls"] = tls
            elif ptype == "hysteria2":
                outbound = {
                    "type": "hysteria2",
                    "server": proxy["server"],
                    "server_port": int(proxy["port"]),
                    "password": str(proxy.get("password") or proxy.get("auth") or ""),
                    "tls": _clash_tls(proxy) or {"enabled": True, "server_name": proxy.get("sni") or proxy["server"]},
                }
                ports = proxy.get("ports") or proxy.get("mport")
                if ports:
                    outbound["server_ports"] = [str(ports).replace("-", ":")]
                obfs = proxy.get("obfs")
                if obfs:
                    outbound["obfs"] = {"type": str(obfs)}
                    if proxy.get("obfs-password"):
                        outbound["obfs"]["password"] = str(proxy["obfs-password"])
            else:
                warnings.append(f"Skipped unsupported Clash node type at #{index}: {ptype}")
                continue
            nodes.append(_build_node(_clean_name(proxy.get("name"), f"node-{index}"), outbound["type"], outbound))
        except Exception as exc:
            warnings.append(f"Skipped Clash node #{index}: {exc}")
    return nodes, warnings


def _looks_base64_subscription(text: str) -> bool:
    try:
        decoded = _decode_base64(text)
    except Exception:
        return False
    return "://" in decoded


def _decode_base64_subscription_text(text: str) -> str | None:
    try:
        decoded = _decode_base64(text)
        if _contains_supported_link(decoded):
            return decoded
    except Exception:
        pass

    candidates = sorted(BASE64_SUBSCRIPTION_RE.findall(text), key=len, reverse=True)
    for candidate in candidates:
        try:
            decoded = _decode_base64(candidate)
        except Exception:
            continue
        if _contains_supported_link(decoded):
            return decoded
    return None


def _contains_supported_link(text: str) -> bool:
    lowered = text.lower()
    return any(f"{scheme}://" in lowered for scheme in SUPPORTED_LINK_SCHEMES)


def parse_text(text: str) -> ImportResult:
    warnings: list[str] = []
    nodes: list[ProxyNode] = []
    working_text = text.strip()

    if "proxies:" in working_text:
        nodes, warnings = parse_clash_yaml(working_text)
    else:
        decoded_subscription = _decode_base64_subscription_text(working_text)
        if decoded_subscription:
            working_text = decoded_subscription
        for raw_line in working_text.splitlines():
            line = raw_line.strip()
            if not line or "://" not in line:
                continue
            scheme = line.split("://", 1)[0].lower()
            if scheme not in SUPPORTED_LINK_SCHEMES:
                warnings.append(f"Skipped unsupported link scheme: {scheme}")
                continue
            try:
                nodes.append(parse_link(line))
            except Exception as exc:
                warnings.append(f"Skipped invalid {scheme} link: {exc}")

    deduped: dict[str, ProxyNode] = {}
    for node in nodes:
        key = node.tag
        deduped.setdefault(key, node)
    result_nodes = list(deduped.values())
    return ImportResult(nodes=result_nodes, count=len(result_nodes), warnings=warnings)


async def import_nodes(url: str | None = None, text: str | None = None) -> ImportResult:
    if not url and not text:
        raise ValueError("Either url or text is required")
    if url:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers=SUBSCRIPTION_HEADERS,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            text = response.text
    assert text is not None
    return parse_text(text)
