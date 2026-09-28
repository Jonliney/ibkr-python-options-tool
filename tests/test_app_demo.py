import json
import os
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")

from httpx import Response
from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication
from starlette.testclient import TestClient

from ibkr_options_manager.app.demo import (
    DEMO_ACCOUNT,
    DEMO_CON_IDS,
    DemoPaperExecutionTransport,
    DemoReadOnlyBroker,
)
from ibkr_options_manager.app.main import build_parser, main
from ibkr_options_manager.app.view_model import PlanForm, UiStatus, ValidationLine
from ibkr_options_manager.app.web import StarUIWorkbench
from ibkr_options_manager.app.web.surface import (
    _active_percentage_for_price,
    _busy_submit_script,
    _contract_display_name,
    _live_active_script,
    _money,
    _next_target_preset_above,
    _position_identity,
    _price_update_impact,
    _projection_gain_value,
    _projection_loss_value,
    _toast_notice,
)
from ibkr_options_manager.app.web_window import (
    StarUIPlannerWindow,
    _LoopbackOnlyRequestInterceptor,
    _start_local_server,
)
from ibkr_options_manager.broker import PortfolioRequest, SnapshotRequest
from ibkr_options_manager.broker.execution import PaperSubmission
from ibkr_options_manager.domain import PriceBand, WorkingOrder
from ibkr_options_manager.execution import (
    ExecutionJournal,
    ExecutionOutcomeUnknown,
    JournalEntry,
    JournalLayer,
    JournalFill,
    JournalLayer,
    LayerOutcome,
    MarketExitCandidate,
    PaperExecutionService,
    PriceUpdateCandidate,
)
from ibkr_options_manager.portfolio import PortfolioCoordinator, PortfolioStatus
from ibkr_options_manager.snapshot import SnapshotCoordinator, SnapshotStatus


def test_demo_flag_is_explicit_and_does_not_require_account_input() -> None:
    args = build_parser().parse_args(["--demo-data"])

    assert args.demo_data is True
    assert args.account == ""
    assert args.con_id is None


def test_demo_broker_exercises_inventory_and_reserved_quantity_without_tws() -> None:
    def clock() -> Decimal:
        return Decimal("100")

    broker = DemoReadOnlyBroker(clock=clock)
    portfolio = PortfolioCoordinator(broker, max_age_seconds=Decimal("15"), clock=clock)
    inventory = portfolio.refresh(
        PortfolioRequest(
            host="127.0.0.1",
            port=7497,
            client_id=17,
            expected_account=DEMO_ACCOUNT,
        )
    )

    assert inventory.status is PortfolioStatus.READY
    assert inventory.snapshot is not None
    assert (
        set(position.key.con_id for position in inventory.snapshot.positions)
        == DEMO_CON_IDS
    )
    assert inventory.snapshot.positions[0].quantity == Decimal("10")
    assert inventory.snapshot.positions[0].working_orders[0].remaining == Decimal("5")

    snapshots = SnapshotCoordinator(broker, max_age_seconds=Decimal("15"), clock=clock)
    selected = inventory.snapshot.positions[0].key.con_id
    snapshot = snapshots.refresh(
        SnapshotRequest(
            host="127.0.0.1",
            port=7497,
            client_id=17,
            expected_account=DEMO_ACCOUNT,
            option_con_id=selected,
        )
    )

    assert snapshot.status is SnapshotStatus.READY
    assert snapshot.snapshot is not None
    assert snapshot.snapshot.quote.market_data_type == "FROZEN"
    assert snapshot.snapshot.position.unit_basis == Decimal("2.74")


def test_demo_launch_populates_the_starui_workbench_without_a_tws_refresh() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()

    assert len(workbench._state.positions) == 4
    assert workbench._state.selected_con_id is not None
    assert workbench._state.quote_calculator is not None
    assert workbench._state.available_quantity == 5


def test_verified_empty_portfolio_shows_refresh_guidance_without_order_review() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._state = replace(workbench._state, positions=())
    workbench._selected_con_id = None

    page = TestClient(workbench.app).get(workbench.path).text

    assert "No option positions detected" in page
    assert "Buy a long option contract in TWS" in page
    assert "Refresh positions" in page
    assert 'data-empty-positions' in page
    assert "ACTION REVIEW" not in page
    assert "LONG POSITIONS" not in page
    assert "Execute paper order" not in page


def test_unverified_empty_portfolio_does_not_claim_no_positions() -> None:
    workbench = _demo_workbench()
    workbench._state = replace(workbench._state, status=UiStatus.BLOCKED)

    page = TestClient(workbench.app).get(workbench.path).text

    assert "No option positions detected" not in page


def test_selected_contract_header_uses_verified_position_and_quote_values() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()

    page = TestClient(workbench.app).get(workbench.path).text

    assert "MSTR Sep25'26 150 Call" in page
    assert 'data-header-status="TWS not connected"' in page
    assert 'data-header-status="Test data"' in page
    assert 'd="M2 2 22 22"' in page
    assert 'data-header-status="New layer ready"' not in page
    assert 'data-header-status="Market data: Frozen"' not in page
    assert "Held / total" in page
    assert ">10 / 10<" in page
    assert "Available" in page
    assert "Existing TWS exit orders" in page
    assert "5 contracts already have exit orders in TWS." in page
    assert "5 remain available for new brackets." in page
    assert "Orders placed outside this app are view-only here." in page
    assert page.index("Realised P&amp;L") < page.index("Existing TWS exit orders")
    assert page.index("Existing TWS exit orders") < page.index("DRAFT 1")
    assert "Expected gain" in page
    assert "Max loss" in page
    assert "+$280.00" in page
    assert "-$340.00" in page
    assert "Projection covers 5 held contracts" not in page
    assert "Average price" in page
    assert ">$2.74<" in page
    assert "Last bid" in page
    assert ">$3.12<" in page
    assert "Last ask" in page
    assert ">$3.18<" in page
    assert "Realised P&amp;L" in page
    assert "Realised P&amp;L · app exits" not in page
    assert 'class="mt-1 block font-mono' not in page
    assert "Build and manage app-owned OCA layers." not in page
    assert "contracts verified available to bracket" not in page
    assert "Cost basis / Ask" not in page

    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    assert (
        _contract_display_name(
            replace(
                snapshot.contract,
                trading_class="SPXW",
                expiry="20260918",
                strike=Decimal("7750"),
            )
        )
        == "SPXW Sep18'26 7750 Call"
    )


def test_header_distinguishes_a_blocked_layer_from_connection_status() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._state = replace(
        workbench._state,
        status=UiStatus.BLOCKED,
        validations=(ValidationLine("INPUT_INVALID", "Target price is invalid"),),
    )

    page = TestClient(workbench.app).get(workbench.path).text

    assert 'data-header-status="TWS not connected"' in page
    assert 'data-header-status="New layer unavailable"' in page
    assert 'title="Target price is invalid"' in page
    assert ">BLOCKED<" not in page


def test_header_omits_full_allocation_and_identifies_paper_account() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._demo_mode = False
    workbench._paper_execution = _OwnedOrderService(set())
    workbench._state = replace(
        workbench._state,
        status=UiStatus.BLOCKED,
        available_quantity=0,
        validations=(
            ValidationLine("POSITION_FULLY_ALLOCATED", "Position fully allocated"),
        ),
    )

    page = TestClient(workbench.app).get(workbench.path).text

    assert 'data-header-status="TWS connected"' in page
    assert 'data-header-status="Paper TWS account"' in page
    assert 'data-header-status="No contracts available"' not in page
    assert 'data-header-status="New layer unavailable"' not in page
    assert 'd="M9 17H7A5' in page  # Bundled Lucide link icon.
    assert 'text-cyan-400' in page
    assert 'd="M12 22s8-4 8-10V5' in page  # Bundled Lucide shield icon.


def test_header_warns_when_a_live_account_is_configured() -> None:
    workbench = _demo_workbench()
    workbench._demo_mode = False
    workbench._settings = replace(workbench._settings, account="U1234567")

    page = TestClient(workbench.app).get(workbench.path).text

    assert 'data-header-status="Live TWS account"' in page
    assert 'text-red-400' in page
    assert 'd="M12 8v4"' in page  # Bundled Lucide shield alert icon.


def test_launch_refresh_never_blocks_the_initial_workbench_page() -> None:
    workbench = _demo_workbench()
    workbench._demo_mode = False
    refresh_started = Event()
    release_refresh = Event()
    responses: list[Response] = []

    def blocked_refresh(_settings: object) -> object:
        refresh_started.set()
        assert release_refresh.wait(timeout=1)
        return workbench._state

    workbench._view_model.refresh_portfolio = blocked_refresh  # type: ignore[method-assign]
    launch = Thread(target=workbench.refresh_on_launch, daemon=True)
    launch.start()
    assert refresh_started.wait(timeout=1)

    def load_page() -> None:
        responses.append(TestClient(workbench.app).get(workbench.path))

    page = Thread(target=load_page, daemon=True)
    page.start()
    try:
        page.join(timeout=0.1)
        assert not page.is_alive()
    finally:
        release_refresh.set()
        page.join(timeout=1)
        launch.join(timeout=1)

    assert len(responses) == 1
    assert responses[0].status_code == 200
    assert "Connecting to TWS" in responses[0].text


def test_launch_connection_failure_stays_in_the_retry_dialog_without_a_toast() -> None:
    workbench = _demo_workbench()
    workbench._demo_mode = False
    blocked = replace(
        workbench._state,
        status=UiStatus.BLOCKED,
        status_message="TWS did not respond",
    )
    workbench._view_model.refresh_portfolio = lambda _settings: blocked  # type: ignore[method-assign]

    workbench.refresh_on_launch()
    page = TestClient(workbench.app).get(workbench.path)

    assert "TWS unavailable" in page.text
    assert "Retry connection" in page.text
    assert "<dialog" in page.text
    assert "data-dialog" in page.text
    # The official Toaster remains mounted for later action feedback, but the
    # launch failure is intentionally shown only in the blocking Dialog.
    assert "TWS did not respond" not in page.text


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
        view.page().runJavaScript(
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
            QTimer.singleShot(250, inspect)

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


def test_retry_connection_waits_for_one_terminal_refresh_response() -> None:
    """Retry must not start a second worker that races the dialog poll."""
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._demo_mode = False
    ready = workbench._state
    calls: list[object] = []

    def refreshed(settings: object) -> object:
        calls.append(settings)
        return ready

    workbench._view_model.refresh_portfolio = refreshed  # type: ignore[method-assign]
    workbench._launch_connection = "failed"
    client = TestClient(workbench.app)

    page = client.post(
        f"/{workbench.session_token}/action",
        data={"action": "launch-refresh"},
    )

    assert page.status_code == 200
    assert len(calls) == 1
    assert workbench._launch_connection == "success"
    assert workbench._launch_refresh_in_progress is False
    assert "TWS unavailable" not in page.text


def test_status_updates_render_as_short_toasts_not_workspace_copy() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._message = "Execution blocked: TWS must be open"

    page = TestClient(workbench.app).get(workbench.path)

    assert workbench._toast is not None
    assert workbench._toast.title == "Couldn't send bracket orders"
    assert "Average price" in page.text
    assert "Dismiss toast" in page.text
    # Action rerenders must replace an already-hydrated empty toast signal.
    assert "data-signals='{toasts:" in page.text
    assert "data-signals:toasts__ifmissing" in page.text
    assert "document.startViewTransition" not in page.text


def test_selecting_fully_allocated_position_does_not_raise_error_toast() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = workbench._selected_con_id
    assert con_id is not None
    workbench._message = "Execution blocked: an earlier action failed"
    blocked = replace(
        workbench._state,
        status=UiStatus.BLOCKED,
        status_message="Plan blocked by validation",
        validations=(
            ValidationLine(
                "POSITION_FULLY_ALLOCATED",
                "existing closing exposure already covers the whole position",
            ),
        ),
        available_quantity=0,
    )
    workbench._view_model.select_position = lambda *_: blocked  # type: ignore[method-assign]

    workbench._select_locked(con_id)

    assert workbench._state.status is UiStatus.BLOCKED
    assert workbench._state.available_quantity == 0
    assert workbench._toast is None
    assert workbench._status_message.endswith(
        "existing closing exposure already covers the whole position"
    )
    page = TestClient(workbench.app).get(workbench.path).text
    assert 'data-signals:toasts__ifmissing' in page
    assert 'data-signals="{toasts: [null, null, null]}"' in page


def test_overallocated_position_still_raises_error_toast() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = workbench._selected_con_id
    assert con_id is not None
    blocked = replace(
        workbench._state,
        status=UiStatus.BLOCKED,
        status_message="Plan blocked by validation",
        validations=(
            ValidationLine(
                "ALLOCATION_EXCEEDS_POSITION",
                "existing closing exposure exceeds the current position",
            ),
        ),
    )
    workbench._view_model.select_position = lambda *_: blocked  # type: ignore[method-assign]

    workbench._select_locked(con_id)

    assert workbench._toast is not None
    assert workbench._toast.variant == "error"


def test_tws_connection_toast_has_a_short_recovery_message() -> None:
    notice = _toast_notice(
        "Portfolio state is not ready: missing completion barriers: positions"
    )

    assert notice.title == "Couldn't connect to TWS"
    assert notice.description == "Check that TWS is open, then try again."
    assert notice.variant == "error"


@pytest.mark.parametrize(
    "message",
    (
        "Verified broker state is ready for read-only planning.",
        "Recovered 2 app-owned orders from TWS. Their journal status is reconciled.",
        "Fresh paper snapshot verified. Review the order plan, then confirm.",
        "Staged action cancelled. No orders were sent to TWS.",
        "Paper submission acknowledged for 2 orders. TWS state refreshed.",
    ),
)
def test_routine_statuses_do_not_create_toasts(message: str) -> None:
    assert _toast_notice(message) is None


def test_problem_toast_has_actionable_title_and_body() -> None:
    notice = _toast_notice(
        "Paper submission acknowledged. Automatic TWS refresh failed; use Refresh before another action."
    )

    assert notice is not None
    assert notice.title == "Couldn't verify the latest state"
    assert notice.description == "TWS received the action. Refresh before making another change."
    assert notice.variant == "error"


@pytest.mark.parametrize(
    ("message", "title", "body"),
    (
        (
            "Submission outcome is unknown: TWS timed out",
            "Order status is uncertain",
            "Check TWS and refresh. Do not resend this draft.",
        ),
        (
            "Market exit outcome is unknown: TWS timed out",
            "Order status is uncertain",
            "Check TWS and refresh. Do not retry until the outcome is clear.",
        ),
        (
            "Bracket cancellation blocked: orders changed",
            "Couldn't cancel bracket orders",
            "orders changed",
        ),
        (
            "Price update blocked: the snapshot is stale",
            "Couldn't change prices",
            "the snapshot is stale",
        ),
        (
            "The fresh quote changes the immediate-sell warning.",
            "Check the changed quote",
            "The price warning changed. Review it before confirming again.",
        ),
        (
            "Targets must be above 0%; stops must be between 0% and 100%.",
            "Fix the layer prices",
            "Target must be above 0%; stop must be between 0% and 100%.",
        ),
    ),
)
def test_problem_toasts_use_short_specific_copy(
    message: str, title: str, body: str
) -> None:
    notice = _toast_notice(message)

    assert notice is not None
    assert (notice.title, notice.description, notice.variant) == (title, body, "error")


def test_failed_post_write_refresh_does_not_show_a_success_toast() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()

    def failed_refresh() -> None:
        raise RuntimeError("connection lost")

    workbench._refresh_locked = failed_refresh  # type: ignore[method-assign]

    assert not workbench._refresh_after_acknowledged_write_locked(
        "Paper submission acknowledged."
    )
    assert workbench._toast is not None
    assert workbench._toast.title == "Couldn't verify the latest state"
    assert workbench._toast.variant == "error"


def test_busy_submit_only_applies_to_explicitly_async_controls() -> None:
    script = _busy_submit_script()

    assert "const text = button.dataset.busyText;" in script
    assert "if (!text) return;" in script
    assert "|| 'Working…'" not in script
    assert "button.disabled = true" not in script
    assert "button.style.pointerEvents = 'none';" in script
    assert "form.dataset.ibkrSubmitting" in script
    assert "button.querySelector('[data-position-quantity]')" in script
    assert "quantity.dataset.loading = 'true';" in script


def test_position_selection_shows_a_quantity_spinner_while_loading() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()

    page = TestClient(workbench.app).get(workbench.path).text
    inventory = page.split('aria-label="Open option positions"', maxsplit=1)[1]

    assert 'data-busy-text="Loading position…"' in inventory
    assert 'data-position-quantity' in inventory
    assert 'data-loading="false"' in inventory
    assert 'data-position-count' in inventory
    assert 'data-position-spinner' in inventory
    assert 'data-position-loading' in inventory
    assert 'name="action" value="select"' in inventory
    css = TestClient(workbench.app).get("/layers.css").text
    assert (
        "[data-position-quantity] [data-position-spinner] {\n"
        "  display: inline-flex;\n  width: 0;"
    ) in css
    assert (
        '[data-position-quantity][data-loading="true"] [data-position-spinner] {\n'
        "  width: 0.75rem;"
    ) in css
    assert '[data-position-quantity][data-loading="true"] [data-position-count]' not in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_window_starts_connection_before_loading_the_first_page() -> None:
    events: list[str] = []

    class Surface:
        def start_launch_refresh(self) -> None:
            events.append("connect")

    class View:
        def setUrl(self, _url: object) -> None:
            events.append("navigate")

    window = SimpleNamespace(_surface=Surface(), _view=View(), _url=object())

    StarUIPlannerWindow.refresh_on_launch(window)

    assert events == ["connect", "navigate"]


def test_launch_dialog_polls_status_without_replacing_the_page_every_interval() -> None:
    workbench = _demo_workbench()
    workbench._demo_mode = False
    workbench._launch_connection = "connecting"
    client = TestClient(workbench.app)

    page = client.get(workbench.path)
    status = client.get(f"{workbench.path}connection-status")

    assert page.status_code == 200
    assert "connection-status" in page.text
    assert "data-dialog" in page.text
    assert "dialog.showModal()" in page.text
    assert "window.setTimeout(check,500)" in page.text
    assert "window.location.reload(), 600" not in page.text
    assert status.json() == {"state": "connecting"}


def test_position_identity_preserves_the_inventory_scan_order() -> None:
    assert _position_identity("MSTR  260925C00150000") == (
        "MSTR",
        "150 CALL · SEP 25 '26",
    )
    assert _position_identity("Unknown contract") == ("Unknown", "contract")


def test_demo_draft_has_no_separate_preview_action() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    page = client.get(workbench.path)

    assert workbench._state.selected_con_id is not None
    assert workbench._state.available_quantity == 5
    assert workbench._state.quote_calculator is not None
    assert "Preview current draft" not in page.text


def test_reconciled_app_orders_do_not_show_a_coverage_alert() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._paper_execution = _OwnedOrderService({496_248_334})
    client = TestClient(workbench.app)

    page = client.get(workbench.path)

    assert "App-managed OCA coverage active" not in page.text
    assert "app-created orders were reconciled with TWS" not in page.text
    assert "Existing TWS exit orders" not in page.text


def test_confirmed_cancelled_layer_does_not_request_tws_fill_verification(
    tmp_path,
) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    journal = ExecutionJournal(tmp_path / "paper-journal.json")
    journal._write((
        JournalEntry(
            fingerprint="a" * 64,
            account=snapshot.selected.account,
            con_id=snapshot.selected.con_id,
            state="RECONCILED",
            order_ids=(101, 102),
            perm_ids=(201, 202),
            layers=(JournalLayer(
                quantity=2,
                target_price="1.20",
                stop_price="0.75",
                tif="GTC",
                target_perm_id=201,
                stop_perm_id=202,
                cancelled=True,
            ),),
        ),
    ))
    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(), journal
    )

    page = TestClient(workbench.app).get(workbench.path).text

    assert "Bracket cancelled" in page
    assert 'data-layer-state="cancelled"' in page
    assert 'value="dismiss-cancelled:' in page
    assert "No fill evidence" not in page
    assert "mt-2 border-t border-border pt-2" in page

    workbench._view_model._latest_snapshot = replace(
        snapshot, executions_complete=True
    )
    removed = TestClient(workbench.app).post(
        workbench.path + "action",
        data={"action": f"dismiss-cancelled:{'a' * 64}:0"},
    )
    assert removed.status_code == 200
    assert 'data-layer-state="cancelled"' not in removed.text
    assert journal.find("a" * 64).layers[0].hidden_from_workspace


def test_manual_tws_confirmation_clears_unknown_only_after_fresh_api_check(
    tmp_path, monkeypatch
) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    journal = ExecutionJournal(tmp_path / "paper-journal.json")
    fingerprint = "b" * 64
    journal._write((JournalEntry(
        fingerprint=fingerprint,
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        state="SUBMISSION_UNKNOWN",
        expected_order_count=2,
        snapshot_captured_at="99",
        layers=(JournalLayer(
            quantity=2,
            target_price="1.20",
            stop_price="0.75",
            tif="GTC",
        ),),
    ),))
    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(), journal
    )
    client = TestClient(workbench.app)
    workbench._submission_review_required = True
    sent_page = client.get(workbench.path).text
    assert 'data-submission-review' in sent_page
    assert 'data-cancelled-bracket-recovery-dialog' not in sent_page
    workbench._submission_review_required = False
    workbench._view_model._latest_snapshot = replace(
        snapshot,
        working_orders=(WorkingOrder(
            perm_id=301,
            client_id=17,
            order_id=201,
            key=snapshot.selected,
            action="SELL",
            order_type="LMT",
            remaining=Decimal("2"),
            status="PreSubmitted",
            oca_group=f"{fingerprint[:12]}/tranche-1",
        ),),
    )
    assert 'data-cancelled-bracket-recovery-dialog' not in client.get(
        workbench.path
    ).text
    workbench._view_model._latest_snapshot = snapshot
    recovery_page = client.get(workbench.path).text
    assert "Verify cancellation" in recovery_page
    assert 'id="cancelled_bracket_recovery"' in recovery_page
    assert 'required' in recovery_page
    assert f"{fingerprint[:12]}/tranche-1" in recovery_page
    assert "LMT target" in recovery_page and "1.20 · 2 contracts" in recovery_page
    assert "STP loss" in recovery_page and "0.75 · 2 contracts" in recovery_page

    unconfirmed = client.post(
        workbench.path + "action",
        data={"action": "resolve-cancelled-bracket", "fingerprint": fingerprint},
    )
    assert 'id="cancelled_bracket_recovery"' in unconfirmed.text
    assert journal.find(fingerprint).state == "SUBMISSION_UNKNOWN"

    fresh = replace(
        snapshot,
        captured_at=Decimal("101"),
        completed_orders_complete=True,
        executions_complete=True,
    )

    def selected(*_args):
        workbench._view_model._latest_snapshot = fresh
        return workbench._state

    monkeypatch.setattr(workbench._view_model, "select_position", selected)
    response = client.post(
        workbench.path + "action",
        data={
            "action": "resolve-cancelled-bracket",
            "confirmed": "yes",
            "fingerprint": fingerprint,
        },
    )

    assert response.status_code == 200
    assert "Cancelled bracket cleared" in response.text
    assert "Verify cancellation" not in response.text
    assert journal.find(fingerprint).state == "CANCELLED_CONFIRMED"


def test_refresh_replaces_a_draft_that_exceeds_newly_available_quantity() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = workbench._selected_con_id
    assert con_id is not None
    original = workbench._current_layers()[0]
    workbench._drafts[con_id] = tuple(replace(original, quantity="4") for _ in range(5))
    blocked = replace(
        workbench._state,
        status=UiStatus.BLOCKED,
        validations=(
            ValidationLine(
                "LAYER_QUANTITY_EXCEEDS_AVAILABLE",
                "draft layers exceed the verified available quantity",
            ),
        ),
        available_quantity=0,
        bracket_form=PlanForm(layers=workbench._drafts[con_id]),
    )
    refreshed = replace(
        workbench._state,
        status=UiStatus.READY,
        validations=(),
        available_quantity=4,
        bracket_form=PlanForm(),
    )
    calls: list[PlanForm] = []

    def select_position(_con_id: int, form: PlanForm) -> object:
        calls.append(form)
        return blocked if len(calls) == 1 else refreshed

    workbench._view_model.select_position = select_position  # type: ignore[method-assign]

    workbench._select_locked(con_id)

    assert len(calls) == 2
    assert len(workbench._current_layers()) == 1
    assert workbench._current_layers()[0].quantity == "4"


def test_empty_draft_stays_empty_until_add_layer() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = workbench._selected_con_id
    assert con_id is not None

    workbench._drafts[con_id] = ()
    workbench._ensure_draft_locked()

    assert workbench._current_layers() == ()

    workbench._add_layer_locked()

    assert len(workbench._current_layers()) == 1
    assert workbench._current_layers()[0].quantity == "5"


def test_existing_tws_bracket_waits_for_add_layer_before_creating_a_draft() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = workbench._selected_con_id
    assert con_id is not None
    assert workbench._state.working_orders
    order = workbench._state.working_orders[0]
    draft = workbench._current_layers()[0]
    workbench._drafts.pop(con_id, None)
    workbench._state = replace(
        workbench._state,
        working_orders=(replace(order, oca_group="existing-bracket"),),
        bracket_form=PlanForm(layers=(draft,)),
    )

    workbench._ensure_draft_locked()

    assert workbench._current_layers() == ()
    page = TestClient(workbench.app).get(workbench.path).text
    assert 'data-draft-empty-state' in page
    assert 'value="add-layer"' in page

    workbench._add_layer_locked()

    assert len(workbench._current_layers()) == 1


def test_active_layers_show_complete_reconciled_lmt_stop_pairs() -> None:
    from ibkr_options_manager.app.view_model import WorkingOrderLine

    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._paper_execution = _OwnedOrderService({101, 102})
    workbench._state = replace(
        workbench._state,
        working_orders=(
            WorkingOrderLine(
                perm_id=101,
                order_id=11,
                action="SELL",
                order_type="LMT",
                remaining="5",
                status="Submitted",
                oca_group="3ad441753bb9/tranche-1",
                limit_price=Decimal("26.20"),
                tif="GTC",
            ),
            WorkingOrderLine(
                perm_id=102,
                order_id=12,
                action="SELL",
                order_type="STP",
                remaining="5",
                status="Submitted",
                oca_group="3ad441753bb9/tranche-1",
                stop_price=Decimal("16.40"),
                tif="GTC",
            ),
        ),
    )
    client = TestClient(workbench.app)

    page = client.get(workbench.path)

    assert "DRAFT 1" in page.text
    assert 'aria-label="OCA layers workspace"' in page.text
    assert 'aria-label="Existing OCA layer rows"' in page.text
    assert 'data-layer-state="working"' in page.text
    assert "$26.20" in page.text
    assert "$16.40" in page.text
    header = page.text.split('id="active-form"', maxsplit=1)[0]
    assert 'aria-label="Move all active stops to B/E"' in header
    assert 'aria-label="Sell all active layers"' in header
    assert 'data-orientation="vertical"' in header
    assert 'form="active-form" name="action" value="market-exit-selected"' in header
    assert header.index('aria-label="Move all active stops to B/E"') < header.index(
        'aria-label="Sell all active layers"'
    ) < header.index('aria-label="Split draft layer quantities"')
    assert "data-reset-active-prices" in page.text
    assert "Cancel changes" in page.text
    active_form = page.text.split('id="active-form"', maxsplit=1)[1].split(
        "</form>", maxsplit=1
    )[0]
    assert 'data-reset-active-prices="true"' not in active_form
    review_footer = page.text.rsplit('data-reset-active-prices', maxsplit=1)[1]
    assert review_footer.index('Cancel changes') < review_footer.index(
        'data-active-execute'
    )
    assert "Update layers" not in page.text
    assert "Close working" not in page.text
    assert 'data-layer-state="draft"' in page.text
    assert 'name="active_target_101"' in page.text
    assert 'name="active_stop_101"' in page.text
    assert "data-active-review-row" in page.text
    assert "data-active-execute" in page.text
    assert 'id="active-quantity-1"' in page.text
    assert 'id="active-tif-1"' in page.text
    assert 'value="cancel-pair-arm:101"' in page.text
    assert 'title="Delete OCA bracket"' in page.text
    assert ">State<" not in page.text
    assert "requires a second confirmation" not in page.text
    assert "DRAFT 1" in page.text
    assert 'aria-current="page"' not in page.text
    assert 'data-active-initial="' in page.text
    assert 'data-live-price="active-target-1"' in page.text
    assert 'data-live-outcome="active-target-1"' in page.text
    assert page.text.index('data-layer-state="working"') < page.text.index(
        'data-layer-state="draft"'
    )
    assert "const targetEdited" in page.text
    active_script = _live_active_script(
        {"basis": "1", "multiplier": "100", "bands": []}
    )
    assert "setHidden(row, !(targetChanged || stopChanged), 'block')" in active_script
    assert "setReviewMode(changed)" in active_script
    assert "input.value = input.dataset.activeInitial || ''" in active_script
    assert "UPDATE SELL LMT" in page.text

    baseline, proposed, _config = workbench._projection_state()
    assert baseline.expected_gain is not None
    assert baseline.max_loss is not None
    assert proposed == baseline

    candidate = MarketExitCandidate(
        account=DEMO_ACCOUNT,
        con_id=workbench._selected_con_id or 0,
        target_order_id=11,
        target_perm_id=101,
        client_id=17,
        quantity=Decimal("5"),
        tif="GTC",
        oca_group="3ad441753bb9/tranche-1",
        stop_order_id=12,
        stop_perm_id=102,
    )
    workbench._armed_price_updates = (
        PriceUpdateCandidate(layer=candidate, stop_price=Decimal("17.40")),
    )
    _before, edited, _config = workbench._projection_state()
    assert edited.expected_gain == baseline.expected_gain
    assert edited.max_loss == baseline.max_loss + Decimal("500")
    improved_page = client.get(workbench.path).text
    assert 'data-loss-arrow="down"' in improved_page
    improvement_label = "Less loss" if baseline.max_loss < 0 else "Higher stop outcome"
    assert f'aria-label="{improvement_label} by $500.00"' in improved_page

    workbench._armed_price_updates = (
        PriceUpdateCandidate(layer=candidate, stop_price=Decimal("15.40")),
    )
    _before, worsened, _config = workbench._projection_state()
    assert worsened.max_loss == baseline.max_loss - Decimal("500")
    worsened_page = client.get(workbench.path).text
    assert 'data-loss-arrow="up"' in worsened_page
    deterioration_label = "More loss" if worsened.max_loss < 0 else "Lower stop outcome"
    assert f'aria-label="{deterioration_label} by $500.00"' in worsened_page

    workbench._armed_price_updates = ()
    workbench._armed_cancellation = candidate
    _before, deleted, _config = workbench._projection_state()
    assert deleted.expected_gain is None
    assert deleted.max_loss is None
    assert deleted.uncovered_quantity == Decimal("5")

    workbench._armed_cancellation = None
    workbench._message = "Execution blocked: previous action failed"
    assert workbench._toast is not None
    added_page = client.post(workbench.path + "action", data={"action": "add-layer"})
    assert workbench._toast is None
    before_add, after_add, _config = workbench._projection_state()
    assert before_add.expected_gain is not None
    assert after_add.expected_gain is not None
    assert after_add.expected_gain != before_add.expected_gain
    assert "Outcome projection" in added_page.text
    delta = after_add.expected_gain - before_add.expected_gain
    direction = "increased" if delta > 0 else "decreased"
    assert f'aria-label="Expected gain {direction} by ${abs(delta):,.2f}"' in added_page.text


def test_cancelled_review_with_no_draft_keeps_active_empty_state_visible() -> None:
    from ibkr_options_manager.app.view_model import WorkingOrderLine

    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = workbench._selected_con_id
    assert con_id is not None
    workbench._paper_execution = _OwnedOrderService({101, 102})
    workbench._drafts[con_id] = ()
    workbench._state = replace(
        workbench._state,
        available_quantity=0,
        working_orders=(
            WorkingOrderLine(
                perm_id=101, order_id=11, action="SELL", order_type="LMT",
                remaining="2", status="Submitted", oca_group="test/tranche-1",
                limit_price=Decimal("26.20"), tif="GTC",
            ),
            WorkingOrderLine(
                perm_id=102, order_id=12, action="SELL", order_type="STP",
                remaining="2", status="Submitted", oca_group="test/tranche-1",
                stop_price=Decimal("16.40"), tif="GTC",
            ),
        ),
    )
    workbench._armed_cancellation = MarketExitCandidate(
        account="DU123", con_id=con_id, target_order_id=11,
        target_perm_id=101, client_id=17, quantity=Decimal("2"),
        tif="GTC", oca_group="test/tranche-1", stop_order_id=12,
        stop_perm_id=102,
    )

    page = TestClient(workbench.app).post(
        workbench.path + "action", data={"action": "cancel-staged"}
    ).text

    assert 'data-has-draft-rows="false"' in page
    active_badge = re.search(r'<span[^>]*data-active-review-badge[^>]*>', page)
    active_review = re.search(r'<div[^>]*data-active-review(?:\s|>)[^>]*>', page)
    assert active_badge is not None
    assert active_review is not None
    badge_classes = re.search(r'class="([^"]+)"', active_badge.group())
    review_classes = re.search(r'class="([^"]+)"', active_review.group())
    assert badge_classes is not None and 'hidden' not in badge_classes[1].split()
    assert review_classes is not None and 'hidden' not in review_classes[1].split()
    assert "Ready to adjust a price?" in page
    assert "Change a target or stop in an active layer to preview the update here." in page
    assert 'data-edit-active-prices' in page
    assert "firstPrice.scrollIntoView" in page
    assert re.search(r'data-active-review-empty class="absolute inset-0 flex', page)
    assert "const showActive = active || !hasDraftRows;" in _live_active_script(
        {"basis": "1", "multiplier": "100", "bands": []}
    )
    assert 'data-draft-execute class="hidden w-full"' in page
    assert 'data-active-execute-control class="w-full"' in page


def test_max_loss_change_uses_unsigned_amount_and_directional_arrows() -> None:
    worse = str(_projection_loss_value(Decimal("-1282.56"), Decimal("-640")))
    better = str(_projection_loss_value(Decimal("-642.56"), Decimal("640")))

    assert 'aria-label="More loss by $640.00"' in worse
    assert 'data-loss-arrow="up"' in worse
    assert re.search(r'data-loss-arrow="down" class="hidden"><span data-icon-sh', worse)
    assert '<span data-loss-amount>$640.00</span>' in worse
    assert 'aria-label="Less loss by $640.00"' in better
    assert 'data-loss-arrow="down"' in better
    assert re.search(r'data-loss-arrow="up" class="hidden"><span data-icon-sh', better)
    assert '<span data-loss-amount>$640.00</span>' in better


def test_expected_gain_change_uses_opposite_arrow_mapping_to_loss() -> None:
    increased = str(_projection_gain_value(Decimal("3882.08"), Decimal("640")))
    decreased = str(_projection_gain_value(Decimal("3242.08"), Decimal("-640")))

    assert 'aria-label="Expected gain increased by $640.00"' in increased
    assert re.search(r'data-gain-arrow="down" class="hidden"><span data-icon-sh', increased)
    assert 'data-gain-change' in increased
    assert 'text-muted-foreground' in increased
    assert '<span data-gain-amount>$640.00</span>' in increased
    assert 'aria-label="Expected gain decreased by $640.00"' in decreased
    assert re.search(r'data-gain-arrow="up" class="hidden"><span data-icon-sh', decreased)
    assert 'text-muted-foreground' in decreased
    assert '<span data-gain-amount>$640.00</span>' in decreased


def test_unchanged_projection_keeps_neutral_placeholder_and_hides_arrows() -> None:
    for metric, markup in (
        ("gain", str(_projection_gain_value(Decimal("3882.08"), Decimal("0")))),
        ("loss", str(_projection_loss_value(Decimal("-642.56"), Decimal("0")))),
    ):
        assert 'aria-label="No change from loaded plan"' in markup
        assert f'<span data-{metric}-amount>—</span>' in markup
        assert re.search(
            rf'data-{metric}-arrow="up" class="hidden"><span data-icon-sh', markup
        )
        assert re.search(
            rf'data-{metric}-arrow="down" class="hidden"><span data-icon-sh', markup
        )
        assert 'text-muted-foreground' in markup


def test_fully_allocated_position_keeps_active_outcome_visible() -> None:
    from ibkr_options_manager.app.view_model import WorkingOrderLine

    workbench = _demo_workbench()
    workbench.load_demo_data()
    selected = workbench._selected_con_id
    assert selected is not None
    workbench._paper_execution = _OwnedOrderService({101, 102})
    workbench._drafts[selected] = ()
    workbench._state = replace(
        workbench._state,
        status=UiStatus.BLOCKED,
        validations=(
            ValidationLine(
                "POSITION_FULLY_ALLOCATED",
                "Existing closing exposure already covers the whole position",
            ),
        ),
        available_quantity=0,
        working_orders=(
            WorkingOrderLine(
                perm_id=101,
                order_id=11,
                action="SELL",
                order_type="LMT",
                remaining="10",
                status="Submitted",
                oca_group="test/tranche-1",
                limit_price=Decimal("3.30"),
                tif="GTC",
            ),
            WorkingOrderLine(
                perm_id=102,
                order_id=12,
                action="SELL",
                order_type="STP",
                remaining="10",
                status="Submitted",
                oca_group="test/tranche-1",
                stop_price=Decimal("2.06"),
                tif="GTC",
            ),
        ),
    )

    _baseline, projected, _config = workbench._projection_state()
    assert projected.expected_gain == Decimal("560")
    assert projected.max_loss == Decimal("-680")
    page = TestClient(workbench.app).get(workbench.path)
    assert 'data-live-metric="gain"' in page.text
    assert "+$560.00" in page.text
    assert "-$680.00" in page.text
    assert "All 10 held contract(s) covered." not in page.text
    assert ">Realized P&amp;L<" not in page.text
    assert ">Cost basis<" not in page.text
    assert "How expected gain is calculated" in page.text
    assert "How max loss is calculated" in page.text
    assert "Realised P&amp;L plus projected gains from the current layer plan." in page.text
    assert "Projected losses at the current layer stops; excludes realised P&amp;L." in page.text
    assert "An up arrow" not in page.text

    workbench._armed_market_exit = MarketExitCandidate(
        account="DU123",
        con_id=selected,
        target_order_id=11,
        target_perm_id=101,
        client_id=17,
        quantity=Decimal("10"),
        tif="GTC",
        oca_group="test/tranche-1",
        stop_order_id=12,
        stop_perm_id=102,
    )
    _baseline, staged, _config = workbench._projection_state()
    assert staged.expected_gain == Decimal("560")
    assert staged.max_loss == Decimal("-680")
    staged_page = TestClient(workbench.app).get(workbench.path).text
    assert "+$560.00" in staged_page
    assert "-$680.00" in staged_page
    assert "Market exit price is unknown until filled." not in staged_page
    workbench._armed_market_exit = None

    workbench._state = replace(
        workbench._state,
        validations=(ValidationLine("OCA_QUANTITY_MISMATCH", "Conflicting orders"),),
    )
    assert workbench._projection_state()[1].expected_gain is None


def test_partial_journal_reconciliation_keeps_surviving_pair_active_in_the_ui(
    tmp_path,
) -> None:
    """A cancelled sibling pair must not turn the live pair into an external order."""
    from ibkr_options_manager.app.view_model import WorkingOrderLine

    workbench = _demo_workbench()
    workbench.load_demo_data()
    selected = workbench._selected_con_id
    assert selected is not None
    assert workbench._state.account != DEMO_ACCOUNT
    journal = ExecutionJournal(tmp_path / "journal.json")
    # Fixture a persisted partial recovery.
    journal._write(
        (
            JournalEntry(
                fingerprint="partial-recovery-fingerprint",
                account=DEMO_ACCOUNT,
                con_id=selected,
                state="PARTIALLY_RECONCILED",
                expected_order_count=4,
                order_ids=(103, 104),
                perm_ids=(203, 204),
            ),
        )
    )
    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(), journal
    )
    # The settings value is stale; ownership must use the full account from
    # the verified selected snapshot rather than its redacted display value.
    workbench._settings = replace(workbench._settings, account="DU-stale")
    workbench._state = replace(
        workbench._state,
        available_quantity=4,
        working_orders=(
            WorkingOrderLine(
                perm_id=203,
                order_id=103,
                action="SELL",
                order_type="LMT",
                remaining="3",
                status="Submitted",
                oca_group="partial-reco/tranche-2",
                limit_price=Decimal("3.30"),
                tif="GTC",
            ),
            WorkingOrderLine(
                perm_id=204,
                order_id=104,
                action="SELL",
                order_type="STP",
                remaining="3",
                status="Submitted",
                oca_group="partial-reco/tranche-2",
                stop_price=Decimal("2.06"),
                tif="GTC",
            ),
        ),
    )

    page = TestClient(workbench.app).get(workbench.path)

    assert "App-managed OCA coverage active" not in page.text
    assert "Existing order coverage detected" not in page.text
    assert 'data-layer-state="working"' in page.text
    assert "Active" in page.text
    assert 'value="3"' in page.text


def test_closed_bracket_profit_is_separate_from_surviving_active_layer(
    tmp_path,
) -> None:
    from ibkr_options_manager.app.view_model import WorkingOrderLine

    workbench = _demo_workbench()
    workbench.load_demo_data()
    selected = workbench._selected_con_id
    assert selected is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    fingerprint = "a" * 64
    journal._write(
        (
            JournalEntry(
                fingerprint=fingerprint,
                account=DEMO_ACCOUNT,
                con_id=selected,
                state="PARTIALLY_RECONCILED",
                expected_order_count=4,
                order_ids=(103, 104),
                perm_ids=(203, 204),
                layers=(
                    JournalLayer(
                        3,
                        "15.50",
                        "9.70",
                        "GTC",
                        201,
                        202,
                        target_percentage="20",
                        stop_percentage="25",
                    ),
                    JournalLayer(2, "20.70", "9.70", "GTC", 203, 204),
                ),
                fills=(
                    JournalFill(
                        exec_id="filled.01",
                        perm_id=201,
                        side="SLD",
                        quantity="3",
                        price="15.50",
                        time="20260925 12:00:00",
                        realized_pnl="557.44",
                        currency="USD",
                    ),
                ),
            ),
        )
    )
    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(), journal
    )
    workbench._state = replace(
        workbench._state,
        working_orders=(
            WorkingOrderLine(
                perm_id=203,
                order_id=103,
                action="SELL",
                order_type="LMT",
                remaining="2",
                status="Submitted",
                oca_group=f"{fingerprint[:12]}/tranche-2",
                limit_price=Decimal("20.70"),
                tif="GTC",
            ),
            WorkingOrderLine(
                perm_id=204,
                order_id=104,
                action="SELL",
                order_type="STP",
                remaining="2",
                status="Submitted",
                oca_group=f"{fingerprint[:12]}/tranche-2",
                stop_price=Decimal("9.70"),
                tif="GTC",
            ),
        ),
    )

    client = TestClient(workbench.app)
    page = client.get(workbench.path)

    assert 'data-layer-state="sold"' in page.text
    assert 'data-result-tone="profit"' in page.text
    assert 'data-revealed="false"' not in page.text
    assert "soldLayerRevealBound" not in page.text
    assert "SOLD" in page.text
    assert "+$557.44 USD" in page.text
    assert workbench._projection_state()[1].realized_pnl == Decimal("557.44")
    assert "Closed" in page.text
    assert "Active" in page.text
    assert "Target filled" not in page.text
    assert "WORKING" not in page.text
    assert 'id="sold-target-1"' in page.text
    assert 'value="20"' in page.text
    assert "$15.50" in page.text
    assert re.search(r'id="sold-target-1"[^>]*disabled', page.text)
    assert 'id="sold-stop-1"' in page.text
    assert 'value="25"' in page.text
    assert "$9.70" in page.text
    assert re.search(r'id="sold-stop-1"[^>]*disabled', page.text)
    assert 'id="sold-quantity-1"' in page.text
    assert 'value="3"' in page.text
    assert 'id="sold-tif-1"' in page.text
    assert "aaaaaaaaaaaa/tranche-1" in page.text
    assert "aaaaaaaaaaaa/tranche-2" in page.text
    assert 'data-slot="tooltip-content"' in page.text
    assert 'href="/layers.css"' in page.text
    layer_css = client.get("/layers.css")
    assert layer_css.status_code == 200
    assert ".oca-layer-list > [data-layer-state]:first-child" in layer_css.text
    assert ".oca-layer-list > [data-layer-state]:last-child" in layer_css.text
    assert "oca-layer-list" in page.text
    assert 'data-layer-state="working"' in page.text
    assert page.text.index('data-layer-state="sold"') < page.text.index(
        'data-layer-state="working"'
    )
    existing = page.text.split('id="active-form"', maxsplit=1)[1].split(
        "</form>", maxsplit=1
    )[0]
    assert 'data-slot="card"' not in existing
    assert "pending TWS verification" not in page.text
    assert "TWS orders are pending verification" not in page.text

    # Sold gains remain in expected gain, while the stop scenario concerns
    # only contracts still held. Full allocation blocks new drafts, not this
    # already-reconciled active-layer projection.
    original_state = workbench._state
    original_snapshot = workbench._view_model.latest_snapshot()
    assert original_snapshot is not None
    original_draft = workbench._drafts[selected]
    workbench._drafts[selected] = ()
    workbench._view_model._latest_snapshot = replace(
        original_snapshot,
        position=replace(original_snapshot.position, quantity=Decimal("2")),
    )
    workbench._state = replace(
        original_state,
        status=UiStatus.BLOCKED,
        validations=(
            ValidationLine("POSITION_FULLY_ALLOCATED", "Position fully allocated"),
        ),
        available_quantity=0,
        positions=tuple(
            replace(position, quantity="2")
            if position.con_id == selected
            else position
            for position in original_state.positions
        ),
        working_orders=(
            replace(original_state.working_orders[0], limit_price=Decimal("3.30")),
            replace(original_state.working_orders[1], stop_price=Decimal("2.06")),
        ),
    )
    _baseline, combined, _config = workbench._projection_state()
    assert combined.expected_gain == Decimal("669.44")
    assert combined.max_loss == Decimal("-136.00")
    combined_page = client.get(workbench.path).text
    assert "+$669.44" in combined_page
    assert "-$136.00" in combined_page
    assert ">2 / 5<" in combined_page
    assert ">+$557.44<" in combined_page
    workbench._state = original_state
    workbench._view_model._latest_snapshot = original_snapshot
    workbench._drafts[selected] = original_draft

    # A stale TWS working-order snapshot must not make the filled layer look
    # editable or allow a new draft while the two sources disagree.
    entry = journal.submission_entries(account=DEMO_ACCOUNT, con_id=selected)[0]
    journal._write((replace(entry, perm_ids=(201, 202, 203, 204)),))
    workbench._state = replace(
        workbench._state,
        working_orders=(
            *workbench._state.working_orders,
            WorkingOrderLine(
                perm_id=201,
                order_id=101,
                action="SELL",
                order_type="LMT",
                remaining="3",
                status="Submitted",
                oca_group=f"{fingerprint[:12]}/tranche-1",
                limit_price=Decimal("15.50"),
                tif="GTC",
            ),
            WorkingOrderLine(
                perm_id=202,
                order_id=102,
                action="SELL",
                order_type="STP",
                remaining="3",
                status="Submitted",
                oca_group=f"{fingerprint[:12]}/tranche-1",
                stop_price=Decimal("9.70"),
                tif="GTC",
            ),
        ),
    )
    conflict = TestClient(workbench.app).get(workbench.path)
    assert "Fill and working order conflict" in conflict.text
    assert 'data-layer-state="verify"' in conflict.text
    assert 'data-layer-state="draft"' not in conflict.text


def test_legacy_sold_layer_recovers_unique_percentages_without_current_basis() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    entry = JournalEntry(
        fingerprint="a" * 64,
        account=DEMO_ACCOUNT,
        con_id=1,
        state="RECONCILED",
        layers=(JournalLayer(3, "15.50", "9.70", "GTC"),),
    )
    outcome = LayerOutcome(
        "CLOSED_PROFIT",
        filled_quantity=Decimal("3"),
        realized_pnl=Decimal("557.44"),
        currency="USD",
        exit_side="Target",
    )

    # The surviving position's basis is unrelated to this old layer.
    workbench._state = replace(workbench._state, unit_basis=Decimal("2.74"))
    recovered = str(workbench._closed_layer_row(1, entry, 0, outcome))
    assert re.search(r'value="≈20"[^>]*id="sold-target-1"', recovered)
    assert re.search(r'value="≈25"[^>]*id="sold-stop-1"', recovered)

    unmatched_entry = replace(
        entry, layers=(replace(entry.layers[0], stop_price="8.60"),)
    )
    unmatched = str(workbench._closed_layer_row(1, unmatched_entry, 0, outcome))
    assert re.search(r'value="—"[^>]*id="sold-target-1"', unmatched)
    assert re.search(r'value="—"[^>]*id="sold-stop-1"', unmatched)


def test_active_layer_prefers_configured_percentage_over_rounded_inverse() -> None:
    """An untouched 20% target remains 20% after TWS exposes its tick price."""
    percentage = _active_percentage_for_price(
        Decimal("20.80"),
        Decimal("17.30"),
        target=True,
        bands=(PriceBand(Decimal("0"), Decimal("0.10")),),
        presets=(Decimal("20"), Decimal("40")),
    )

    assert percentage == "20"


def test_draft_prices_stay_fixed_when_an_action_keeps_percentages() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    original = workbench._current_layers()[0]
    changed_basis = (workbench._state.unit_basis or Decimal("1")) + Decimal("0.01")
    workbench._state = replace(workbench._state, unit_basis=changed_basis)

    workbench._save_form_locked({"quantity_1": original.quantity})
    saved = workbench._current_layers()[0]
    assert saved.target_price == original.target_price
    assert saved.stop_price == original.stop_price

    workbench._save_form_locked({"target_1": "40"})
    edited = workbench._current_layers()[0]
    assert edited.target_price != original.target_price
    assert edited.stop_price == original.stop_price


def test_active_layer_keeps_an_acknowledged_stop_display_until_tws_refreshes_it() -> (
    None
):
    """An immediate post-write snapshot must not visually undo a 0% stop."""
    from ibkr_options_manager.app.view_model import WorkingOrderLine

    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._paper_execution = _OwnedOrderService({101, 102})
    workbench._state = replace(
        workbench._state,
        working_orders=(
            WorkingOrderLine(
                perm_id=101,
                order_id=11,
                action="SELL",
                order_type="LMT",
                remaining="4",
                status="Submitted",
                oca_group="acknowledged/tranche-1",
                limit_price=Decimal("3.30"),
                tif="GTC",
            ),
            WorkingOrderLine(
                perm_id=102,
                order_id=12,
                action="SELL",
                order_type="STP",
                remaining="4",
                status="Submitted",
                oca_group="acknowledged/tranche-1",
                stop_price=Decimal("2.06"),
                tif="GTC",
            ),
        ),
    )

    # TWS has acknowledged a B/E amendment, but the first automatic snapshot
    # still carries the prior $2.06 stop price.
    workbench._remember_pending_active_prices_locked(
        target_perm_id=101,
        stop_perm_id=102,
        target_price=None,
        stop_price=Decimal("2.74"),
    )
    page = TestClient(workbench.app).get(workbench.path)

    assert 'name="active_stop_101"' in page.text
    assert 'value="0.0" name="active_stop_101"' in page.text
    assert "$2.74" in page.text

    # The local display override disappears once a later TWS snapshot carries
    # the broker-confirmed B/E stop price.
    workbench._state = replace(
        workbench._state,
        working_orders=(
            workbench._state.working_orders[0],
            replace(workbench._state.working_orders[1], stop_price=Decimal("2.74")),
        ),
    )
    workbench._reconcile_pending_active_prices_locked()

    assert workbench._pending_active_prices == {}


def test_reset_active_prices_restores_target_and_stop_after_move_to_be() -> None:
    application = QApplication.instance() or QApplication([])
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
        '<input data-active-input="stop" data-active-perm-id="1" '
        'data-live-layer="1" data-active-initial="25" '
        'data-active-original="7.5" value="25">'
        '<button type="button" data-move-stops-to-be>Move stop to B/E</button>'
        '</form><div data-price-edit-reset data-reset-visible="false" aria-hidden="true">'
        '<button type="button" data-reset-active-prices disabled>Cancel changes</button>'
        '</div>'
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
              const reset = document.querySelector('[data-reset-active-prices]');
              const slot = document.querySelector('[data-price-edit-reset]');
              target.value = '60';
              target.dispatchEvent(new Event('input', { bubbles: true }));
              document.querySelector('[data-move-stops-to-be]').click();
              const afterMove = [target.value, stop.value, reset.disabled, slot.dataset.resetVisible];
              reset.click();
              return JSON.stringify({ afterMove, afterReset: [target.value, stop.value, reset.disabled, slot.dataset.resetVisible] });
            })()""",
            lambda value: (results.append(value), loop.quit()),
        )

    view.loadFinished.connect(inspect)
    view.setHtml(html)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    view.close()

    assert results == [
        '{"afterMove":["60","0",false,"true"],"afterReset":["50","25",true,"false"]}'
    ]


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


def test_price_update_confirmation_uses_the_shared_cancel_confirm_bar() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    layer = MarketExitCandidate(
        account=DEMO_ACCOUNT,
        con_id=workbench._selected_con_id or 0,
        target_order_id=11,
        target_perm_id=101,
        client_id=17,
        quantity=Decimal("4"),
        tif="GTC",
        oca_group="example/tranche-1",
        stop_order_id=12,
        stop_perm_id=102,
    )
    workbench._armed_price_updates = (
        PriceUpdateCandidate(layer=layer, stop_price=Decimal("2.74")),
    )

    sidebar = (
        TestClient(workbench.app)
        .get(workbench.path)
        .text.split("ACTION REVIEW", maxsplit=1)[1]
    )

    assert 'value="price-update-confirm"' in sidebar
    assert "Quote status: frozen" in sidebar
    assert ">Cancel<" in sidebar
    assert ">Confirm<" in sidebar
    assert "Click to confirm" not in sidebar


def test_stop_above_latest_ask_warns_before_price_update_and_quote_change_rearms(
    monkeypatch,
) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    layer = MarketExitCandidate(
        account=DEMO_ACCOUNT,
        con_id=snapshot.selected.con_id,
        target_order_id=11,
        target_perm_id=101,
        client_id=17,
        quantity=Decimal("1"),
        tif="GTC",
        oca_group="example/tranche-1",
        stop_order_id=12,
        stop_perm_id=102,
    )
    updates = (PriceUpdateCandidate(layer=layer, stop_price=Decimal("10.10")),)
    safe = replace(
        snapshot,
        quote=replace(
            snapshot.quote,
            bid=Decimal("10.00"),
            ask=Decimal("10.20"),
            market_data_type="LIVE",
            fresh=True,
        ),
    )
    risky = replace(
        safe, quote=replace(safe.quote, bid=Decimal("9.70"), ask=Decimal("9.80"))
    )
    impact = _price_update_impact(risky, updates)
    assert impact.title == "Possible immediate sell"
    assert "SELL STP $10.10 is at or above the current ask $9.80" in impact.details[0]

    workbench._paper_execution = object()  # type: ignore[assignment]
    workbench._armed_price_updates = updates
    workbench._warned_price_update_concerns = _price_update_impact(safe, updates).concerns
    monkeypatch.setattr(workbench._view_model, "select_position", lambda *_: workbench._state)
    monkeypatch.setattr(workbench._view_model, "latest_snapshot", lambda: risky)
    monkeypatch.setattr(workbench, "_announce_reconciliation_locked", lambda: None)

    workbench._confirm_price_updates_locked({})

    assert "confirm again" in workbench._message
    assert workbench._armed_price_updates == updates
    assert workbench._warned_price_update_concerns == impact.concerns


@pytest.mark.parametrize("prior_unknown", [False, True])
def test_arming_price_update_preserves_edited_percentage_in_active_input(
    tmp_path,
    monkeypatch,
    prior_unknown,
) -> None:
    from ibkr_options_manager.app.view_model import WorkingOrderLine

    trace_path = tmp_path / "price-amendments.jsonl"
    monkeypatch.setenv("IBKR_OPTIONS_MANAGER_PRICE_TRACE", str(trace_path))

    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._select_locked(1_002_100_161)
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    fingerprint = "a" * 64
    group = f"{fingerprint[:12]}/tranche-1"
    target = WorkingOrder(
        perm_id=201,
        client_id=17,
        order_id=101,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("2"),
        status="Submitted",
        oca_group=group,
        limit_price=Decimal("29.10"),
        tif="GTC",
    )
    stop = replace(
        target,
        perm_id=202,
        order_id=102,
        order_type="STP",
        limit_price=None,
        stop_price=Decimal("18.20"),
    )
    active_snapshot = replace(
        snapshot,
        read_only_api=False,
        position=replace(snapshot.position, unit_basis=Decimal("24.22")),
        working_orders=(target, stop),
    )
    workbench._state = replace(
        workbench._state,
        unit_basis=Decimal("24.22"),
        working_orders=(
            WorkingOrderLine(
                perm_id=201,
                order_id=101,
                action="SELL",
                order_type="LMT",
                remaining="2",
                status="Submitted",
                oca_group=group,
                limit_price=Decimal("29.10"),
                tif="GTC",
            ),
            WorkingOrderLine(
                perm_id=202,
                order_id=102,
                action="SELL",
                order_type="STP",
                remaining="2",
                status="Submitted",
                oca_group=group,
                stop_price=Decimal("18.20"),
                tif="GTC",
            ),
        ),
    )
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal._write(
        (
            JournalEntry(
                fingerprint=fingerprint,
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                state="RECONCILED",
                expected_order_count=2,
                order_ids=(101, 102),
                perm_ids=(201, 202),
            ),
        )
    )
    if prior_unknown:
        journal.begin_management(
            replace(active_snapshot, captured_at=Decimal("0")),
            operation="price-update",
            material=(
                101, 201, Decimal("29.10"), Decimal("31.50"),
                102, 202, Decimal("18.20"), None,
            ),
            expected_order_count=1,
        )
        prior_entry = journal.latest_management_attempt(
            active_snapshot,
            operation="price-update",
            material=(
                101, 201, Decimal("29.10"), Decimal("31.50"),
                102, 202, Decimal("18.20"), None,
            ),
        )
        assert prior_entry is not None
        journal.mark_unknown(prior_entry.fingerprint)

    class PriceWriter(DemoPaperExecutionTransport):
        def __init__(self) -> None:
            self.prices: list[Decimal | None] = []

        def modify_prices(self, _snapshot, updates, **_kwargs) -> PaperSubmission:
            self.prices.extend(update.target_price for update in updates)
            return PaperSubmission(order_ids=(101,), perm_ids=(201,))

    writer = PriceWriter()
    workbench._paper_execution = PaperExecutionService(writer, journal)
    workbench._view_model.select_position = lambda *_args: workbench._state  # type: ignore[method-assign]
    workbench._view_model.latest_snapshot = lambda: active_snapshot  # type: ignore[method-assign]

    page = TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            "active_target_201": "30",
            "active_stop_201": "25",
        },
    )

    assert workbench._armed_price_updates[0].target_price == Decimal("31.50")
    assert "29.1" in page.text and "31.5" in page.text
    target_input = re.search(r'<input[^>]*name="active_target_201"[^>]*>', page.text)
    assert target_input is not None
    assert 'value="30"' in target_input.group()
    assert ('name="ack_unknown_price_update"' in page.text) is prior_unknown
    arm_toast_revision = workbench._toast_revision

    if prior_unknown:
        blocked = TestClient(workbench.app).post(
            workbench.path + "action", data={"action": "price-update-confirm"}
        )
        assert writer.prices == []
        assert "acknowledge" in blocked.text
        assert workbench._armed_price_updates

    confirmed = TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "price-update-confirm",
            **({"ack_unknown_price_update": "on"} if prior_unknown else {}),
        },
    )

    assert writer.prices == [Decimal("31.50")]
    assert workbench._status_message.startswith(
        "TWS acknowledged 1 app-owned OCA price amendment"
    ), workbench._status_message
    assert workbench._toast_revision > arm_toast_revision
    assert "Price update sent to TWS" in confirmed.text
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    requested = next(
        event for event in events if event["event"] == "ui_confirm_requested"
    )
    assert requested["requested"][0]["target_price"] == "31.50"
    assert requested["retry_acknowledged"] is prior_unknown
    assert events[-1]["event"] == "ui_result"
    assert events[-1]["outcome"] == "acknowledged"


def test_close_all_review_lists_pair_cancellations_then_one_market_order() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._armed_market_exits = (
        MarketExitCandidate(
            account="DU123",
            con_id=101,
            target_order_id=11,
            target_perm_id=101,
            client_id=17,
            quantity=Decimal("10"),
            tif="GTC",
            oca_group="example/tranche-1",
            stop_order_id=12,
            stop_perm_id=102,
        ),
        MarketExitCandidate(
            account="DU123",
            con_id=101,
            target_order_id=13,
            target_perm_id=103,
            client_id=17,
            quantity=Decimal("5"),
            tif="GTC",
            oca_group="example/tranche-2",
            stop_order_id=14,
            stop_perm_id=104,
        ),
    )
    client = TestClient(workbench.app)

    review = client.get(workbench.path)
    sidebar = review.text.split("ACTION REVIEW", maxsplit=1)[1]

    assert "OCA-1" in sidebar
    assert "OCA-2" in sidebar
    assert "CANCEL BRACKET" in sidebar
    assert "example/tranche-1" in sidebar
    assert "example/tranche-2" in sidebar
    assert "Create MKT sell order" in sidebar
    assert "SELL MKT" in sidebar
    assert "text-emerald-400" in sidebar
    assert "15 contracts" in sidebar
    assert "Market exit price is unknown until filled." not in sidebar
    baseline, proposed, _ = workbench._projection_state()
    assert proposed.expected_gain == baseline.expected_gain
    assert proposed.max_loss == baseline.max_loss
    assert 'value="active-action-execute"' in sidebar
    assert ">Confirm<" not in sidebar
    assert ">Cancel changes<" in sidebar
    assert "Wait for both cancellation confirmations" not in sidebar
    assert "Selected app-owned OCA layer" not in sidebar
    assert "GTC" in sidebar

    workbench._active_action_verified = True
    assert ">Confirm<" in client.get(workbench.path).text

    client.post(workbench.path + "action", data={"action": "cancel-staged"})

    assert workbench._armed_market_exits == ()
    assert workbench._status_message == "Staged action cancelled. No orders were sent to TWS."
    assert workbench._toast is None


def test_delete_active_layer_review_cancels_only_that_oca_bracket() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._armed_cancellation = MarketExitCandidate(
        account="DU123",
        con_id=101,
        target_order_id=11,
        target_perm_id=101,
        client_id=17,
        quantity=Decimal("5"),
        tif="GTC",
        oca_group="example/tranche-1",
        stop_order_id=12,
        stop_perm_id=102,
    )

    page = TestClient(workbench.app).get(workbench.path)
    sidebar = page.text.split("ACTION REVIEW", maxsplit=1)[1]

    assert "CANCEL" in sidebar
    assert "OCA-1" in sidebar
    assert "5 contracts · GTC" in sidebar
    assert "CANCEL BRACKET" in sidebar
    assert "example/tranche-1" in sidebar
    assert "SELL MKT" not in sidebar
    assert 'value="active-action-execute"' in sidebar
    assert "Review the action above" not in sidebar
    assert ">Confirm<" not in sidebar
    assert ">Cancel changes<" in sidebar

    workbench._active_action_verified = True
    assert ">Confirm<" in TestClient(workbench.app).get(workbench.path).text


@pytest.mark.parametrize(
    ("review_action", "confirm_action", "layer_count"),
    [
        ("cancel-pair-arm:101", "cancel-pair-confirm", 1),
        ("market-exit-arm:101", "market-exit-confirm", 1),
        ("market-exit-selected", "market-exit-confirm", 2),
    ],
)
def test_active_action_reviews_before_refresh_and_requires_execute(
    monkeypatch, review_action: str, confirm_action: str, layer_count: int
) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    candidates = tuple(
        MarketExitCandidate(
            account=snapshot.selected.account,
            con_id=snapshot.selected.con_id,
            target_order_id=11 + index * 2,
            target_perm_id=101 + index * 2,
            client_id=17,
            quantity=Decimal("2"),
            tif="GTC",
            oca_group=f"example/tranche-{index + 1}",
            stop_order_id=12 + index * 2,
            stop_perm_id=102 + index * 2,
        )
        for index in range(layer_count)
    )
    workbench._paper_execution = SimpleNamespace(  # type: ignore[assignment]
        owned_perm_ids=lambda **_kwargs: frozenset(),
        prepare_market_exit=lambda *_args, **_kwargs: candidates[0],
        prepare_market_exits=lambda *_args, **_kwargs: candidates,
    )
    monkeypatch.setattr(workbench, "_active_target_perm_ids", lambda: (101, 103))
    monkeypatch.setattr(workbench, "_announce_reconciliation_locked", lambda: None)
    refreshes: list[int] = []

    def refresh_selected(con_id: int, _form: PlanForm) -> object:
        refreshes.append(con_id)
        return workbench._state

    monkeypatch.setattr(workbench._view_model, "select_position", refresh_selected)
    client = TestClient(workbench.app)

    review = client.post(workbench.path + "action", data={"action": review_action})
    assert refreshes == []
    assert workbench._toast is None
    assert "Review cancellation" not in review.text
    assert 'value="active-action-execute"' in review.text
    assert "Cancel changes" in review.text
    assert "Review the action above" not in review.text
    assert 'value="' + confirm_action + '"' not in review.text

    client.post(workbench.path + "action", data={"action": confirm_action})
    assert refreshes == []
    assert not workbench._active_action_verified

    execute = client.post(
        workbench.path + "action", data={"action": "active-action-execute"}
    )
    assert refreshes == [snapshot.selected.con_id]
    assert workbench._active_action_verified
    assert 'value="' + confirm_action + '"' in execute.text

    if review_action == "cancel-pair-arm:101":
        workbench._paper_execution.prepare_market_exit = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: replace(candidates[0], quantity=Decimal("1"))
        )
        changed = client.post(
            workbench.path + "action", data={"action": "active-action-execute"}
        )
        assert not workbench._active_action_verified
        assert workbench._armed_cancellation is None
        assert 'value="cancel-pair-confirm"' not in changed.text
    elif review_action == "market-exit-selected":
        monkeypatch.setattr(workbench, "_active_target_perm_ids", lambda: (101, 103, 105))
        changed = client.post(
            workbench.path + "action", data={"action": "active-action-execute"}
        )
        assert not workbench._active_action_verified
        assert workbench._armed_market_exits == ()
        assert 'value="market-exit-confirm"' not in changed.text


def test_draft_allocation_rejects_out_of_range_quantity() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    form = {
        "action": "save-draft",
        "target_1": "20",
        "stop_1": "25",
        "quantity_1": "2",
    }

    response = client.post(workbench.path + "action", data=form)
    assert response.status_code == 200
    assert workbench._current_layers()[0].quantity == "2"
    assert 'data-live-allocation-bar' not in response.text

    for invalid in ("0", "6", "2.5"):
        response = client.post(
            workbench.path + "action", data={**form, "quantity_1": invalid}
        )
        assert workbench._current_layers()[0].quantity == "2"
        assert "Draft quantities must be whole contracts from 1 to 5" in response.text

    workbench._add_layer_locked()
    before = tuple(layer.quantity for layer in workbench._current_layers())
    response = client.post(
        workbench.path + "action",
        data={
            **form,
            "action": "add-layer",
            "quantity_1": "4",
            "quantity_2": "4",
        },
    )
    assert tuple(layer.quantity for layer in workbench._current_layers()) == before
    assert "no more than 5 assigned in total" in response.text

    con_id = workbench._selected_con_id
    assert con_id is not None
    workbench._drafts[con_id] = (replace(workbench._current_layers()[0], quantity="6"),)
    overallocated = client.get(workbench.path)
    assert 'data-live-allocation-bar' not in overallocated.text

    workbench._paper_execution = _OwnedOrderService(set())
    blocked = client.get(workbench.path).text
    assert "6 contracts drafted; 5 available." in blocked
    assert "Reduce a layer" in blocked
    assert blocked.index("6 contracts drafted") < blocked.index("Outcome projection")
    alert = re.search(r'<div[^>]*data-draft-quantity-alert[^>]*>', blocked)
    assert alert is not None
    assert "bg-destructive" in alert.group()
    assert "border-destructive/70" in alert.group()
    assert "text-white" in alert.group()
    css = client.get("/layers.css").text
    assert ".draft-quantity-alert" not in css
    library_css = client.get("/starui.css").text
    assert ".bg-destructive{" in library_css
    assert ".border-destructive\\/70{" in library_css
    assert ".text-white{" in library_css
    execute = re.search(r'<button[^>]*value="execute-arm"[^>]*>', blocked)
    assert execute is not None and re.search(r"\sdisabled(?:\s|>)", execute.group())


def test_starui_workbench_renders_and_adds_a_layer_from_a_server_owned_form() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)

    page = client.get(workbench.path)
    assert page.status_code == 200
    assert "DRAFT 1" in page.text
    assert re.search(r"Last refreshed \d{2}:\d{2}:\d{2}", page.text)
    assert "Verified 0.0 s" not in page.text
    assert "Each layer creates one SELL LMT + SELL STP OCA pair." not in page.text
    assert "Transmission locked" not in page.text
    assert "Preview only" not in page.text
    assert "SELL LMT" in page.text
    assert "SELL STP" in page.text
    assert "150 CALL · SEP 25 '26" in page.text
    assert "Connection &amp; layer defaults" in page.text
    assert "<dialog" in page.text
    assert "h-screen overflow-hidden" in page.text
    assert 'class="workspace-content flex min-w-0 min-h-0 flex-col overflow-hidden px-8 py-6"' in page.text
    layout_css = client.get("/layers.css").text
    assert ".workspace-content {" in layout_css
    assert "max-width: 80rem;" in layout_css
    assert "margin-inline: auto;" in layout_css
    assert 'aria-label="Draft layer rows"' in page.text
    assert 'aria-label="Draft contracts allocated"' not in page.text
    assert 'data-live-allocation-text' not in page.text
    assert 'aria-label="OCA layers workspace"' in page.text
    assert 'aria-label="Planned order actions"' in page.text
    assert 'id="draft-form"' in page.text
    assert 'data-live-input="target"' in page.text
    assert 'data-live-review-price="target-1"' in page.text
    assert 'data-live-metric="gain"' in page.text
    assert "Preview current draft" not in page.text
    assert "cdn.jsdelivr.net" not in page.text
    assert "api.iconify.design" not in page.text
    assert "@ibkr_options_manager/position" in page.text
    position_script = client.get("/_pkg/ibkr_options_manager/position.js")
    assert position_script.status_code == 200
    assert "cdn.jsdelivr.net" not in position_script.text
    floating_ui = client.get("/_pkg/ibkr_options_manager/floating-ui-dom.mjs")
    assert floating_ui.status_code == 200

    draft = page.text.split('id="draft-form"', maxsplit=1)[1].split(
        "</form>", maxsplit=1
    )[0]
    action_panel = page.text.split('aria-label="Planned order actions"', maxsplit=1)[1]
    assert action_panel.index("Outcome projection") < action_panel.index(
        "Execute paper order"
    )
    assert "data-outcome-projection" in action_panel
    assert "Expected gain" in action_panel
    assert "Max loss" in action_panel
    assert "Covered subtotal:" not in action_panel
    assert 'data-layer-state="draft"' in draft
    assert 'data-slot="card"' not in draft
    header = page.text.split('id="draft-form"', maxsplit=1)[0]
    assert 'data-live-allocation-bar' not in header
    assert 'data-live-allocation-text' not in header
    assert header.index("Split all available") < header.index("Split assigned")
    assert header.index("Split assigned") < header.index("Add Layer")
    assert 'aria-label="Split draft layer quantities"' in header
    split_trigger = re.search(
        r'<button[^>]*aria-label="Split draft layer quantities"[^>]*>', header
    )
    assert split_trigger is not None and re.search(
        r"\sdisabled(?:\s|>)", split_trigger.group()
    )
    assert 'aria-label="Create new OCA bracket"' in header
    assert 'form="draft-form" name="action" value="add-layer"' in header
    assert 'id="split-all-submit"' in draft
    assert 'id="split-assigned-submit"' in draft
    assert "Execute paper order" not in draft
    assert 'name="action" value="save-draft"' not in draft
    assert "font-mono" not in draft
    assert "text-xs font-semibold text-foreground" in draft
    assert "+$280.00 gain" in draft
    assert "-$340.00 max loss" in draft
    assert ">%</span>" in draft
    assert 'for="target_1"' in draft
    assert 'id="target_1"' in draft
    assert 'for="stop_1"' in draft
    assert 'id="stop_1"' in draft
    assert 'for="quantity_1"' in draft
    assert 'id="quantity_1"' in draft
    quantity_input = re.search(r'<input[^>]*id="quantity_1"[^>]*>', draft)
    assert quantity_input is not None
    assert 'min="1"' in quantity_input.group()
    assert 'max="5"' in quantity_input.group()
    assert 'for="tif_1"' in draft
    assert 'id="tif_1"' in draft
    assert "w-full min-w-[41rem]" in draft
    assert "items-start gap-3" in draft
    assert (
        "grid-cols-[5rem_minmax(10rem,1fr)_minmax(10rem,1fr)_minmax(5rem,0.6fr)_5rem_2.25rem]"
        in draft
    )
    assert 'aria-label="Draft layer rows"' in draft
    assert "overflow-x-auto overflow-y-hidden" in draft

    response = client.post(
        workbench.path + "action",
        data={
            "action": "add-layer",
            "target_presets": "20, 40, 60, 100",
            "stop_presets": "25",
            "target_1": "20",
            "stop_1": "25",
            "quantity_1": "5",
            "tif_1": "DAY",
        },
    )

    assert response.status_code == 200
    assert [layer.quantity for layer in workbench._current_layers()] == ["3", "2"]
    assert workbench._current_layers()[0].tif == "DAY"
    assert "DRAFT 2" in response.text
    split_trigger = re.search(
        r'<button[^>]*aria-label="Split draft layer quantities"[^>]*>',
        response.text,
    )
    assert split_trigger is not None and not re.search(
        r"\sdisabled(?:\s|>)", split_trigger.group()
    )

    response = client.post(
        workbench.path + "action",
        data={
            "action": "equal-split-assigned",
            "target_presets": "20, 40, 60, 100",
            "stop_presets": "25",
            "target_1": "20",
            "stop_1": "25",
            "quantity_1": "1",
            "target_2": "40",
            "stop_2": "25",
            "quantity_2": "2",
        },
    )

    assert response.status_code == 200
    assert [layer.quantity for layer in workbench._current_layers()] == ["2", "1"]

    response = client.post(
        workbench.path + "action",
        data={
            "action": "equal-split-available",
            "target_presets": "20, 40, 60, 100",
            "stop_presets": "25",
            "target_1": "20",
            "stop_1": "25",
            "quantity_1": "2",
            "target_2": "40",
            "stop_2": "25",
            "quantity_2": "1",
        },
    )

    assert response.status_code == 200
    assert [layer.quantity for layer in workbench._current_layers()] == ["3", "2"]

    response = client.post(
        workbench.path + "action",
        data={
            "action": "remove-layer:1",
            "target_presets": "20, 40, 60, 100",
            "stop_presets": "25",
            "target_1": "20",
            "stop_1": "25",
            "quantity_1": "3",
            "target_2": "40",
            "stop_2": "25",
            "quantity_2": "2",
        },
    )

    assert response.status_code == 200
    assert [layer.quantity for layer in workbench._current_layers()] == ["2"]

    response = client.post(
        workbench.path + "action",
        data={
            "action": "remove-layer:1",
            "target_1": "40",
            "stop_1": "25",
            "quantity_1": "2",
        },
    )
    assert response.status_code == 200
    assert workbench._current_layers() == ()
    assert 'aria-label="Remove layer 1"' not in response.text
    assert 'data-draft-empty-state' in response.text
    assert "Add a new layer" in response.text
    assert "Start a draft exit bracket for the available contracts." in response.text
    assert 'name="action" value="add-layer"' in response.text
    assert response.text.count('name="action" value="add-layer"') == 2
    assert "Add a layer or modify an existing one to continue." in response.text
    execute = re.search(r'<button[^>]*value="execute-arm"[^>]*>', response.text)
    assert execute is not None and re.search(r"\sdisabled(?:\s|>)", execute.group())

    selected = workbench._selected_con_id
    assert selected is not None
    client.post(
        workbench.path + "action",
        data={"action": "select", "con_id": str(selected)},
    )
    assert workbench._current_layers() == ()

    restored = client.post(workbench.path + "action", data={"action": "add-layer"})
    assert restored.status_code == 200
    assert len(workbench._current_layers()) == 1
    assert 'aria-label="Remove layer 1"' in restored.text
    assert 'data-draft-empty-state' not in restored.text


def test_paper_execution_control_submits_the_current_draft_form() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._paper_execution = _OwnedOrderService(set())

    page = TestClient(workbench.app).get(workbench.path)

    assert 'id="draft-form"' in page.text
    assert 'form="draft-form"' in page.text
    assert 'name="action" value="execute-arm"' in page.text
    execute = re.search(r'<button[^>]*value="execute-arm"[^>]*>', page.text)
    assert execute is not None and not re.search(
        r"\sdisabled(?:\s|>)", execute.group()
    )
    assert "Add a layer or modify an existing one to continue." not in page.text

    emptied = TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "remove-layer:1",
            "target_1": "20",
            "stop_1": "25",
            "quantity_1": "5",
        },
    )
    assert workbench._current_layers() == ()
    execute = re.search(r'<button[^>]*value="execute-arm"[^>]*>', emptied.text)
    assert execute is not None and re.search(r"\sdisabled(?:\s|>)", execute.group())


def test_demo_execution_brackets_unreserved_contracts_beside_external_order(
    tmp_path,
) -> None:
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

    journal = ExecutionJournal(tmp_path / "paper-journal.json")
    workbench = StarUIWorkbench(
        PlannerViewModel(snapshots, portfolio=portfolio, clock=clock),
        initial_account=DEMO_ACCOUNT,
        demo_mode=True,
        paper_execution=PaperExecutionService(
            DemoPaperExecutionTransport(),
            journal,
        ),
    )
    workbench.load_demo_data()
    selected = 1_001_500_251  # MSTR has an external SELL for 5 of 10 contracts.
    workbench._add_layer_locked()
    workbench._add_layer_locked()
    assert [layer.quantity for layer in workbench._current_layers()] == ["2", "2", "1"]
    client = TestClient(workbench.app)
    original_refresh = workbench._view_model.refresh_portfolio
    refresh_calls: list[object] = []

    def refreshed(settings: object) -> object:
        refresh_calls.append(settings)
        return original_refresh(settings)

    workbench._view_model.refresh_portfolio = refreshed  # type: ignore[method-assign]

    armed = client.post(
        workbench.path + "action",
        data={"action": "execute-arm"},
    )

    assert armed.status_code == 200
    assert ">Confirm<" in armed.text
    assert workbench._status_message.startswith("Fresh paper snapshot verified")
    assert workbench._toast is None

    submitted = client.post(
        workbench.path + "action",
        data={"action": "execute-confirm"},
    )

    assert submitted.status_code == 200
    assert "Orders sent to TWS" in submitted.text
    assert "The orders were sent to TWS." in submitted.text
    assert "Confirm or transmit them" in submitted.text
    assert "Refresh order status" in submitted.text
    assert 'data-submission-review' in submitted.text
    assert 'data-cancelled-bracket-recovery-dialog' not in submitted.text
    assert workbench._toast is None
    assert "Awaiting TWS verification" in submitted.text
    assert "VERIFY IN TWS" in submitted.text
    assert 'data-layer-state="verify"' in submitted.text
    assert "Waiting for TWS" not in submitted.text
    assert "One or more brackets need review in TWS" not in submitted.text
    assert 'value="execute-arm"' not in submitted.text
    _baseline, pending_projection, projection_config = workbench._projection_state()
    assert projection_config["unresolved"] is False
    assert projection_config["pendingQuantity"] == "5"
    assert pending_projection.covered_quantity == 5
    assert pending_projection.covered_gain is not None
    assert _money(pending_projection.covered_gain) in submitted.text
    assert _money(pending_projection.covered_loss) in submitted.text
    assert "The other 5 have existing orders" not in submitted.text
    assert workbench._status_message.endswith("TWS state refreshed.")
    assert len(refresh_calls) == 1

    refreshed = client.post(workbench.path + "action", data={"action": "refresh"})
    assert 'data-submission-review' not in refreshed.text

    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(),
        ExecutionJournal(tmp_path / "paper-journal.json"),
    )
    assert "Awaiting TWS verification" in client.get(workbench.path).text

    entry = journal.submission_entries(account=DEMO_ACCOUNT, con_id=selected)[0]
    assert len(entry.layers) == 3
    assert sum(layer.quantity for layer in entry.layers) == 5
    journal.mark_unknown(entry.fingerprint)
    unknown = client.get(workbench.path)
    assert 'data-layer-state="verify"' in unknown.text
    assert "Outcome not confirmed" in unknown.text
    assert 'value="execute-arm"' not in unknown.text


def test_unknown_submission_uses_guided_refresh_without_error_toast(
    tmp_path, monkeypatch
) -> None:
    def clock() -> Decimal:
        return Decimal("100")

    broker = DemoReadOnlyBroker(clock=clock, paper_execution_enabled=True)
    from ibkr_options_manager.app.view_model import PlannerViewModel

    service = PaperExecutionService(
        DemoPaperExecutionTransport(),
        ExecutionJournal(tmp_path / "paper-journal.json"),
    )
    workbench = StarUIWorkbench(
        PlannerViewModel(
            SnapshotCoordinator(broker, max_age_seconds=Decimal("15"), clock=clock),
            portfolio=PortfolioCoordinator(
                broker,
                max_age_seconds=Decimal("15"),
                clock=clock,
                paper_execution_mode=True,
            ),
            clock=clock,
        ),
        initial_account=DEMO_ACCOUNT,
        demo_mode=True,
        paper_execution=service,
    )
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    assert ">Confirm<" in client.post(
        workbench.path + "action", data={"action": "execute-arm"}
    ).text

    def uncertain(*args, **kwargs):
        raise ExecutionOutcomeUnknown("TWS did not acknowledge every order")

    monkeypatch.setattr(service, "submit", uncertain)
    response = client.post(
        workbench.path + "action", data={"action": "execute-confirm"}
    )

    assert response.status_code == 200
    assert "Orders sent to TWS" in response.text, workbench._status_message
    assert 'data-submission-review' in response.text
    assert 'data-cancelled-bracket-recovery-dialog' not in response.text
    assert workbench._toast is None


def test_order_status_check_closes_dialog_even_when_planning_stays_blocked(
    monkeypatch,
) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._submission_review_required = True
    calls = 0

    def refreshed() -> None:
        nonlocal calls
        calls += 1
        workbench._state = replace(workbench._state, status=UiStatus.BLOCKED)

    monkeypatch.setattr(workbench, "_refresh_locked", refreshed)
    client = TestClient(workbench.app)
    prompt = client.get(workbench.path).text
    assert "Refresh order status" in prompt
    assert "If you cancel the bracket" not in prompt

    response = client.post(workbench.path + "action", data={"action": "refresh"})

    assert calls == 1
    assert response.status_code == 200
    assert 'data-submission-review' not in response.text


def test_pending_three_contracts_leave_four_available_for_drafting(tmp_path) -> None:
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
            ExecutionJournal(tmp_path / "paper-journal.json"),
        ),
    )
    workbench.load_demo_data()
    selected = 1_002_100_161  # NVDA has 7 held and no related demo order.
    workbench._select_locked(selected)
    workbench._drafts[selected] = (
        replace(workbench._current_layers()[0], quantity="3"),
    )
    client = TestClient(workbench.app)

    assert ">Confirm<" in client.post(
        workbench.path + "action", data={"action": "execute-arm"}
    ).text
    submitted = client.post(
        workbench.path + "action", data={"action": "execute-confirm"}
    )

    assert submitted.status_code == 200
    assert workbench._planning_available_quantity() == 4
    assert workbench._current_layers() == ()
    assert "Available" in submitted.text
    assert "Available to plan" not in submitted.text
    assert "VERIFY IN TWS" in submitted.text
    assert 'id="verify-quantity-1"' in submitted.text
    assert 'value="cancel-pair-arm:' not in submitted.text
    assert 'value="add-layer"' in submitted.text
    assert 'value="execute-arm"' not in submitted.text
    pending_execute = re.search(
        r'<button[^>]*disabled[^>]*>\s*Execute paper order\s*</button>',
        submitted.text,
    )
    assert pending_execute is not None
    assert "Add a layer or modify an existing one to continue." in submitted.text
    assert "One or more brackets need review in TWS" not in submitted.text
    _baseline, pending_projection, config = workbench._projection_state()
    assert config["unresolved"] is False
    assert config["pendingQuantity"] == "3"
    assert pending_projection.covered_quantity == 3
    assert pending_projection.covered_gain is not None
    assert "Includes 3 contracts awaiting TWS verification" not in submitted.text

    client.post(workbench.path + "action", data={"action": "add-layer"})
    assert [layer.quantity for layer in workbench._current_layers()] == ["4"]
    assert [layer.target_percentage for layer in workbench._current_layers()] == ["40"]
    _baseline, projected, config = workbench._projection_state()
    assert config["unresolved"] is False
    assert config["pendingQuantity"] == "3"
    assert projected.covered_quantity == 7
    assert projected.expected_gain is not None
    assert projected.max_loss is not None
    projected_page = client.get(workbench.path).text
    assert _money(projected.expected_gain) in projected_page
    assert _money(projected.max_loss) in projected_page
    assert "Includes 3 contracts awaiting TWS verification" not in projected_page
    client.post(workbench.path + "action", data={"action": "add-layer"})
    assert [layer.quantity for layer in workbench._current_layers()] == ["2", "2"]
    assert [layer.target_percentage for layer in workbench._current_layers()] == [
        "40", "60"
    ]
    client.post(workbench.path + "action", data={"action": "add-layer"})
    repeated = client.post(workbench.path + "action", data={"action": "add-layer"})
    assert repeated.status_code == 200
    assert [layer.target_percentage for layer in workbench._current_layers()] == [
        "40", "60", "100", "100"
    ]
    assert [layer.stop_percentage for layer in workbench._current_layers()] == [
        "25", "25", "25", "25"
    ]
    assert "add a higher LMT target preset" not in repeated.text

    blocked = client.post(
        workbench.path + "action", data={"action": "execute-arm"}
    )
    assert blocked.status_code == 200
    assert workbench._message.startswith("Review pending brackets in TWS and Refresh")
    assert workbench._armed_execution is None


def test_next_draft_target_uses_pending_limit_price() -> None:
    bands = (PriceBand(Decimal("0"), Decimal("0.05")),)
    presets = (Decimal("20"), Decimal("40"), Decimal("60"))
    assert _next_target_preset_above(
        (Decimal("5.90"),),
        basis=Decimal("4.20"),
        bands=bands,
        presets=presets,
    ) == Decimal("60")
    assert _next_target_preset_above(
        (Decimal("6.75"),), basis=Decimal("4.20"), bands=bands, presets=presets
    ) is None


@pytest.mark.parametrize("new_remaining", ["4", "6"])
def test_external_order_change_before_confirmation_blocks_demo_submission(
    tmp_path, new_remaining: str,
) -> None:
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

    journal = ExecutionJournal(tmp_path / "paper-journal.json")
    workbench = StarUIWorkbench(
        PlannerViewModel(snapshots, portfolio=portfolio, clock=clock),
        initial_account=DEMO_ACCOUNT,
        demo_mode=True,
        paper_execution=PaperExecutionService(DemoPaperExecutionTransport(), journal),
    )
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    armed = client.post(workbench.path + "action", data={"action": "execute-arm"})
    assert ">Confirm<" in armed.text

    original_capture = broker.capture

    def changed_capture(request):
        capture = original_capture(request)
        return replace(
            capture,
            orders=tuple(
                replace(order, remaining=Decimal(new_remaining))
                for order in capture.orders
            ),
        )

    broker.capture = changed_capture  # type: ignore[method-assign]
    confirmed = client.post(
        workbench.path + "action", data={"action": "execute-confirm"}
    )

    assert any(
        word in (workbench._message or "").lower()
        for word in ("blocked", "changed")
    ), workbench._message
    assert journal.submission_entries(account=DEMO_ACCOUNT, con_id=1_001_500_251) == ()


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
    server, server_thread, port = _start_local_server(workbench.app)
    application = QApplication.instance() or QApplication([])
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
        application.quit()

    def javascript(script: str, callback) -> None:
        view.page().runJavaScript(script, callback)

    def click_button(label: str) -> None:
        javascript(
            """
            (() => {
              const button = Array.from(document.querySelectorAll('button'))
                .find((candidate) => candidate.textContent.trim() === $LABEL);
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
        QTimer.singleShot(100, lambda: click_button("Add Layer"))

    def inspect_acknowledgement(visible: object) -> None:
        result["acknowledgement_visible"] = bool(visible)
        click_button("Refresh")

    def inspect_page(text: object) -> None:
        nonlocal phase
        body = str(text)
        if phase == 1:
            if "DRAFT 2" not in body:
                finish("Add Layer did not render a second layer")
                return
            result["add_layer"] = True
            phase = 2
            click_button("Execute paper order")
        elif phase == 2:
            if "Confirm" not in body:
                finish("Execute paper order did not arm its confirmation")
                return
            phase = 3
            click_button("Confirm")
        elif phase == 3:
            if "Bracket orders sent to TWS" not in body:
                finish("Paper execution did not render its acknowledgement")
                return
            result["execute_arm"] = True
            phase = 4
            result["execute_confirm"] = True
            javascript(
                """(() => {
                  const toast = Array.from(document.querySelectorAll('[role="status"]'))
                    .find((node) => node.textContent.includes('Bracket orders sent to TWS')
                      && node.getBoundingClientRect().height > 0
                      && getComputedStyle(node).display !== 'none');
                  return !!toast;
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
                    QTest.mouseClick(
                        view.focusProxy() or view, Qt.MouseButton.LeftButton,
                        pos=QPoint(*map(int, json.loads(point))),
                    ),
                    QTimer.singleShot(
                        250,
                        lambda: javascript(
                            """(() => {
                              const dialog = document.getElementById(
                                'connection_settings');
                              return dialog?.open === true
                                && dialog.getBoundingClientRect().height > 100
                                && getComputedStyle(dialog).visibility === 'visible';
                            })();""",
                            inspect_settings,
                        ),
                    ),
                ) if point else finish(f"Settings was not clickable: {point!r}"),
            )
            return
        QTimer.singleShot(
            150, lambda: javascript("document.body.innerText", inspect_page)
        )

    view.loadFinished.connect(loaded)
    view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
    QTimer.singleShot(10_000, lambda: finish("Timed out waiting for browser controls"))
    application.exec()

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


def _demo_workbench() -> StarUIWorkbench:
    def clock() -> Decimal:
        return Decimal("100")

    broker = DemoReadOnlyBroker(clock=clock)
    snapshots = SnapshotCoordinator(broker, max_age_seconds=Decimal("15"), clock=clock)
    portfolio = PortfolioCoordinator(broker, max_age_seconds=Decimal("15"), clock=clock)
    from ibkr_options_manager.app.view_model import PlannerViewModel

    return StarUIWorkbench(
        PlannerViewModel(snapshots, portfolio=portfolio, clock=clock),
        initial_account=DEMO_ACCOUNT,
        demo_mode=True,
    )


class _WindowStub:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.demo_loaded = False
        self.launch_refresh_requested = False

    def show(self) -> None:
        pass

    def load_demo_data(self) -> None:
        self.demo_loaded = True

    def refresh_on_launch(self) -> None:
        self.launch_refresh_requested = True
        self.load_demo_data()


class _OwnedOrderService:
    def __init__(self, perm_ids: set[int]) -> None:
        self._perm_ids = perm_ids

    def owned_perm_ids(self, *, account: str, con_id: int) -> frozenset[int]:
        del account, con_id
        return frozenset(self._perm_ids)
