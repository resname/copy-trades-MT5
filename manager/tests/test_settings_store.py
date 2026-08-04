# manager/tests/test_settings_store.py
import json
from pathlib import Path

from manager.settings.store import SettingsStore


def _store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(path=tmp_path / "settings.json")


def test_load_missing_file_returns_empty_dict():
    store = _store(Path("/no/such/dir"))
    assert store.load() == {}


def test_save_then_load_round_trip(tmp_path):
    store = _store(tmp_path)
    data = {"accounts": {"master": {"login": 5001, "server": "Demo-Server"}},
            "provisioned_instances": [], "global": {"heartbeat_seconds": 5}}
    store.save(data)
    assert store.load() == data


def test_load_corrupt_json_returns_empty_dict(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{ not valid json", encoding="utf-8")
    store = SettingsStore(path=p)
    assert store.load() == {}


def test_provisioned_instance_registry_add_list_remove(tmp_path):
    store = _store(tmp_path)
    assert store.list_provisioned_instances() == []
    store.add_provisioned_instance(r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_0")
    store.add_provisioned_instance(r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_1")
    assert store.list_provisioned_instances() == [
        r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_0",
        r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_1"]
    # persisted across a new store instance
    assert SettingsStore(path=tmp_path / "settings.json").list_provisioned_instances() == [
        r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_0",
        r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_1"]
    store.remove_provisioned_instance(r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_0")
    assert store.list_provisioned_instances() == [
        r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_1"]


def test_add_provisioned_instance_is_idempotent(tmp_path):
    store = _store(tmp_path)
    store.add_provisioned_instance("C:/inst_0")
    store.add_provisioned_instance("C:/inst_0")
    assert store.list_provisioned_instances() == ["C:/inst_0"]


def test_save_preserves_existing_provisioned_instances(tmp_path):
    """add_provisioned_instance must not clobber accounts/global data."""
    store = _store(tmp_path)
    store.save({"accounts": {"master": {"login": 1}}, "provisioned_instances": [],
                "global": {"x": 1}})
    store.add_provisioned_instance("C:/inst_0")
    loaded = store.load()
    assert loaded["accounts"] == {"master": {"login": 1}}
    assert loaded["global"] == {"x": 1}
    assert loaded["provisioned_instances"] == ["C:/inst_0"]


def test_load_config_empty_when_absent(tmp_path):
    from manager.settings.store import SettingsStore
    s = SettingsStore(path=tmp_path / "settings.json")
    assert s.load_config() == {}


def test_save_then_load_config_round_trip(tmp_path):
    from manager.settings.store import SettingsStore
    s = SettingsStore(path=tmp_path / "settings.json")
    cfg = {"master": {"terminal_path": "C:/t/terminal64.exe"},
           "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
                       "symbol_map_csv": "", "step_amount": 100.0,
                       "step_size": 0.01, "max_lot": 10.0,
                       "max_trade_age_minutes": 10.0, "normalize_sltp": True}]}
    s.save_config(cfg)
    assert s.load_config() == cfg


def test_save_then_load_config_round_trip_with_sizing_fields(tmp_path):
    from manager.settings.store import SettingsStore
    s = SettingsStore(path=tmp_path / "settings.json")
    cfg = {"master": {"terminal_path": "C:/t/terminal64.exe"},
           "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
                       "symbol_map_csv": "", "step_amount": 100.0,
                       "step_size": 0.01, "max_lot": 10.0,
                       "max_trade_age_minutes": 10.0, "normalize_sltp": True,
                       "sizing_mode": "copy_master", "master_base_lot": 0.1,
                       "fixed_lot": 0.05}]}
    s.save_config(cfg)
    assert s.load_config() == cfg


def test_save_config_preserves_other_keys(tmp_path):
    from manager.settings.store import SettingsStore
    s = SettingsStore(path=tmp_path / "settings.json")
    s.save({"accounts": {"master": {"id": "master"}}, "provisioned_instances": ["C:/x"],
            "global": {}})
    s.save_config({"master": {"terminal_path": "C:/t/terminal64.exe"}, "slaves": []})
    data = s.load()
    assert data["accounts"] == {"master": {"id": "master"}}
    assert data["provisioned_instances"] == ["C:/x"]
    assert data["config"]["master"]["terminal_path"] == "C:/t/terminal64.exe"


def test_save_then_load_config_round_trip_with_autostart(tmp_path):
    from manager.settings.store import SettingsStore
    s = SettingsStore(path=tmp_path / "settings.json")
    cfg = {"master": {"terminal_path": "C:/t/terminal64.exe"},
           "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
           "autostart": {"on_boot": True, "auto_copy": False}}
    s.save_config(cfg)
    assert s.load_config() == cfg