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


def test_parse_vless_reality_link():
    text = (
        "vless://00000000-0000-0000-0000-000000000000@example.com:443"
        "?security=reality&flow=xtls-rprx-vision&fp=chrome&sni=www.mozilla.org&pbk=publicKey123&sid=abcd1234&spx=%2Fprobe#US"
    )
    result = parse_text(text)
    assert result.count == 1
    node = result.nodes[0]
    assert node.outbound["flow"] == "xtls-rprx-vision"
    assert node.outbound["tls"]["reality"]["enabled"] is True
    assert node.outbound["tls"]["reality"]["public_key"] == "publicKey123"
    assert node.outbound["tls"]["reality"]["short_id"] == "abcd1234"
    assert node.outbound["tls"]["reality"]["spider_x"] == "/probe"


def test_parse_tuic_link():
    text = (
        "tuic://00000000-0000-0000-0000-000000000000:secret@example.com:443"
        "?sni=www.example.com&congestion_control=bbr&udp_relay_mode=native&alpn=h3,hq-29#TUIC"
    )
    result = parse_text(text)
    assert result.count == 1
    node = result.nodes[0]
    assert node.type == "tuic"
    assert node.outbound["password"] == "secret"
    assert node.outbound["tls"]["server_name"] == "www.example.com"
    assert node.outbound["tls"]["alpn"] == ["h3", "hq-29"]
    assert node.outbound["congestion_control"] == "bbr"
    assert node.outbound["udp_relay_mode"] == "native"


def test_parse_anytls_link():
    link = (
        "anytls://test-password@node.example.com:13301"
        "?security=tls&sni=cdn.example.com&insecure=1&type=tcp&headerType=none"
        "&fp=chrome&alpn=h2,http%2F1.1&idle_session_check_interval=30"
        "&idle-session-timeout=45s&min_idle_session=2#%F0%9F%87%B8%F0%9F%87%AC%20SG%2001"
    )
    result = parse_text(link)

    assert result.count == 1
    node = result.nodes[0]
    assert node.name == "🇸🇬 SG 01"
    assert node.type == "anytls"
    assert node.server == "node.example.com"
    assert node.server_port == 13301
    assert node.outbound["password"] == "test-password"
    assert node.outbound["tls"]["server_name"] == "cdn.example.com"
    assert node.outbound["tls"]["insecure"] is True
    assert node.outbound["tls"]["utls"]["fingerprint"] == "chrome"
    assert node.outbound["tls"]["alpn"] == ["h2", "http/1.1"]
    assert node.outbound["idle_session_check_interval"] == "30s"
    assert node.outbound["idle_session_timeout"] == "45s"
    assert node.outbound["min_idle_session"] == 2
    assert "transport" not in node.outbound


def test_parse_base64_anytls_subscription():
    link = "anytls://secret@node.example.com:443?sni=cdn.example.com#AnyTLS"
    encoded = base64.urlsafe_b64encode((link + "\r\n").encode()).decode()

    result = parse_text(encoded)

    assert result.count == 1
    assert result.nodes[0].type == "anytls"
    assert result.nodes[0].outbound["password"] == "secret"


def test_parse_clash_yaml_anytls():
    text = """
proxies:
  - name: SG AnyTLS
    type: anytls
    server: node.example.com
    port: 443
    password: secret
    sni: cdn.example.com
    client-fingerprint: chrome
    skip-cert-verify: true
    alpn: [h2, http/1.1]
    idle-session-check-interval: 30
    idle-session-timeout: 45s
    min-idle-session: 1
"""
    result = parse_text(text)

    assert result.count == 1
    node = result.nodes[0]
    assert node.type == "anytls"
    assert node.outbound["tls"]["server_name"] == "cdn.example.com"
    assert node.outbound["tls"]["insecure"] is True
    assert node.outbound["tls"]["utls"]["fingerprint"] == "chrome"
    assert node.outbound["tls"]["alpn"] == ["h2", "http/1.1"]
    assert node.outbound["idle_session_check_interval"] == "30s"
    assert node.outbound["idle_session_timeout"] == "45s"
    assert node.outbound["min_idle_session"] == 1


def test_invalid_anytls_is_warning():
    result = parse_text("anytls://@node.example.com:443#Invalid")

    assert result.count == 0
    assert "AnyTLS password is required" in result.warnings[0]


def test_parse_ss_v2ray_plugin_link():
    userinfo = base64.urlsafe_b64encode(b"aes-256-gcm:secret").decode().rstrip("=")
    text = f"ss://{userinfo}@ss.example.com:8388?plugin=v2ray-plugin%3Btls%3Bhost%3Dcdn.example.com%3Bpath%3D%252Fws#SS"
    result = parse_text(text)
    assert result.count == 1
    node = result.nodes[0]
    assert node.outbound["plugin"]["type"] == "v2ray-plugin"
    assert node.outbound["plugin"]["tls"] is True
    assert node.outbound["plugin"]["host"] == "cdn.example.com"
    assert node.outbound["plugin"]["path"] == "/ws"


def test_parse_subscription_skips_info_lines():
    text = "\n".join([
        "vless://00000000-0000-0000-0000-000000000000@example.com:443?security=reality&pbk=pk&sid=11&sni=www.mozilla.org#剩余流量：10GB",
        "vless://00000000-0000-0000-0000-000000000001@example.com:443?security=tls&sni=www.mozilla.org#Node-1",
    ])
    result = parse_text(text)
    assert result.count == 1
    assert result.nodes[0].name == "Node-1"
    assert result.warnings



def test_parse_base64_subscription_with_non_ascii_noise():
    subscription = "\n".join([
        "vless://00000000-0000-0000-0000-000000000000@example.com:443?security=reality&pbk=pk123&sid=1122&sni=www.mozilla.org#Node-1",
        "vless://00000000-0000-0000-0000-000000000001@example.com:443?security=tls&sni=www.mozilla.org#Node-2",
    ])
    encoded = base64.urlsafe_b64encode(subscription.encode()).decode()
    noisy = "﻿" + encoded[:40] + "剩余流量" + encoded[40:] + "​"

    result = parse_text(noisy)

    assert result.count == 2
    assert result.nodes[0].outbound["tls"]["reality"]["public_key"] == "pk123"



def test_parse_clash_yaml_vless_reality_and_skip_info_nodes():
    text = """
proxies:
  - name: 剩余流量：10GB
    type: vless
    server: hk.example.com
    port: 443
    uuid: 00000000-0000-0000-0000-000000000000
    tls: true
    servername: www.mozilla.org
    client-fingerprint: chrome
    flow: xtls-rprx-vision
    reality-opts:
      public-key: pk-info
      short-id: 1111aaaa
  - name: HK-REALITY
    type: vless
    server: hk2.example.com
    port: 443
    uuid: 00000000-0000-0000-0000-000000000001
    tls: true
    servername: www.mozilla.org
    client-fingerprint: chrome
    flow: xtls-rprx-vision
    reality-opts:
      public-key: pk-real
      short-id: abcd1234
"""
    result = parse_text(text)
    assert result.count == 1
    node = result.nodes[0]
    assert node.name == "HK-REALITY"
    assert node.outbound["flow"] == "xtls-rprx-vision"
    assert node.outbound["tls"]["reality"]["public_key"] == "pk-real"
    assert node.outbound["tls"]["reality"]["short_id"] == "abcd1234"
    assert result.warnings
