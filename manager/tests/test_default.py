from manager.brokers.default import load_default
from manager.brokers.catalog import Broker


def test_load_default_returns_brokers_with_servers():
    brokers = load_default()
    assert brokers
    assert all(isinstance(b, Broker) for b in brokers)
    # the shipped snapshot is non-empty and has at least one demo server
    assert any(b.servers for b in brokers)
    assert any(s.type == "demo" for b in brokers for s in b.servers)


def test_load_default_missing_file_returns_empty(tmp_path):
    assert load_default(tmp_path / "nope.json") == []