import json
import os
import re
from dataclasses import replace
from decimal import Decimal
from threading import Event, Thread
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")

from httpx import Response
from PySide6.QtCore import QTimer, QUrl
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
    _live_active_script,
    _position_identity,
    _toast_notice,
)
from ibkr_options_manager.app.web_window import StarUIPlannerWindow, _start_local_server
from ibkr_options_manager.broker import PortfolioRequest, SnapshotRequest
from ibkr_options_manager.broker.execution import PaperSubmission
from ibkr_options_manager.domain import PriceBand, WorkingOrder
from ibkr_options_manager.execution import (
    ExecutionJournal,
    JournalEntry,
    JournalFill,
    JournalLayer,
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

    assert "Execution blocked" in page.text
    assert "Build and manage app-owned OCA layers." in page.text
    assert "Dismiss toast" in page.text
    # Action rerenders must replace an already-hydrated empty toast signal.
    assert "data-signals='{toasts:" in page.text
    assert "data-signals:toasts__ifmissing" in page.text
    assert "document.startViewTransition" not in page.text


def test_tws_connection_toast_has_a_short_recovery_message() -> None:
    notice = _toast_notice(
        "Portfolio state is not ready: missing completion barriers: positions"
    )

    assert notice.title == "Could not connect to TWS"
    assert notice.description == "Make sure TWS is open and try again."
    assert notice.variant == "error"


def test_busy_submit_only_applies_to_explicitly_async_controls() -> None:
    script = _busy_submit_script()

    assert "const text = button.dataset.busyText;" in script
    assert "if (!text) return;" in script
    assert "|| 'Working…'" not in script
    assert "button.disabled = true" not in script
    assert "button.style.pointerEvents = 'none';" in script
    assert "form.dataset.ibkrSubmitting" in script


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


def test_reconciled_app_orders_are_not_described_as_external_coverage() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._paper_execution = _OwnedOrderService({496_248_334})
    client = TestClient(workbench.app)

    page = client.get(workbench.path)

    assert "App-managed OCA coverage active" in page.text
    assert "1 app-created orders were reconciled with TWS" in page.text
    assert "5 contracts remain available for a new bracket" in page.text
    assert "Associated external orders remain inspect-only" not in page.text


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


def test_empty_draft_rehydrates_when_a_refresh_frees_contracts() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = workbench._selected_con_id
    assert con_id is not None

    workbench._drafts[con_id] = ()
    workbench._ensure_draft_locked()

    assert len(workbench._current_layers()) == 1
    assert workbench._current_layers()[0].quantity == "5"


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
                remaining="4",
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
                remaining="4",
                status="Submitted",
                oca_group="3ad441753bb9/tranche-1",
                stop_price=Decimal("16.40"),
                tif="GTC",
            ),
        ),
    )
    client = TestClient(workbench.app)

    page = client.get(workbench.path)

    assert "Layered OCA draft" in page.text
    assert 'aria-label="OCA layers workspace"' in page.text
    assert 'aria-label="Active OCA layer rows"' in page.text
    assert "OCA-1" in page.text
    assert "$26.20" in page.text
    assert "$16.40" in page.text
    assert "Move stop to B/E" in page.text
    assert "Update layers" not in page.text
    assert "Close all" in page.text
    assert "New bracket layers" in page.text
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
    assert "Layered OCA draft" in page.text
    assert 'aria-current="page"' not in page.text
    assert 'data-active-initial="' in page.text
    assert 'data-live-price="active-target-1"' in page.text
    assert 'data-live-outcome="active-target-1"' in page.text
    assert page.text.index("Active OCA layers") < page.text.index("New bracket layers")
    assert page.text.index("New bracket layers") < page.text.index("Layered OCA draft")
    assert "const targetEdited" in page.text
    active_script = _live_active_script(
        {"basis": "1", "multiplier": "100", "bands": []}
    )
    assert "setHidden(row, !(targetChanged || stopChanged), 'block')" in active_script
    assert "setReviewMode(changed)" in active_script
    assert "UPDATE SELL LMT" in page.text


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

    assert "App-managed OCA coverage active" in page.text
    assert "4 contracts remain available for a new bracket" in page.text
    assert "Existing order coverage detected" not in page.text
    assert "Active OCA layers" in page.text
    assert "OCA-1" in page.text
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
                    JournalLayer(3, "15.50", "9.70", "GTC", 201, 202),
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

    page = TestClient(workbench.app).get(workbench.path)

    assert "Closed bracket history" in page.text
    assert "Profit" in page.text
    assert "USD +557.44" in page.text
    assert "Target filled" in page.text
    assert "Active OCA layers" in page.text
    assert "pending TWS verification" not in page.text
    assert "TWS orders are pending verification" not in page.text


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
    assert ">Cancel<" in sidebar
    assert ">Confirm<" in sidebar
    assert "Click to confirm" not in sidebar


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
    assert ">Confirm<" in sidebar
    assert ">Cancel<" in sidebar
    assert "Wait for both cancellation confirmations" not in sidebar
    assert "Selected app-owned OCA layer" not in sidebar
    assert "GTC" in sidebar

    cancelled = client.post(workbench.path + "action", data={"action": "cancel-staged"})

    assert workbench._armed_market_exits == ()
    assert "Staged action cancelled. No orders were sent to TWS." in cancelled.text


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
    assert ">Confirm<" in sidebar
    assert ">Cancel<" in sidebar


def test_starui_workbench_renders_and_adds_a_layer_from_a_server_owned_form() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)

    page = client.get(workbench.path)
    assert page.status_code == 200
    assert "Layered OCA draft" in page.text
    assert "Last refreshed " in page.text
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
    assert 'aria-label="Draft layer rows"' in page.text
    assert 'aria-label="OCA layers workspace"' in page.text
    assert 'aria-label="Planned order actions"' in page.text
    assert 'id="draft-form"' in page.text
    assert 'data-live-input="target"' in page.text
    assert 'data-live-review-price="target-1"' in page.text
    assert 'data-live-metric="gain"' in page.text
    assert "Preview current draft" not in page.text
    assert "cdn.jsdelivr.net" not in page.text
    assert "api.iconify.design" not in page.text
    assert "@starhtml/plugins/position" in page.text
    assert client.get("/_pkg/starhtml/plugins/position.js").status_code == 200

    draft = page.text.split("Layered OCA draft", maxsplit=1)[1].split(
        "</form>", maxsplit=1
    )[0]
    action_panel = page.text.split('aria-label="Planned order actions"', maxsplit=1)[1]
    assert action_panel.index("Execute paper order") < action_panel.index(
        "Outcome projection"
    )
    assert "data-draft-outcome" in action_panel
    assert 'data-slot="card-action"' in draft
    assert "Split all available" in draft
    assert "Split assigned" in draft
    assert "Add layer" in draft
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

    assert "mt-5" in draft

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
    assert "LAYER 2" in response.text

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


def test_paper_execution_control_submits_the_current_draft_form() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._paper_execution = _OwnedOrderService(set())

    page = TestClient(workbench.app).get(workbench.path)

    assert 'id="draft-form"' in page.text
    assert 'form="draft-form"' in page.text
    assert 'name="action" value="execute-arm"' in page.text
    assert "Add a layer or modify an existing one to continue." not in page.text


def test_demo_execution_uses_the_same_two_click_flow_without_contacting_tws(
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
    workbench._select_locked(1_002_100_161)  # NVDA has no associated demo order.
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
    assert "Fresh paper snapshot verified" in armed.text

    submitted = client.post(
        workbench.path + "action",
        data={"action": "execute-confirm"},
    )

    assert submitted.status_code == 200
    assert "Orders sent to TWS" in submitted.text
    assert "2 orders acknowledged" in submitted.text
    assert "Active layers · pending TWS verification" in submitted.text
    assert "SELL LMT" in submitted.text
    assert "Waiting for TWS" in submitted.text
    assert 'value="execute-arm"' not in submitted.text
    assert workbench._status_message.endswith("TWS state refreshed.")
    assert len(refresh_calls) == 1

    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(),
        ExecutionJournal(tmp_path / "paper-journal.json"),
    )
    assert "Active layers · pending TWS verification" in client.get(workbench.path).text

    entry = journal.submission_entries(account=DEMO_ACCOUNT, con_id=1_002_100_161)[0]
    journal.mark_unknown(entry.fingerprint)
    unknown = client.get(workbench.path)
    assert "Active layers · TWS outcome unknown" in unknown.text
    assert "Outcome not confirmed" in unknown.text
    assert 'value="execute-arm"' not in unknown.text


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
        QTimer.singleShot(100, lambda: click_button("Add layer"))

    def inspect_acknowledgement(visible: object) -> None:
        result["acknowledgement_visible"] = bool(visible)
        click_button("Refresh")

    def inspect_page(text: object) -> None:
        nonlocal phase
        body = str(text)
        if phase == 1:
            if "LAYER 2" not in body:
                finish("Add layer did not render a second layer")
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
            if "Orders sent to TWS" not in body:
                finish("Paper execution did not render its acknowledgement")
                return
            result["execute_arm"] = True
            phase = 4
            result["execute_confirm"] = True
            javascript(
                """(() => {
                  const toast = Array.from(document.querySelectorAll('[role="status"]'))
                    .find((node) => node.textContent.includes('Orders sent to TWS')
                      && node.getBoundingClientRect().height > 0
                      && getComputedStyle(node).display !== 'none');
                  return !!toast;
                })();""",
                inspect_acknowledgement,
            )
        elif phase == 4:
            if "Active layers · pending TWS verification" not in body:
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
                  trigger.click();
                  return document.getElementById('connection_settings')?.open === true;
                })();
                """,
                inspect_settings,
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
