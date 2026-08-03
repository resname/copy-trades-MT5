from manager.brokers import learned
from manager.settings.store import SettingsStore


def _store(tmp_path):
    return SettingsStore(path=tmp_path / "settings.json")


def test_load_empty_when_absent(tmp_path):
    assert learned.load(_store(tmp_path)) == []


def test_record_then_load_persists(tmp_path):
    s = _store(tmp_path)
    learned.record(s, "ICMarketsSC-Demo")
    assert learned.load(s) == ["ICMarketsSC-Demo"]
    # persisted across a new store instance pointing at the same file
    assert learned.load(SettingsStore(path=tmp_path / "settings.json")) == \
           ["ICMarketsSC-Demo"]


def test_record_dedups(tmp_path):
    s = _store(tmp_path)
    learned.record(s, "A")
    learned.record(s, "A")
    learned.record(s, "B")
    assert learned.load(s) == ["A", "B"]


def test_record_ignores_blank(tmp_path):
    s = _store(tmp_path)
    learned.record(s, "   ")
    assert learned.load(s) == []


def test_record_preserves_other_settings(tmp_path):
    s = _store(tmp_path)
    s.save({"accounts": {"master": {"login": 1}}, "provisioned_instances": [],
            "global": {}})
    learned.record(s, "A")
    loaded = s.load()
    assert loaded["accounts"] == {"master": {"login": 1}}
    assert loaded["learned_servers"] == ["A"]