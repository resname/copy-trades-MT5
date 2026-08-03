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


def test_password_blob_survives_round_trip(tmp_path):
    """The store is crypto-agnostic: an opaque base64 password blob stored
    in an account dict is returned byte-for-byte after a save/load cycle."""
    store = _store(tmp_path)
    acct = {"login": 5001, "server": "Demo-Server",
            "password_blob": "ZW5jcnlwdGVk"}
    store.save({"accounts": {"s1": acct}, "provisioned_instances": [],
                "global": {}})
    loaded = store.load()
    assert loaded["accounts"]["s1"]["password_blob"] == "ZW5jcnlwdGVk"
    assert loaded["accounts"]["s1"]["login"] == 5001


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