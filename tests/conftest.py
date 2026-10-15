"""Isolated state for every test, with explicit ownership of GUI resources."""

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
os.environ.setdefault("IBKR_OPTIONS_MANAGER_REDUCE_MOTION", "1")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    yield application


@pytest.fixture(autouse=True)
def isolated_application_state(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    from ibkr_options_manager import execution
    from ibkr_options_manager.app import account_preferences
    from ibkr_options_manager.app.web import surface

    monkeypatch.setenv(
        "IBKR_OPTIONS_MANAGER_CANCEL_TRACE", str(tmp_path / "cancellations.jsonl")
    )
    monkeypatch.setenv(
        "IBKR_OPTIONS_MANAGER_PRICE_TRACE", str(tmp_path / "prices.jsonl")
    )
    monkeypatch.setattr(
        account_preferences,
        "_preferences",
        lambda: QSettings(str(tmp_path / "account.ini"), QSettings.Format.IniFormat),
    )

    def journal_path():
        return Path(tmp_path / "execution.json")

    monkeypatch.setattr(execution, "default_paper_journal_path", journal_path)
    main_module = sys.modules.get("ibkr_options_manager.app.main")
    if main_module is not None:
        monkeypatch.setattr(main_module, "default_paper_journal_path", journal_path)
    monkeypatch.setattr(surface, "_INVENTORY_HEARTBEAT_SECONDS", 0.02)

    workbenches = []
    original_init = surface.StarUIWorkbench.__init__

    def track_workbench(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        workbenches.append(self)

    monkeypatch.setattr(surface.StarUIWorkbench, "__init__", track_workbench)
    yield
    for workbench in reversed(workbenches):
        workbench.close()


@pytest.fixture(autouse=True)
def owned_gui_resources(request, monkeypatch):
    if request.node.get_closest_marker("gui") is None:
        yield
        return
    application = request.getfixturevalue("qapp")
    from PySide6.QtCore import QCoreApplication, QEvent, QTimer
    from PySide6.QtWebEngineWidgets import QWebEngineView

    timers = []

    def single_shot(milliseconds, callback):
        timer = QTimer(application)
        timer.setSingleShot(True)
        timer.timeout.connect(callback)
        timers.append(timer)
        timer.start(milliseconds)

    monkeypatch.setattr(QTimer, "singleShot", single_shot)
    yield
    for timer in timers:
        timer.stop()
        timer.deleteLater()
    for window in application.topLevelWidgets():
        if isinstance(window, QWebEngineView):
            window.page().deleteLater()
        window.close()
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()
