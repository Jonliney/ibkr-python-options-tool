import asyncio
import json
import os
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace

import pytest


def test_bundled_starui_css_styles_selected_toggle_group_items() -> None:
    css = (
        Path(__file__).resolve().parents[1]
        / "src/ibkr_options_manager/app/web/static/starui.css"
    ).read_text()
    assert "data-\\[state\\=on\\]\\:bg-accent[data-state=on]" in css
    assert "first\\:rounded-l-md:first-child" in css
    assert "last\\:rounded-r-md:last-child" in css


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")

from httpx import Response
from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication
from starhtml import to_xml
from starlette.testclient import TestClient

from ibkr_options_manager.app.demo import (
    DEMO_ACCOUNT,
    DEMO_CON_IDS,
    DemoPaperExecutionTransport,
    DemoReadOnlyBroker,
    seed_demo_journal,
)
from ibkr_options_manager.app.main import build_parser, main
from ibkr_options_manager.app.view_model import PlanForm, UiStatus, ValidationLine
from ibkr_options_manager.app.web import StarUIWorkbench
from ibkr_options_manager.app.web.surface import (
    _active_percentage_for_price,
    _busy_submit_script,
    _contract_display_name,
    _edited_active_price,
    _live_active_script,
    _money,
    _next_target_preset_above,
    _position_identity,
    _price_update_fills_verified,
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
from ibkr_options_manager.domain import ObservedExecution, PriceBand, WorkingOrder
from ibkr_options_manager.execution import (
    ExecutionBlocked,
    ExecutionJournal,
    ExecutionOutcomeUnknown,
    JournalEntry,
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


def test_closed_position_remains_read_only_for_current_session() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    closed_id = workbench._selected_con_id
    assert closed_id is not None
    assert len(workbench._state.positions) > 1
    remaining = tuple(p for p in workbench._state.positions if p.con_id != closed_id)
    workbench._apply_refreshed_portfolio_locked(
        replace(workbench._state, positions=remaining, selected_con_id=None)
    )

    assert workbench._selected_closed_con_id == closed_id
    page = to_xml(workbench._page())
    assert "CLOSED THIS SESSION" in page
    assert "Closed this session" not in page
    assert (
        "workspace-content flex min-w-0 min-h-0 flex-col overflow-hidden px-8 py-6"
        in page
    )
    assert re.search(r"<button[^>]*disabled[^>]*>.*Add Layer</button>", page)
    assert "grid-cols-[16rem_minmax(0,1fr)_19rem]" in page
    assert "Held / total" in page
    assert "ACTION REVIEW" in page
    assert "Review order" in page
    assert re.search(r"<button[^>]*disabled[^>]*>Review order</button>", page)
    assert "data-closed-session" in page
    assert 'value="select-session-closed"' in page
    assert 'value="price-update-confirm"' not in page

    class History:
        def submission_entries(self, **_kwargs):
            return (
                JournalEntry(
                    fingerprint="a" * 64,
                    account=workbench._settings.account,
                    con_id=closed_id,
                    state="RECONCILED",
                    layers=(
                        JournalLayer(
                            1,
                            "12.10",
                            "8.80",
                            "GTC",
                            201,
                            202,
                            target_percentage="2",
                            stop_percentage="-25",
                        ),
                    ),
                ),
            )

    workbench._paper_execution = History()  # type: ignore[assignment]
    history_page = to_xml(workbench._page())
    assert "VERIFY IN TWS" in history_page
    assert "$12.10" in history_page
    assert 'value="2"' in history_page

    workbench._settings = replace(workbench._settings, account="DU_OTHER")
    workbench._record_verified_positions_locked(
        replace(workbench._state, positions=remaining)
    )
    assert not workbench._session_closed_positions


def test_closed_session_hides_cancelled_brackets_and_keeps_verified_pnl() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = workbench._selected_con_id
    assert con_id is not None
    workbench._session_closed_positions[con_id] = next(
        position for position in workbench._state.positions if position.con_id == con_id
    )
    workbench._selected_closed_con_id = con_id
    workbench._selected_con_id = None
    closed = JournalEntry(
        fingerprint="a" * 64,
        account=workbench._settings.account,
        con_id=con_id,
        state="RECONCILED",
        layers=(JournalLayer(1, "20", "10", "GTC", 201, 202),),
        fills=(JournalFill("fill.01", 201, "SLD", "1", "20", "now", "125", "USD"),),
    )
    cancelled = JournalEntry(
        fingerprint="b" * 64,
        account=workbench._settings.account,
        con_id=con_id,
        state="RECONCILED",
        layers=(JournalLayer(1, "25", "10", "GTC", 203, 204, cancelled=True),),
    )

    class History:
        def submission_entries(self, **_kwargs):
            return (closed, cancelled)

    workbench._paper_execution = History()  # type: ignore[assignment]
    page = to_xml(workbench._page())
    assert "+$125.00" in page
    assert "oca-layer-list" in page
    assert "Bracket cancelled" not in page
    assert "Awaiting TWS review" not in page


def test_closed_history_refresh_records_exact_broker_evidence(monkeypatch) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = workbench._selected_con_id
    assert con_id is not None
    baseline = workbench._view_model.latest_snapshot()
    assert baseline is not None
    workbench._session_contract_snapshots[con_id] = baseline
    workbench._session_closed_positions[con_id] = next(
        position for position in workbench._state.positions if position.con_id == con_id
    )
    calls = []

    class History:
        def record_completed_orders(self, snapshot):
            calls.append(("orders", snapshot.complete, snapshot.selected.con_id))

        def record_executions(self, snapshot):
            calls.append(("fills", snapshot.complete, snapshot.selected.con_id))

    workbench._paper_execution = History()  # type: ignore[assignment]
    monkeypatch.setattr(
        workbench._view_model,
        "refresh_closed_history",
        lambda _settings, _baseline: replace(baseline, complete=False, fresh=False),
    )
    workbench._refresh_closed_history_locked()
    assert calls == [("orders", False, con_id), ("fills", False, con_id)]


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


def test_existing_exit_order_explanation_opens_from_available_metric() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()

    page = TestClient(workbench.app).get(workbench.path).text
    assert 'aria-label="Why are fewer contracts available?"' in page
    assert 'd="m21.73 18l-8-14' in page
    assert 'id="existing_exit_orders"' in page
    assert "5 contracts already have exit orders in TWS." in page
    assert "Orders placed outside this app are view-only here." in page
    assert page.index("Available") < page.index("Existing TWS exit orders")
    assert page.index("Existing TWS exit orders") < page.index("Average price")
    assert "border-amber-500/40 bg-amber-500/10 text-amber-100" not in page

    workbench._select_locked(1_002_100_161)
    no_external_orders = TestClient(workbench.app).get(workbench.path).text
    assert 'aria-label="Why are fewer contracts available?"' not in no_external_orders


def test_seeded_nvda_demo_bracket_can_be_verified_absent_without_reseeding(
    tmp_path,
) -> None:
    def clock() -> Decimal:
        return Decimal("100")

    from ibkr_options_manager.app.view_model import PlannerViewModel

    path = tmp_path / "demo-execution-journal.json"
    journal = seed_demo_journal(path)
    broker = DemoReadOnlyBroker(clock=clock, paper_execution_enabled=True)
    broker.use_journal(journal)
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
        initial_con_id=1_002_100_161,
        demo_mode=True,
        paper_execution=PaperExecutionService(
            DemoPaperExecutionTransport(journal),
            journal,
        ),
    )
    workbench.load_demo_data()
    assert workbench._selected_con_id == 1_002_100_161
    assert workbench._planning_available_quantity() == 4
    client = TestClient(workbench.app)
    page = client.get(workbench.path).text
    assert "Awaiting TWS review" in page
    assert 'aria-label="Verify bracket status of layer 1"' in page
    assert "data-cancelled-bracket-recovery-dialog" not in page

    entry = journal.submission_entries(
        account=DEMO_ACCOUNT,
        con_id=1_002_100_161,
    )[0]
    resolved = client.post(
        workbench.path + "action",
        data={
            "action": "resolve-cancelled-bracket",
            "confirmed": "yes",
            "fingerprint": entry.fingerprint,
        },
    )
    assert resolved.status_code == 200
    assert journal.find(entry.fingerprint).state == "CANCELLED_CONFIRMED"
    assert workbench._planning_available_quantity() == 7
    seed_demo_journal(path)
    assert (
        journal.submission_entries(
            account=DEMO_ACCOUNT,
            con_id=1_002_100_161,
        )
        == ()
    )


def test_nvda_verification_example_is_added_once_to_existing_demo_journal(
    tmp_path,
) -> None:
    path = tmp_path / "demo-execution-journal.json"
    journal = ExecutionJournal(path)
    existing = JournalEntry(
        fingerprint="a" * 64,
        account=DEMO_ACCOUNT,
        con_id=1_004_470_201,
        state="CANCELLED_CONFIRMED",
    )
    journal._write((existing,))

    seed_demo_journal(path)
    first = journal._entries()
    assert len(first) == 2
    assert first[0] == existing
    assert first[1].con_id == 1_002_100_161
    assert first[1].layers[0].quantity == 3

    seed_demo_journal(path)
    assert journal._entries() == first


def test_observed_new_position_updates_sidebar_without_changing_selection() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    original_selection = workbench._selected_con_id
    original_drafts = dict(workbench._drafts)
    new_position = replace(
        workbench._state.positions[0],
        con_id=987654321,
        local_symbol="NEW  261016C07000000",
    )
    workbench._observe_positions = True
    workbench._observer_generation = 1

    def refresh(
        *, auto_select: bool = True, preserve_invalid_drafts: bool = False
    ) -> None:
        del auto_select, preserve_invalid_drafts
        state = replace(
            workbench._state,
            positions=(*workbench._state.positions, new_position),
        )
        workbench._record_verified_positions_locked(state)
        workbench._state = state

    workbench._refresh_locked = refresh  # type: ignore[method-assign]
    worker = Thread(target=workbench._observation_loop, daemon=True)
    worker.start()
    try:
        workbench._position_hint(0)
        assert workbench._pending_observation is False
        workbench._position_hint(1)
        for _ in range(100):
            if 987654321 in workbench._position_changes.new_ids:
                break
            Event().wait(0.01)
        assert 987654321 in workbench._position_changes.new_ids
        assert workbench._selected_con_id == original_selection
        assert workbench._drafts == original_drafts
        assert workbench._observation_requires_reload is False
        fragment = TestClient(workbench.app).get(workbench.path + "inventory-fragment")
        assert fragment.status_code == 200
        assert "NEW" in fragment.text
        assert (
            'data-position-name class="flex min-w-0 items-center gap-1.5"'
            in fragment.text
        )
        assert "data-new-position" in fragment.text
        assert "new-position-badge" in fragment.text
        assert "color: #f2c14e" in TestClient(workbench.app).get("/layers.css").text
        assert fragment.headers["X-Selected-Changed"] == "0"
    finally:
        workbench.close()
        worker.join(timeout=1)


def test_first_observed_position_is_selected_and_reloads_empty_workbench() -> None:
    source = _demo_workbench()
    source.load_demo_data()
    position = source._state.positions[0]
    ready = replace(source._state, positions=(position,), selected_con_id=None)
    workbench = _demo_workbench()
    workbench._observe_positions = True
    workbench._observer_generation = 1
    workbench._view_model.refresh_portfolio = lambda _settings: ready  # type: ignore[method-assign]
    workbench._view_model.select_position = (  # type: ignore[method-assign]
        lambda con_id, _form: replace(ready, selected_con_id=con_id)
    )

    worker = Thread(target=workbench._observation_loop, daemon=True)
    worker.start()
    try:
        workbench._position_hint(1)
        for _ in range(100):
            if workbench._inventory_revision:
                break
            Event().wait(0.01)
        assert workbench._inventory_revision > 0
        assert workbench._selected_con_id == position.con_id
        fragment = TestClient(workbench.app).get(workbench.path + "inventory-fragment")
        assert fragment.headers["X-Selected-Changed"] == "1"
        page = TestClient(workbench.app).get(workbench.path).text
        assert "Select an option position" not in page
    finally:
        workbench.close()
        worker.join(timeout=1)


@pytest.mark.parametrize("multiple", [False, True])
def test_empty_workbench_does_not_auto_select_ambiguous_or_unverified_arrival(
    multiple: bool,
) -> None:
    source = _demo_workbench()
    source.load_demo_data()
    positions = (
        source._state.positions[:2]
        if multiple
        else (replace(source._state.positions[0], eligible=False),)
    )
    workbench = _demo_workbench()
    workbench._view_model.select_position = (  # type: ignore[method-assign]
        lambda *_args: pytest.fail("arrival must not be selected")
    )

    workbench._apply_refreshed_portfolio_locked(
        replace(source._state, positions=positions, selected_con_id=None),
        auto_select=False,
    )

    assert workbench._selected_con_id is None


def test_selected_position_quantity_change_offers_update_without_losing_draft() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._build_draft_locked()
    workbench._observe_positions = True
    original = workbench._state
    con_id = workbench._selected_con_id
    assert con_id is not None
    initial = next(
        position for position in original.positions if position.con_id == con_id
    )
    initial_quantity = int(Decimal(initial.quantity))
    draft = workbench._current_layers()
    assert draft

    def publish(quantity: int, *, blocked: bool = False) -> None:
        positions = tuple(
            replace(position, quantity=str(quantity))
            if position.con_id == con_id
            else position
            for position in original.positions
        )
        workbench._view_model.select_position = (  # type: ignore[method-assign]
            lambda selected, _form: replace(
                original,
                status=UiStatus.BLOCKED if blocked else UiStatus.READY,
                positions=positions,
                selected_con_id=selected,
            )
        )
        workbench._apply_refreshed_portfolio_locked(
            replace(original, positions=positions, selected_con_id=None),
            auto_select=False,
            preserve_invalid_drafts=True,
        )

    publish(initial_quantity + 6)
    client = TestClient(workbench.app)
    fragment = client.get(workbench.path + "inventory-fragment")
    assert fragment.headers["X-Selected-Quantity-Change"] == "6"
    page = client.get(workbench.path)
    assert "6 new contracts were added to this position" in page.text
    assert 'id="position-change-update"' in page.text
    notice = re.search(r'<div[^>]*id="selected-quantity-notice"[^>]*>', page.text)
    assert notice is not None and " hidden" not in notice.group()
    assert workbench._current_layers() == draft

    workbench._state = replace(workbench._state, status=UiStatus.STALE)
    publish(initial_quantity - 2, blocked=True)
    fragment = client.get(workbench.path + "inventory-fragment")
    assert fragment.headers["X-Selected-Quantity-Change"] == "-2"
    page = client.get(workbench.path)
    assert "2 contracts were removed from this position" in page.text
    assert workbench._current_layers() == draft

    publish(initial_quantity - 2, blocked=True)
    assert (
        client.get(workbench.path + "inventory-fragment").headers[
            "X-Selected-Quantity-Change"
        ]
        == "-2"
    )

    acknowledged = client.post(
        workbench.path + "action",
        data={"action": "acknowledge-position-change", "quantity_1": "99"},
    )
    assert acknowledged.status_code == 200
    assert workbench._position_changes.selected_change is None
    assert workbench._current_layers() == draft


def test_observed_quantity_decrease_does_not_reload_or_discard_invalid_draft() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    original = workbench._state
    con_id = workbench._selected_con_id
    assert con_id is not None
    draft = workbench._current_layers()
    original_position = next(
        position for position in original.positions if position.con_id == con_id
    )
    positions = tuple(
        replace(position, quantity="1") if position.con_id == con_id else position
        for position in original.positions
    )
    assert int(Decimal(original_position.quantity)) > 1
    workbench._view_model.refresh_portfolio = (  # type: ignore[method-assign]
        lambda _settings: replace(original, positions=positions, selected_con_id=None)
    )
    workbench._view_model.select_position = (  # type: ignore[method-assign]
        lambda selected, _form: replace(
            original,
            status=UiStatus.BLOCKED,
            positions=positions,
            selected_con_id=selected,
            available_quantity=1,
            validations=(
                ValidationLine(
                    "LAYER_QUANTITY_EXCEEDS_AVAILABLE",
                    "Draft quantity exceeds the verified available quantity.",
                ),
            ),
        )
    )
    workbench._observe_positions = True
    workbench._observer_generation = 1
    worker = Thread(target=workbench._observation_loop, daemon=True)
    worker.start()
    try:
        workbench._position_hint(1)
        for _ in range(100):
            if workbench._inventory_revision:
                break
            Event().wait(0.01)
        assert workbench._inventory_revision > 0
        assert workbench._observation_requires_reload is False
        assert workbench._current_layers() == draft
        assert workbench._position_changes.selected_change == (
            con_id,
            int(Decimal(original_position.quantity)),
            1,
        )
    finally:
        workbench.close()
        worker.join(timeout=1)


def test_position_becomes_new_when_contract_verification_completes() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._observe_positions = True
    workbench._observer_generation = 1
    verified = replace(
        workbench._state.positions[0],
        con_id=987654321,
        local_symbol="SPX  261016P07000000",
    )
    unresolved = replace(verified, eligible=False, eligibility="Unverified contract")
    updates = [unresolved, verified]

    def refresh(
        *, auto_select: bool = True, preserve_invalid_drafts: bool = False
    ) -> None:
        del auto_select, preserve_invalid_drafts
        update = updates.pop(0)
        positions = [
            position
            for position in workbench._state.positions
            if position.con_id != update.con_id
        ]
        positions.insert(1, update)
        state = replace(workbench._state, positions=tuple(positions))
        workbench._record_verified_positions_locked(state)
        workbench._state = state

    workbench._refresh_locked = refresh  # type: ignore[method-assign]
    worker = Thread(target=workbench._observation_loop, daemon=True)
    worker.start()
    try:
        for expected_revision in (1, 2):
            workbench._position_hint(1)
            for _ in range(100):
                if workbench._inventory_revision >= expected_revision:
                    break
                Event().wait(0.01)
            assert workbench._inventory_revision >= expected_revision
        assert 987654321 in workbench._position_changes.new_ids
    finally:
        workbench.close()
        worker.join(timeout=1)


def test_reconnect_does_not_hide_a_new_verified_position() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._observe_positions = True
    workbench._observer_generation = 1
    workbench._observer_health = "disconnected"
    workbench._observer_retry_at = float("inf")
    new_position = replace(
        workbench._state.positions[0],
        con_id=987654321,
        local_symbol="SPX  261016P07000000",
    )

    def refresh(
        *, auto_select: bool = True, preserve_invalid_drafts: bool = False
    ) -> None:
        del auto_select, preserve_invalid_drafts
        state = replace(
            workbench._state,
            positions=(*workbench._state.positions, new_position),
        )
        workbench._record_verified_positions_locked(state)
        workbench._state = state

    workbench._refresh_locked = refresh  # type: ignore[method-assign]
    worker = Thread(target=workbench._observation_loop, daemon=True)
    worker.start()
    try:
        workbench._position_hint(1)
        for _ in range(100):
            if workbench._inventory_revision:
                break
            Event().wait(0.01)
        assert workbench._inventory_revision > 0
        assert 987654321 in workbench._position_changes.new_ids
    finally:
        workbench.close()
        worker.join(timeout=1)


def test_manual_refresh_marks_a_new_verified_position() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    original = workbench._state
    new_position = replace(
        original.positions[0],
        con_id=987654321,
        local_symbol="SPX  261016P07000000",
    )
    positions = (*original.positions, new_position)
    workbench._view_model.refresh_portfolio = (  # type: ignore[method-assign]
        lambda _settings: replace(original, positions=positions, selected_con_id=None)
    )
    workbench._view_model.select_position = (  # type: ignore[method-assign]
        lambda con_id, _form: replace(
            original, positions=positions, selected_con_id=con_id
        )
    )

    response = TestClient(workbench.app).post(
        workbench.path + "action", data={"action": "refresh"}
    )

    assert response.status_code == 200
    assert 987654321 in workbench._position_changes.new_ids
    assert "data-new-position" in response.text
    assert re.search(
        r"<div data-position-name[^>]*>\s*<span[^>]*>SPX</span><span data-new-position",
        response.text,
    )


def test_lost_position_observer_blocks_paper_order_review() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._observe_positions = True
    workbench._observer_health = "disconnected"

    response = TestClient(workbench.app).post(
        workbench.path + "action", data={"action": "execute-arm"}
    )

    assert response.status_code == 200
    assert workbench._armed_execution is None
    assert "TWS observation is unavailable" in workbench._status_message

    assert workbench._toast is not None
    assert "Dismiss toast" in response.text
    reloaded = TestClient(workbench.app).get(workbench.path)
    assert "TWS observation is unavailable" not in reloaded.text
    second_reload = TestClient(workbench.app).get(workbench.path)
    assert "TWS observation is unavailable" not in second_reload.text

    first_revision = workbench._toast_revision
    repeated = TestClient(workbench.app).post(
        workbench.path + "action", data={"action": "execute-arm"}
    )
    assert repeated.status_code == 200
    assert workbench._toast is not None
    assert workbench._toast_revision == first_revision + 1
    assert "Dismiss toast" in repeated.text


def test_unverified_projection_shows_layer_estimate_without_enabling_review() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._build_draft_locked()
    workbench._state = replace(
        workbench._state, status=UiStatus.STALE, can_preview=False
    )

    _baseline, outcome, config = workbench._projection_state()
    page = TestClient(workbench.app).get(workbench.path).text

    assert config["unresolved"] is True
    assert outcome.expected_gain is None
    assert (
        "Estimate from shown layers. Refresh TWS before reviewing an order." not in page
    )
    assert "Expected gain" in page
    assert "Max loss" in page
    assert "Estimated gain" not in page
    assert "Estimated stop result" not in page
    assert _money(outcome.covered_gain) in page


def test_single_tws_header_and_heartbeat_report_observer_health() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._observe_positions = True
    page = TestClient(workbench.app).get(workbench.path).text

    assert 'id="tws-updates-status"' in page
    assert page.count('id="tws-updates-status"') == 1
    assert 'data-header-status="Checking TWS updates"' in page
    assert 'data-header-status="TWS not connected"' not in page
    assert "TWS updates reconnecting" in page
    assert "TWS updates delayed" in page

    async def read_heartbeats() -> tuple[dict[str, object], dict[str, object]]:
        request = SimpleNamespace(
            is_disconnected=lambda: asyncio.sleep(0, result=False)
        )
        response = await workbench._inventory_events(request)
        events = response.body_iterator
        first = await events.__anext__()
        workbench._observer_health = "disconnected"
        second = await asyncio.wait_for(events.__anext__(), timeout=3)
        await events.aclose()
        return (
            json.loads(first.removeprefix("data: ").strip()),
            json.loads(second.removeprefix("data: ").strip()),
        )

    first, second = asyncio.run(read_heartbeats())
    assert first == {"revision": workbench._inventory_revision, "observer": "idle"}
    assert second == {
        "revision": workbench._inventory_revision,
        "observer": "disconnected",
    }
    workbench.close()


def test_refresh_reuses_healthy_position_subscription(monkeypatch) -> None:
    from ibkr_options_manager.app.web import surface

    starts = []

    class FakeObserver:
        def __init__(self, _on_change, _on_health):
            pass

        def start(self, settings, on_generation=None):
            starts.append(settings)
            if on_generation is not None:
                on_generation(len(starts))

        def stop(self):
            pass

    monkeypatch.setattr(surface, "PositionObserver", FakeObserver)
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._observe_positions = True
    try:
        with workbench._lock:
            workbench._start_observer_locked()
            workbench._observer_health = "connected"
            workbench._start_observer_locked()
            assert len(starts) == 1
            workbench._observer_health = "disconnected"
            workbench._start_observer_locked()
            assert len(starts) == 2
    finally:
        workbench.close()


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


def test_verified_empty_portfolio_shows_refresh_guidance_without_order_review() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._state = replace(workbench._state, positions=())
    workbench._selected_con_id = None

    page = TestClient(workbench.app).get(workbench.path).text

    assert "No option positions detected" in page
    assert "Buy a long option contract in TWS" in page
    assert "Refresh positions" in page
    assert "data-empty-positions" in page
    assert "ACTION REVIEW" not in page
    assert "LONG POSITIONS" not in page
    assert "Review order" not in page


def test_unverified_empty_portfolio_does_not_claim_no_positions() -> None:
    workbench = _demo_workbench()
    workbench._state = replace(workbench._state, status=UiStatus.BLOCKED)

    page = TestClient(workbench.app).get(workbench.path).text

    assert "No option positions detected" not in page


def test_selected_contract_header_uses_verified_position_and_quote_values() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._build_draft_locked()

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
    assert 'aria-label="Why are fewer contracts available?"' in page
    assert 'id="existing_exit_orders"' in page
    assert page.index("Available") < page.index("Existing TWS exit orders")
    assert page.index("Existing TWS exit orders") < page.index("Average price")
    assert "Expected gain" in page
    assert "Max loss" in page
    assert "+$659.00" in page
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
    assert "text-cyan-400" in page
    assert 'd="M12 22s8-4 8-10V5' in page  # Bundled Lucide shield icon.
    for label in ("Split draft layer quantities", "Create new OCA bracket"):
        button = re.search(rf'<button[^>]*aria-label="{label}"[^>]*>', page)
        assert button is not None and re.search(r"\sdisabled(?:\s|>)", button.group())


def test_header_warns_when_a_live_account_is_configured() -> None:
    workbench = _demo_workbench()
    workbench._demo_mode = False
    workbench._settings = replace(workbench._settings, account="U1234567")

    page = TestClient(workbench.app).get(workbench.path).text

    assert 'data-header-status="Live TWS account"' in page
    assert "text-red-400" in page
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


def test_missing_paper_account_prompts_without_contacting_tws() -> None:
    workbench = _demo_workbench()
    workbench._demo_mode = False
    workbench._settings = replace(workbench._settings, account="")
    calls: list[object] = []
    workbench._view_model.refresh_portfolio = lambda settings: calls.append(settings)  # type: ignore[method-assign]

    workbench.refresh_on_launch()
    page = TestClient(workbench.app).get(workbench.path).text

    assert calls == []
    assert workbench._launch_connection == "failed"
    assert "TWS unavailable" in page
    assert 'name="account"' in page
    assert "Enter your paper account ID" in page


def test_launch_retry_rejects_invalid_account_without_contacting_tws() -> None:
    workbench = _demo_workbench()
    workbench._demo_mode = False
    workbench._settings = replace(workbench._settings, account="")
    workbench._launch_connection = "failed"
    calls: list[object] = []
    workbench._view_model.refresh_portfolio = lambda settings: calls.append(settings)  # type: ignore[method-assign]

    response = TestClient(workbench.app).post(
        workbench.path + "action",
        data={"action": "launch-refresh", "account": "U123456"},
    )

    assert calls == []
    assert workbench._settings.account == ""
    assert workbench._launch_connection == "failed"
    assert "Enter your paper account ID (starts with DU)" in response.text


def test_launch_retry_saves_paper_account_and_refreshes() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    ready = workbench._state
    workbench._demo_mode = False
    workbench._settings = replace(workbench._settings, account="")
    workbench._launch_connection = "failed"
    saved: list[str] = []
    calls: list[object] = []
    workbench._save_account = saved.append

    def refreshed(settings: object) -> object:
        calls.append(settings)
        assert workbench._state.status is UiStatus.EMPTY
        assert workbench._selected_con_id is None
        return ready

    workbench._view_model.refresh_portfolio = refreshed  # type: ignore[method-assign]
    response = TestClient(workbench.app).post(
        workbench.path + "action",
        data={"action": "launch-refresh", "account": f"  {DEMO_ACCOUNT}  "},
    )

    assert response.status_code == 200
    assert saved == [DEMO_ACCOUNT]
    assert len(calls) == 1
    assert workbench._settings.account == DEMO_ACCOUNT
    assert workbench._launch_connection == "success"


def test_settings_refresh_saves_paper_account_for_next_launch() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    saved: list[str] = []
    workbench._save_account = saved.append

    response = TestClient(workbench.app).post(
        workbench.path + "action",
        data={"action": "refresh", "account": DEMO_ACCOUNT},
    )

    assert response.status_code == 200
    assert saved == [DEMO_ACCOUNT]


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
        data={"action": "launch-refresh", "account": workbench._settings.account},
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
    assert "data-signals:toasts__ifmissing" in page
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


@pytest.mark.parametrize(
    "message",
    [
        "Paper submission acknowledged. Automatic TWS refresh failed; "
        "use Refresh before another action.",
        "TWS acknowledged the amendment. TWS refresh could not verify "
        "the new state; use Refresh before another action.",
    ],
)
def test_acknowledged_action_with_unverified_refresh_is_a_warning(message: str) -> None:
    notice = _toast_notice(message)

    assert notice is not None
    assert notice.title == "Action acknowledged by TWS"
    assert (
        notice.description
        == "Refresh to verify the latest orders and position before another change."
    )
    assert notice.variant == "warning"


def test_price_update_fill_requires_fresh_exact_complete_execution() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    layer = MarketExitCandidate(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        target_order_id=101,
        target_perm_id=201,
        client_id=17,
        quantity=Decimal("3"),
        tif="GTC",
        oca_group="app/tranche-1",
        stop_order_id=102,
        stop_perm_id=202,
    )
    update = PriceUpdateCandidate(layer=layer, target_price=Decimal("12.10"))
    fill = ObservedExecution(
        exec_id="new-fill",
        account=layer.account,
        con_id=layer.con_id,
        perm_id=layer.target_perm_id,
        side="SLD",
        quantity=Decimal("3"),
        price=Decimal("12.40"),
        time="now",
    )
    observed = replace(
        snapshot,
        connected=True,
        complete=True,
        fresh=True,
        executions_complete=True,
        executions=(fill,),
    )
    assert _price_update_fills_verified(observed, (update,), set())
    assert not _price_update_fills_verified(observed, (update,), {"new-fill"})
    assert not _price_update_fills_verified(
        replace(observed, executions=(replace(fill, quantity=Decimal("1")),)),
        (update,),
        set(),
    )
    assert not _price_update_fills_verified(
        replace(observed, executions=(replace(fill, perm_id=999),)),
        (update,),
        set(),
    )
    assert not _price_update_fills_verified(
        replace(observed, executions_complete=False),
        (update,),
        set(),
    )


def test_unchanged_active_percentage_keeps_exact_working_stop_price() -> None:
    working = Decimal("18.50")
    shown = Decimal("-23.6")
    calculated = Decimal("18.60")  # inverse rounding of the displayed percentage

    assert _edited_active_price(working, calculated, shown, shown) is None
    assert (
        _edited_active_price(working, calculated, Decimal("-23.2"), shown) == calculated
    )


def test_arming_target_only_does_not_reprice_untouched_stop(monkeypatch) -> None:
    from ibkr_options_manager.app.view_model import WorkingOrderLine

    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    target = WorkingOrder(
        perm_id=201,
        client_id=17,
        order_id=101,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("1"),
        status="Submitted",
        oca_group="owned/tranche-1",
        limit_price=Decimal("29.50"),
        tif="GTC",
    )
    stop = replace(
        target,
        perm_id=202,
        order_id=102,
        order_type="STP",
        limit_price=None,
        stop_price=Decimal("18.50"),
    )
    current = replace(
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
                201,
                "SELL",
                "LMT",
                "1",
                "Submitted",
                101,
                "owned/tranche-1",
                Decimal("29.50"),
                None,
                "GTC",
            ),
            WorkingOrderLine(
                202,
                "SELL",
                "STP",
                "1",
                "Submitted",
                102,
                "owned/tranche-1",
                None,
                Decimal("18.50"),
                "GTC",
            ),
        ),
    )
    candidate = MarketExitCandidate(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        target_order_id=101,
        target_perm_id=201,
        client_id=17,
        quantity=Decimal("1"),
        tif="GTC",
        oca_group="owned/tranche-1",
        stop_order_id=102,
        stop_perm_id=202,
    )

    class PriceService:
        def owned_perm_ids(self, **_kwargs):
            return frozenset({201, 202})

        def prepare_market_exits(self, *_args, **_kwargs):
            return (candidate,)

        def prepare_price_updates(self, _snapshot, *, updates, **_kwargs):
            return updates

        def price_update_attempt_state(self, *_args):
            return None

    workbench._paper_execution = PriceService()  # type: ignore[assignment]
    monkeypatch.setattr(
        workbench._view_model, "select_position", lambda *_: workbench._state
    )
    monkeypatch.setattr(workbench._view_model, "latest_snapshot", lambda: current)
    monkeypatch.setattr(workbench, "_announce_reconciliation_locked", lambda: None)

    workbench._arm_price_updates_locked(
        {
            "active_target_201": "1",
            "active_stop_201": "-23.6",
        }
    )

    assert len(workbench._armed_price_updates) == 1
    assert workbench._armed_price_updates[0].target_price is not None
    assert workbench._armed_price_updates[0].stop_price is None


@pytest.mark.parametrize("refreshed", [True, False])
def test_quote_movement_without_new_sell_risk_still_sends_amendment(
    monkeypatch, refreshed: bool
) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    layer = MarketExitCandidate(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        target_order_id=101,
        target_perm_id=201,
        client_id=17,
        quantity=Decimal("1"),
        tif="GTC",
        oca_group="owned/tranche-1",
        stop_order_id=102,
        stop_perm_id=202,
    )
    update = PriceUpdateCandidate(layer=layer, target_price=Decimal("12.10"))
    before = replace(
        snapshot,
        quote=replace(
            snapshot.quote,
            bid=Decimal("11.50"),
            ask=Decimal("11.70"),
            market_data_type="LIVE",
            fresh=True,
        ),
    )
    after = replace(before, quote=replace(before.quote, bid=Decimal("11.60")))
    sent = []

    class PriceService:
        def prepare_price_updates(self, _snapshot, *, updates, **_kwargs):
            return updates

        def modify_prices(self, _snapshot, updates, **_kwargs):
            sent.extend(updates)
            return SimpleNamespace(entry=SimpleNamespace(order_ids=(101,)))

        def record_verified_price_updates(self, *_args):
            pass

    workbench._paper_execution = PriceService()  # type: ignore[assignment]
    workbench._armed_price_updates = (update,)
    workbench._armed_execution_deadline = monotonic() + 10
    workbench._armed_active_percentages = {201: ("1", "-25")}
    workbench._warned_price_update_concerns = _price_update_impact(
        before, (update,)
    ).concerns
    monkeypatch.setattr(
        workbench._view_model, "select_position", lambda *_: workbench._state
    )
    monkeypatch.setattr(workbench._view_model, "latest_snapshot", lambda: after)
    monkeypatch.setattr(workbench, "_announce_reconciliation_locked", lambda: None)
    monkeypatch.setattr(
        workbench, "_refresh_after_acknowledged_write_locked", lambda *_: refreshed
    )

    workbench._confirm_price_updates_locked({})

    assert sent == [update]
    assert workbench._toast is not None
    assert workbench._toast.variant == "success"
    assert workbench._toast.title == "Simulated price update acknowledged"
    assert (
        "Refresh before another order change" in workbench._toast.description
    ) is not refreshed


def test_immediate_price_update_fill_records_edited_values(monkeypatch) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    layer = MarketExitCandidate(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        target_order_id=101,
        target_perm_id=201,
        client_id=17,
        quantity=Decimal("1"),
        tif="GTC",
        oca_group="app/tranche-1",
        stop_order_id=102,
        stop_perm_id=202,
    )
    update = PriceUpdateCandidate(layer=layer, target_price=Decimal("12.10"))
    fill = ObservedExecution(
        exec_id="immediate-fill",
        account=layer.account,
        con_id=layer.con_id,
        perm_id=layer.target_perm_id,
        side="SLD",
        quantity=Decimal("1"),
        price=Decimal("12.40"),
        time="now",
    )
    initial = replace(
        snapshot,
        connected=True,
        complete=True,
        fresh=True,
        executions_complete=True,
    )
    refreshed = replace(initial, executions=(fill,))
    current = [initial]
    recorded = []

    class PriceService:
        def prepare_price_updates(self, _snapshot, *, updates, **_kwargs):
            return updates

        def modify_prices(self, *_args, **_kwargs):
            raise ExecutionOutcomeUnknown("IBKR error code=202: Order Canceled")

        def record_verified_price_updates(self, _snapshot, updates, percentages):
            recorded.append((updates, percentages))

    workbench._paper_execution = PriceService()  # type: ignore[assignment]
    workbench._armed_price_updates = (update,)
    workbench._armed_execution_deadline = monotonic() + 10
    workbench._armed_active_percentages = {201: ("2", "-25")}
    workbench._warned_price_update_concerns = _price_update_impact(
        initial, (update,)
    ).concerns
    monkeypatch.setattr(
        workbench._view_model, "select_position", lambda *_: workbench._state
    )
    monkeypatch.setattr(workbench._view_model, "latest_snapshot", lambda: current[0])
    monkeypatch.setattr(workbench, "_announce_reconciliation_locked", lambda: None)
    monkeypatch.setattr(
        workbench, "_refresh_locked", lambda: current.__setitem__(0, refreshed)
    )

    workbench._confirm_price_updates_locked({})

    assert recorded == [((update,), {201: ("2", "-25")})], (
        workbench._message,
        workbench._toast,
    )
    assert workbench._toast is None


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
            "Target must be above 0%; stop must be below its target.",
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
    assert workbench._toast.title == "Action acknowledged by TWS"
    assert workbench._toast.variant == "warning"


def test_post_write_snapshot_is_verified_even_if_draft_plan_is_blocked() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None and snapshot.complete and snapshot.fresh

    def refreshed_but_plan_blocked() -> None:
        workbench._state = replace(workbench._state, status=UiStatus.BLOCKED)

    workbench._refresh_locked = refreshed_but_plan_blocked  # type: ignore[method-assign]

    assert workbench._refresh_after_acknowledged_write_locked(
        "TWS acknowledged the price amendment."
    )
    assert workbench._status_message.endswith("TWS state refreshed.")
    assert workbench._toast is None


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
    assert "data-position-quantity" in inventory
    assert 'data-loading="false"' in inventory
    assert "data-position-count" in inventory
    assert "data-position-spinner" in inventory
    assert "data-position-loading" in inventory
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
    assert (
        '[data-position-quantity][data-loading="true"] [data-position-count]' not in css
    )
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
    journal._write(
        (
            JournalEntry(
                fingerprint="a" * 64,
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                state="RECONCILED",
                order_ids=(101, 102),
                perm_ids=(201, 202),
                layers=(
                    JournalLayer(
                        quantity=2,
                        target_price="1.20",
                        stop_price="0.75",
                        tif="GTC",
                        target_perm_id=201,
                        stop_perm_id=202,
                        cancelled=True,
                    ),
                ),
            ),
        )
    )
    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(), journal
    )

    page = TestClient(workbench.app).get(workbench.path).text

    assert "Bracket cancelled" in page
    assert 'data-layer-state="cancelled"' in page
    assert "data-draft-empty-state" not in page
    assert 'value="dismiss-cancelled:' in page
    assert "No fill evidence" not in page
    assert "mt-2 border-t border-border pt-2" not in page

    workbench._view_model._latest_snapshot = replace(
        snapshot, complete=False, fresh=False
    )
    removed = TestClient(workbench.app).post(
        workbench.path + "action",
        data={"action": f"dismiss-cancelled:{'a' * 64}::0"},
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
    journal._write(
        (
            JournalEntry(
                fingerprint=fingerprint,
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                state="SUBMISSION_UNKNOWN",
                expected_order_count=2,
                snapshot_captured_at="99",
                layers=(
                    JournalLayer(
                        quantity=2,
                        target_price="1.20",
                        stop_price="0.75",
                        tif="GTC",
                    ),
                ),
            ),
        )
    )
    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(), journal
    )
    client = TestClient(workbench.app)
    workbench._submission_review_required = True
    sent_page = client.get(workbench.path).text
    assert "data-submission-review" in sent_page
    assert "data-cancelled-bracket-recovery-dialog" not in sent_page
    assert "Orders need a decision in TWS" in sent_page
    workbench._submission_review_required = False
    workbench._view_model._latest_snapshot = replace(
        snapshot,
        working_orders=(
            WorkingOrder(
                perm_id=301,
                client_id=17,
                order_id=201,
                key=snapshot.selected,
                action="SELL",
                order_type="LMT",
                remaining=Decimal("2"),
                status="PreSubmitted",
                oca_group=f"{fingerprint[:12]}/tranche-1",
            ),
        ),
    )
    assert (
        "data-cancelled-bracket-recovery-dialog" not in client.get(workbench.path).text
    )
    workbench._view_model._latest_snapshot = snapshot
    workbench._message = "Journal reconciliation blocked: incomplete TWS read"
    assert workbench._toast is not None and workbench._toast.variant == "error"
    assert (
        "data-cancelled-bracket-recovery-dialog" not in client.get(workbench.path).text
    )
    workbench._toast = None
    assert (
        "data-cancelled-bracket-recovery-dialog" not in client.get(workbench.path).text
    )
    recovery_page = client.post(
        workbench.path + "action",
        data={"action": f"verify-cancelled-bracket:{fingerprint}"},
    ).text
    assert "Refresh layers" in recovery_page
    assert "Clear unverified bracket" in recovery_page
    assert 'name="confirmed"' in recovery_page
    assert 'id="cancelled_bracket_recovery"' in recovery_page
    assert "required" in recovery_page
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
    assert "Cancelled bracket cleared" not in response.text
    assert "Clear unverified bracket" not in response.text
    assert journal.find(fingerprint).state == "CANCELLED_CONFIRMED"


def test_reconciled_bracket_missing_after_manual_tws_cancel_offers_verification(
    tmp_path,
    monkeypatch,
) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    fingerprint = "c" * 64
    journal = ExecutionJournal(tmp_path / "paper-journal.json")
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
                snapshot_captured_at="99",
                layers=(
                    JournalLayer(
                        quantity=2,
                        target_price="1.20",
                        stop_price="0.75",
                        tif="GTC",
                        target_perm_id=201,
                        stop_perm_id=202,
                    ),
                ),
            ),
        )
    )
    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(), journal
    )

    page = TestClient(workbench.app).get(workbench.path).text

    assert "No fill evidence" in page
    assert 'value="verify-cancelled-bracket:' + fingerprint + '"' in page
    assert 'aria-label="Verify bracket status of layer 1"' in page
    assert "verify-layer-button" not in page
    assert (
        "grid-cols-[5rem_minmax(10rem,1fr)_minmax(10rem,1fr)_minmax(5rem,0.6fr)_5rem_2.25rem]"
        in page
    )
    assert "data-cancelled-bracket-recovery-dialog" not in page
    # A blocked plan refresh may clear the view model's snapshot. The explicit
    # Verify action must still open; confirmation obtains a new TWS read.
    workbench._view_model._latest_snapshot = None
    requested = TestClient(workbench.app).post(
        workbench.path + "action",
        data={"action": "verify-cancelled-bracket:" + fingerprint},
    )
    assert "Clear unverified bracket" in requested.text
    assert "data-cancelled-bracket-recovery-dialog" in requested.text
    clean = replace(
        snapshot,
        captured_at=Decimal("101"),
        completed_orders_complete=True,
        executions_complete=True,
    )
    with pytest.raises(ExecutionBlocked, match="confirm both"):
        journal.confirm_cancelled_unknown(
            clean,
            fingerprint,
            confirmed_in_tws=False,
        )

    def refreshed(*_args):
        workbench._view_model._latest_snapshot = clean
        return workbench._state

    monkeypatch.setattr(workbench._view_model, "select_position", refreshed)
    resolved_page = TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "resolve-cancelled-bracket",
            "confirmed": "yes",
            "fingerprint": fingerprint,
        },
    )
    assert "Cancelled bracket cleared" not in resolved_page.text
    assert journal.find(fingerprint).state == "CANCELLED_CONFIRMED"


def test_old_missing_bracket_does_not_interrupt_new_active_brackets(tmp_path) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    journal = ExecutionJournal(tmp_path / "paper-journal.json")
    journal._write(
        (
            JournalEntry(
                fingerprint="d" * 64,
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                state="RECONCILED",
                order_ids=(101, 102),
                perm_ids=(201, 202),
                snapshot_captured_at="99",
                layers=(
                    JournalLayer(
                        quantity=2,
                        target_price="1.20",
                        stop_price="0.75",
                        tif="GTC",
                        target_perm_id=201,
                        stop_perm_id=202,
                    ),
                ),
            ),
        )
    )
    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(), journal
    )
    new_target = WorkingOrder(
        perm_id=301,
        client_id=17,
        order_id=201,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("2"),
        status="Submitted",
        oca_group="new-attempt/tranche-1",
        tif="GTC",
    )
    workbench._view_model._latest_snapshot = replace(
        snapshot,
        working_orders=(
            new_target,
            replace(
                new_target,
                perm_id=302,
                order_id=202,
                order_type="STP",
            ),
        ),
    )

    page = TestClient(workbench.app).get(workbench.path).text
    assert "data-cancelled-bracket-recovery-dialog" not in page
    assert 'value="verify-cancelled-bracket:' + "d" * 64 + '"' in page
    requested = TestClient(workbench.app).post(
        workbench.path + "action",
        data={"action": "verify-cancelled-bracket:" + "d" * 64},
    )
    assert "data-cancelled-bracket-recovery-dialog" in requested.text


def test_reused_legacy_oca_group_shows_tws_conflict_and_confirmed_old_cancellations(
    tmp_path,
) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    fingerprint = "e" * 64
    prior_layers = (
        JournalLayer(1, "24.80", "18.40", "GTC", 201, 202),
        JournalLayer(1, "34.40", "18.40", "GTC", 203, 204),
    )
    journal = ExecutionJournal(tmp_path / "paper-journal.json")
    journal._write(
        (
            JournalEntry(
                fingerprint=fingerprint,
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                state="SUPERSEDED",
                snapshot_captured_at="98",
                resolution_captured_at="99",
                order_ids=(101, 102, 103, 104),
                perm_ids=(201, 202, 203, 204),
                layers=prior_layers,
            ),
            JournalEntry(
                fingerprint=fingerprint,
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                state="SUBMISSION_UNKNOWN",
                snapshot_captured_at="99",
                expected_order_count=4,
                layers=(
                    JournalLayer(1, "29.50", "18.40", "GTC", 201),
                    JournalLayer(1, "34.40", "18.40", "GTC", 203),
                ),
            ),
        )
    )
    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(),
        journal,
    )

    page = TestClient(workbench.app).get(workbench.path).text
    assert "Conflicting bracket orders in TWS" in page
    assert "Do not transmit the pending orders" in page
    assert "Orders placed outside this app are view-only here" not in page
    assert page.count("Conflicting order group") == 2
    assert page.count("Bracket cancelled") == 2
    assert "No fill evidence" not in page
    assert "data-cancelled-bracket-recovery-dialog" not in page
    hidden = journal.dismiss_cancelled_layer(snapshot, fingerprint, "98", 0)
    assert hidden.layers[0].hidden_from_workspace


def test_stale_paper_bracket_confirmation_expires_before_any_send(monkeypatch) -> None:
    from ibkr_options_manager.app.web import surface

    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._armed_execution = object()  # type: ignore[assignment]
    workbench._armed_execution_deadline = 1009.0
    monkeypatch.setattr(surface, "monotonic", lambda: 1010.0)

    workbench._confirm_execution_locked()

    assert workbench._armed_execution is None
    assert "expired" in workbench._message.lower()
    assert workbench._toast is not None
    assert workbench._toast.variant == "warning"


def test_paper_bracket_confirm_button_counts_down_to_server_deadline(
    monkeypatch,
) -> None:
    from ibkr_options_manager.app.web import surface

    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._armed_execution = object()  # type: ignore[assignment]
    workbench._armed_execution_deadline = 1010.0
    monkeypatch.setattr(surface, "monotonic", lambda: 1000.0)
    page = TestClient(workbench.app).get(workbench.path).text
    assert "Confirm (10s)" in page
    assert 'data-confirm-countdown-ms="10000"' in page
    assert "performance.now()" in page

    monkeypatch.setattr(surface, "monotonic", lambda: 1006.2)
    later = TestClient(workbench.app).get(workbench.path).text
    assert "Confirm (4s)" in later
    assert 'data-confirm-countdown-ms="3800"' in later
    monkeypatch.setattr(surface, "monotonic", lambda: 1010.0)
    expired = TestClient(workbench.app).get(workbench.path).text
    assert workbench._armed_execution is None
    assert 'value="execute-arm"' in expired
    assert 'value="execute-confirm"' not in expired


def test_expired_active_and_price_reviews_return_to_execute(monkeypatch) -> None:
    from ibkr_options_manager.app.web import surface

    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    candidate = MarketExitCandidate(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        target_order_id=101,
        target_perm_id=201,
        client_id=17,
        quantity=Decimal("1"),
        tif="GTC",
        oca_group="app/tranche-1",
        stop_order_id=102,
        stop_perm_id=202,
    )
    monkeypatch.setattr(surface, "monotonic", lambda: 1000.0)
    workbench._armed_cancellation = candidate
    workbench._active_action_verified = True
    workbench._armed_execution_deadline = 1010.0
    active_review = TestClient(workbench.app).get(workbench.path).text
    assert "Confirm (10s)" in active_review
    assert 'value="cancel-pair-confirm"' in active_review
    monkeypatch.setattr(surface, "monotonic", lambda: 1010.0)
    workbench._confirm_cancellation_locked()
    assert workbench._armed_cancellation == candidate
    assert not workbench._active_action_verified
    assert (
        'value="active-action-execute"'
        in TestClient(workbench.app).get(workbench.path).text
    )

    workbench._disarm_execution_locked()
    workbench._armed_price_updates = (
        PriceUpdateCandidate(layer=candidate, target_price=Decimal("12.10")),
    )
    workbench._armed_active_percentages = {201: ("2", "-25")}
    workbench._armed_execution_deadline = 1010.0
    monkeypatch.setattr(surface, "monotonic", lambda: 1000.0)
    price_review = TestClient(workbench.app).get(workbench.path).text
    assert "Confirm (10s)" in price_review
    assert 'value="price-update-confirm"' in price_review
    monkeypatch.setattr(surface, "monotonic", lambda: 1010.0)
    workbench._confirm_price_updates_locked({})
    assert not workbench._armed_price_updates
    assert workbench._armed_active_percentages == {201: ("2", "-25")}
    assert (
        'value="price-update-confirm"'
        not in TestClient(workbench.app).get(workbench.path).text
    )


@pytest.mark.parametrize("action", ["market-exit", "cancel-all"])
def test_expired_bulk_active_review_requires_execute_again(monkeypatch, action) -> None:
    from ibkr_options_manager.app.web import surface

    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    candidate = MarketExitCandidate(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        target_order_id=101,
        target_perm_id=201,
        client_id=17,
        quantity=Decimal("1"),
        tif="GTC",
        oca_group="app/tranche-1",
        stop_order_id=102,
        stop_perm_id=202,
    )
    if action == "market-exit":
        workbench._armed_market_exits = (candidate,)
        confirm = workbench._confirm_market_exit_locked
    else:
        workbench._armed_cancellations = (candidate,)
        confirm = workbench._confirm_all_cancellations_locked
    workbench._active_action_verified = True
    workbench._armed_execution_deadline = 1010.0
    monkeypatch.setattr(surface, "monotonic", lambda: 1010.0)

    confirm()

    assert not workbench._active_action_verified
    assert workbench._armed_market_exits or workbench._armed_cancellations
    assert (
        'value="active-action-execute"'
        in TestClient(workbench.app).get(workbench.path).text
    )


def test_refresh_clears_a_draft_that_exceeds_newly_available_quantity() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._build_draft_locked()
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
    assert workbench._current_layers() == ()


def test_empty_draft_stays_empty_until_add_layer() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = workbench._selected_con_id
    assert con_id is not None

    assert workbench._current_layers() == ()
    assert (
        "data-draft-empty-state" in TestClient(workbench.app).get(workbench.path).text
    )
    workbench._ensure_draft_locked()

    assert workbench._current_layers() == ()

    workbench._add_layer_locked()

    assert len(workbench._current_layers()) == 1
    assert workbench._current_layers()[0].quantity == "5"


def test_draft_bulk_stops_allow_break_even_and_keep_active_controls_separate() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._add_layer_locked()
    workbench._add_layer_locked()

    page = TestClient(workbench.app).get(workbench.path).text
    assert 'aria-label="Set all draft stops"' in page
    assert 'aria-label="Move all draft stops to B/E"' in page
    assert page.index('aria-label="Move all draft stops to B/E"') < page.index(
        "data-draft-stop-type-group"
    )
    assert "mb-3 flex items-center justify-between gap-2" in page
    assert 'data-slot="tooltip-content"' in page
    assert "Set every draft stop to one price" in page
    assert "Move every draft stop to break even" in page
    assert "data-apply-all-draft-stops" in page
    assert "data-draft-stop-mode-group" in page
    assert "Choose a percentage or price" not in page
    assert 'aria-label="Stop value unit"' in page
    assert 'data-value="return"' in page and 'data-value="price"' in page
    assert 'name="draft_stop_price_1"' in page
    assert "stopLoss.toFixed(1)" in page
    assert "data-move-draft-stops-to-be" in page

    assert workbench._save_form_locked({"stop_1": "0", "stop_2": "0"})
    assert all(layer.stop_percentage == "0" for layer in workbench._current_layers())
    assert all(Decimal(layer.stop_price) > 0 for layer in workbench._current_layers())


def test_set_all_draft_stops_starts_from_first_configured_stop_preset() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._stop_presets = "35,40"
    workbench._add_layer_locked()

    page = TestClient(workbench.app).get(workbench.path).text
    stop_value = re.search(r'<input[^>]*id="all-draft-stop-value"[^>]*>', page)
    assert stop_value is not None
    assert 'value="-35"' in stop_value.group()
    assert "data-draft-stop-mode-group" in page
    assert re.search(r'data-draft-stop-mode-group[^>]*__ifmissing=\'"return"\'', page)


def test_draft_stop_above_entry_must_remain_below_target() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._add_layer_locked()

    assert workbench._save_form_locked({"stop_1": "-5"})
    assert (
        Decimal(workbench._current_layers()[0].stop_price) > workbench._state.unit_basis
    )
    assert not workbench._save_form_locked({"stop_1": "-200"})


def test_bulk_draft_stop_keeps_exact_tick_with_a_short_display_percentage() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._add_layer_locked()
    basis = workbench._state.unit_basis
    calculator = workbench._state.quote_calculator
    assert basis is not None and calculator is not None
    from ibkr_options_manager.domain.planner import round_up_price

    exact = round_up_price(basis * Decimal("0.75"), calculator.bands)
    shown_loss = ((Decimal("1") - exact / basis) * 100).quantize(Decimal("0.1"))
    assert workbench._save_form_locked(
        {
            "stop_1": format(shown_loss, "f"),
            "draft_stop_price_1": format(exact, "f"),
        }
    )
    layer = workbench._current_layers()[0]
    assert Decimal(layer.stop_price) == exact
    assert Decimal(layer.stop_percentage) == shown_loss
    assert not workbench._save_form_locked(
        {
            "stop_1": "10",
            "draft_stop_price_1": format(exact, "f"),
        }
    )


def test_nvda_demo_draft_stop_keeps_the_selected_twenty_percent() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._select_locked(1_002_100_161)
    workbench._add_layer_locked()
    calculator = workbench._state.quote_calculator
    assert calculator is not None
    from ibkr_options_manager.domain import preview_reference_prices

    chosen = preview_reference_prices(
        Decimal("4.20"),
        Decimal("20"),
        Decimal("20"),
        calculator.bands,
    ).stop_price
    assert chosen == Decimal("3.36")
    assert workbench._save_form_locked(
        {
            "stop_1": "20",
            "draft_stop_price_1": "3.36",
        }
    )
    assert workbench._current_layers()[0].stop_percentage == "20"


def test_coarse_tick_draft_stop_retains_requested_percentage() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._select_locked(1_002_100_161)
    workbench._add_layer_locked()
    calculator = workbench._state.quote_calculator
    assert calculator is not None
    workbench._state = replace(
        workbench._state,
        quote_calculator=replace(
            calculator,
            bands=(PriceBand(Decimal("0"), Decimal("0.05")),),
        ),
    )

    assert workbench._save_form_locked(
        {
            "stop_1": "20",
            "draft_stop_price_1": "3.40",
        }
    )
    layer = workbench._current_layers()[0]
    assert layer.stop_percentage == "20"
    assert layer.stop_price == "3.40"


def test_empty_draft_state_shows_when_no_app_layers_exist() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._drafts[workbench._selected_con_id] = ()

    page = TestClient(workbench.app).get(workbench.path).text

    assert "data-draft-empty-state" in page
    assert 'value="build-draft"' in page


@pytest.mark.parametrize(
    ("available", "targets", "quantities"),
    [
        (5, ("20", "40", "60", "100"), ("2", "1", "1", "1")),
        (3, ("20", "40", "60"), ("1", "1", "1")),
    ],
)
def test_build_draft_uses_lmt_defaults_and_available_contracts(
    available: int, targets: tuple[str, ...], quantities: tuple[str, ...]
) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = workbench._selected_con_id
    assert con_id is not None
    workbench._drafts[con_id] = ()
    workbench._state = replace(workbench._state, available_quantity=available)
    client = TestClient(workbench.app)

    empty = client.get(workbench.path).text
    assert "data-draft-empty-state" in empty
    assert 'value="build-draft"' in empty
    assert 'value="add-layer"' in empty
    assert "draft-build-button" in empty
    css = client.get("/layers.css").text
    assert ".draft-build-button {" in css
    assert "background: #fff;" in css

    response = client.post(workbench.path + "action", data={"action": "build-draft"})

    assert response.status_code == 200
    layers = workbench._current_layers()
    assert tuple(layer.target_percentage for layer in layers) == targets
    assert tuple(layer.quantity for layer in layers) == quantities
    assert all(layer.stop_percentage == "25" for layer in layers)
    assert all(Decimal(layer.target_price) > 0 for layer in layers)
    assert "data-draft-empty-state" not in response.text
    assert 'data-quantity-ring="1"' in response.text
    assert (
        f"--quantity-share: {100 * int(quantities[0]) / available:.4f}%"
        in response.text
    )
    assert f"{quantities[0]} of {available} available contracts" in response.text
    assert 'id="tif_1_trigger"' in response.text
    assert "data-position:tif_1_trigger__" in response.text


def test_build_draft_preserves_existing_rows_and_fails_closed_on_bad_defaults() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._build_draft_locked()
    con_id = workbench._selected_con_id
    assert con_id is not None
    client = TestClient(workbench.app)
    existing = workbench._current_layers()

    client.post(
        workbench.path + "action",
        data={"action": "build-draft", "quantity_1": "1", "target_1": "100"},
    )
    assert workbench._current_layers() == existing

    workbench._drafts[con_id] = ()
    workbench._target_presets = "20, invalid, 60"
    response = client.post(workbench.path + "action", data={"action": "build-draft"})
    assert workbench._current_layers() == ()
    assert "Enter valid LMT and STP defaults" in response.text

    workbench._target_presets = "20, 40, 60, 100"
    workbench._state = replace(workbench._state, available_quantity=0)
    response = client.post(workbench.path + "action", data={"action": "build-draft"})
    assert workbench._current_layers() == ()
    assert response.status_code == 200
    assert workbench._message == (
        "Refresh a position with available contracts before building a draft."
    )


def test_existing_tws_bracket_waits_for_add_layer_before_creating_a_draft() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._build_draft_locked()
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
    assert "data-draft-empty-state" in page
    assert 'value="add-layer"' in page

    workbench._add_layer_locked()

    assert len(workbench._current_layers()) == 1


def test_stop_limit_choice_is_saved_but_paper_execution_stays_launch_gated() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._build_draft_locked()
    response = TestClient(workbench.app).post(
        f"/{workbench.session_token}/action",
        data={
            "action": "execute-arm",
            "draft_stop_type": "STP LMT",
            "draft_stop_limit_offset": "5",
            "draft_stop_limit_unit": "percent",
        },
    )

    assert response.status_code == 200
    assert workbench._armed_execution is None
    assert workbench._stop_configuration() == ("STP LMT", "5", "percent")
    assert "Paper transmission is disabled" in workbench._status_message


@pytest.mark.parametrize("offset", ["0", "100", "NaN", "bogus"])
def test_stop_limit_draft_rejects_invalid_offset_before_arming(offset: str) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._build_draft_locked()
    response = TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "execute-arm",
            "draft_stop_type": "STP LMT",
            "draft_stop_limit_offset": offset,
            "draft_stop_limit_unit": "percent",
        },
    )
    assert response.status_code == 200
    assert workbench._armed_execution is None
    assert "valid stop-limit offset" in workbench._status_message


def test_global_stop_limit_default_can_be_saved() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    initial = TestClient(workbench.app).get(workbench.path).text
    assert "Stop order for new layers" in initial
    assert initial.index("data-global-stop-type-group") < initial.index(
        "How far below the stop?"
    )
    assert 'data-value="STP LMT"' in initial
    assert 'name="global_stop_limit_unit" value="percent"' in initial
    assert "data-global-stop-unit-group" in initial
    assert "mt-3 flex flex-wrap items-end gap-3" in initial
    response = TestClient(workbench.app).post(
        f"/{workbench.session_token}/action",
        data={
            "action": "refresh",
            "global_stop_type": "STP LMT",
            "global_stop_limit_offset": "7.5",
        },
    )

    assert response.status_code == 200
    assert workbench._default_stop_type == "STP LMT"
    assert workbench._default_stop_limit_offset == "7.5"
    assert workbench._stop_configuration() == ("STP LMT", "7.5", "percent")


def test_global_stop_limit_default_accepts_dollar_offset() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    response = TestClient(workbench.app).post(
        f"/{workbench.session_token}/action",
        data={
            "action": "refresh",
            "global_stop_type": "STP LMT",
            "global_stop_limit_offset": "1.25",
            "global_stop_limit_unit": "dollars",
        },
    )

    assert response.status_code == 200
    assert workbench._stop_configuration() == ("STP LMT", "1.25", "dollars")
    assert workbench._plan_form(()).stop_limit_unit == "dollars"
    assert 'name="global_stop_limit_unit" value="dollars"' in response.text


def test_global_stop_limit_rejects_invalid_percent_without_changing_unit() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    client.post(
        workbench.path + "action",
        data={
            "action": "refresh",
            "global_stop_type": "STP LMT",
            "global_stop_limit_offset": "1.25",
            "global_stop_limit_unit": "dollars",
        },
    )
    rejected = client.post(
        workbench.path + "action",
        data={
            "action": "refresh",
            "global_stop_type": "STP LMT",
            "global_stop_limit_offset": "100",
            "global_stop_limit_unit": "percent",
        },
    )

    assert rejected.status_code == 200
    assert workbench._stop_configuration() == ("STP LMT", "1.25", "dollars")
    assert "below 100%" in workbench._status_message


def test_saving_dollar_stop_limit_default_without_draft_does_not_validate_a_layer() -> (
    None
):
    workbench = _demo_workbench()
    workbench.load_demo_data()
    assert workbench._current_layers() == ()

    response = TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "refresh",
            "global_stop_type": "STP LMT",
            "global_stop_limit_offset": "5",
            "global_stop_limit_unit": "dollars",
        },
    )

    assert response.status_code == 200
    assert workbench._stop_configuration() == ("STP LMT", "5", "dollars")
    assert workbench._current_layers() == ()
    assert workbench._state.status is UiStatus.READY
    assert not any(
        validation.code == "STOP_LIMIT_PRICE_INVALID"
        for validation in workbench._state.validations
    )
    assert "Plan needs attention" not in response.text

    workbench._add_layer_locked()
    assert workbench._current_layers()
    assert (
        workbench._plan_form(workbench._current_layers()).stop_order_type == "STP LMT"
    )
    actual_draft = workbench._view_model.select_position(
        workbench._selected_con_id,
        workbench._plan_form(workbench._current_layers()),
    )
    assert actual_draft.status is UiStatus.READY
    assert actual_draft.pairs
    assert all(
        pair.stop_limit_price is not None
        and 0 < pair.stop_limit_price < pair.stop_price
        for pair in actual_draft.pairs
    )
    workbench._apply_state_locked(actual_draft)
    reviewed = TestClient(workbench.app).get(workbench.path).text
    assert 'data-live-review-price="stop-limit-1"' in reviewed
    assert f"${actual_draft.pairs[0].stop_limit_price}" in reviewed


def test_new_default_replaces_old_position_choice_after_last_draft_is_removed() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    client.post(workbench.path + "action", data={"action": "add-layer"})
    assert len(workbench._current_layers()) == 1
    client.post(
        workbench.path + "action",
        data={
            "action": "remove-layer:1",
            "draft_stop_type": "STP LMT",
            "draft_stop_limit_offset": "5",
            "draft_stop_limit_unit": "percent",
        },
    )
    assert workbench._current_layers() == ()
    assert workbench._stop_configuration() == ("STP", "5", "percent")

    response = client.post(
        workbench.path + "action",
        data={
            "action": "refresh",
            "global_stop_type": "STP LMT",
            "global_stop_limit_offset": "5",
            "global_stop_limit_unit": "dollars",
        },
    )

    assert response.status_code == 200
    assert workbench._stop_configuration() == ("STP LMT", "5", "dollars")
    next_draft = client.post(workbench.path + "action", data={"action": "add-layer"})
    assert workbench._stop_configuration() == ("STP LMT", "5", "dollars")
    assert 'name="draft_stop_limit_unit" value="dollars"' in next_draft.text
    assert 'data-selected-unit="dollars"' in next_draft.text


def test_settings_refresh_discards_stale_position_choice_without_a_draft() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = workbench._selected_con_id
    assert con_id is not None and workbench._current_layers() == ()
    workbench._position_stop_config[con_id] = ("STP LMT", "5", "percent")

    response = TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "refresh",
            "global_stop_type": "STP LMT",
            "global_stop_limit_offset": "5",
            "global_stop_limit_unit": "dollars",
        },
    )

    assert response.status_code == 200
    assert workbench._stop_configuration() == ("STP LMT", "5", "dollars")
    assert con_id not in workbench._position_stop_config


def test_settings_refresh_preserves_stop_choice_for_existing_draft() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._add_layer_locked()
    con_id = workbench._selected_con_id
    assert con_id is not None and workbench._current_layers()
    workbench._position_stop_config[con_id] = ("STP LMT", "5", "percent")

    response = TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "refresh",
            "global_stop_type": "STP LMT",
            "global_stop_limit_offset": "5",
            "global_stop_limit_unit": "dollars",
        },
    )

    assert response.status_code == 200
    assert workbench._default_stop_limit_unit == "dollars"
    assert workbench._stop_configuration() == ("STP LMT", "5", "percent")


def test_active_stop_limit_layer_shows_both_prices_and_locks_price_edits() -> None:
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
                remaining="2",
                status="Submitted",
                oca_group="owned/tranche-1",
                limit_price=Decimal("26.20"),
                tif="GTC",
            ),
            WorkingOrderLine(
                perm_id=102,
                order_id=12,
                action="SELL",
                order_type="STP LMT",
                remaining="2",
                status="Submitted",
                oca_group="owned/tranche-1",
                stop_price=Decimal("16.40"),
                limit_price=Decimal("15.55"),
                tif="GTC",
            ),
        ),
    )
    page = TestClient(workbench.app).get(workbench.path).text
    assert 'data-layer-state="working"' in page
    assert re.search(r'<label[^>]*for="active-target-1"[^>]*>\s*LMT\s*</label>', page)
    assert re.search(r'<label[^>]*for="active-stop-1"[^>]*>\s*STP\s*</label>', page)
    assert 'data-active-stop-limit-price="15.55"' in page
    stop_input = re.search(r'<input[^>]*name="active_stop_101"[^>]*>', page)
    assert stop_input is not None and "disabled" in stop_input.group()


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

    empty_page = client.get(workbench.path)
    assert 'data-layer-state="working"' in empty_page.text
    assert "data-draft-empty-state" not in empty_page.text
    workbench._build_draft_locked()
    page = client.get(workbench.path)

    assert "data-draft-empty-state" not in page.text
    assert 'aria-label="OCA layers workspace"' in page.text
    assert 'aria-label="Existing OCA layer rows"' in page.text
    assert 'data-layer-state="working"' in page.text
    assert "$26.20" in page.text
    assert "$16.40" in page.text
    original_latest_snapshot = workbench._view_model.latest_snapshot
    snapshot_for_cost = original_latest_snapshot()
    assert snapshot_for_cost is not None
    workbench._view_model.latest_snapshot = lambda: replace(  # type: ignore[method-assign]
        snapshot_for_cost,
        position=replace(
            snapshot_for_cost.position, unit_basis=Decimal("27.6128028335")
        ),
    )
    cost_page = client.get(workbench.path).text
    assert "$27.61" in cost_page
    assert "$27.6128028335" not in cost_page
    workbench._view_model.latest_snapshot = original_latest_snapshot  # type: ignore[method-assign]
    header = page.text.split('id="active-form"', maxsplit=1)[0]
    assert 'aria-label="Set all active stops"' in header
    assert "evt.stopPropagation()" in header
    assert 'aria-label="Enter return percentage from entry"' in header
    assert "data-stop-mode-group" in header
    assert "Choose a percentage or price" not in header
    assert 'aria-label="Stop value unit"' in header
    assert (
        "Enter a stop price or return percentage to be applied to all active layers."
        in header
    )
    assert "data-stop-dialog-inverse" in header
    assert "Active layers" in header and "Entry cost" in header
    assert "Latest ask" in header
    assert "data-stop-dialog-summary" in header
    assert 'data-stop-preset="-20"' in header
    assert 'data-stop-preset="-25"' in header
    assert 'data-stop-preset="-35"' in header
    assert 'data-stop-preset="20"' in header
    assert header.index('data-stop-preset="20"') < header.index('data-stop-preset="0"')
    assert header.index('data-stop-preset="0"') < header.index('data-stop-preset="-20"')
    assert header.index('data-stop-preset="-35"') < header.rindex("Active layers")
    assert "Apply to active layers" in header
    assert 'aria-label="Move all active stops to B/E"' in header
    assert 'aria-label="Delete all active layers"' in header
    assert 'aria-label="Sell all active layers"' in header
    assert 'data-orientation="vertical"' in header
    assert 'form="active-form" name="action" value="market-exit-selected"' in header
    assert 'form="active-form" name="action" value="cancel-all-active"' in header
    assert (
        header.index('aria-label="Move all active stops to B/E"')
        < header.index('aria-label="Delete all active layers"')
        < header.index('aria-label="Sell all active layers"')
        < header.index('aria-label="Split draft layer quantities"')
    )
    assert header.index('aria-label="Set all active stops"') < header.index(
        'aria-label="Move all active stops to B/E"'
    )
    assert "data-reset-active-prices" in page.text
    assert "Cancel changes" in page.text
    active_form = page.text.split('id="active-form"', maxsplit=1)[1].split(
        "</form>", maxsplit=1
    )[0]
    assert 'data-reset-active-prices="true"' not in active_form
    review_footer = page.text.rsplit("data-reset-active-prices", maxsplit=1)[1]
    assert review_footer.index("Cancel changes") < review_footer.index(
        "data-active-execute"
    )
    assert "Update layers" not in page.text
    assert "Close working" not in page.text
    assert 'data-layer-state="draft"' in page.text
    assert 'name="active_target_101"' in page.text
    assert 'name="active_stop_101"' in page.text
    stop_input = re.search(r'<input[^>]*name="active_stop_101"[^>]*>', page.text)
    assert stop_input is not None
    assert 'step="any"' in stop_input.group()
    assert 'name="active_stop_price_101"' in page.text
    assert "rate.toFixed(2)" in page.text
    assert "data-draft-stop-type-group" in page.text
    assert 'data-value="STP LMT"' in page.text
    assert 'role="radiogroup"' in page.text
    assert "data-stop-limit-settings-trigger" in page.text
    assert "How far below the stop?" in page.text
    assert "mt-5 border-t border-border pt-4" in page.text
    assert "data-stop-limit-unit-group" in page.text
    assert 'data-value="percent"' in page.text
    assert 'data-value="dollars"' in page.text
    assert 'data-live-stop-limit-price="1"' in page.text
    assert 'data-draft-review-stop-limit-row="1"' in page.text
    assert 'data-live-review-price="stop-limit-1"' in page.text
    assert "SELL STP LMT" in page.text
    assert "STP SELL" not in page.text
    assert "SELL STP" in page.text
    assert "STP loss (with LMT)" not in page.text
    assert "stop-limit-settings-change" in page.text
    assert "The limit is rounded down to a valid price increment" not in page.text
    assert 'name="draft_stop_type"' in page.text
    assert "data-active-review-row" in page.text
    assert "data-active-execute" in page.text
    assert 'id="active-quantity-1"' in page.text
    assert 'id="active-tif-1"' in page.text
    assert 'value="cancel-pair-arm:101"' in page.text
    assert "Cancel this active layer" in page.text
    assert ">State<" not in page.text
    assert "requires a second confirmation" not in page.text
    assert 'aria-current="page"' not in page.text
    assert 'data-active-initial="' in page.text
    assert 'data-live-price="active-target-1"' in page.text
    assert 'data-live-outcome="active-target-1"' in page.text
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
    assert (
        f'aria-label="Expected gain {direction} by ${abs(delta):,.2f}"'
        in added_page.text
    )


@pytest.mark.parametrize("available_quantity", [0, 1])
def test_cancelled_review_with_no_draft_prioritises_unprotected_contracts(
    available_quantity: int,
) -> None:
    from ibkr_options_manager.app.view_model import WorkingOrderLine

    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = workbench._selected_con_id
    assert con_id is not None
    workbench._paper_execution = _OwnedOrderService({101, 102})
    workbench._drafts[con_id] = ()
    workbench._state = replace(
        workbench._state,
        available_quantity=available_quantity,
        working_orders=(
            WorkingOrderLine(
                perm_id=101,
                order_id=11,
                action="SELL",
                order_type="LMT",
                remaining="2",
                status="Submitted",
                oca_group="test/tranche-1",
                limit_price=Decimal("26.20"),
                tif="GTC",
            ),
            WorkingOrderLine(
                perm_id=102,
                order_id=12,
                action="SELL",
                order_type="STP",
                remaining="2",
                status="Submitted",
                oca_group="test/tranche-1",
                stop_price=Decimal("16.40"),
                tif="GTC",
            ),
        ),
    )
    workbench._armed_cancellation = MarketExitCandidate(
        account="DU123",
        con_id=con_id,
        target_order_id=11,
        target_perm_id=101,
        client_id=17,
        quantity=Decimal("2"),
        tif="GTC",
        oca_group="test/tranche-1",
        stop_order_id=12,
        stop_perm_id=102,
    )

    page = (
        TestClient(workbench.app)
        .post(workbench.path + "action", data={"action": "cancel-staged"})
        .text
    )

    assert 'data-has-draft-rows="false"' in page
    active_badge = re.search(r"<span[^>]*data-active-review-badge[^>]*>", page)
    active_review = re.search(r"<div[^>]*data-active-review(?:\s|>)[^>]*>", page)
    assert active_badge is not None
    assert active_review is not None
    assert "NEXT STEP" in page
    badge_classes = re.search(r'class="([^"]+)"', active_badge.group())
    review_classes = re.search(r'class="([^"]+)"', active_review.group())
    assert badge_classes is not None and "hidden" not in badge_classes[1].split()
    assert review_classes is not None and "hidden" not in review_classes[1].split()
    if available_quantity:
        assert "Contracts still need protection" in page
        assert "1 contract is available for a new exit layer." in page
        assert 'form="draft-form" name="action" value="add-layer"' in page
        assert "Ready to adjust a price?" not in page
    else:
        assert "Ready to adjust a price?" in page
        assert (
            "Change a target or stop in an active layer to preview the update here."
            in page
        )
        assert "data-edit-active-prices" in page
    assert "firstPrice.scrollIntoView" in page
    assert re.search(r'data-active-review-empty class="absolute inset-0 flex', page)
    assert "const showActive = active || !hasDraftRows;" in _live_active_script(
        {"basis": "1", "multiplier": "100", "bands": []}
    )
    assert "badge.textContent = active ? 'PRICE UPDATE' : 'NEXT STEP';" in (
        _live_active_script({"basis": "1", "multiplier": "100", "bands": []})
    )
    assert 'data-draft-execute class="hidden w-full"' in page
    assert 'data-active-execute-control class="w-full"' in page


def test_max_loss_change_uses_unsigned_amount_and_directional_arrows() -> None:
    worse = str(_projection_loss_value(Decimal("-1282.56"), Decimal("-640")))
    better = str(_projection_loss_value(Decimal("-642.56"), Decimal("640")))

    assert 'aria-label="More loss by $640.00"' in worse
    assert 'data-loss-arrow="up"' in worse
    assert re.search(r'data-loss-arrow="down" class="hidden"><span data-icon-sh', worse)
    assert "<span data-loss-amount>$640.00</span>" in worse
    assert 'aria-label="Less loss by $640.00"' in better
    assert 'data-loss-arrow="down"' in better
    assert re.search(r'data-loss-arrow="up" class="hidden"><span data-icon-sh', better)
    assert "<span data-loss-amount>$640.00</span>" in better


def test_expected_gain_change_uses_opposite_arrow_mapping_to_loss() -> None:
    increased = str(_projection_gain_value(Decimal("3882.08"), Decimal("640")))
    decreased = str(_projection_gain_value(Decimal("3242.08"), Decimal("-640")))

    assert 'aria-label="Expected gain increased by $640.00"' in increased
    assert re.search(
        r'data-gain-arrow="down" class="hidden"><span data-icon-sh', increased
    )
    assert "data-gain-change" in increased
    assert "text-muted-foreground" in increased
    assert "<span data-gain-amount>$640.00</span>" in increased
    assert 'aria-label="Expected gain decreased by $640.00"' in decreased
    assert re.search(
        r'data-gain-arrow="up" class="hidden"><span data-icon-sh', decreased
    )
    assert "text-muted-foreground" in decreased
    assert "<span data-gain-amount>$640.00</span>" in decreased


def test_unchanged_projection_keeps_neutral_placeholder_and_hides_arrows() -> None:
    for metric, markup in (
        ("gain", str(_projection_gain_value(Decimal("3882.08"), Decimal("0")))),
        ("loss", str(_projection_loss_value(Decimal("-642.56"), Decimal("0")))),
    ):
        assert 'aria-label="No change from loaded plan"' in markup
        assert f"<span data-{metric}-amount>—</span>" in markup
        assert re.search(
            rf'data-{metric}-arrow="up" class="hidden"><span data-icon-sh', markup
        )
        assert re.search(
            rf'data-{metric}-arrow="down" class="hidden"><span data-icon-sh', markup
        )
        assert "text-muted-foreground" in markup


def test_projection_percent_uses_total_cost_basis_and_hides_invalid_ratio() -> None:
    gain = str(_projection_gain_value(Decimal("500"), Decimal("0"), Decimal("1000")))
    loss = str(_projection_loss_value(Decimal("-250"), Decimal("0"), Decimal("1000")))
    assert "data-gain-percent" in gain and "+50.0%" in gain
    assert "data-loss-percent" in loss and "-25.0%" in loss
    assert "+50.0%" not in str(
        _projection_gain_value(Decimal("500"), None, Decimal("0"))
    )
    assert "+50.0%" not in str(_projection_gain_value(None, None, Decimal("1000")))


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
    assert (
        "Realised P&amp;L plus projected gains from the current layer plan."
        in page.text
    )
    assert (
        "Projected losses at the current layer stops; excludes realised P&amp;L."
        in page.text
    )
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
    assert "data-draft-empty-state" not in page.text
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
    assert re.search(
        r'<label[^>]*for="sold-target-1"[^>]*>\s*LMT\s*</label>', page.text
    )
    assert 'value="20"' in page.text
    assert "$15.50" in page.text
    assert re.search(r'id="sold-target-1"[^>]*disabled', page.text)
    assert 'id="sold-stop-1"' in page.text
    assert re.search(r'<label[^>]*for="sold-stop-1"[^>]*>\s*STP\s*</label>', page.text)
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
            replace(position, quantity="2") if position.con_id == selected else position
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
    assert re.search(
        r'<label[^>]*for="verify-target-1"[^>]*>\s*LMT\s*</label>', conflict.text
    )
    assert re.search(
        r'<label[^>]*for="verify-stop-1"[^>]*>\s*STP\s*</label>', conflict.text
    )
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
    workbench._state = replace(
        workbench._state,
        unit_basis=Decimal("2.74"),
        quote_calculator=replace(
            workbench._state.quote_calculator,
            bands=(PriceBand(Decimal("0"), Decimal("0.05")),),
        ),
    )
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
    workbench._build_draft_locked()
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
        '<input data-active-input="stop" data-active-perm-id="1" '
        'data-live-layer="1" data-active-initial="25" '
        'data-active-original="7.5" value="25">'
        '<button type="button" data-move-stops-to-be>Move stop to B/E</button>'
        '</form><div data-price-edit-reset data-reset-visible="false" '
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
              const reset = document.querySelector('[data-reset-active-prices]');
              const slot = document.querySelector('[data-price-edit-reset]');
              target.value = '60';
              target.dispatchEvent(new Event('input', { bubbles: true }));
              document.querySelector('[data-move-stops-to-be]').click();
              const afterMove = [target.value, stop.value, reset.disabled,
                                 slot.dataset.resetVisible];
              reset.click();
              return JSON.stringify({ afterMove, afterReset: [target.value,
                stop.value, reset.disabled, slot.dataset.resetVisible] });
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
    assert "Quote status:" not in sidebar
    assert ">Cancel<" in sidebar
    assert ">Confirm<" in sidebar
    assert "Click to confirm" not in sidebar


def test_safe_price_update_omits_immediate_sell_alert(monkeypatch) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    safe = replace(
        snapshot,
        quote=replace(
            snapshot.quote,
            bid=Decimal("14.30"),
            ask=Decimal("14.50"),
            market_data_type="LIVE",
            fresh=True,
        ),
    )
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
    workbench._armed_price_updates = (
        PriceUpdateCandidate(layer=layer, stop_price=Decimal("12.00")),
    )
    monkeypatch.setattr(workbench._view_model, "latest_snapshot", lambda: safe)

    controls = to_xml(workbench._execution_control())

    assert 'value="price-update-confirm"' in controls
    assert "No immediate sell indicated by quote" not in controls
    assert "Immediate sell risk cannot be assessed" not in controls


@pytest.mark.parametrize(
    ("kind", "confirm_action"),
    (
        ("draft", "execute-confirm"),
        ("price", "price-update-confirm"),
        ("cancel", "cancel-pair-confirm"),
        ("exit", "market-exit-confirm"),
    ),
)
def test_every_paper_write_uses_the_same_final_confirmation(
    kind: str,
    confirm_action: str,
) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    layer = MarketExitCandidate(
        account=DEMO_ACCOUNT,
        con_id=workbench._selected_con_id or 0,
        target_order_id=11,
        target_perm_id=101,
        client_id=17,
        quantity=Decimal("1"),
        tif="GTC",
        oca_group="example/tranche-1",
        stop_order_id=12,
        stop_perm_id=102,
    )
    if kind == "draft":
        leg = SimpleNamespace(outside_rth=True)
        workbench._armed_execution = SimpleNamespace(
            plan=SimpleNamespace(pairs=(SimpleNamespace(target=leg, stop=leg),))
        )  # type: ignore[assignment]
    elif kind == "price":
        workbench._armed_price_updates = (
            PriceUpdateCandidate(layer=layer, stop_price=Decimal("2.74")),
        )
    elif kind == "cancel":
        workbench._armed_cancellation = layer
        workbench._active_action_verified = True
    else:
        workbench._armed_market_exits = (layer,)
        workbench._active_action_verified = True

    html = to_xml(workbench._execution_control())
    confirm = re.search(r"<button[^>]*data-paper-confirm[^>]*>", html)
    assert confirm is not None
    assert f'value="{confirm_action}"' in confirm.group()
    assert "bg-destructive" in confirm.group()
    assert 'value="cancel-staged"' in html
    assert html.index('value="cancel-staged"') < html.index(f'value="{confirm_action}"')
    if kind == "draft":
        assert "Submit paper brackets" not in html
        assert "Outside RTH" not in html


def test_stop_above_latest_bid_warns_before_price_update_and_quote_change_rearms(
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
            bid=Decimal("10.15"),
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
    assert impact.details == (
        (
            "Confirming may cause one or more of these sell orders to execute soon "
            "and close their OCA brackets. A stop does not guarantee its fill price."
        ),
    )
    frozen = replace(risky, quote=replace(risky.quote, market_data_type="FROZEN"))
    frozen_impact = _price_update_impact(frozen, updates)
    assert frozen_impact.details == impact.details

    workbench._paper_execution = object()  # type: ignore[assignment]
    workbench._armed_price_updates = updates
    workbench._armed_execution_deadline = monotonic() + 10
    workbench._warned_price_update_concerns = _price_update_impact(
        safe, updates
    ).concerns
    monkeypatch.setattr(
        workbench._view_model, "select_position", lambda *_: workbench._state
    )
    monkeypatch.setattr(workbench._view_model, "latest_snapshot", lambda: risky)
    monkeypatch.setattr(workbench, "_announce_reconciliation_locked", lambda: None)

    workbench._confirm_price_updates_locked({})

    assert "confirm again" in workbench._message
    assert workbench._armed_price_updates == updates
    assert workbench._warned_price_update_concerns == impact.concerns


def test_stop_above_bid_warns_even_when_below_ask() -> None:
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
    quote = replace(
        snapshot.quote,
        bid=Decimal("14.30"),
        ask=Decimal("21.00"),
        market_data_type="LIVE",
        fresh=True,
    )

    impact = _price_update_impact(
        replace(snapshot, quote=quote),
        (PriceUpdateCandidate(layer=layer, stop_price=Decimal("20.80")),),
    )

    assert impact.title == "Possible immediate sell"
    assert len(impact.details) == 1
    assert "Confirming may cause one or more" in impact.details[0]

    layers = tuple(
        PriceUpdateCandidate(
            layer=replace(layer, target_perm_id=101 + index, stop_perm_id=201 + index),
            stop_price=Decimal("20.80"),
        )
        for index in range(4)
    )
    multi_layer_impact = _price_update_impact(replace(snapshot, quote=quote), layers)
    assert multi_layer_impact.details == impact.details
    assert len(multi_layer_impact.concerns) == 4


@pytest.mark.parametrize("safe_quote", [False, True])
@pytest.mark.parametrize("exact_price", [False, True])
def test_active_stop_accepts_positive_return_from_entry(
    monkeypatch, safe_quote, exact_price
) -> None:
    from ibkr_options_manager.app.view_model import WorkingOrderLine

    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    basis = Decimal("10.03" if exact_price else "10.00")
    group = "owned/tranche-1"
    target = WorkingOrder(
        perm_id=101,
        client_id=17,
        order_id=11,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("1"),
        status="Submitted",
        oca_group=group,
        limit_price=Decimal("25.00"),
        tif="GTC",
    )
    stop = replace(
        target,
        perm_id=102,
        order_id=12,
        order_type="STP",
        limit_price=None,
        stop_price=Decimal("8.00"),
    )
    active_snapshot = replace(
        snapshot,
        read_only_api=False,
        position=replace(snapshot.position, unit_basis=basis),
        working_orders=(target, stop),
        quote=replace(
            snapshot.quote,
            bid=Decimal("15.50"),
            ask=Decimal("16.00"),
            market_data_type="LIVE",
            fresh=True,
        )
        if safe_quote
        else snapshot.quote,
    )
    workbench._state = replace(
        workbench._state,
        unit_basis=basis,
        working_orders=(
            WorkingOrderLine(
                perm_id=101,
                order_id=11,
                action="SELL",
                order_type="LMT",
                remaining="1",
                status="Submitted",
                oca_group=group,
                limit_price=Decimal("25.00"),
                tif="GTC",
            ),
            WorkingOrderLine(
                perm_id=102,
                order_id=12,
                action="SELL",
                order_type="STP",
                remaining="1",
                status="Submitted",
                oca_group=group,
                stop_price=Decimal("8.00"),
                tif="GTC",
            ),
        ),
    )
    candidate = MarketExitCandidate(
        account=DEMO_ACCOUNT,
        con_id=snapshot.selected.con_id,
        target_order_id=11,
        target_perm_id=101,
        client_id=17,
        quantity=Decimal("1"),
        tif="GTC",
        oca_group=group,
        stop_order_id=12,
        stop_perm_id=102,
    )

    class PriceService(_OwnedOrderService):
        def prepare_market_exits(self, *_args, **_kwargs):
            return (candidate,)

        def prepare_price_updates(self, _snapshot, *, updates, **_kwargs):
            return updates

        def price_update_attempt_state(self, *_args):
            return None

    workbench._paper_execution = PriceService({101, 102})  # type: ignore[assignment]
    monkeypatch.setattr(
        workbench._view_model, "select_position", lambda *_: workbench._state
    )
    monkeypatch.setattr(
        workbench._view_model, "latest_snapshot", lambda: active_snapshot
    )
    monkeypatch.setattr(workbench, "_announce_reconciliation_locked", lambda: None)
    sent = []
    monkeypatch.setattr(
        workbench, "_confirm_price_updates_locked", lambda values: sent.append(values)
    )

    target_return = format((Decimal("25.00") / basis - 1) * 100, "f")
    values = {
        "active_target_101": target_return,
        "active_stop_101": "49.55" if exact_price else "50",
    }
    if exact_price:
        values["active_stop_price_101"] = "15.00"
        workbench._arm_price_updates_locked(
            {**values, "active_stop_price_101": "15.10"}
        )
        assert not workbench._armed_price_updates
    workbench._arm_price_updates_locked(values)

    assert len(workbench._armed_price_updates) == 1
    assert workbench._armed_price_updates[0].stop_price == Decimal("15.00")
    assert workbench._armed_price_updates[0].target_price is None
    assert sent == []
    assert workbench._armed_execution_deadline is not None


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
                layers=(
                    JournalLayer(
                        2,
                        "29.10",
                        "18.20",
                        "GTC",
                        201,
                        202,
                        target_percentage="20",
                        stop_percentage="-25",
                    ),
                ),
            ),
        )
    )
    if prior_unknown:
        journal.begin_management(
            replace(active_snapshot, captured_at=Decimal("0")),
            operation="price-update",
            material=(
                101,
                201,
                Decimal("29.10"),
                Decimal("31.49"),
                102,
                202,
                Decimal("18.20"),
                Decimal("18.17"),
            ),
            expected_order_count=2,
        )
        prior_entry = journal.latest_management_attempt(
            active_snapshot,
            operation="price-update",
            material=(
                101,
                201,
                Decimal("29.10"),
                Decimal("31.49"),
                102,
                202,
                Decimal("18.20"),
                Decimal("18.17"),
            ),
        )
        assert prior_entry is not None
        journal.mark_unknown(prior_entry.fingerprint)

    class PriceWriter(DemoPaperExecutionTransport):
        def __init__(self) -> None:
            self.prices: list[Decimal | None] = []

        def modify_prices(self, _snapshot, updates, **_kwargs) -> PaperSubmission:
            self.prices.extend(update.target_price for update in updates)
            return PaperSubmission(order_ids=(101, 102), perm_ids=(201, 202))

    writer = PriceWriter()
    workbench._paper_execution = PaperExecutionService(writer, journal)
    workbench._view_model.select_position = lambda *_args: workbench._state  # type: ignore[method-assign]
    workbench._view_model.latest_snapshot = lambda: active_snapshot  # type: ignore[method-assign]

    if prior_unknown:
        restarted_journal = ExecutionJournal(tmp_path / "journal.json")
        assert restarted_journal.unresolved_management_entries(
            account=snapshot.selected.account, con_id=snapshot.selected.con_id
        )
        workbench._paper_execution = PaperExecutionService(writer, restarted_journal)
        locked = TestClient(workbench.app).get(workbench.path).text
        assert "data-contract-lockdown" in locked
        assert re.search(r"<fieldset[^>]*disabled", locked)
        assert "Order changes locked" in locked
        for action in (
            "active-update-arm",
            "cancel-pair-arm:201",
            "execute-arm",
            "add-layer",
        ):
            blocked = TestClient(workbench.app).post(
                workbench.path + "action",
                data={
                    "action": action,
                    "active_target_201": "30",
                    "active_stop_201": "-25",
                },
            )
            assert not workbench._armed_price_updates
            assert "locked" in blocked.text.lower()
        unconfirmed = TestClient(workbench.app).post(
            workbench.path + "action",
            data={
                "action": "verify-management",
                "fingerprint": prior_entry.fingerprint,
            },
        )
        assert "data-contract-lockdown" in unconfirmed.text
        verified = TestClient(workbench.app).post(
            workbench.path + "action",
            data={
                "action": "verify-management",
                "fingerprint": prior_entry.fingerprint,
                "confirmed": "yes",
            },
        )
        assert "Order status verified" not in verified.text
        assert "data-contract-lockdown" not in verified.text
        assert journal.find(prior_entry.fingerprint).state == "RESOLVED"

    page = TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            "active_target_201": "30",
            "active_stop_201": "-25",
        },
    )

    assert workbench._armed_price_updates[0].target_price == Decimal("31.49")
    assert "29.1" in page.text and "31.49" in page.text
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

    assert writer.prices == [Decimal("31.49")]
    assert workbench._status_message.startswith(
        "Simulated broker acknowledged 2 app-owned OCA price amendment"
    ), workbench._status_message
    assert workbench._toast_revision > arm_toast_revision
    assert "Simulated price update acknowledged" in confirmed.text
    source = journal.find(fingerprint)
    assert source is not None
    assert source.layers[0].target_percentage == "30"
    assert source.layers[0].target_price == "31.49"
    sold = str(
        workbench._closed_layer_row(
            1,
            source,
            0,
            LayerOutcome("CLOSED_PNL_UNKNOWN", filled_quantity=Decimal("2")),
        )
    )
    assert re.search(r'value="30"[^>]*id="sold-target-1"', sold)
    assert "$31.49" in sold
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    requested = next(
        event for event in events if event["event"] == "ui_confirm_requested"
    )
    assert requested["requested"][0]["target_price"] == "31.49"
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
    assert ">Review market sell</button>" in sidebar
    assert ">Confirm<" not in sidebar
    assert ">Cancel changes<" in sidebar
    assert "Wait for both cancellation confirmations" not in sidebar
    assert "Selected app-owned OCA layer" not in sidebar
    assert "GTC" in sidebar

    workbench._active_action_verified = True
    assert ">Confirm<" in client.get(workbench.path).text

    client.post(workbench.path + "action", data={"action": "cancel-staged"})

    assert workbench._armed_market_exits == ()
    assert (
        workbench._status_message
        == "Staged action cancelled. No orders were sent to TWS."
    )
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
    assert ">Review cancellation</button>" in sidebar
    assert "Review the action above" not in sidebar
    assert ">Confirm<" not in sidebar
    assert ">Cancel changes<" in sidebar

    workbench._active_action_verified = True
    assert ">Confirm<" in TestClient(workbench.app).get(workbench.path).text


def test_delete_all_active_layers_reviews_every_bracket_and_blocks_changed_set(
    monkeypatch,
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
        for index in range(2)
    )
    workbench._paper_execution = SimpleNamespace(  # type: ignore[assignment]
        owned_perm_ids=lambda **_kwargs: frozenset(),
        prepare_market_exits=lambda *_args, **_kwargs: candidates,
    )
    active_ids = (101, 103)
    monkeypatch.setattr(workbench, "_active_target_perm_ids", lambda: active_ids)
    monkeypatch.setattr(workbench, "_announce_reconciliation_locked", lambda: None)
    monkeypatch.setattr(
        workbench._view_model,
        "select_position",
        lambda _con_id, _form: workbench._state,
    )
    client = TestClient(workbench.app)

    review = client.post(
        workbench.path + "action", data={"action": "cancel-all-active"}
    )
    assert review.status_code == 200
    assert workbench._armed_cancellations == candidates
    assert "example/tranche-1" in review.text
    assert "example/tranche-2" in review.text
    assert "SELL MKT" not in review.text.split("ACTION REVIEW", 1)[1]
    assert 'value="active-action-execute"' in review.text
    assert 'value="cancel-all-confirm"' not in review.text

    client.post(workbench.path + "action", data={"action": "cancel-all-confirm"})
    assert not workbench._active_action_verified
    execute = client.post(
        workbench.path + "action", data={"action": "active-action-execute"}
    )
    assert workbench._active_action_verified
    assert 'value="cancel-all-confirm"' in execute.text
    assert "All active brackets will close" not in execute.text
    assert (
        "Confirm requests cancellation of every reviewed active OCA bracket."
        not in execute.text
    )

    active_ids = (101, 103, 105)
    changed = client.post(
        workbench.path + "action", data={"action": "active-action-execute"}
    )
    assert not workbench._active_action_verified
    assert workbench._armed_cancellations == ()
    assert 'value="cancel-all-confirm"' not in changed.text


def test_delete_all_active_layers_stops_after_a_failed_pair(monkeypatch) -> None:
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
        for index in range(3)
    )
    attempts: list[int] = []

    def cancel_pair(_snapshot, candidate, **_kwargs) -> None:
        attempts.append(candidate.target_perm_id)
        if len(attempts) == 2:
            raise ExecutionOutcomeUnknown("TWS acknowledgement incomplete")

    workbench._paper_execution = SimpleNamespace(  # type: ignore[assignment]
        prepare_market_exits=lambda _snapshot, *, target_perm_ids, **_kwargs: tuple(
            candidate
            for candidate in candidates
            if candidate.target_perm_id in target_perm_ids
        ),
        cancel_pair=cancel_pair,
    )
    workbench._armed_cancellations = candidates
    workbench._active_action_verified = True
    workbench._armed_execution_deadline = monotonic() + 10
    monkeypatch.setattr(
        workbench,
        "_active_target_perm_ids",
        lambda: tuple(
            candidate.target_perm_id for candidate in candidates[len(attempts) :]
        ),
    )
    monkeypatch.setattr(workbench, "_announce_reconciliation_locked", lambda: None)
    monkeypatch.setattr(
        workbench._view_model,
        "select_position",
        lambda _con_id, _form: workbench._state,
    )

    workbench._confirm_all_cancellations_locked()

    assert attempts == [101, 103]
    assert "Cancelled 1 of 3 brackets" in workbench._status_message
    assert workbench._armed_cancellations == ()


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
    assert (
        "Review cancellation"
        if review_action.startswith("cancel-")
        else "Review market sell"
    ) in review.text
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
        monkeypatch.setattr(
            workbench, "_active_target_perm_ids", lambda: (101, 103, 105)
        )
        changed = client.post(
            workbench.path + "action", data={"action": "active-action-execute"}
        )
        assert not workbench._active_action_verified
        assert workbench._armed_market_exits == ()
        assert 'value="market-exit-confirm"' not in changed.text


def test_draft_allocation_rejects_out_of_range_quantity() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._build_draft_locked()
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
    assert "data-live-allocation-bar" not in response.text

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
    workbench._position_stop_config[con_id] = ("STP LMT", "5", "percent")
    overallocated = client.get(workbench.path)
    assert "data-live-allocation-bar" not in overallocated.text

    workbench._paper_execution = _OwnedOrderService(set())
    blocked = client.get(workbench.path).text
    assert "6 contracts drafted; 5 available." in blocked
    assert "Reduce a layer" in blocked
    assert blocked.index("Outcome projection") < blocked.index("6 contracts drafted")
    alert = re.search(r"<div[^>]*data-review-alert[^>]*>", blocked)
    assert alert is not None
    assert "bg-red-950" in alert.group()
    assert "border-destructive/70" in alert.group()
    assert "text-red-50" in alert.group()
    assert "Draft exceeds available contracts" in blocked
    assert "Complete valid prices and quantities for every edited layer." not in blocked
    assert 'data-quantity-over="true"' in alert.group()
    assert (
        "<svg"
        not in blocked[
            alert.end() : blocked.index('data-slot="alert-title"', alert.end())
        ]
    )
    assert len(re.findall(r'<div role="alert"[^>]*data-review-alert', blocked)) == 1
    alert_text = blocked[
        alert.end() : blocked.index('data-slot="alert-description"', alert.end())
    ]
    assert "Your stop may not sell the option" not in alert_text
    assert 'data-slot="alert-title"' in blocked
    css = client.get("/layers.css").text
    assert ".draft-quantity-alert" not in css
    library_css = client.get("/starui.css").text
    assert ".bg-red-950{" in library_css
    assert ".border-destructive\\/70{" in library_css
    assert ".text-red-50{" in library_css
    execute = re.search(r'<button[^>]*value="execute-arm"[^>]*>', blocked)
    assert execute is not None and re.search(r"\sdisabled(?:\s|>)", execute.group())


def test_removing_overallocated_layer_preserves_survivors() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._build_draft_locked()
    workbench._paper_execution = _OwnedOrderService(set())
    con_id = workbench._selected_con_id
    assert con_id is not None
    original = workbench._current_layers()[0]
    workbench._state = replace(workbench._state, available_quantity=12)
    workbench._drafts[con_id] = (
        replace(original, quantity="4"),
        replace(original, quantity="4"),
        replace(original, quantity="4"),
    )
    client = TestClient(workbench.app)

    response = client.post(
        workbench.path + "action",
        data={
            "action": "remove-layer:3",
            "quantity_1": "6",
            "quantity_2": "4",
            "quantity_3": "4",
        },
    )

    assert response.status_code == 200
    assert [layer.quantity for layer in workbench._current_layers()] == ["6", "4"]
    assert "Draft quantities must" not in response.text
    assert "Assign the remaining" not in response.text
    execute = re.search(r'<button[^>]*value="execute-arm"[^>]*>', response.text)
    assert execute is not None and not re.search(r"\sdisabled(?:\s|>)", execute.group())

    added = client.post(
        workbench.path + "action",
        data={"action": "add-layer", "quantity_1": "6", "quantity_2": "4"},
    )
    assert added.status_code == 200
    assert [layer.quantity for layer in workbench._current_layers()] == ["4", "4", "4"]


def test_partial_draft_can_be_reviewed_for_paper_execution(tmp_path: Path) -> None:
    def clock() -> Decimal:
        return Decimal("100")

    broker = DemoReadOnlyBroker(clock=clock, paper_execution_enabled=True)
    snapshots = SnapshotCoordinator(broker, max_age_seconds=Decimal("15"), clock=clock)
    portfolio = PortfolioCoordinator(
        broker, max_age_seconds=Decimal("15"), clock=clock, paper_execution_mode=True
    )
    from ibkr_options_manager.app.view_model import PlannerViewModel

    workbench = StarUIWorkbench(
        PlannerViewModel(snapshots, portfolio=portfolio, clock=clock),
        initial_account=DEMO_ACCOUNT,
        demo_mode=True,
        paper_execution=PaperExecutionService(
            DemoPaperExecutionTransport(),
            ExecutionJournal(tmp_path / "partial-journal.json"),
        ),
    )
    workbench.load_demo_data()
    workbench._build_draft_locked()
    con_id = workbench._selected_con_id
    assert con_id is not None
    first = workbench._current_layers()[0]
    workbench._drafts[con_id] = (replace(first, quantity="2"),)
    client = TestClient(workbench.app)

    page = client.get(workbench.path).text
    execute = re.search(r'<button[^>]*value="execute-arm"[^>]*>', page)
    assert execute is not None and not re.search(r"\sdisabled(?:\s|>)", execute.group())
    assert "Assign the remaining" not in page

    reviewed = client.post(workbench.path + "action", data={"action": "execute-arm"})
    assert reviewed.status_code == 200
    assert workbench._armed_execution is not None, workbench._message
    assert workbench._armed_execution.plan.planned_quantity == 2
    submitted = client.post(
        workbench.path + "action", data={"action": "execute-confirm"}
    )
    assert submitted.status_code == 200
    assert "Orders sent to TWS" in submitted.text


def test_starui_workbench_renders_and_adds_a_layer_from_a_server_owned_form() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._add_layer_locked()
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
    assert (
        'class="workspace-content flex min-w-0 min-h-0 flex-col '
        'overflow-hidden px-8 py-6"' in page.text
    )
    layout_css = client.get("/layers.css").text
    assert ".workspace-content {" in layout_css
    assert "max-width: 80rem;" in layout_css
    assert "margin-inline: auto;" in layout_css
    assert 'aria-label="Draft layer rows"' in page.text
    assert 'aria-label="Draft contracts allocated"' not in page.text
    assert "data-live-allocation-text" not in page.text
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
    assert action_panel.index("Outcome projection") < action_panel.index("Review order")
    assert "data-outcome-projection" in action_panel
    assert "Expected gain" in action_panel
    assert "Max loss" in action_panel
    assert "Covered subtotal:" not in action_panel
    assert 'data-layer-state="draft"' in draft
    assert 'data-slot="card"' not in draft
    header = page.text.split('id="draft-form"', maxsplit=1)[0]
    assert "data-live-allocation-bar" not in header
    assert "data-live-allocation-text" not in header
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
    assert "Review order" not in draft
    assert 'name="action" value="save-draft"' not in draft
    assert "font-mono" not in draft
    assert "text-xs font-semibold text-foreground" in draft
    assert "+$275.00 gain" in draft
    assert "-$340.00 max loss" in draft
    assert ">%</span>" in draft
    assert 'for="target_1"' in draft
    assert re.search(r'<label[^>]*for="target_1"[^>]*>\s*LMT\s*</label>', draft)
    assert 'id="target_1"' in draft
    assert 'for="stop_1"' in draft
    assert re.search(r'<label[^>]*for="stop_1"[^>]*>\s*STP\s*</label>', draft)
    assert 'id="stop_1"' in draft
    assert 'for="quantity_1"' in draft
    assert 'id="quantity_1"' in draft
    quantity_input = re.search(r'<input[^>]*id="quantity_1"[^>]*>', draft)
    assert quantity_input is not None
    assert 'min="1"' in quantity_input.group()
    assert 'max="5"' in quantity_input.group()
    assert 'for="tif_1_trigger"' in draft
    assert 'id="tif_1_trigger"' in draft
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
    assert "data-draft-empty-state" in response.text
    assert "Build your exit draft" in response.text
    assert (
        "Use your LMT targets to split the available contracts into layers"
        in response.text
    )
    assert 'name="action" value="add-layer"' in response.text
    assert response.text.count('name="action" value="add-layer"') == 3
    assert "Contracts still need protection" in response.text
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
    assert "data-draft-empty-state" not in restored.text


def test_paper_execution_control_submits_the_current_draft_form() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._add_layer_locked()
    workbench._paper_execution = _OwnedOrderService(set())

    page = TestClient(workbench.app).get(workbench.path)

    assert 'id="draft-form"' in page.text
    assert 'form="draft-form"' in page.text
    assert 'name="action" value="execute-arm"' in page.text
    execute = re.search(r'<button[^>]*value="execute-arm"[^>]*>', page.text)
    assert execute is not None and not re.search(r"\sdisabled(?:\s|>)", execute.group())
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
    assert "Confirm (" in armed.text
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
    assert "data-submission-review" in submitted.text
    assert "data-cancelled-bracket-recovery-dialog" not in submitted.text
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
    assert "data-submission-review" not in refreshed.text

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
    assert "Awaiting TWS review" in unknown.text
    assert 'value="execute-arm"' not in unknown.text


def test_demo_stop_limit_draft_reviews_and_journals_the_paper_pair(tmp_path) -> None:
    def clock() -> Decimal:
        return Decimal("100")

    broker = DemoReadOnlyBroker(clock=clock, paper_execution_enabled=True)
    from ibkr_options_manager.app.view_model import PlannerViewModel

    journal = ExecutionJournal(tmp_path / "paper-journal.json")
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
        paper_execution=PaperExecutionService(DemoPaperExecutionTransport(), journal),
    )
    workbench.load_demo_data()
    workbench._add_layer_locked()
    client = TestClient(workbench.app)
    armed = client.post(
        workbench.path + "action",
        data={
            "action": "execute-arm",
            "draft_stop_type": "STP LMT",
            "draft_stop_limit_offset": "1",
            "draft_stop_limit_unit": "dollars",
        },
    )
    assert armed.status_code == 200
    assert workbench._armed_execution is not None
    pair = workbench._armed_execution.plan.pairs[0]
    assert pair.stop.order_type == "STP LMT"
    assert pair.stop.limit_price is not None
    assert pair.stop.limit_price < pair.stop.rounded_price
    assert f'data-reviewed-limit-price="{pair.stop.limit_price}"' in armed.text
    assert "Your stop may not sell the option" in armed.text
    assert "you may still own the option and lose more than shown above" in armed.text
    warning_match = re.search(
        r'<div[^>]*data-stop-limit-warning[^>]*>.*?data-slot="alert-description"',
        armed.text,
        re.S,
    )
    assert warning_match is not None
    warning = warning_match.group()
    assert "border-amber-500/40 bg-amber-500/10 text-amber-100" in warning
    assert "<svg" not in warning
    assert armed.text.index("Your stop may not sell the option") < armed.text.index(
        "Confirm ("
    )
    assert "Confirm (" in armed.text

    submitted = client.post(
        workbench.path + "action", data={"action": "execute-confirm"}
    )
    assert submitted.status_code == 200
    assert "Orders sent to TWS" in submitted.text
    assert f"(LMT ${pair.stop.limit_price})" in submitted.text
    entries = journal.submission_entries(
        account=DEMO_ACCOUNT,
        con_id=workbench._selected_con_id,
    )
    assert len(entries) == 1
    assert entries[0].layers[0].stop_order_type == "STP LMT"
    assert entries[0].layers[0].stop_limit_price == str(pair.stop.limit_price)


def test_demo_weekend_stop_to_break_even_uses_acknowledged_price_update(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "IBKR_OPTIONS_MANAGER_PRICE_TRACE", str(tmp_path / "price-amendments.jsonl")
    )
    now = [Decimal("100")]

    def clock() -> Decimal:
        return now[0]

    from ibkr_options_manager.app.view_model import PlannerViewModel

    journal = ExecutionJournal(tmp_path / "demo-execution-journal.json")
    broker = DemoReadOnlyBroker(clock=clock, paper_execution_enabled=True)
    broker.use_journal(journal)
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
        paper_execution=PaperExecutionService(
            DemoPaperExecutionTransport(journal), journal
        ),
    )
    workbench.load_demo_data()
    workbench._select_locked(1_003_625_093)  # SPY has a frozen bid below break-even.
    workbench._add_layer_locked()
    client = TestClient(workbench.app)
    assert (
        "Confirm ("
        in client.post(workbench.path + "action", data={"action": "execute-arm"}).text
    )
    assert (
        "Orders sent to TWS"
        in client.post(
            workbench.path + "action", data={"action": "execute-confirm"}
        ).text
    ), (workbench._message, workbench._status_message, workbench._current_layers())
    pairs = workbench._active_oca_pairs()
    assert len(pairs) == 1
    _group, target, stop = pairs[0]
    assert stop.stop_price is not None
    armed = client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"active_target_{target.perm_id}": "20",
            f"active_stop_{target.perm_id}": "0",
        },
    )
    assert "Confirm (" in armed.text
    confirmed = client.post(
        workbench.path + "action",
        data={
            "action": "price-update-confirm",
        },
    )
    assert "Simulated price update acknowledged" in confirmed.text, (
        workbench._message,
        workbench._status_message,
        workbench._toast,
    )
    assert "Order status is uncertain" not in confirmed.text
    assert workbench._active_oca_pairs()[0][2].stop_price == Decimal("1.85")
    assert not journal.unresolved_management_entries(
        account=DEMO_ACCOUNT,
        con_id=1_003_625_093,
    )

    # Missing transport capability is a preflight block, never an uncertain send.
    workbench._paper_execution = PaperExecutionService(object(), journal)
    assert (
        "Confirm ("
        in client.post(
            workbench.path + "action",
            data={
                "action": "active-update-arm",
                f"active_target_{target.perm_id}": "20",
                f"active_stop_{target.perm_id}": "-20",
            },
        ).text
    )
    unsupported = client.post(
        workbench.path + "action",
        data={
            "action": "price-update-confirm",
        },
    )
    assert unsupported.status_code == 200
    assert workbench._toast is not None
    assert workbench._toast.title == "Couldn't change prices"
    assert "Price update blocked" in workbench._message
    assert not journal.unresolved_management_entries(
        account=DEMO_ACCOUNT,
        con_id=1_003_625_093,
    )

    class LostAcknowledgement(DemoPaperExecutionTransport):
        def modify_prices(self, *_args, **_kwargs):
            raise ExecutionOutcomeUnknown("simulated acknowledgement lost")

    workbench._paper_execution = PaperExecutionService(
        LostAcknowledgement(journal), journal
    )
    assert (
        "Confirm ("
        in client.post(
            workbench.path + "action",
            data={
                "action": "active-update-arm",
                f"active_target_{target.perm_id}": "20",
                f"active_stop_{target.perm_id}": "-20",
            },
        ).text
    )
    uncertain = client.post(
        workbench.path + "action",
        data={
            "action": "price-update-confirm",
        },
    )
    assert workbench._toast is not None
    assert workbench._toast.title == "Order status is uncertain"
    assert "data-contract-lockdown" in uncertain.text
    assert journal.unresolved_management_entries(
        account=DEMO_ACCOUNT,
        con_id=1_003_625_093,
    )
    blocked = client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"active_target_{target.perm_id}": "20",
            f"active_stop_{target.perm_id}": "-25",
        },
    )
    assert "contract is locked" in workbench._message.lower()
    assert "data-contract-lockdown" in blocked.text
    now[0] = Decimal("101")
    pending = journal.unresolved_management_entries(
        account=DEMO_ACCOUNT,
        con_id=1_003_625_093,
    )[0]
    verified = client.post(
        workbench.path + "action",
        data={
            "action": "verify-management",
            "fingerprint": pending.fingerprint,
            "confirmed": "yes",
        },
    )
    assert "data-contract-lockdown" not in verified.text
    assert journal.find(pending.fingerprint).state == "RESOLVED"


def test_demo_acknowledged_bracket_reappears_as_active_after_refresh_and_restart(
    tmp_path,
) -> None:
    def clock() -> Decimal:
        return Decimal("100")

    from ibkr_options_manager.app.view_model import PlannerViewModel

    journal_path = tmp_path / "paper-journal.json"
    journal = ExecutionJournal(journal_path)
    broker = DemoReadOnlyBroker(clock=clock, paper_execution_enabled=True)
    broker.use_journal(journal)

    def workbench_for(
        source: DemoReadOnlyBroker, record: ExecutionJournal
    ) -> StarUIWorkbench:
        return StarUIWorkbench(
            PlannerViewModel(
                SnapshotCoordinator(source, max_age_seconds=Decimal("15"), clock=clock),
                portfolio=PortfolioCoordinator(
                    source,
                    max_age_seconds=Decimal("15"),
                    clock=clock,
                    paper_execution_mode=True,
                ),
                clock=clock,
            ),
            initial_account=DEMO_ACCOUNT,
            demo_mode=True,
            paper_execution=PaperExecutionService(
                DemoPaperExecutionTransport(record),
                record,
            ),
        )

    workbench = workbench_for(broker, journal)
    workbench.load_demo_data()
    workbench._add_layer_locked()
    client = TestClient(workbench.app)
    assert (
        "Confirm ("
        in client.post(
            workbench.path + "action",
            data={
                "action": "execute-arm",
                "draft_stop_type": "STP LMT",
                "draft_stop_limit_offset": "5",
                "draft_stop_limit_unit": "percent",
            },
        ).text
    )
    submitted = client.post(
        workbench.path + "action", data={"action": "execute-confirm"}
    )
    assert "Orders sent to TWS" in submitted.text
    selected = workbench._selected_con_id
    assert selected is not None
    entry = journal.submission_entries(account=DEMO_ACCOUNT, con_id=selected)[0]
    assert len(entry.perm_ids) == 2
    assert entry.layers[0].stop_order_type == "STP LMT"
    assert entry.layers[0].stop_limit_offset == "5"
    assert entry.layers[0].stop_limit_unit == "percent"
    refreshed_page = client.post(
        workbench.path + "action", data={"action": "refresh"}
    ).text
    assert 'data-layer-state="working"' in refreshed_page, (
        workbench._state.status,
        workbench._message,
        workbench._status_message,
        [
            (e.state, e.perm_ids)
            for e in journal.submission_entries(account=DEMO_ACCOUNT, con_id=selected)
        ],
    )
    active_target = entry.perm_ids[0]
    target_input = re.search(
        rf'<input[^>]*name="active_target_{active_target}"[^>]*>', refreshed_page
    )
    assert target_input is not None
    target_value = re.search(r'value="([^"]+)"', target_input.group())
    assert target_value is not None
    armed_page = client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"active_target_{active_target}": target_value.group(1),
            f"active_stop_{active_target}": "0",
        },
    ).text
    assert workbench._armed_price_updates
    assert workbench._armed_price_updates[0].stop_limit_price is not None
    assert "UPDATE SELL STP LMT" in armed_page
    assert "STOP LIMIT PRICE" in armed_page
    workbench._disarm_execution_locked()
    stop_input = re.search(
        rf'<input[^>]*name="active_stop_{active_target}"[^>]*>', refreshed_page
    )
    assert stop_input is not None
    stop_value = re.search(r'value="([^"]+)"', stop_input.group())
    assert stop_value is not None
    client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"active_target_{active_target}": target_value.group(1),
            f"active_stop_{active_target}": stop_value.group(1),
            f"active_stop_limit_offset_{active_target}": "0.50",
            f"active_stop_limit_unit_{active_target}": "dollars",
        },
    )
    assert workbench._armed_price_updates[0].stop_limit_offset == Decimal("0.50")
    assert workbench._armed_price_updates[0].stop_limit_unit == "dollars"

    restarted_journal = ExecutionJournal(journal_path)
    restarted_broker = DemoReadOnlyBroker(clock=clock, paper_execution_enabled=True)
    restarted_broker.use_journal(restarted_journal)
    restarted = workbench_for(restarted_broker, restarted_journal)
    restarted.load_demo_data()
    assert restarted_journal.stop_limit_rule(
        account=DEMO_ACCOUNT,
        con_id=selected,
        target_perm_id=entry.perm_ids[0],
        stop_perm_id=entry.perm_ids[1],
    ) == (Decimal("5"), "percent")
    assert (
        'data-layer-state="working"'
        in TestClient(restarted.app).get(restarted.path).text
    )


def test_pending_bracket_verify_button_stays_available_and_requires_broker_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    fingerprint = "d" * 64
    journal = ExecutionJournal(tmp_path / "paper-journal.json")
    journal._write(
        (
            JournalEntry(
                fingerprint=fingerprint,
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                state="SUBMITTED",
                expected_order_count=2,
                order_ids=(101, 102),
                perm_ids=(201, 202),
                snapshot_captured_at="99",
                layers=(
                    JournalLayer(
                        quantity=2,
                        target_price="1.20",
                        stop_price="0.75",
                        tif="GTC",
                        target_perm_id=201,
                        stop_perm_id=202,
                    ),
                ),
            ),
        )
    )
    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(),
        journal,
    )
    client = TestClient(workbench.app)
    page = client.get(workbench.path).text
    assert 'value="verify-cancelled-bracket:' + fingerprint + '"' in page
    dialog = client.post(
        workbench.path + "action",
        data={
            "action": "verify-cancelled-bracket:" + fingerprint,
        },
    ).text
    assert "Refresh layers" in dialog
    assert "Clear unverified bracket" in dialog
    assert 'name="verification_choice"' not in dialog
    assert 'name="confirmed"' in dialog
    assert 'data-slot="checkbox"' in dialog
    assert 'id="bracket-refresh-form"' in dialog
    assert 'form="bracket-clear-form"' in dialog
    clear_button = re.search(r'<button[^>]*form="bracket-clear-form"[^>]*>', dialog)
    assert clear_button is not None
    assert "disabled" in clear_button.group()
    assert 'data-attr:disabled="!($bracket_absence_confirmed)"' in clear_button.group()
    assert "If either order filled, refresh to recover the execution" in dialog
    assert "flex w-full flex-wrap items-center justify-between gap-3" in dialog
    assert 'aria-label="Close"' in dialog
    assert ">Cancel</button>" in dialog
    no_choice = client.post(
        workbench.path + "action",
        data={
            "action": "resolve-cancelled-bracket",
            "fingerprint": fingerprint,
        },
    ).text
    assert "Confirm neither bracket leg is working or filled in TWS first" in no_choice
    assert journal.find(fingerprint).state == "SUBMITTED"

    def unchanged(*_args):
        workbench._view_model._latest_snapshot = snapshot
        return workbench._state

    monkeypatch.setattr(workbench._view_model, "select_position", unchanged)
    still_pending = client.post(
        workbench.path + "action",
        data={
            "action": "verify-bracket-exists",
            "fingerprint": fingerprint,
        },
    ).text
    assert "Bracket not verified" in still_pending
    assert journal.find(fingerprint).state == "SUBMITTED"

    matched = replace(
        snapshot,
        captured_at=Decimal("101"),
        working_orders=(
            WorkingOrder(
                perm_id=201,
                client_id=17,
                order_id=101,
                key=snapshot.selected,
                action="SELL",
                order_type="LMT",
                remaining=Decimal("2"),
                status="Submitted",
                oca_group=f"{fingerprint[:12]}/tranche-1",
                limit_price=Decimal("1.20"),
                tif="GTC",
            ),
            WorkingOrder(
                perm_id=202,
                client_id=17,
                order_id=102,
                key=snapshot.selected,
                action="SELL",
                order_type="STP",
                remaining=Decimal("2"),
                status="Submitted",
                oca_group=f"{fingerprint[:12]}/tranche-1",
                stop_price=Decimal("0.75"),
                tif="GTC",
            ),
        ),
    )

    from ibkr_options_manager.app.view_model import WorkingOrderLine

    def matching(*_args):
        workbench._view_model._latest_snapshot = matched
        return replace(
            workbench._state,
            working_orders=tuple(
                WorkingOrderLine(
                    perm_id=order.perm_id,
                    action=order.action,
                    order_type=order.order_type,
                    remaining="2",
                    status=order.status,
                    order_id=order.order_id,
                    oca_group=order.oca_group,
                    limit_price=order.limit_price,
                    stop_price=order.stop_price,
                    tif=order.tif,
                )
                for order in matched.working_orders
            ),
        )

    monkeypatch.setattr(workbench._view_model, "select_position", matching)
    verified = client.post(
        workbench.path + "action",
        data={
            "action": "verify-bracket-exists",
            "fingerprint": fingerprint,
        },
    ).text
    assert "Bracket verified" not in verified, (
        workbench._message,
        workbench._status_message,
        journal.find(fingerprint).state,
        [
            (outcome.status)
            for _entry, _index, outcome in workbench._submission_outcomes()
        ],
    )
    assert journal.find(fingerprint).state == "RECONCILED"

    entry = journal.find(fingerprint)
    assert entry is not None
    journal._write(
        (
            replace(
                entry,
                fills=(
                    JournalFill(
                        "filled.01", 201, "SLD", "2", "1.20", "now", "40", "USD"
                    ),
                ),
            ),
        )
    )
    filled_snapshot = replace(matched, captured_at=Decimal("102"), working_orders=())

    def filled(*_args):
        workbench._view_model._latest_snapshot = filled_snapshot
        return replace(workbench._state, working_orders=())

    monkeypatch.setattr(workbench._view_model, "select_position", filled)
    filled_page = client.post(
        workbench.path + "action",
        data={
            "action": "verify-bracket-exists",
            "fingerprint": fingerprint,
        },
    ).text
    assert "Bracket status updated" not in filled_page
    assert workbench._recovery_requested_fingerprint is None


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
    workbench._build_draft_locked()
    client = TestClient(workbench.app)
    assert (
        "Confirm ("
        in client.post(workbench.path + "action", data={"action": "execute-arm"}).text
    )

    def uncertain(*args, **kwargs):
        raise ExecutionOutcomeUnknown("TWS did not acknowledge every order")

    monkeypatch.setattr(service, "submit", uncertain)
    response = client.post(
        workbench.path + "action", data={"action": "execute-confirm"}
    )

    assert response.status_code == 200
    assert "Orders sent to TWS" in response.text, workbench._status_message
    assert "data-submission-review" in response.text
    assert "data-cancelled-bracket-recovery-dialog" not in response.text
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
    assert "data-submission-review" not in response.text


def test_pending_full_allocation_blocks_new_drafts(tmp_path) -> None:
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
    workbench._build_draft_locked()
    client = TestClient(workbench.app)

    assert (
        "Confirm ("
        in client.post(workbench.path + "action", data={"action": "execute-arm"}).text
    )
    submitted = client.post(
        workbench.path + "action", data={"action": "execute-confirm"}
    )

    assert submitted.status_code == 200
    assert workbench._planning_available_quantity() == 0
    assert workbench._current_layers() == ()
    assert "VERIFY IN TWS" in submitted.text
    assert 'value="execute-arm"' not in submitted.text
    _baseline, pending_projection, config = workbench._projection_state()
    assert config["pendingQuantity"] == "7"
    assert pending_projection.covered_quantity == 7
    assert pending_projection.covered_gain is not None

    blocked = client.post(workbench.path + "action", data={"action": "execute-arm"})
    assert blocked.status_code == 200
    assert "Draft quantity must be positive" in workbench._message
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
    assert (
        _next_target_preset_above(
            (Decimal("6.75"),), basis=Decimal("4.20"), bands=bands, presets=presets
        )
        is None
    )


@pytest.mark.parametrize("new_remaining", ["4", "6"])
def test_external_order_change_before_confirmation_blocks_demo_submission(
    tmp_path,
    new_remaining: str,
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
    workbench._build_draft_locked()
    client = TestClient(workbench.app)
    armed = client.post(workbench.path + "action", data={"action": "execute-arm"})
    assert "Confirm (" in armed.text

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
    client.post(workbench.path + "action", data={"action": "execute-confirm"})

    assert any(
        word in (workbench._message or "").lower() for word in ("blocked", "changed")
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
        QTimer.singleShot(100, lambda: click_button("Add Layer"))

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
                    )
                    if point
                    else finish(f"Settings was not clickable: {point!r}")
                ),
            )
            return
        QTimer.singleShot(
            150, lambda: javascript("document.body.innerText", inspect_page)
        )

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
    def __init__(self, *_args: object, **kwargs: object) -> None:
        self.demo_loaded = False
        self.launch_refresh_requested = False
        self.initial_account = kwargs.get("initial_account")

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
