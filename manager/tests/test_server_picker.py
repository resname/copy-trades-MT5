import pytest

pytest.importorskip("PySide6")

from manager.brokers.catalog import Broker, BrokerServer, BrokerCatalog


class FakeController:
    def __init__(self, catalog):
        self._catalog = catalog

    def get_catalog(self):
        return self._catalog

    def refresh_brokers(self):
        return self._catalog


def _catalog():
    return BrokerCatalog(
        default=[Broker(
            "IC Markets",
            (BrokerServer("ICMarketsSC-Demo", "demo"),
             BrokerServer("ICMarketsSC-Live", "real")), "default")],
        learned_servers=["MyOldServer"])


def test_picker_populates_brokers(qapp):
    from manager.gui.server_picker import BrokerServerPicker
    p = BrokerServerPicker(FakeController(_catalog()))
    names = [p.broker_combo.itemText(i) for i in range(p.broker_combo.count())]
    assert "(Previously used)" in names
    assert "IC Markets" in names


def test_picker_servers_demo_first(qapp):
    from manager.gui.server_picker import BrokerServerPicker
    p = BrokerServerPicker(FakeController(_catalog()))
    p.set_broker("IC Markets")
    items = [p.server_combo.itemData(i) for i in range(p.server_combo.count())]
    assert items == ["ICMarketsSC-Demo", "ICMarketsSC-Live"]  # demo first


def test_picker_server_returns_raw_name_no_label(qapp):
    from manager.gui.server_picker import BrokerServerPicker
    p = BrokerServerPicker(FakeController(_catalog()))
    p.set_broker("IC Markets")
    p.set_server("ICMarketsSC-Live")
    assert p.server() == "ICMarketsSC-Live"  # no "(real)" suffix


def test_picker_free_text_server(qapp):
    from manager.gui.server_picker import BrokerServerPicker
    p = BrokerServerPicker(FakeController(_catalog()))
    p.server_combo.setEditText("MyCustomServer")
    assert p.server() == "MyCustomServer"


def test_picker_strips_label_from_free_text(qapp):
    from manager.gui.server_picker import BrokerServerPicker
    p = BrokerServerPicker(FakeController(_catalog()))
    p.server_combo.setEditText("SomeServer (demo)")
    assert p.server() == "SomeServer"


def test_picker_empty_catalog_allows_free_text(qapp):
    from manager.gui.server_picker import BrokerServerPicker
    p = BrokerServerPicker(FakeController(BrokerCatalog()))
    p.server_combo.setEditText("Anything")
    assert p.server() == "Anything"