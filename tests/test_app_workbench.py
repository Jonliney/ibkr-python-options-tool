import asyncio
import json
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace

import pytest
from httpx import Response
from starhtml import to_xml
from starlette.testclient import TestClient

from ibkr_options_manager.app.demo import (
    DEMO_ACCOUNT,
    DEMO_CON_IDS,
    DemoPaperExecutionTransport,
    DemoReadOnlyBroker,
    seed_demo_journal,
)
from ibkr_options_manager.app.main import build_parser
from ibkr_options_manager.app.view_model import PlanForm, UiStatus, ValidationLine
from ibkr_options_manager.app.web import StarUIWorkbench
from ibkr_options_manager.app.web.surface import (
    _action_notice,
    _busy_submit_script,
    _contract_display_name,
    _next_target_preset_above,
    _price_update_impact,
    _projection_gain_value,
    _projection_loss_value,
)
from ibkr_options_manager.app.web_window import (
    StarUIPlannerWindow,
)
from ibkr_options_manager.broker import PortfolioRequest, SnapshotRequest
from ibkr_options_manager.domain import (
    PriceBand,
    WorkingOrder,
)
from ibkr_options_manager.execution import (
    ExecutionBlocked,
    ExecutionJournal,
    JournalEntry,
    JournalLayer,
    LayerOutcome,
    MarketExitCandidate,
    PaperExecutionService,
    PriceUpdateCandidate,
)
from ibkr_options_manager.portfolio import PortfolioCoordinator, PortfolioStatus
from ibkr_options_manager.snapshot import SnapshotCoordinator, SnapshotStatus
from support.workbench import (
    _demo_workbench,
    _OwnedOrderService,
    _trailing_scenario_workbench,
)


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
    assert "Projected P&amp;L" in page
    assert "P&amp;L at STP" in page
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


def test_launch_connection_failure_stays_in_retry_dialog() -> None:
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


def test_errors_stay_beside_review_controls_across_reloads() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._message = "Execution blocked: TWS must be open"

    page = TestClient(workbench.app).get(workbench.path)

    assert workbench._feedback is not None
    assert workbench._feedback.title == "Couldn't send bracket orders"
    assert "Average price" in page.text
    assert 'data-action-feedback="review"' in page.text
    assert 'role="alert"' in page.text
    assert "Dismiss toast" not in page.text
    assert "toasts__ifmissing" not in page.text
    reloaded = TestClient(workbench.app).get(workbench.path)
    assert "TWS must be open" in reloaded.text
    assert 'data-action-feedback="review"' in reloaded.text
    assert "document.startViewTransition" not in page.text


@pytest.mark.parametrize("feedback", ["refresh", "settings"])
def test_refresh_completion_stays_in_its_control_and_does_not_replay(feedback) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)

    response = client.post(
        workbench.path + "action",
        data={"action": "refresh", "feedback": feedback},
    )

    assert f'data-completion-feedback="{feedback}"' in response.text
    assert "Dismiss toast" not in response.text
    reloaded = client.get(workbench.path)
    assert f'data-completion-feedback="{feedback}"' not in reloaded.text


def test_order_acknowledgement_is_local_and_unverified_state_stays_visible() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._show_order_feedback_locked("Acknowledged", "Orders refreshed")

    assert workbench._feedback_component("action") is None
    success = str(to_xml(workbench._order_completion_control()))
    assert 'data-order-completion="price-update"' in success
    assert "Successfully edited" in success
    assert "disabled" in success
    assert "action-complete" in success
    assert workbench._order_completion_control() is None

    workbench._show_order_feedback_locked(
        "Acknowledged", "Refresh required", verified=False
    )
    for _ in range(2):
        warning = str(to_xml(workbench._feedback_component("review")))
        assert 'data-feedback-tone="warning"' in warning
        assert 'role="alert"' in warning
        assert "data-transient-feedback" not in warning


def test_market_acknowledgement_uses_action_button_without_claiming_a_fill() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._show_order_feedback_locked(
        "Market sell sent to TWS", "Check TWS for the fill", action="market-exit"
    )

    control = str(to_xml(workbench._order_completion_control()))

    assert 'data-order-completion="market-exit"' in control
    assert "Sell order sent" in control
    assert "Successfully sold" not in control
    assert "disabled" in control


def test_edit_success_keeps_draft_review_controls_available_for_later_use() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._build_draft_locked()
    workbench._show_order_feedback_locked("Acknowledged", "Orders refreshed")

    controls = str(to_xml(workbench._execution_control()))

    assert "data-draft-execute" in controls
    assert 'data-order-completion="price-update"' in controls
    assert "Successfully edited" in controls


def test_adding_a_draft_highlights_the_new_layer_without_replaying_on_reload() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)

    response = client.post(workbench.path + "action", data={"action": "add-layer"})

    assert "data-draft-created" in response.text
    assert "data-draft-created" not in client.get(workbench.path).text


def test_refresh_failure_stays_in_connection_strip_without_success_feedback() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()

    def failed_refresh(**_kwargs):
        workbench._message = "TWS connection failed"

    workbench._refresh_locked = failed_refresh  # type: ignore[method-assign]
    client = TestClient(workbench.app)
    response = client.post(workbench.path + "action", data={"action": "refresh"})

    assert 'data-action-feedback="connection"' in response.text
    assert 'data-completion-feedback="refresh"' not in response.text
    assert 'data-action-feedback="connection"' in client.get(workbench.path).text


def test_only_settings_form_marks_refresh_as_a_settings_save() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._submission_review_required = True

    settings = str(to_xml(workbench._settings_dialog()))
    submission = str(to_xml(workbench._submission_review_dialog()))

    assert 'name="feedback" value="settings"' in settings
    assert 'name="feedback" value="settings"' not in submission


def test_selecting_fully_allocated_position_does_not_raise_error_feedback() -> None:
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
    assert workbench._feedback is None
    assert workbench._status_message.endswith(
        "existing closing exposure already covers the whole position"
    )
    page = TestClient(workbench.app).get(workbench.path).text
    assert "toasts__ifmissing" not in page
    assert "data-action-feedback=" not in page


def test_overallocated_position_still_raises_error_feedback() -> None:
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

    assert workbench._feedback is not None
    assert workbench._feedback.variant == "error"


def test_tws_connection_feedback_has_a_short_recovery_message() -> None:
    notice = _action_notice(
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
def test_routine_statuses_do_not_create_feedback(message: str) -> None:
    assert _action_notice(message) is None


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
    notice = _action_notice(message)

    assert notice is not None
    assert notice.title == "Action acknowledged by TWS"
    assert (
        notice.description
        == "Refresh to verify the latest orders and position before another change."
    )
    assert notice.variant == "warning"


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
        workbench,
        "_refresh_after_acknowledged_write_locked",
        lambda *_, **_kwargs: refreshed,
    )

    workbench._confirm_price_updates_locked({})

    assert sent == [update]
    assert workbench._feedback is not None
    assert workbench._feedback.variant == ("success" if refreshed else "warning")
    assert workbench._feedback.title == "Simulated price update acknowledged"
    assert (
        "Refresh before another order change" in workbench._feedback.description
    ) is not refreshed


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
def test_problem_feedback_use_short_specific_copy(
    message: str, title: str, body: str
) -> None:
    notice = _action_notice(message)

    assert notice is not None
    assert (notice.title, notice.description, notice.variant) == (title, body, "error")


def test_failed_post_write_refresh_does_not_show_a_success_feedback() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()

    def failed_refresh(**_kwargs) -> None:
        raise RuntimeError("connection lost")

    workbench._refresh_locked = failed_refresh  # type: ignore[method-assign]

    assert not workbench._refresh_after_acknowledged_write_locked(
        "Paper submission acknowledged."
    )
    assert workbench._feedback is not None
    assert workbench._feedback.title == "Action acknowledged by TWS"
    assert workbench._feedback.variant == "warning"


def test_post_write_snapshot_is_verified_even_if_draft_plan_is_blocked() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None and snapshot.complete and snapshot.fresh

    def refreshed_but_plan_blocked(**_kwargs) -> None:
        workbench._state = replace(workbench._state, status=UiStatus.BLOCKED)

    workbench._refresh_locked = refreshed_but_plan_blocked  # type: ignore[method-assign]

    assert workbench._refresh_after_acknowledged_write_locked(
        "TWS acknowledged the price amendment."
    )
    assert workbench._status_message.endswith("TWS state refreshed.")
    assert workbench._feedback is None


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
    assert workbench._feedback is not None and workbench._feedback.variant == "error"
    assert (
        "data-cancelled-bracket-recovery-dialog" not in client.get(workbench.path).text
    )
    workbench._feedback = None
    assert (
        "data-cancelled-bracket-recovery-dialog" not in client.get(workbench.path).text
    )
    recovery_page = client.post(
        workbench.path + "action",
        data={"action": f"verify-cancelled-bracket:{fingerprint}:0"},
    ).text
    assert "Refresh layers" in recovery_page
    assert "Clear layer 1" in recovery_page
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

    def selected(*_args, **_kwargs):
        workbench._view_model._latest_snapshot = fresh
        return workbench._state

    monkeypatch.setattr(workbench._view_model, "select_position", selected)
    response = client.post(
        workbench.path + "action",
        data={
            "action": "resolve-cancelled-bracket",
            "confirmed": "yes",
            "fingerprint": fingerprint,
            "layer_index": "0",
        },
    )

    assert response.status_code == 200
    assert "Cancelled bracket cleared" not in response.text
    assert "Clear layer 1" not in response.text
    assert journal.find(fingerprint).layers[0].cancelled, workbench._message


def test_verification_dialog_shows_only_selected_layer(tmp_path) -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    fingerprint = "e" * 64
    entry = JournalEntry(
        fingerprint=fingerprint,
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        state="RECONCILED",
        expected_order_count=8,
        snapshot_captured_at="1",
        layers=tuple(
            JournalLayer(
                quantity=1, target_price=f"{i}.02", stop_price="1.89", tif="GTC"
            )
            for i in range(3, 7)
        ),
    )
    journal = ExecutionJournal(tmp_path / "paper-journal.json")
    journal._write((entry,))
    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(), journal
    )
    workbench._recovery_requested_fingerprint = fingerprint
    workbench._recovery_requested_layer_index = 0
    dialog = to_xml(
        workbench._cancelled_bracket_recovery(
            ((entry, 0, LayerOutcome("NO_EXECUTION_EVIDENCE")),)
        )
    )
    assert f"{fingerprint[:12]}/tranche-1" in dialog
    assert f"{fingerprint[:12]}/tranche-2" not in dialog
    assert "3.02 · 1 contracts" in dialog
    assert "4.02 · 1 contracts" not in dialog
    assert 'name="layer_index" value="0"' in dialog


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
    assert 'aria-label="Draft order time in force"' not in response.text
    assert 'name="tif_1"' not in response.text


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

    assert 'aria-label="Projected P&amp;L increased by $640.00"' in increased
    assert re.search(
        r'data-gain-arrow="down" class="hidden"><span data-icon-sh', increased
    )
    assert "data-gain-change" in increased
    assert "text-muted-foreground" in increased
    assert "<span data-gain-amount>$640.00</span>" in increased
    assert 'aria-label="Projected P&amp;L decreased by $640.00"' in decreased
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


def test_starui_workbench_renders_and_adds_a_layer_from_a_server_owned_form() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._add_layer_locked()
    client = TestClient(workbench.app)

    page = client.get(workbench.path)
    assert page.status_code == 200
    assert "DRAFT 1" in page.text
    assert re.search(r"Last refreshed \d{2}:\d{2}:\d{2}", page.text)
    assert "SELL LMT" in page.text
    assert "SELL STP" in page.text
    assert "150 CALL · SEP 25 '26" in page.text
    assert "Connection &amp; layer defaults" in page.text
    assert "<dialog" in page.text
    assert 'aria-label="Draft layer rows"' in page.text
    assert 'aria-label="OCA layers workspace"' in page.text
    assert 'aria-label="Planned order actions"' in page.text
    assert 'id="draft-form"' in page.text
    assert 'data-live-input="target"' in page.text
    assert 'data-live-review-price="target-1"' in page.text
    assert 'data-live-metric="gain"' in page.text
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
    assert "Projected P&amp;L" in action_panel
    assert "P&amp;L at STP" in action_panel
    assert 'data-layer-state="draft"' in draft
    header = page.text.split('id="draft-form"', maxsplit=1)[0]
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
    assert "+$275.00 Projected P&amp;L" in draft
    assert "-$340.00 P&amp;L at STP" in draft
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
    assert 'aria-label="Draft order time in force"' not in draft
    assert 'name="tif_1"' not in draft
    assert 'aria-label="Draft layer rows"' in draft

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
    assert workbench._current_layers()[0].tif == "GTC"
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


def test_offset_edit_preserves_ratcheted_stop_and_rejects_invalid_rule(
    tmp_path: Path,
) -> None:
    from ibkr_options_manager.trailing import trailing_edit_prices

    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    order = replace(
        next(order for order in snapshot.working_orders if order.perm_id == 810201),
        stop_price=Decimal("8.90"),
    )
    assert trailing_edit_prices(
        snapshot, order, Decimal("25"), "dollars", Decimal("25"), "dollars"
    ) == (Decimal("8.90"), Decimal("0.25"), Decimal("0.25"))
    for value, unit, offset, offset_unit in (
        ("0", "dollars", "25", "dollars"),
        ("100", "percent", "25", "dollars"),
        ("25", "dollars", "900", "dollars"),
        ("25", "dollars", "100", "percent"),
        ("25", "unknown", "25", "dollars"),
        ("NaN", "dollars", "25", "dollars"),
    ):
        with pytest.raises(ExecutionBlocked):
            trailing_edit_prices(
                snapshot, order, Decimal(value), unit, Decimal(offset), offset_unit
            )
    with pytest.raises(ExecutionBlocked, match="fresh bid"):
        trailing_edit_prices(
            replace(snapshot, quote=replace(snapshot.quote, fresh=False)),
            order,
            Decimal("25"),
            "dollars",
            None,
            "dollars",
        )


def test_closed_position_keeps_failure_modal_until_fresh_orphan_absence(
    tmp_path, monkeypatch
):
    from support.orders import _plan, _snapshot

    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    journal = ExecutionJournal(tmp_path / "closed-failure.json")
    entry = journal.begin(snapshot, _plan(_snapshot()))
    entry = journal.mark_unknown(entry.fingerprint, reason="stop filled before target")
    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(journal), journal
    )
    workbench._selected_closed_con_id = snapshot.selected.con_id
    workbench._selected_con_id = None
    assert workbench._submission_outcomes()[0][0].fingerprint == entry.fingerprint
    # Closed history records the fill but must not infer sibling cancellation.
    monkeypatch.setattr(
        workbench,
        "_submission_outcomes",
        lambda: ((entry, 0, LayerOutcome("CLOSED_LOSS")),),
    )
    group = f"{entry.oca_prefix or entry.fingerprint[:12]}/tranche-1"
    for evidence in (None, (frozenset({group}), frozenset({123}))):
        monkeypatch.setattr(
            workbench._view_model,
            "closed_order_evidence",
            lambda *_, result=evidence: result,
        )
        assert workbench._unresolved_bracket_submissions()
        assert "stop filled before target" in to_xml(
            workbench._submission_review_dialog()
        )
    monkeypatch.setattr(
        workbench._view_model,
        "closed_order_evidence",
        lambda *_: (frozenset(), frozenset()),
    )
    assert workbench._unresolved_bracket_submissions() == ()
