import pytest

from app.generator import ConfigError, generate_config, generate_pool_router_config
from app.models import PoolMember, PortMapping, ProxyNode, ProxyPool


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
    assert config["route"]["rules"][0]["action"] == "sniff"
    assert config["route"]["rules"][1]["inbound"] == ["port-8001"]
    assert config["route"]["rules"][1]["outbound"] == node.tag
    assert {"type": "direct", "tag": "direct"} in config["outbounds"]


def test_generate_config_uses_current_runtime_addresses(monkeypatch):
    monkeypatch.setattr("app.generator.current_proxy_listen_host", lambda: "0.0.0.0")
    monkeypatch.setattr("app.generator.current_clash_api_addr", lambda: "127.0.0.1:10000")
    monkeypatch.setattr("app.generator.current_domain_resolve_strategy", lambda: "ipv4_only")
    node = make_node()

    config = generate_config([node], {"8001": PortMapping(node_tag=node.tag)})

    assert config["inbounds"][0]["listen"] == "0.0.0.0"
    assert config["experimental"]["clash_api"]["external_controller"] == "127.0.0.1:10000"
    assert config["route"]["rules"][0]["action"] == "sniff"
    assert config["route"]["rules"][1]["action"] == "resolve"
    assert config["route"]["rules"][1]["strategy"] == "ipv4_only"
    assert config["route"]["rules"][2]["outbound"] == node.tag


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


def test_generate_config_allows_explicit_listen_host_override(monkeypatch):
    monkeypatch.setattr("app.generator.current_proxy_listen_host", lambda: "0.0.0.0")
    node = make_node()

    config = generate_config(
        [node],
        {"19001": PortMapping(node_tag=node.tag)},
        include_clash_api=False,
        listen_host="127.0.0.1",
    )
    assert config["inbounds"][0]["listen"] == "127.0.0.1"


def test_generate_config_enables_sniffing():
    node = make_node()
    config = generate_config([node], {"8001": PortMapping(node_tag=node.tag)})
    inbound = config["inbounds"][0]
    assert "sniff" not in inbound
    assert "sniff_override_destination" not in inbound
    assert "sniff_timeout" not in inbound
    assert config["route"]["rules"][0]["inbound"] == ["port-8001"]
    assert config["route"]["rules"][0]["action"] == "sniff"


def test_generate_config_includes_dns_config(monkeypatch):
    monkeypatch.setattr("app.generator.current_domain_resolve_strategy", lambda: "ipv4_only")
    node = make_node()
    config = generate_config([node], {"8001": PortMapping(node_tag=node.tag)})
    dns = config["dns"]
    assert len(dns["servers"]) == 1
    assert dns["servers"][0]["tag"] == "dns_direct"
    assert dns["servers"][0]["type"] == "local"
    assert dns["strategy"] == "ipv4_only"
    assert config["route"]["default_domain_resolver"] == "dns_direct"


def test_generate_config_adds_tcp_fast_open_but_not_multiplex_by_default():
    node = make_node()
    config = generate_config([node], {"8001": PortMapping(node_tag=node.tag)})
    outbound = [o for o in config["outbounds"] if o["tag"] == node.tag][0]
    assert outbound["tcp_fast_open"] is True
    assert "multiplex" not in outbound


def test_generate_config_does_not_add_unsupported_tcp_fast_open_to_anytls():
    node = ProxyNode(
        tag="node-anytls-a",
        name="AnyTLS",
        type="anytls",
        server="node.example.com",
        server_port=443,
        outbound={
            "type": "anytls",
            "tag": "node-anytls-a",
            "server": "node.example.com",
            "server_port": 443,
            "password": "secret",
            "tls": {"enabled": True, "server_name": "cdn.example.com"},
        },
    )

    config = generate_config([node], {"8001": PortMapping(node_tag=node.tag)})

    outbound = [item for item in config["outbounds"] if item["tag"] == node.tag][0]
    assert "tcp_fast_open" not in outbound


def test_generate_config_preserves_existing_multiplex():
    node = make_node()
    node.outbound["multiplex"] = {
        "enabled": True,
        "protocol": "h2mux",
        "max_streams": 16,
    }
    config = generate_config([node], {"8001": PortMapping(node_tag=node.tag)})
    outbound = [o for o in config["outbounds"] if o["tag"] == node.tag][0]
    assert outbound["tcp_fast_open"] is True
    assert outbound["multiplex"]["enabled"] is True
    assert outbound["multiplex"]["protocol"] == "h2mux"
    assert outbound["multiplex"]["max_streams"] == 16


def test_generate_router_mode_creates_internal_fixed_and_pool_member_inbounds():
    first = make_node("node-a")
    second = make_node("node-b")
    pool = ProxyPool(
        id="pool-ai",
        name="AI",
        listen_port=8201,
        members=[PoolMember(node_tag=first.tag), PoolMember(node_tag=second.tag, weight=3)],
    )

    config = generate_config(
        [first, second], {"8001": PortMapping(node_tag=first.tag)}, pools=[pool], router_mode=True
    )

    assert {item["listen"] for item in config["inbounds"]} == {"127.0.0.1"}
    assert {item["tag"] for item in config["inbounds"]} == {
        "port-8001", "pool-member-pool-ai-node-a", "pool-member-pool-ai-node-b"
    }
    assert all(item["listen_port"] >= 18000 for item in config["inbounds"])


def test_generate_pool_router_config_keeps_fixed_routes_and_weights(monkeypatch):
    monkeypatch.setattr("app.generator.current_proxy_listen_host", lambda: "0.0.0.0")
    first = make_node("node-a")
    second = make_node("node-b")
    pool = ProxyPool(
        id="pool-ai", name="AI", listen_port=8201,
        members=[PoolMember(node_tag=first.tag), PoolMember(node_tag=second.tag, weight=3)],
    )

    config = generate_pool_router_config({"8001": PortMapping(node_tag=first.tag)}, [pool])

    listeners = {item["id"]: item for item in config["listeners"]}
    assert listeners["port-8001"]["listen"] == "0.0.0.0:8001"
    assert listeners["pool-pool-ai"]["policy"] == "weighted_round_robin"
    assert [item["weight"] for item in listeners["pool-pool-ai"]["backends"]] == [1, 3]


def test_generate_config_rejects_pool_port_collision():
    node = make_node()
    pool = ProxyPool(id="pool-a", name="A", listen_port=8001, members=[PoolMember(node_tag=node.tag)])

    with pytest.raises(ConfigError, match="already assigned"):
        generate_config([node], {"8001": PortMapping(node_tag=node.tag)}, pools=[pool], router_mode=True)



