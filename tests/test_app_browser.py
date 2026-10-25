import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication
from starlette.testclient import TestClient

from ibkr_options_manager.app.demo import (
    DEMO_ACCOUNT,
    DemoPaperExecutionTransport,
    DemoReadOnlyBroker,
)
from ibkr_options_manager.app.main import main
from ibkr_options_manager.app.view_model import UiStatus
from ibkr_options_manager.app.web import StarUIWorkbench
from ibkr_options_manager.app.web.surface import (
    _live_active_script,
    _local_feedback_script,
    _money,
)
from ibkr_options_manager.app.web_window import (
    _LoopbackOnlyRequestInterceptor,
    _start_local_server,
)
from ibkr_options_manager.execution import (
    ExecutionJournal,
    PaperExecutionService,
)
from ibkr_options_manager.portfolio import PortfolioCoordinator
from ibkr_options_manager.snapshot import SnapshotCoordinator
from support.qt import wait_for_javascript
from support.workbench import _demo_workbench, _trailing_scenario_workbench, _WindowStub

pytestmark = pytest.mark.gui


def test_management_scroll_survives_review_creation_and_order_status_refresh(tmp_path):
    workbench = _trailing_scenario_workbench(tmp_path, "convert")
    workbench._select_locked(1_002_100_161)
    workbench._build_draft_locked()
    TestClient(workbench.app).post(
        workbench.path + "action", data={"action": "show-management"}
    )
    server, thread, port = _start_local_server(workbench.app)
    view = QWebEngineView()
    view.resize(1120, 920)
    view.show()
    loop = QEventLoop()
    observations = []
    stage = [0]
    desired = [0]
    viewport = '[aria-label="OCA layers workspace"][data-slot="scroll-area-viewport"]'

    def inspect():
        view.page().runJavaScript(
            f"""(() => {{
              const v = document.querySelector('{viewport}');
              return JSON.stringify({{top:v.scrollTop,
                max:v.scrollHeight-v.clientHeight,
                sent:!!document.querySelector('[data-order-completion="bracket-create"]'),
                saved:!!document.querySelector('[data-completion-feedback="settings"]'),
                refreshed:!!document.querySelector('[data-completion-feedback="refresh"]')
              }});
            }})()""", observed
        )

    def observed(value):
        observations.append(json.loads(value))
        current = stage[0]
        stage[0] += 1
        if current == 0:
            view.page().runJavaScript(
                f"""(() => {{
                  const v = document.querySelector('{viewport}'); v.scrollTop = 200;
                  const top = v.scrollTop;
                  document.querySelector('button[value="execute-arm"]').click();
                  return top;
                }})()""", lambda top: desired.__setitem__(0, top)
            )
        elif current == 1:
            view.page().runJavaScript(
                "document.querySelector('button[value=\"execute-confirm\"]').click()"
            )
        elif current == 2:
            view.grab().save(str(tmp_path / "bracket-created.png"))
            view.page().runJavaScript(
                "document.querySelector('[data-submission-review] button[type=submit]')"
                ".click()"
            )
        elif current in {3, 4}:
            con_id = 1_003_625_093 if current == 3 else 1_002_100_161
            view.page().runJavaScript(
                f"""Array.from(document.querySelectorAll('#position-inventory form'))
                  .find(f => f.querySelector('[name="con_id"]')?.value === '{con_id}')
                  .querySelector('button').click()"""
            )
        else:
            loop.quit()

    def loaded(ok):
        assert ok
        wait_for_javascript(
            view, f"!!document.querySelector('{viewport}')",
            lambda _: QTimer.singleShot(100, inspect),
        )

    try:
        view.loadFinished.connect(loaded)
        view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
        QTimer.singleShot(12000, loop.quit)
        loop.exec()
    finally:
        view.close()
        server.should_exit = True
        thread.join(timeout=2)

    assert len(observations) == 6, observations
    assert desired[0] >= 100, observations
    for observation in [*observations[1:4], observations[5]]:
        assert abs(observation["top"] - min(desired[0], observation["max"])) < 2
    assert observations[4]["top"] == 0, observations
    assert observations[2]["sent"], observations
    assert not observations[3]["saved"], observations
    assert observations[3]["sent"], observations


def test_local_feedback_settles_without_hiding_errors_or_replacing_busy_controls():
    view = QWebEngineView()
    view.show()
    loop = QEventLoop()
    results = []

    def loaded(ok):
        assert ok

        def inspect():
            view.page().runJavaScript(
                """JSON.stringify({
                  editLabel: document.getElementById('edit').textContent,
                  editDisabled: document.getElementById('edit').disabled,
                  editGreen: document.getElementById('edit').classList.contains(
                    'action-complete'),
                  error: document.querySelector('[role=alert]').textContent,
                  settings: document.getElementById('settings').textContent,
                  refreshIconRestored: !document.querySelector(
                    '[data-refresh-idle-icon]').hidden,
                  checkRemoved: !document.querySelector('[data-completion-icon]'),
                  busy: document.getElementById('busy').textContent
                })""",
                lambda value: (results.append(json.loads(value)), loop.quit()),
            )

        wait_for_javascript(
            view,
            "!document.querySelector('[data-order-completion]')",
            lambda _: inspect(),
            timeout=6,
        )

    view.loadFinished.connect(loaded)
    view.setHtml(
        '<button id="edit" data-order-completion="price-update" '
        'data-restore-label="Review price changes" class="action-complete" disabled>'
        'Successfully edited</button>'
        '<p role="alert">Order state uncertain</p>'
        '<button id="settings" data-completion-feedback="settings">Saved</button>'
        '<button data-completion-feedback="refresh">'
        '<span data-refresh-idle-icon hidden>Refresh</span>'
        '<span data-completion-icon>Check</span></button>'
        '<button id="busy" data-completion-feedback="settings" '
        'aria-busy="true">Refreshing…</button>'
        f"<script>{_local_feedback_script()}</script>"
    )
    QTimer.singleShot(7000, loop.quit)
    loop.exec()
    view.close()

    assert results == [{
        "editLabel": "Review price changes",
        "editDisabled": True,
        "editGreen": False,
        "error": "Order state uncertain",
        "settings": "Settings",
        "refreshIconRestored": True,
        "checkRemoved": True,
        "busy": "Refreshing…",
    }]


def test_contextual_feedback_fits_management_at_desktop_sizes(tmp_path):
    workbench = _demo_workbench()
    workbench.load_demo_data()
    TestClient(workbench.app).post(
        workbench.path + "action", data={"action": "show-management"}
    )
    workbench._build_draft_locked()
    server, thread, port = _start_local_server(workbench.app)
    view = QWebEngineView()
    view.show()
    try:
        for scope in ("review", "action", "settings"):
            for width in (1120, 1500):
                workbench._feedback = None
                if scope == "review":
                    workbench._message = (
                        "Price update blocked: the snapshot is stale. "
                        "Refresh before reviewing the change again."
                    )
                elif scope == "action":
                    workbench._show_order_feedback_locked(
                        "Simulated price update acknowledged",
                        "Demo order state refreshed; no TWS order was sent.",
                    )
                else:
                    workbench._completed_action = "settings"
                selector = (
                    '[data-completion-feedback="settings"]'
                    if scope == "settings"
                    else '[data-order-completion="price-update"]'
                    if scope == "action"
                    else f'[data-action-feedback="{scope}"]'
                )
                view.resize(width, 920)
                loop = QEventLoop()
                observations = []

                def loaded(ok, selector=selector, observations=observations, loop=loop):
                    assert ok

                    def inspect(_):
                        view.page().runJavaScript(
                            f"""(() => {{
                              const e = document.querySelector('{selector}');
                              const r = e?.getBoundingClientRect();
                              const success = e?.hasAttribute('data-order-completion');
                              return !!r && r.width > 0 && r.x >= 0
                                && r.right <= innerWidth && r.bottom <= innerHeight
                                && document.documentElement.scrollWidth <= innerWidth
                                && (!success || (e.disabled
                                  && getComputedStyle(e).opacity === '1'
                                  && getComputedStyle(e).backgroundColor ===
                                    'rgb(110, 231, 183)'));
                            }})()""",
                            lambda value: (observations.append(value), loop.quit()),
                        )

                    wait_for_javascript(
                        view, f"!!document.querySelector('{selector}')", inspect
                    )

                view.loadFinished.connect(loaded)
                view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}?{scope}-{width}"))
                QTimer.singleShot(5000, loop.quit)
                loop.exec()
                view.loadFinished.disconnect(loaded)
                assert observations == [True]
                QTest.qWait(100)
                view.grab().save(str(tmp_path / f"feedback-{scope}-{width}.png"))
    finally:
        view.close()
        server.should_exit = True
        thread.join(timeout=2)


def test_new_edit_interrupts_success_without_timer_disabling_the_next_review():
    view = QWebEngineView()
    view.show()
    loop = QEventLoop()
    observations = []
    script = _live_active_script({
        "basis": "10", "multiplier": "100",
        "bands": [{"low": "0", "increment": "0.1"}],
    })

    def loaded(ok):
        assert ok
        view.page().runJavaScript("""(() => {
          const target = document.querySelector('[data-active-input="target"]');
          target.value = '60';
          target.dispatchEvent(new Event('input', {bubbles:true}));
        })()""")

        def inspect():
            view.page().runJavaScript("""(() => {
              const b = document.querySelector('[data-active-execute]');
              return JSON.stringify({label:b.textContent, disabled:b.disabled,
                success:b.hasAttribute('data-order-completion'),
                green:b.classList.contains('action-complete')});
            })()""", lambda v: (observations.append(json.loads(v)), loop.quit()))

        QTimer.singleShot(2900, inspect)

    view.loadFinished.connect(loaded)
    view.setHtml(
        '<form id="active-form">'
        '<input data-active-input="target" data-active-perm-id="1" '
        'data-live-layer="1" data-active-initial="50" '
        'data-active-original="15" value="50">'
        '<input data-active-input="stop" data-active-perm-id="1" '
        'data-active-initial="-25" data-active-original="7.5" value="-25">'
        '<input data-active-quantity="1" value="2">'
        '</form><button data-active-execute disabled class="action-complete" '
        'data-order-completion="price-update" '
        'data-restore-label="Review price changes">Successfully edited</button>'
        f"<script>{script}</script><script>{_local_feedback_script()}</script>"
    )
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    view.close()
    assert observations == [{
        "label": "Review price changes", "disabled": False,
        "success": False, "green": False,
    }]


@pytest.mark.parametrize("with_limit", [False, True])
def test_working_trail_input_opens_active_review(
    tmp_path: Path, with_limit: bool
) -> None:
    workbench = _trailing_scenario_workbench(
        tmp_path, "working-trail" if with_limit else "convert"
    )
    if not with_limit:
        client = TestClient(workbench.app)
        client.post(
            workbench.path + "action",
            data={
                "action": "trailing-convert-arm",
                "trail_value": "25",
                "trail_unit": "dollars",
            },
        )
        client.post(workbench.path + "action", data={"action": "active-action-execute"})
        client.post(
            workbench.path + "action", data={"action": "trailing-convert-confirm"}
        )
    TestClient(workbench.app).post(
        workbench.path + "action", data={"action": "refresh"}
    )
    server, server_thread, port = _start_local_server(workbench.app)
    QApplication.instance() or QApplication([])
    loop = QEventLoop()
    view = QWebEngineView()
    view.resize(1500, 920)
    view.show()
    observations: list[str] = []

    def loaded(ok: bool) -> None:
        if not ok:
            loop.quit()
            return
        view.page().runJavaScript(
            """(() => {
              const stop = document.querySelector('[data-trailing-edit="STP"]');
              const gap = document.querySelector('[data-trailing-edit="LMT"]');
              gap.value = '15';
              gap.dispatchEvent(new Event('input', { bubbles: true }));
              const review = document.querySelector('[data-trailing-review-row]');
              const button = document.querySelector('[data-active-execute]');
              const firstReview = !review.classList.contains('hidden')
                && !button.disabled;
              const title = review.querySelector('p').textContent;
              const linkedRows = review.querySelectorAll(
                '.border-l-2 .justify-between').length;
              const stopReview = review.querySelector(
                '[data-trailing-review-stop]').textContent;
              const gapReview = review.querySelector(
                '[data-trailing-review-limit]').textContent;
              const gain = document.querySelector('[data-gain-value]').textContent;
              const tones = () => ['gain'].map((kind) => {
                const node = document.querySelector(`[data-live-metric="${kind}"]`);
                const percent = node.querySelector(`[data-${kind}-percent]`);
                const amount = node.querySelector(`[data-${kind}-value]`);
                return { tone: ['text-emerald-400', 'text-rose-400',
                    'text-muted-foreground'].filter(
                      (cls) => node.classList.contains(cls)),
                  sameColour: getComputedStyle(percent).color ===
                    getComputedStyle(amount).color };
              });
              const positiveTones = tones();
              gap.value = '';
              gap.dispatchEvent(new Event('input', { bubbles: true }));
              const blankGapReview = !review.classList.contains('hidden')
                && !button.disabled;
              gap.value = '20'; stop.value = '';
              stop.dispatchEvent(new Event('input', { bubbles: true }));
              const missingStop = review.querySelector(
                '[data-trailing-review-error]').textContent;
              const blankStopRejected = button.disabled;
              const invalidReviewVisible = !review.classList.contains('hidden');
              stop.value = '75'; gap.value = '900';
              gap.dispatchEvent(new Event('input', { bubbles: true }));
              const highLimit = review.querySelector(
                '[data-trailing-review-error]').textContent;
              const highLimitRejected = button.disabled;
              const fieldInvalid = gap.getAttribute('aria-invalid');
              const invalidGain = document.querySelector(
                '[data-gain-value]').textContent;
              const unknownTones = tones();
              gap.value = '20';
              gap.dispatchEvent(new Event('input', { bubbles: true }));
              const recoveredGain = document.querySelector(
                '[data-gain-value]').textContent;
              const recovered = !button.disabled &&
                gap.getAttribute('aria-invalid') === 'false';
              const negativeTones = tones();
              stop.value = '45'; gap.value = '';
              gap.dispatchEvent(new Event('input', { bubbles: true }));
              const zeroTones = tones();
              return JSON.stringify({
                stopMetricHidden: !document.querySelector('[data-live-metric="loss"]'),
                positiveTones, negativeTones, unknownTones, zeroTones,
                stopDisabled: stop?.matches(':disabled'),
                gapDisabled: gap?.matches(':disabled'),
                firstReview, blankGapReview, title, linkedRows,
                stopReview, gapReview, gain,
                blankStopRejected, missingStop, invalidReviewVisible,
                highLimit, highLimitRejected, fieldInvalid, invalidGain,
                recovered, recoveredGain
              });
            })()""",
            lambda result: (observations.append(result), loop.quit()),
        )

    try:
        view.loadFinished.connect(loaded)
        view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
        QTimer.singleShot(5_000, loop.quit)
        loop.exec()
    finally:
        view.close()
        server.should_exit = True
        server_thread.join(timeout=2)

    assert len(observations) == 1
    assert workbench._state.unit_basis is not None
    assert workbench._state.multiplier is not None
    gain = (Decimal("8.65") - workbench._state.unit_basis) * workbench._state.multiplier
    gain *= Decimal(workbench._state.working_orders[0].remaining)
    assert json.loads(observations[0]) == {
        "stopMetricHidden": True,
        "positiveTones": [{"tone": ["text-emerald-400"], "sameColour": True}],
        "negativeTones": [{"tone": ["text-rose-400"], "sameColour": True}],
        "unknownTones": [{"tone": ["text-muted-foreground"], "sameColour": True}],
        "zeroTones": [{"tone": ["text-muted-foreground"], "sameColour": True}],
        "stopDisabled": False,
        "gapDisabled": False,
        "firstReview": True,
        "blankGapReview": with_limit,
        "title": "TRAIL-1",
        "linkedRows": 3,
        "stopReview": "$8.80",
        "gapReview": "$8.65",
        "gain": _money(gain),
        "blankStopRejected": True,
        "missingStop": "A trail amount is required before entering a LMT offset.",
        "invalidReviewVisible": True,
        "highLimit": "LMT offset must leave a positive limit price below STP.",
        "highLimitRejected": True,
        "fieldInvalid": "true",
        "invalidGain": "— Unknown",
        "recovered": True,
        "recoveredGain": _money(
            (Decimal("8.10") - workbench._state.unit_basis)
            * workbench._state.multiplier
            * Decimal(workbench._state.working_orders[0].remaining)
        ),
    }


def test_embedded_tws_indicator_tracks_observer_disconnect() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._observe_positions = True
    workbench._observer_health = "connected"
    server, server_thread, port = _start_local_server(workbench.app)
    application = QApplication.instance() or QApplication([])
    view = QWebEngineView()
    view.resize(1200, 800)
    view.show()
    observed: list[str] = []

    def inspect() -> None:
        view.page().runJavaScript(
            "document.getElementById('tws-updates-status')?.dataset.headerStatus",
            record,
        )

    def record(label: object) -> None:
        if label == "TWS connected" and not observed:
            observed.append(str(label))
            with workbench._lock:
                workbench._observer_health = "disconnected"
        elif label == "TWS updates unavailable" and observed:
            observed.append(str(label))
            application.quit()
            return
        QTimer.singleShot(150, inspect)

    try:
        view.loadFinished.connect(lambda ok: inspect() if ok else application.quit())
        view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
        QTimer.singleShot(8_000, application.quit)
        application.exec()
    finally:
        view.close()
        server.should_exit = True
        server_thread.join(timeout=2)
        workbench.close()

    assert observed == ["TWS connected", "TWS updates unavailable"]


@pytest.mark.parametrize("launch_connection", ["failed", "success"])
def test_settings_open_in_blocked_desktop_webview(launch_connection: str) -> None:
    workbench = _demo_workbench()
    workbench._demo_mode = False
    workbench._state = replace(workbench._state, status=UiStatus.BLOCKED)
    workbench._launch_connection = launch_connection
    server, server_thread, port = _start_local_server(workbench.app)
    application = QApplication.instance() or QApplication([])
    view = QWebEngineView()
    view.resize(1500, 920)
    view.show()
    interceptor = _LoopbackOnlyRequestInterceptor(view)
    view.page().profile().setUrlRequestInterceptor(interceptor)
    opened: list[bool] = []
    backdrop: list[bool] = []

    def inspect() -> None:
        wait_for_javascript(
            view,
            """(() => {
              const dialog = document.getElementById('connection_settings');
              if (!dialog?.open || getComputedStyle(dialog).visibility !== 'visible') {
                return false;
              }
              const rect = dialog.getBoundingClientRect();
              const hit = document.elementFromPoint(
                rect.x + rect.width / 2, rect.y + rect.height / 2);
              return rect.height > 100 && dialog.contains(hit);
            })();""",
            lambda result: (opened.append(bool(result)), application.quit()),
        )

    def loaded(ok: bool) -> None:
        if not ok:
            application.quit()
            return

        def click(point: str) -> None:
            if not point:
                application.quit()
                return
            QTest.mouseClick(
                view.focusProxy() or view,
                Qt.MouseButton.LeftButton,
                pos=QPoint(*map(int, json.loads(point))),
            )
            inspect()

        def click_settings() -> None:
            view.page().runJavaScript(
                """(() => {
              const rect = document.querySelector('[aria-haspopup="dialog"]')
                .getBoundingClientRect();
              return JSON.stringify([
                rect.x + rect.width / 2, rect.y + rect.height / 2
              ]);
            })();""",
                click,
            )

        if launch_connection == "failed":
            view.page().runJavaScript(
                """(() => {
                  const dialog = document.getElementById('launch_connection');
                  const overlay = document.querySelector('[data-launch-backdrop]');
                  if (!dialog?.open || dialog.matches(':modal') || !overlay) {
                    return false;
                  }
                  const style = getComputedStyle(overlay);
                  return style.pointerEvents === 'none'
                    && style.backgroundColor !== 'transparent'
                    && style.backgroundColor !== 'rgba(0, 0, 0, 0)'
                    && overlay.getBoundingClientRect().width >= innerWidth;
                })();""",
                lambda result: (backdrop.append(bool(result)), click_settings()),
            )
        else:
            click_settings()

    try:
        view.loadFinished.connect(loaded)
        view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
        QTimer.singleShot(5_000, application.quit)
        application.exec()
    finally:
        view.close()
        server.should_exit = True
        server_thread.join(timeout=2)

    assert opened == [True]
    if launch_connection == "failed":
        assert backdrop == [True]


def test_reset_active_prices_restores_target_and_stop_after_move_to_be() -> None:
    QApplication.instance() or QApplication([])
    view = QWebEngineView()
    loop = QEventLoop()
    results: list[object] = []
    script = _live_active_script(
        {
            "basis": "10",
            "multiplier": "100",
            "bands": [{"low": "0", "increment": "0.1"}],
        }
    )
    html = (
        '<form id="active-form">'
        '<input data-active-input="target" data-active-perm-id="1" '
        'data-live-layer="1" data-active-initial="50" '
        'data-active-original="15" value="50">'
        '<input type="number" data-active-input="stop" data-active-perm-id="1" '
        'data-live-layer="1" data-active-initial="25" '
        'data-active-original="7.5" data-active-stop-limit-price="7.0" '
        'data-active-stop-limit-offset="5" '
        'data-active-stop-limit-unit="percent" value="25">'
        '<button type="button" data-move-stops-to-be>Move stop to B/E</button>'
        '</form><div data-active-review-row="1">'
        '<div data-active-review-stop-row="1">'
        '<span data-active-review-stop="1"></span></div>'
        '<div data-active-review-stop-limit-row="1">'
        '<span data-active-review-stop-limit="1"></span></div>'
        '</div><div data-price-edit-reset data-reset-visible="false" '
        'aria-hidden="true">'
        '<button type="button" data-reset-active-prices disabled>'
        "Cancel changes</button>"
        "</div>"
        f"<script>{script}</script>"
    )

    def inspect(loaded: bool) -> None:
        if not loaded:
            loop.quit()
            return
        view.page().runJavaScript(
            """(() => {
              const target = document.querySelector('[data-active-input="target"]');
              const stop = document.querySelector('[data-active-input="stop"]');
              let stopEvents = 0;
              stop.addEventListener('input', () => { stopEvents += 1; });
              const reset = document.querySelector('[data-reset-active-prices]');
              const slot = document.querySelector('[data-price-edit-reset]');
              const stopReview = document.querySelector(
                '[data-active-review-stop="1"]');
              const limitReview = document.querySelector(
                '[data-active-review-stop-limit="1"]');
              target.value = '60';
              target.dispatchEvent(new Event('input', { bubbles: true }));
              document.querySelector('[data-move-stops-to-be]').click();
              const afterMove = [target.value, stop.value, stopEvents, reset.disabled,
                                 slot.dataset.resetVisible, stopReview.textContent,
                                 limitReview.textContent];
              reset.click();
              return JSON.stringify({ afterMove, afterReset: [target.value,
                stop.value, stop.dataset.activeInitial, reset.disabled,
                slot.dataset.resetVisible] });
            })()""",
            lambda value: (results.append(value), loop.quit()),
        )

    view.loadFinished.connect(inspect)
    view.setHtml(html)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    view.close()

    assert results == [
        '{"afterMove":["60","25",0,false,"true","",""],"afterReset":["50","25","25",true,"false"]}'
    ]


@pytest.mark.parametrize("mode", ["price", "return"])
def test_bulk_active_stop_submits_the_displayed_decimal_price(mode) -> None:
    QApplication.instance() or QApplication([])
    view = QWebEngineView()
    loop = QEventLoop()
    results = []
    script = _live_active_script(
        {
            "basis": "2.36185",
            "multiplier": "100",
            "bands": [{"low": "0", "increment": "0.01"}],
        }
    )
    html = """<form id="active-form">
      <input data-active-input="target" data-active-perm-id="1"
        data-live-layer="1" data-active-initial="20"
        data-active-original="2.84" value="20">
      <input data-active-input="stop" data-active-perm-id="1"
        data-live-layer="1" data-active-initial="-34.80"
        data-active-original="1.54" value="-34.80">
      <input data-active-exact-stop="1" name="active_stop_price_1">
      <input data-active-quantity="1" value="2">
      <input data-raise-stops-only>
      </form>
      <div data-stop-dialog>
        <div data-stop-mode-group><button data-value="return">%</button></div>
        <input data-stop-dialog-value value="1.66">
        <span data-stop-input-label></span><span data-stop-dialog-inverse></span>
        <span data-stop-dialog-summary></span><span data-stop-dialog-error></span>
        <button data-apply-all-stops>Apply</button>
      </div>
      <span data-active-review-stop="1"></span>"""

    def inspect(loaded):
        if not loaded:
            loop.quit()
            return
        view.page().runJavaScript(
            (
                """document.querySelector('[data-stop-mode-group] button').click();
             document.querySelector('[data-stop-dialog-value]').value = '-30';
             const input = document.querySelector('[data-stop-dialog-value]');
             input.dispatchEvent(new Event('input'));
             """
                if mode == "return"
                else ""
            )
            + """(() => {
          document.querySelector('[data-apply-all-stops]').click();
          return JSON.stringify({
            price: document.querySelector('[data-active-exact-stop="1"]').value,
            percentage: document.querySelector('[data-active-input="stop"]').value,
            review: document.querySelector('[data-active-review-stop="1"]').textContent
          });
        })()""",
            lambda value: (results.append(value), loop.quit()),
        )

    view.loadFinished.connect(inspect)
    view.setHtml(html + f"<script>{script}</script>")
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    view.close()
    assert len(results) == 1
    submitted = json.loads(results[0])
    assert submitted == {"price": "1.66", "percentage": "-29.72", "review": "$1.66"}


def test_staged_cancel_uses_entry_animation_on_page_render() -> None:
    QApplication.instance() or QApplication([])
    workbench = _demo_workbench()
    css = (
        Path(__file__).resolve().parents[1]
        / "src/ibkr_options_manager/app/web/static/layers.css"
    ).read_text()
    view = QWebEngineView()
    loop = QEventLoop()
    results: list[str] = []

    def inspect(loaded: bool) -> None:
        if not loaded:
            loop.quit()
            return
        view.page().runJavaScript(
            "getComputedStyle(document.querySelector('[data-staged-cancel]')).animationName",
            lambda value: (results.append(value), loop.quit()),
        )

    view.loadFinished.connect(inspect)
    view.setHtml(
        f"<style>{css}</style>{workbench._cancel_changes_control(staged=True)}"
    )
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    view.close()

    assert results == ["staged-cancel-in"]


def test_embedded_webview_regresses_settings_refresh_add_and_execute_controls(
    tmp_path,
) -> None:
    """Exercise the controls that depend on a real browser submit/navigation."""

    def clock() -> Decimal:
        return Decimal("100")

    broker = DemoReadOnlyBroker(clock=clock, paper_execution_enabled=True)
    snapshots = SnapshotCoordinator(broker, max_age_seconds=Decimal("15"), clock=clock)
    portfolio = PortfolioCoordinator(
        broker,
        max_age_seconds=Decimal("15"),
        clock=clock,
        paper_execution_mode=True,
    )
    from ibkr_options_manager.app.view_model import PlannerViewModel

    workbench = StarUIWorkbench(
        PlannerViewModel(snapshots, portfolio=portfolio, clock=clock),
        initial_account=DEMO_ACCOUNT,
        demo_mode=True,
        paper_execution=PaperExecutionService(
            DemoPaperExecutionTransport(),
            ExecutionJournal(tmp_path / "webview-paper-journal.json"),
        ),
    )
    workbench.load_demo_data()
    workbench._select_locked(1_002_100_161)  # NVDA has no related demo order.
    workbench._add_layer_locked()
    server, server_thread, port = _start_local_server(workbench.app)
    QApplication.instance() or QApplication([])
    loop = QEventLoop()
    view = QWebEngineView()
    view.resize(1500, 920)
    view.show()
    phase = 0
    failure: list[str] = []
    result: dict[str, bool] = {}
    finished = False

    def finish(reason: str | None = None) -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        if reason is not None:
            failure.append(reason)
        server.should_exit = True
        server_thread.join(timeout=2)
        loop.quit()

    def javascript(script: str, callback) -> None:
        view.page().runJavaScript(script, callback)

    def click_button(label: str) -> None:
        javascript(
            """
            (() => {
              const button = Array.from(document.querySelectorAll('button'))
                .find((candidate) => candidate.textContent.trim() === $LABEL
                  || ($LABEL === 'Confirm'
                    && candidate.textContent.trim().startsWith('Confirm (')));
              if (!button || button.disabled) return false;
              button.click();
              return true;
            })();
            """.replace("$LABEL", repr(label)),
            lambda clicked: None if clicked else finish(f"{label!r} was not clickable"),
        )

    def inspect_settings(opened: object) -> None:
        result["settings"] = bool(opened)
        if not opened:
            finish("Settings did not open its Dialog")
            return
        click_button("Cancel")
        click_button("Add Layer")

    def inspect_acknowledgement(visible: object) -> None:
        result["acknowledgement_visible"] = bool(visible)
        click_button("Refresh order status")

    def inspect_page(text: object) -> None:
        nonlocal phase
        body = str(text)
        if phase == 1:
            if "DRAFT 2" not in body:
                finish("Add Layer did not render a second layer")
                return
            result["add_layer"] = True
            phase = 2
            click_button("Review order")
        elif phase == 2:
            if "Confirm" not in body:
                finish("Review order did not arm its confirmation")
                return
            phase = 3
            click_button("Confirm")
        elif phase == 3:
            if "The orders were sent to TWS" not in body:
                finish("Paper execution did not render its acknowledgement")
                return
            result["execute_arm"] = True
            phase = 4
            result["execute_confirm"] = True
            javascript(
                """(() => {
                  const dialog = document.querySelector(
                    '[data-submission-review] dialog');
                  return !!dialog && dialog.open
                    && dialog.getBoundingClientRect().height > 0;
                })();""",
                inspect_acknowledgement,
            )
        elif phase == 4:
            if "Awaiting TWS verification" not in body:
                finish("Refresh did not render the workbench")
                return
            result["refresh"] = True
            finish()

    def loaded(ok: bool) -> None:
        nonlocal phase
        if not ok:
            finish("The embedded workbench failed to load")
            return
        if phase == 0:
            phase = 1
            javascript(
                """
                (() => {
                  const trigger = document.querySelector('[aria-haspopup="dialog"]');
                  if (!trigger) return false;
                  const rect = trigger.getBoundingClientRect();
                  return JSON.stringify([
                    rect.x + rect.width / 2, rect.y + rect.height / 2
                  ]);
                })();
                """,
                lambda point: (
                    (
                        QTest.mouseClick(
                            view.focusProxy() or view,
                            Qt.MouseButton.LeftButton,
                            pos=QPoint(*map(int, json.loads(point))),
                        ),
                        wait_for_javascript(
                            view,
                            """(() => {
                              const dialog = document.getElementById(
                                'connection_settings');
                              if (!dialog?.open ||
                                dialog.getBoundingClientRect().height <= 100 ||
                                getComputedStyle(dialog).visibility !== 'visible') {
                                return false;
                              }
                              const group = dialog.querySelector(
                                '[data-slot="toggle-group"]');
                              const on = group?.querySelector('[data-state="on"]');
                              const off = group?.querySelector('[data-state="off"]');
                              if (!on || !off) return false;
                              return getComputedStyle(on).backgroundColor !==
                                  getComputedStyle(off).backgroundColor;
                            })();""",
                            inspect_settings,
                        ),
                    )
                    if point
                    else finish(f"Settings was not clickable: {point!r}")
                ),
            )
            return
        javascript("document.body.innerText", inspect_page)

    view.loadFinished.connect(loaded)
    view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
    QTimer.singleShot(10_000, lambda: finish("Timed out waiting for browser controls"))
    loop.exec()

    assert failure == []
    assert result == {
        "settings": True,
        "add_layer": True,
        "execute_arm": True,
        "execute_confirm": True,
        "acknowledgement_visible": True,
        "refresh": True,
    }


def test_main_builds_the_embedded_starui_window(monkeypatch: object) -> None:
    QApplication.instance() or QApplication([])
    created: list[_WindowStub] = []

    def window_factory(*args: object, **kwargs: object) -> _WindowStub:
        window = _WindowStub(*args, **kwargs)
        created.append(window)
        return window

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "ibkr_options_manager.app.main.StarUIPlannerWindow", window_factory
    )

    assert main(["--demo-data"]) == 0
    assert len(created) == 1
    assert created[0].demo_loaded is True
    assert created[0].launch_refresh_requested is True


def test_named_demo_launch_uses_only_a_disposable_simulation_journal(
    monkeypatch: object,
) -> None:
    QApplication.instance() or QApplication([])
    created: list[_WindowStub] = []

    def window_factory(*args: object, **kwargs: object) -> _WindowStub:
        window = _WindowStub(*args, **kwargs)
        created.append(window)
        return window

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "ibkr_options_manager.app.main.StarUIPlannerWindow", window_factory
    )
    assert main(["--demo-data", "--demo-scenario", "working-trail"]) == 0
    assert created[0].initial_con_id == 1_004_470_201
    assert created[0].paper_execution is not None
    assert created[0].demo_journal_dir is not None
    created[0].demo_journal_dir.cleanup()
    with pytest.raises(SystemExit):
        main(["--demo-scenario", "working-trail"])


def test_main_prefills_saved_account_without_cli_argument(monkeypatch: object) -> None:
    QApplication.instance() or QApplication([])
    created: list[_WindowStub] = []

    def window_factory(*args: object, **kwargs: object) -> _WindowStub:
        window = _WindowStub(*args, **kwargs)
        created.append(window)
        return window

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "ibkr_options_manager.app.main.StarUIPlannerWindow", window_factory
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "ibkr_options_manager.app.main.load_saved_account", lambda: "DU7654321"
    )

    assert main([]) == 0
    assert created[0].initial_account == "DU7654321"

    assert main(["--account", "DU1111111"]) == 0
    assert created[1].initial_account == "DU1111111"


def test_trailing_dialog_open_closes_its_tooltip(tmp_path: Path) -> None:
    workbench = _trailing_scenario_workbench(tmp_path, "convert")
    assert 'aria-label="Set a trailing exit for this position"' in (
        TestClient(workbench.app).get(workbench.path).text
    )
    server, server_thread, port = _start_local_server(workbench.app)
    QApplication.instance() or QApplication([])
    loop = QEventLoop()
    view = QWebEngineView()
    view.resize(1500, 920)
    view.show()
    observations: list[str] = []

    def inspect() -> None:
        wait_for_javascript(
            view,
            """(() => {
              const button = document.querySelector(
                'button[aria-label="Set a trailing exit for this position"]');
              const tooltip = button?.closest('[data-slot="tooltip"]')
                ?.querySelector('[data-slot="tooltip-content"]');
              const dialog = button?.closest('[data-slot="tooltip"]')
                ?.querySelector('dialog');
              if (!dialog?.open || tooltip?.dataset.state !== 'closed'
                || !dialog.contains(document.activeElement)) return false;
              return JSON.stringify({
                open: !!dialog?.open,
                tooltip: tooltip?.dataset.state ?? null,
                focusInsideDialog: !!dialog?.contains(document.activeElement)
              });
            })()""",
            lambda result: (observations.append(result), loop.quit()),
        )

    def loaded(ok: bool) -> None:
        if not ok:
            loop.quit()
            return
        view.page().runJavaScript(
            """document.querySelector(
              'button[aria-label="Set a trailing exit for this position"]')
              ?.click()""",
            lambda _result: inspect(),
        )

    try:
        view.loadFinished.connect(loaded)
        view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
        QTimer.singleShot(5_000, loop.quit)
        loop.exec()
    finally:
        view.close()
        server.should_exit = True
        server_thread.join(timeout=2)

    assert observations == ['{"open":true,"tooltip":"closed","focusInsideDialog":true}']


def test_trail_review_click_preserves_prices_and_shows_confirm_in_browser(
    tmp_path: Path,
) -> None:
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    server, thread, port = _start_local_server(workbench.app)
    QApplication.instance() or QApplication([])
    view = QWebEngineView()
    view.resize(1500, 920)
    view.show()
    loop = QEventLoop()
    observations: list[str] = []
    submitted = False

    def loaded(ok: bool) -> None:
        nonlocal submitted
        if not ok:
            loop.quit()
            return
        if not submitted:
            submitted = True
            view.page().runJavaScript("""(() => {
                const stop = document.querySelector('[data-trailing-edit="STP"]');
                const limit = document.querySelector('[data-trailing-edit="LMT"]');
                stop.value = '75'; limit.value = '20';
                limit.dispatchEvent(new Event('input', {bubbles: true}));
                document.querySelector('[data-active-execute]').click();
            })()""")
            return
        view.page().runJavaScript(
            """(() => {
            const stop = document.querySelector('[data-trailing-edit="STP"]');
            const limit = document.querySelector('[data-trailing-edit="LMT"]');
            return JSON.stringify({stop: stop.value, limit: limit.value,
                readonly: stop.readOnly && limit.readOnly,
                confirm: Boolean(document.querySelector(
                  '[value="trailing-change-confirm"]')),
                extraReview: Boolean(document.querySelector(
                  '[value="active-action-execute"]'))});
        })()""",
            lambda result: (observations.append(result), loop.quit()),
        )

    try:
        view.loadFinished.connect(loaded)
        view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
    finally:
        view.close()
        server.should_exit = True
        thread.join(timeout=2)
    assert len(observations) == 1
    assert json.loads(observations[0]) == {
        "stop": "75",
        "limit": "20",
        "readonly": True,
        "confirm": True,
        "extraReview": False,
    }
    assert workbench._trailing_entries()[0].trailing_limit_offset == "0.10"


@pytest.mark.parametrize("bid_state", ["fresh", "missing", "stale", "below-stop"])
@pytest.mark.parametrize(
    ("preset", "stop_price", "distance"),
    [("-20", "6.88", "217"), ("-25", "6.45", "260")],
)
def test_set_all_stops_trail_dialog_preserves_fields_when_conversion_unavailable(
    tmp_path: Path, bid_state: str, preset: str, stop_price: str, distance: str
) -> None:
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    if bid_state != "fresh":
        quote = replace(
            snapshot.quote,
            bid=None
            if bid_state == "missing"
            else Decimal("6")
            if bid_state == "below-stop"
            else snapshot.quote.bid,
            fresh=bid_state != "stale",
        )
        workbench._view_model._latest_snapshot = replace(snapshot, quote=quote)
    server, thread, port = _start_local_server(workbench.app)
    QApplication.instance() or QApplication([])
    view = QWebEngineView()
    view.resize(1500, 920)
    loop = QEventLoop()
    observations: list[str] = []

    def loaded(ok: bool) -> None:
        if not ok:
            loop.quit()
            return
        view.page().runJavaScript(
            """(() => {
            document.querySelector('[aria-label="Set all stops…"]').click();
            const dialog = document.querySelector('[data-stop-dialog]');
            const stop = document.querySelector('[data-trailing-edit="STP"]');
            const limit = document.querySelector('[data-trailing-edit="LMT"]');
            const before = [stop.value, limit.value];
            dialog.querySelector('[data-stop-preset="PRESET"]').click();
            const apply = dialog.querySelector('[data-apply-all-stops]');
            const disabled = apply.disabled;
            apply.click();
            return JSON.stringify({before, after: [stop.value, limit.value], disabled,
                summary: dialog.querySelector('[data-stop-dialog-summary]').textContent,
                error: dialog.querySelector('[data-stop-dialog-error]')
                    ?.textContent || '',
                exact: document.querySelector('[data-trailing-exact-stop]').value});
        })()""".replace("PRESET", preset),
            lambda result: (observations.append(result), loop.quit()),
        )

    try:
        view.loadFinished.connect(loaded)
        view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
    finally:
        view.close()
        server.should_exit = True
        thread.join(timeout=2)
    assert len(observations) == 1
    result = json.loads(observations[0])
    assert result["summary"] == f"${stop_price} ({preset}%)"
    if bid_state == "fresh":
        assert not result["disabled"]
        assert result["after"] == [distance, result["before"][1]]
        assert Decimal(result["exact"]) == Decimal(stop_price)
        perm_id = workbench._trailing_entries()[0].perm_ids[0]
        TestClient(workbench.app).post(
            workbench.path + "action",
            data={
                "action": "active-update-arm",
                f"trailing_value_{perm_id}": distance,
                f"trailing_value_unit_{perm_id}": "dollars",
                f"trailing_offset_{perm_id}": result["after"][1],
                f"trailing_offset_unit_{perm_id}": "dollars",
                f"trailing_exact_stop_{perm_id}": stop_price,
            },
        )
        candidate = workbench._armed_trailing_change
        assert candidate is not None, workbench._message
        assert candidate.stop_price == Decimal(stop_price)
        assert candidate.trail_value == Decimal(distance) / 100
        assert candidate.limit_offset == Decimal("0.10")
    else:
        assert result["disabled"], result
        assert result["after"] == result["before"]
        assert "bid" in result["error"].lower()
