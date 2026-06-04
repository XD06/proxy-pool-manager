import base64
import asyncio
import json

from app import parser as parser_module
from app.parser import import_nodes, parse_text


def test_parse_vless_link():
    text = (
        "vless://00000000-0000-0000-0000-000000000000@example.com:443"
        "?security=tls&type=ws&host=edge.example.com&path=%2Fws&sni=edge.example.com#HK"
    )
    result = parse_text(text)
    assert result.count == 1
    node = result.nodes[0]
    assert node.name == "HK"
    assert node.type == "vless"
    assert node.server == "example.com"
    assert node.outbound["transport"]["type"] == "ws"
    assert node.outbound["tls"]["server_name"] == "edge.example.com"


def test_parse_vmess_link():
    payload = {
        "v": "2",
        "ps": "JP",
        "add": "jp.example.com",
        "port": "443",
        "id": "00000000-0000-0000-0000-000000000001",
        "aid": "0",
        "net": "ws",
        "type": "none",
        "host": "jp.example.com",
        "path": "/ray",
        "tls": "tls",
        "sni": "jp.example.com",
        "scy": "auto",
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    result = parse_text(f"vmess://{encoded}")
    assert result.count == 1
    node = result.nodes[0]
    assert node.name == "JP"
    assert node.type == "vmess"
    assert node.outbound["alter_id"] == 0
    assert node.outbound["transport"]["path"] == "/ray"


def test_parse_ss_link():
    userinfo = base64.urlsafe_b64encode(b"aes-256-gcm:secret").decode().rstrip("=")
    result = parse_text(f"ss://{userinfo}@ss.example.com:8388#SS")
    assert result.count == 1
    node = result.nodes[0]
    assert node.type == "shadowsocks"
    assert node.outbound["method"] == "aes-256-gcm"
    assert node.outbound["password"] == "secret"


def test_parse_trojan_link():
    result = parse_text("trojan://password@trojan.example.com:443?sni=trojan.example.com#TR")
    assert result.count == 1
    node = result.nodes[0]
    assert node.name == "TR"
    assert node.type == "trojan"
    assert node.outbound["password"] == "password"


def test_parse_hysteria2_link_with_mport():
    link = (
        "hysteria2://e65273d3-d654-4f2a-b81c-23e82ecea918@138.2.2.120:20000"
        "?sni=www.bing.com&insecure=1&allowInsecure=1&mport=20000-55000"
        "#%F0%9F%87%AF%F0%9F%87%B5%E6%97%A5%E6%9C%AC%E4%B8%93%E7%BA%BF7"
    )
    result = parse_text(link)
    assert result.count == 1
    node = result.nodes[0]
    assert node.type == "hysteria2"
    assert node.server == "138.2.2.120"
    assert node.outbound["password"] == "e65273d3-d654-4f2a-b81c-23e82ecea918"
    assert node.outbound["server_ports"] == ["20000:55000"]
    assert node.outbound["tls"]["server_name"] == "www.bing.com"
    assert node.outbound["tls"]["insecure"] is True


def test_parse_base64_subscription_with_trailing_noise():
    subscription = "\r\n".join([
        (
            "hysteria2://e65273d3-d654-4f2a-b81c-23e82ecea918@203.10.98.187:443/"
            "?insecure=1&sni=www.bing.com#JP"
        ),
        (
            "hysteria2://e65273d3-d654-4f2a-b81c-23e82ecea918@203.10.99.51:20000/"
            "?insecure=1&sni=www.bing.com&mport=20000-55000#JP2"
        ),
    ])
    encoded = base64.urlsafe_b64encode(subscription.encode()).decode()
    text = encoded + "zheg1fefji1https://www.yfjc.xyz/api/v1/client/subscribe?token=bad"

    result = parse_text(text)

    assert result.count == 2
    assert {node.server for node in result.nodes} == {"203.10.98.187", "203.10.99.51"}
    assert result.nodes[1].outbound["server_ports"] == ["20000:55000"]


def test_import_nodes_url_uses_subscription_client_options(monkeypatch):
    seen = {}

    class FakeResponse:
        text = (
            "hysteria2://e65273d3-d654-4f2a-b81c-23e82ecea918@203.10.98.187:443/"
            "?insecure=1&sni=www.bing.com#JP"
        )

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, url):
            seen["url"] = url
            return FakeResponse()

    monkeypatch.setattr(parser_module.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(import_nodes(url="https://example.com/sub"))

    assert result.count == 1
    assert seen["url"] == "https://example.com/sub"
    assert seen["follow_redirects"] is True
    assert seen["timeout"] == 30
    assert "Clash.Meta" in seen["headers"]["User-Agent"]


def test_parse_clash_yaml():
    text = """
proxies:
  - name: HK-SS
    type: ss
    server: ss.example.com
    port: 8388
    cipher: aes-256-gcm
    password: secret
  - name: HK-VLESS
    type: vless
    server: vless.example.com
    port: 443
    uuid: 00000000-0000-0000-0000-000000000000
    tls: true
    servername: vless.example.com
"""
    result = parse_text(text)
    assert result.count == 2
    assert {node.type for node in result.nodes} == {"shadowsocks", "vless"}


def test_unsupported_link_is_warning():
    result = parse_text("ssr://unsupported")
    assert result.count == 0
    assert result.warnings
