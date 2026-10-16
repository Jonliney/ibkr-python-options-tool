import json

import pytest
from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWebEngineWidgets import QWebEngineView
from starlette.testclient import TestClient

from ibkr_options_manager.app.web_window import _start_local_server
from support.qt import wait_for_javascript
from support.workbench import _demo_workbench

pytestmark = pytest.mark.gui


def test_journal_views_render_at_desktop_and_minimum_size(tmp_path):
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    server, thread, port = _start_local_server(workbench.app)
    view = QWebEngineView()
    view.show()
    screenshots = tmp_path / "captures"
    screenshots.mkdir(parents=True, exist_ok=True)
    selected = next(
        r
        for r in workbench._journal_rows()
        if r.contract.con_id == workbench._selected_con_id
    )
    try:
        for name, action in (
            ("dashboard", {"action": "show-dashboard"}),
            ("analysis", {"action": "open-trade", "trade_id": selected.row_id}),
            ("management", {"action": "show-management"}),
        ):
            client.post(workbench.path + "action", data=action)
            for width in (1500, 1120):
                view.resize(width, 920)
                loop = QEventLoop()
                observations = []

                def loaded(ok, observations=observations, loop=loop):
                    assert ok

                    def settled(value):
                        view.page().runJavaScript(
                            """JSON.stringify({
                            overflow: document.documentElement.scrollWidth > innerWidth,
                            chart: Array.from(document.querySelectorAll(
                              '.journal-equity-chart,.journal-price-chart'
                            )).every(e => e.getBoundingClientRect().width > 0),
                            dashboard: !!document.querySelector(
                              '[data-journal-dashboard]'),
                            analysis: !!document.querySelector(
                              '[data-position-analysis]')
                        })""",
                            lambda result: (
                                observations.append(json.loads(result)),
                                loop.quit(),
                            ),
                        )

                    wait_for_javascript(
                        view,
                        "document.readyState === 'complete' && "
                        "!!document.querySelector('.journal-app-navigation')",
                        settled,
                    )

                view.loadFinished.connect(loaded)
                view.setUrl(
                    QUrl(
                        f"http://127.0.0.1:{port}{workbench.path}?capture={name}-{width}"
                    )
                )
                QTimer.singleShot(8000, loop.quit)
                loop.exec()
                view.loadFinished.disconnect(loaded)
                assert observations
                assert not observations[0]["overflow"]
                assert observations[0]["chart"]
                assert observations[0]["dashboard"] == (name == "dashboard")
                assert observations[0]["analysis"] == (name == "analysis")
                QTest.qWait(150)  # allow the rendered compositor frame to reach grab()
                view.grab().save(str(screenshots / f"{name}-{width}.png"))
    finally:
        view.close()
        view.deleteLater()
        server.should_exit = True
        thread.join(timeout=2)
        workbench.close()


def test_entry_settings_update_in_place_and_keep_input_focus(tmp_path):
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    client.post(workbench.path + "action", data={"action": "show-dashboard"})
    server, thread, port = _start_local_server(workbench.app)
    view = QWebEngineView()
    view.resize(1500, 920)
    view.show()
    loop = QEventLoop()
    seen = []
    try:

        def loaded(ok):
            assert ok
            view.page().runJavaScript("""(() => {
              window.journalOriginalDocument = document.documentElement;
              document.querySelector('[aria-label="Entry timing settings"]').click();
            })()""")

            def opened(value):
                if not value:
                    seen.append(False)
                    loop.quit()
                    return
                view.page().runJavaScript("""(() => {
                  const stop = document.querySelector('#journal-stop');
                  const target = document.querySelector('#journal-target');
                  stop.value='40'; target.value='35'; target.focus();
                  stop.dispatchEvent(new Event('input',{bubbles:true}));
                  target.dispatchEvent(new Event('input',{bubbles:true}));
                })()""")

                def updated(value):
                    if not value:
                        seen.append(False)
                        loop.quit()
                        return
                    view.page().runJavaScript(
                        """(() => {
                      const input = document.querySelector('#journal-target');
                      return window.journalOriginalDocument === document.documentElement
                        && document.activeElement === input
                        && input.closest('[popover]').matches(':popover-open')
                        && document.querySelectorAll(
                          '.journal-recovery-table tr').length > 3;
                    })()""",
                        lambda result: (seen.append(result), loop.quit()),
                    )

                wait_for_javascript(
                    view,
                    "document.querySelector('[data-entry-results]')"
                    ".textContent.includes('trades reached +35%')",
                    updated,
                    timeout=5,
                )

            wait_for_javascript(
                view,
                "document.querySelector('#journal-stop').closest('[popover]').matches(':popover-open')",
                opened,
                timeout=5,
            )

        view.loadFinished.connect(loaded)
        view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
        QTimer.singleShot(12000, loop.quit)
        loop.exec()
        assert seen == [True]
        assert workbench._journal_stop == 40 and workbench._journal_target == 35
        QTest.qWait(150)
        view.grab().save(str(tmp_path / "entry-settings-open.png"))
    finally:
        view.close()
        view.deleteLater()
        server.should_exit = True
        thread.join(timeout=2)
        workbench.close()
