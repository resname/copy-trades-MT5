from manager.brokers.catalog import (
    Broker, BrokerServer, BrokerCatalog, PREVIOUSLY_USED, parse_brokers_json,
)


def test_parse_brokers_json_tradevps_shape():
    data = {"brokers": [
        {"name": "IC Markets",
         "servers": [{"name": "ICMarketsSC-Demo", "type": "demo"},
                      {"name": "ICMarketsSC-Live", "type": "real"}]}]}
    out = parse_brokers_json(data, "default")
    assert out == [Broker(
        "IC Markets",
        (BrokerServer("ICMarketsSC-Demo", "demo"),
         BrokerServer("ICMarketsSC-Live", "real")),
        "default")]


def test_parse_brokers_json_unknown_type_becomes_unknown():
    out = parse_brokers_json(
        {"brokers": [{"name": "X", "servers": [{"name": "S", "type": "weird"}]}]},
        "live")
    assert out[0].servers[0].type == "unknown"


def test_parse_brokers_json_missing_type_is_unknown():
    out = parse_brokers_json(
        {"brokers": [{"name": "X", "servers": [{"name": "S"}]}]}, "live")
    assert out[0].servers[0].type == "unknown"


def test_parse_brokers_json_skips_empty_server_names():
    out = parse_brokers_json(
        {"brokers": [{"name": "X", "servers": [{"name": ""}, {"name": "S"}]}]},
        "default")
    assert [s.name for s in out[0].servers] == ["S"]


def test_merge_dedups_servers_across_sources():
    default = [Broker("IC Markets",
                      (BrokerServer("ICMarketsSC-Demo", "demo"),), "default")]
    live = [Broker("IC Markets",
                   (BrokerServer("ICMarketsSC-Demo", "demo"),
                    BrokerServer("ICMarketsSC-Live", "real")), "live")]
    cat = BrokerCatalog(default=default, live=live)
    ic = [b for b in cat.brokers if b.name == "IC Markets"][0]
    assert {s.name for s in ic.servers} == {"ICMarketsSC-Demo", "ICMarketsSC-Live"}


def test_merge_normalizes_broker_name_case_and_whitespace():
    a = [Broker("IC Markets", (BrokerServer("S1", "demo"),), "default")]
    b = [Broker("  ic markets ", (BrokerServer("S2", "real"),), "live")]
    cat = BrokerCatalog(default=a, live=b)
    ics = [bk for bk in cat.brokers if bk.name.strip().lower() == "ic markets"]
    assert len(ics) == 1
    assert {s.name for s in ics[0].servers} == {"S1", "S2"}


def test_servers_for_demo_first_then_real_then_unknown():
    servers = (BrokerServer("Z-Real", "real"), BrokerServer("A-Demo", "demo"),
               BrokerServer("M-Unknown", "unknown"), BrokerServer("B-Demo", "demo"))
    cat = BrokerCatalog(default=[Broker("X", servers, "default")])
    assert [s.name for s in cat.servers_for("X")] == \
           ["A-Demo", "B-Demo", "Z-Real", "M-Unknown"]


def test_broker_names_previously_used_first_when_learned():
    cat = BrokerCatalog(
        default=[Broker("Beta", (BrokerServer("B", "demo"),), "default"),
                 Broker("Alpha", (BrokerServer("A", "demo"),), "default")],
        learned_servers=["LearnedServer"])
    names = cat.broker_names()
    assert names[0] == PREVIOUSLY_USED
    assert names[1:] == ["Alpha", "Beta"]  # real brokers alphabetical


def test_broker_names_no_previously_used_when_empty():
    cat = BrokerCatalog(default=[Broker("Alpha", (), "default")])
    assert cat.broker_names() == ["Alpha"]


def test_servers_for_previously_used_returns_learned():
    cat = BrokerCatalog(learned_servers=["S1", "S2"])
    assert {s.name for s in cat.servers_for(PREVIOUSLY_USED)} == {"S1", "S2"}


def test_add_learned_dedups_in_memory():
    cat = BrokerCatalog(learned_servers=["S1"])
    cat.add_learned("S1")
    cat.add_learned("S2")
    assert {s.name for s in cat.servers_for(PREVIOUSLY_USED)} == {"S1", "S2"}


def test_servers_for_unknown_broker_returns_empty():
    cat = BrokerCatalog(default=[Broker("X", (BrokerServer("S", "demo"),), "default")])
    assert cat.servers_for("Nope") == []