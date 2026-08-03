import pytest


@pytest.fixture
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _cleanup_qt_widgets():
    """Delete top-level Qt widgets after each test.

    MainWindow auto-starts a periodic-update QTimer and schedules a
    one-shot check whose bound-method target keeps the widget alive. Without
    per-test cleanup these widgets accumulate until pytest's final
    gc_collect_harder (pytest_unconfigure), where destroying many timer-bearing
    widgets at once heap-corrupts the process (Windows fatal exception
    0xc0000374) and the suite exits non-zero with no "X passed" summary. This
    deletes top-level widgets each test so none survive to the final GC. It
    no-ops when PySide6 isn't installed or no QApplication exists (headless
    tests), so it is safe to leave autouse.
    """
    yield
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.topLevelWidgets():
        w.deleteLater()
    # Flush the DeferredDelete events so the C++ widgets are actually gone
    # before this fixture returns (and long before pytest's final GC).
    app.processEvents()