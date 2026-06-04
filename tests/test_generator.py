import pytest

from app.generator import ConfigError, generate_config
from app.models import PortMapping, ProxyNode


def make_node(tag="node-vless-a"):
    outbound = {
        "type": "vless",
        "tag": tag,
        "server": "example.com",
        "server_port": 443,
        "uuid": "00000000-0000-0000-0000-000000000000",
    }
    return ProxyNode(
        tag=tag,
        name="Node",
        type="vless",
        server="example.com",
        server_port=443,
        outbound=outbound,
    )


def test_generate_config_binds_port_to_outbound():
    node = make_node()
    config = generate_config([node], {"8001": PortMapping(node_tag=node.tag)})
    assert config["inbounds"][0]["tag"] == "port-8001"
    assert config["inbounds"][0]["type"] == "mixed"
    assert config["route"]["rules"][0]["inbound"] == ["port-8001"]
    assert config["route"]["rules"][0]["outbound"] == node.tag
    assert {"type": "direct", "tag": "direct"} in config["outbounds"]


def test_generate_config_uses_current_runtime_addresses(monkeypatch):
    monkeypatch.setattr("app.generator.current_proxy_listen_host", lambda: "0.0.0.0")
    monkeypatch.setattr("app.generator.current_clash_api_addr", lambda: "127.0.0.1:10000")
    monkeypatch.setattr("app.generator.current_domain_resolve_strategy", lambda: "ipv4_only")
    node = make_node()

    config = generate_config([node], {"8001": PortMapping(node_tag=node.tag)})

    assert config["inbounds"][0]["listen"] == "0.0.0.0"
    assert config["experimental"]["clash_api"]["external_controller"] == "127.0.0.1:10000"
    assert config["route"]["rules"][0]["action"] == "resolve"
    assert config["route"]["rules"][0]["strategy"] == "ipv4_only"
    assert config["route"]["rules"][1]["outbound"] == node.tag


def test_generate_config_rejects_missing_node():
    with pytest.raises(ConfigError):
        generate_config([], {"8001": PortMapping(node_tag="missing")})


def test_generate_config_rejects_invalid_port():
    node = make_node()
    with pytest.raises(ConfigError):
        generate_config([node], {"80": PortMapping(node_tag=node.tag)})


def test_generate_temp_config_can_disable_clash_api():
    node = make_node()
    config = generate_config(
        [node],
        {"19001": PortMapping(node_tag=node.tag)},
        include_clash_api=False,
    )
    assert "experimental" not in config
    assert config["inbounds"][0]["listen_port"] == 19001
