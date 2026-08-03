import json

from manager.brokers import live

TRADEVPS = {"brokers": [
    {"name": "IC Markets", "servers": [{"name": "ICMarketsSC-Demo", "type": "demo"}]}]}


class _Resp:
    def __init__(self, body=b"{}", status=200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_live_parses(monkeypatch):
    monkeypatch.setattr(
        live.urllib.request, "urlopen",
        lambda url, timeout=None: _Resp(json.dumps(TRADEVPS).encode("utf-8")))
    assert live.fetch_live(timeout=5.0) == TRADEVPS


def test_fetch_live_returns_none_on_network_error(monkeypatch):
    def boom(url, timeout=None):
        raise OSError("no network")
    monkeypatch.setattr(live.urllib.request, "urlopen", boom)
    assert live.fetch_live(timeout=1.0) is None


def test_fetch_live_returns_none_on_non200(monkeypatch):
    monkeypatch.setattr(
        live.urllib.request, "urlopen",
        lambda url, timeout=None: _Resp(b"{}", status=500))
    assert live.fetch_live(timeout=1.0) is None


def test_fetch_live_returns_none_on_bad_json(monkeypatch):
    monkeypatch.setattr(
        live.urllib.request, "urlopen",
        lambda url, timeout=None: _Resp(b"not json"))
    assert live.fetch_live(timeout=1.0) is None


def test_refresh_cache_writes_then_loads_round_trip(tmp_path, monkeypatch):
    cache = tmp_path / "brokers_cache.json"
    monkeypatch.setattr(
        live.urllib.request, "urlopen",
        lambda url, timeout=None: _Resp(json.dumps(TRADEVPS).encode("utf-8")))
    payload = live.refresh_cache(cache, timeout=5.0, now=1000.0)
    assert payload is not None
    assert payload["brokers"] == TRADEVPS["brokers"]
    loaded = live.load_cache(cache)
    assert loaded is not None
    assert loaded["brokers"] == TRADEVPS["brokers"]


def test_refresh_cache_failure_leaves_cache_untouched(tmp_path, monkeypatch):
    cache = tmp_path / "brokers_cache.json"
    cache.write_text(json.dumps({"fetched_at": "1970-01-01T00:00:00",
                                 "brokers": []}), encoding="utf-8")

    def boom(url, timeout=None):
        raise OSError("offline")
    monkeypatch.setattr(live.urllib.request, "urlopen", boom)
    assert live.refresh_cache(cache, timeout=1.0) is None
    # existing cache file unchanged
    assert json.loads(cache.read_text(encoding="utf-8"))["brokers"] == []


def test_load_cache_missing_returns_none(tmp_path):
    assert live.load_cache(tmp_path / "nope.json") is None


def test_load_cache_corrupt_returns_none(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("not json", encoding="utf-8")
    assert live.load_cache(p) is None


def test_is_fresh_true_within_24h():
    payload = {"fetched_at": live.iso_from_ts(1000.0)}
    assert live.is_fresh(payload, now=1000.0 + 3600.0) is True


def test_is_fresh_false_after_24h():
    payload = {"fetched_at": live.iso_from_ts(1000.0)}
    assert live.is_fresh(payload, now=1000.0 + 25 * 3600.0) is False


def test_is_fresh_false_when_fetched_at_missing():
    assert live.is_fresh({"fetched_at": ""}, now=1000.0) is False