import json
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest
from starhtml import to_xml
from starlette.testclient import TestClient

from ibkr_options_manager.app.demo import (
    DEMO_ACCOUNT,
    DemoPaperExecutionTransport,
    DemoReadOnlyBroker,
    seed_demo_journal,
)
from ibkr_options_manager.app.view_model import PlanForm, UiStatus, ValidationLine
from ibkr_options_manager.app.web import StarUIWorkbench
from ibkr_options_manager.app.web.surface import (
    _active_percentage_for_price,
    _edited_active_price,
    _live_active_script,
    _money,
    _position_identity,
    _price_update_fills_verified,
    _price_update_impact,
)
from ibkr_options_manager.broker import SnapshotRequest
from ibkr_options_manager.broker.execution import PaperSubmission
from ibkr_options_manager.domain import (
    ObservedExecution,
    PriceBand,
    WorkingOrder,
)
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
    PositionMarketExitPlan,
    PriceUpdateCandidate,
)
from ibkr_options_manager.portfolio import PortfolioCoordinator
from ibkr_options_manager.snapshot import SnapshotCoordinator
from support.workbench import (
    _demo_workbench,
    _OwnedOrderService,
    _trailing_scenario_workbench,
)


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
            "layer_index": "0",
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
    assert "Projected P&amp;L" in page
    assert "P&amp;L at STP" in page
    assert "Estimated gain" not in page
    assert "Estimated stop result" not in page
    assert _money(outcome.covered_gain) in page


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
    assert 'value="verify-cancelled-bracket:' + fingerprint + ':0"' in page
    assert 'aria-label="Verify bracket status of layer 1"' in page
    assert "verify-layer-button" not in page
    assert "layer-row-grid" in page
    assert "data-cancelled-bracket-recovery-dialog" not in page
    # A blocked plan refresh may clear the view model's snapshot. The explicit
    # Verify action must still open; confirmation obtains a new TWS read.
    workbench._view_model._latest_snapshot = None
    requested = TestClient(workbench.app).post(
        workbench.path + "action",
        data={"action": "verify-cancelled-bracket:" + fingerprint + ":0"},
    )
    assert "Clear layer 1" in requested.text
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

    def refreshed(*_args, **_kwargs):
        workbench._view_model._latest_snapshot = clean
        return workbench._state

    monkeypatch.setattr(workbench._view_model, "select_position", refreshed)
    resolved_page = TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "resolve-cancelled-bracket",
            "confirmed": "yes",
            "fingerprint": fingerprint,
            "layer_index": "0",
        },
    )
    assert "Cancelled bracket cleared" not in resolved_page.text
    assert journal.find(fingerprint).layers[0].cancelled, workbench._message


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
    assert 'value="verify-cancelled-bracket:' + "d" * 64 + ':0"' in page
    requested = TestClient(workbench.app).post(
        workbench.path + "action",
        data={"action": "verify-cancelled-bracket:" + "d" * 64 + ":0"},
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


def test_draft_stop_above_entry_must_remain_below_target() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._add_layer_locked()

    assert workbench._save_form_locked({"stop_1": "-5"})
    assert (
        Decimal(workbench._current_layers()[0].stop_price) > workbench._state.unit_basis
    )
    assert not workbench._save_form_locked({"stop_1": "-200"})


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
    assert 'data-active-review-stop-limit-row="101"' in page
    assert 'data-active-review-stop-limit="101"' in page
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
    assert 'aria-label="Set all stops…"' in header
    assert "evt.stopPropagation()" in header
    assert 'aria-label="Enter return percentage from entry"' in header
    assert "data-stop-mode-group" in header
    assert "Choose a percentage or price" not in header
    assert 'aria-label="Stop value unit"' in header
    assert (
        "Choose a stop price or return from entry. For trails, the distance is "
        "calculated from the latest bid; the limit offset is kept unless you "
        "override it." in header
    )
    assert "data-stop-dialog-inverse" in header
    assert "Active layers" in header and "Entry cost" in header
    assert "Latest bid" in header
    assert "Latest ask" not in header
    assert snapshot_for_cost.quote.bid is not None
    assert f"${snapshot_for_cost.quote.bid:,.2f}" in header
    assert "data-stop-dialog-summary" in header
    assert 'data-stop-preset="-20"' in header
    assert 'data-stop-preset="-25"' in header
    assert 'data-stop-preset="-35"' in header
    assert 'data-stop-preset="20"' in header
    assert header.index('data-stop-preset="20"') < header.index('data-stop-preset="0"')
    assert header.index('data-stop-preset="0"') < header.index('data-stop-preset="-20"')
    assert header.index('data-stop-preset="-35"') < header.rindex("Active layers")
    assert "Apply to active layers" in header
    assert 'aria-label="Raise stops to B/E"' in header
    assert re.search(r'<button[^>]*type="button"[^>]*data-move-stops-to-be', header)
    assert 'aria-label="Cancel all exits"' in header
    assert 'aria-label="Sell all brackets now"' in header
    assert 'data-orientation="vertical"' in header
    assert 'form="active-form" name="action" value="market-exit-selected"' in header
    assert 'form="active-form" name="action" value="cancel-all-active"' in header
    assert (
        header.index('aria-label="Raise stops to B/E"')
        < header.index('aria-label="Cancel all exits"')
        < header.index('aria-label="Sell all brackets now"')
        < header.index('aria-label="Split draft layer quantities"')
    )
    assert header.index('aria-label="Set all stops…"') < header.index(
        'aria-label="Raise stops to B/E"'
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
    assert 'id="active-tif-1"' not in page.text
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
    assert (
        "setHidden(row, !(targetChanged || stopChanged || limitChanged), 'block')"
        in active_script
    )
    assert "setReviewMode(changed || invalid)" in active_script
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
        f'aria-label="Projected P&amp;L {direction} by ${abs(delta):,.2f}"'
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
    assert "How projected p&amp;l is calculated" in page.text
    assert "How p&amp;l at stp is calculated" in page.text
    assert (
        "Brackets use the profit-taking LMT; trails use the shown LMT, "
        "or STP without a LMT." in page.text
    )
    assert (
        "A trigger is not a fill price or a maximum loss: "
        "stops can slip and stop-limit orders can remain unfilled." in page.text
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
    assert "How was" not in page.text
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
    assert 'id="sold-tif-1"' not in page.text
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
@pytest.mark.parametrize(
    "basis_text,stop_return,exact_stop",
    [("10.00", "50", None), ("10.03", "49.55", "15.00"), ("2.36185", "-29.72", "1.66")],
)
def test_active_stop_review_preserves_validated_exact_price(
    monkeypatch, safe_quote, basis_text, stop_return, exact_stop
) -> None:
    from ibkr_options_manager.app.view_model import WorkingOrderLine

    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    basis = Decimal(basis_text)
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
        "active_stop_101": stop_return,
    }
    if exact_stop:
        values["active_stop_price_101"] = exact_stop
        workbench._arm_price_updates_locked(
            {
                **values,
                "active_stop_price_101": str(
                    Decimal(exact_stop) + Decimal("0.0000000000000001")
                ),
            }
        )
        assert not workbench._armed_price_updates
        workbench._arm_price_updates_locked(
            {
                **values,
                "active_stop_price_101": str(Decimal(exact_stop) + Decimal("0.10")),
            }
        )
        assert not workbench._armed_price_updates
    workbench._arm_price_updates_locked(values)

    assert len(workbench._armed_price_updates) == 1
    assert workbench._armed_price_updates[0].stop_price == Decimal(
        exact_stop or "15.00"
    )
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
    workbench._view_model.select_position = lambda *_args, **_kwargs: workbench._state  # type: ignore[method-assign]
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
        lambda _con_id, _form, **_kwargs: workbench._state,
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


def test_delete_all_active_layers_stops_after_a_failed_pair(
    monkeypatch, tmp_path
) -> None:
    trace_path = tmp_path / "bracket-cancellations.jsonl"
    monkeypatch.setenv("IBKR_OPTIONS_MANAGER_CANCEL_TRACE", str(trace_path))
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
        lambda _con_id, _form, **_kwargs: workbench._state,
    )
    workbench._arm_all_cancellations_locked()
    assert workbench._armed_cancellations == candidates
    workbench._active_action_verified = True
    workbench._armed_execution_deadline = monotonic() + 10

    workbench._confirm_all_cancellations_locked()

    assert attempts == [101, 103]
    assert "Cancelled 1 of 3 brackets" in workbench._status_message
    assert workbench._armed_cancellations == ()
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert {event["run_id"] for event in events} == {events[0]["run_id"]}
    assert [
        event["event"] for event in events if event["event"] == "snapshot_complete"
    ] == ["snapshot_complete", "snapshot_complete"]
    assert any(
        event["event"] == "stopped" and event["cancelled_count"] == 1
        for event in events
    )
    assert any(
        event["event"] == "finished" and event["cancelled_count"] == 1
        for event in events
    )
    assert all(event.get("elapsed_ms", 0) >= 0 for event in events)


def test_delete_all_active_layers_traces_final_refresh_after_disarm(
    monkeypatch, tmp_path
) -> None:
    from ibkr_options_manager.cancellation_trace import current_snapshot_context

    trace_path = tmp_path / "bracket-cancellations.jsonl"
    monkeypatch.setenv("IBKR_OPTIONS_MANAGER_CANCEL_TRACE", str(trace_path))
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    candidate = MarketExitCandidate(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        target_order_id=11,
        target_perm_id=101,
        client_id=17,
        quantity=Decimal("2"),
        tif="GTC",
        oca_group="example/tranche-1",
        stop_order_id=12,
        stop_perm_id=102,
    )
    workbench._paper_execution = SimpleNamespace(  # type: ignore[assignment]
        prepare_market_exits=lambda *_args, **_kwargs: (candidate,),
        cancel_pair=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(workbench, "_active_target_perm_ids", lambda: (101,))
    monkeypatch.setattr(workbench, "_announce_reconciliation_locked", lambda: None)
    capture_contexts = []
    quote_requests = []

    def select_position(_con_id, _form, **_kwargs):
        capture_contexts.append(current_snapshot_context.get())
        quote_requests.append(_kwargs["include_quote"])
        return workbench._state

    monkeypatch.setattr(workbench._view_model, "select_position", select_position)

    def refresh_after_write(_acknowledgement: str, **_kwargs) -> bool:
        capture_contexts.append(current_snapshot_context.get())
        workbench._disarm_execution_locked()
        return False

    monkeypatch.setattr(
        workbench, "_refresh_after_acknowledged_write_locked", refresh_after_write
    )
    workbench._arm_all_cancellations_locked()
    workbench._active_action_verified = True
    workbench._armed_execution_deadline = monotonic() + 10

    workbench._confirm_all_cancellations_locked()

    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert [event["event"] for event in events[-3:]] == [
        "final_refresh_start",
        "final_refresh_complete",
        "finished",
    ]
    assert events[-2]["verified"] is False
    assert events[-1]["cancelled_count"] == 1
    assert {event["run_id"] for event in events} == {events[0]["run_id"]}
    assert capture_contexts == [
        (events[0]["run_id"], "before_pair", 1),
        (events[0]["run_id"], "final_refresh", None),
    ]
    assert quote_requests == [False]


@pytest.mark.parametrize(
    ("review_action", "confirm_action", "layer_count"),
    [
        ("cancel-pair-arm:101", "cancel-pair-confirm", 1),
        ("market-exit-arm:101", "market-exit-confirm", 1),
        ("market-exit-selected", "position-exit-confirm", 2),
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
        prepare_position_market_exit=lambda *_args, **_kwargs: PositionMarketExitPlan(
            snapshot.selected,
            snapshot.contract,
            snapshot.position.quantity,
            snapshot.position.unit_basis,
            snapshot.position.raw_average_cost,
            frozenset(),
            snapshot.connection_epoch,
            candidates,
        ),
        verify_position_exit_baseline=lambda *_args: None,
    )
    monkeypatch.setattr(workbench, "_active_target_perm_ids", lambda: (101, 103))
    monkeypatch.setattr(workbench, "_announce_reconciliation_locked", lambda: None)
    refreshes: list[int] = []

    def refresh_selected(con_id: int, _form: PlanForm, **_kwargs) -> object:
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
    original_capture = broker.capture
    quote_requests: list[bool] = []

    def capture_with_quote_record(request):
        if isinstance(request, SnapshotRequest):
            quote_requests.append(request.include_quote)
        return original_capture(request)

    broker.capture = capture_with_quote_record  # type: ignore[method-assign]

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
    assert quote_requests == [True, False, False]
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
    assert "SELL STP" in armed_page
    assert "SELL STP LMT" in armed_page
    assert "STOP LIMIT PRICE" not in armed_page
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
    assert 'value="verify-cancelled-bracket:' + fingerprint + ':0"' in page
    dialog = client.post(
        workbench.path + "action",
        data={
            "action": "verify-cancelled-bracket:" + fingerprint + ":0",
        },
    ).text
    assert "Refresh layers" in dialog
    assert "Clear layer 1" in dialog
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

    def unchanged(*_args, **_kwargs):
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

    def matching(*_args, **_kwargs):
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

    def filled(*_args, **_kwargs):
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
    assert "Transmission not verified" in response.text, workbench._status_message
    assert "TWS did not acknowledge every order" in response.text
    assert workbench._submission_review_error == "TWS did not acknowledge every order"
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


@pytest.mark.parametrize("scenario", ["convert", "working-trail"])
def test_cancellation_only_review_hides_projection_and_cancel_changes_restores_it(
    tmp_path, scenario
):
    workbench = _trailing_scenario_workbench(tmp_path, scenario)
    client = TestClient(workbench.app)
    assert workbench._outcome_projection(workbench._projection_state()) is not None
    page = client.post(
        workbench.path + "action", data={"action": "cancel-all-active"}
    ).text
    assert (
        workbench._armed_cancellation
        or workbench._armed_cancellations
        or (
            workbench._armed_trailing_change and workbench._armed_trailing_change.cancel
        )
    )
    assert workbench._outcome_projection(workbench._projection_state()) is None
    assert "data-outcome-projection" not in page
    client.post(workbench.path + "action", data={"action": "active-action-execute"})
    assert workbench._outcome_projection(workbench._projection_state()) is None
    client.post(workbench.path + "action", data={"action": "cancel-staged"})
    assert workbench._outcome_projection(workbench._projection_state()) is not None


@pytest.mark.parametrize(
    "value, tone",
    [
        (Decimal("15"), "text-emerald-400"),
        (Decimal("-60"), "text-rose-400"),
        (Decimal("0"), "text-muted-foreground"),
        (None, "text-muted-foreground"),
    ],
)
@pytest.mark.parametrize("trail_only", [False, True])
def test_projection_metrics_use_sign_colour_in_server_review(
    tmp_path: Path, value: Decimal | None, tone: str, monkeypatch, trail_only
) -> None:
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    monkeypatch.setattr(workbench, "_trail_only_projection", lambda: trail_only)
    baseline, outcome, config = workbench._projection_state()
    outcome = replace(outcome, expected_gain=value, max_loss=value)
    page = to_xml(workbench._outcome_projection((baseline, outcome, config)))
    for metric in ("gain",) if trail_only else ("gain", "loss"):
        metric_node = re.search(rf'<p[^>]*data-live-metric="{metric}"[^>]*>', page)
        assert metric_node is not None
        for candidate in ("text-emerald-400", "text-rose-400", "text-muted-foreground"):
            assert (candidate in metric_node.group()) == (candidate == tone)
    assert "Projected P&amp;L" in page
    assert ("P&amp;L at STP" in page) is (not trail_only)
    assert "Brackets use the profit-taking LMT" in page
    assert "trails use the shown LMT, or STP without a LMT" in page
    if trail_only:
        assert "Stops can slip; limits may not fill" in page
    else:
        assert "stop-limit orders can remain unfilled" in page


@pytest.mark.parametrize("bid", [Decimal("10.40"), Decimal("8.40")])
def test_sell_all_review_estimates_entire_position_at_bid(tmp_path, monkeypatch, bid):
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    snapshot = replace(
        snapshot,
        quote=replace(snapshot.quote, bid=bid, fresh=True, market_data_type="LIVE"),
    )
    monkeypatch.setattr(workbench._view_model, "latest_snapshot", lambda: snapshot)
    client = TestClient(workbench.app)
    page = client.post(
        workbench.path + "action", data={"action": "market-exit-selected"}
    ).text
    plan = workbench._armed_position_exit
    assert plan is not None, workbench._message
    assert plan.quantity == snapshot.position.quantity
    assert f"≈ ${bid}" in page
    assert (
        "Sells the entire held position, including contracts without brackets."
        not in page
    )
    assert "Sell all brackets now" in page
    assert "Entire held position</span>" not in page
    projection = to_xml(workbench._outcome_projection(workbench._projection_state()))
    expected = (bid - plan.basis) * plan.contract.multiplier * plan.quantity
    assert f"{'+' if expected >= 0 else '-'}${abs(expected):,.2f}" in projection
    assert "Market fills may differ" in projection
    assert "P&L at STP" not in projection
    assert 'data-live-metric="gain"' not in projection
    assert 'data-live-metric="loss"' not in projection
    assert not workbench._active_action_verified


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "stale",
        "frozen",
        "zero",
        "nan",
        "wrong-quantity",
        "stale-snapshot",
        "delayed",
    ],
)
def test_sell_all_bid_estimate_handles_unusable_and_delayed_quotes(
    tmp_path, monkeypatch, problem
):
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    client = TestClient(workbench.app)
    client.post(workbench.path + "action", data={"action": "market-exit-selected"})
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    quote = replace(snapshot.quote, fresh=True, market_data_type="LIVE")
    if problem == "missing":
        quote = replace(quote, bid=None)
    elif problem == "stale":
        quote = replace(quote, fresh=False)
    elif problem == "frozen":
        quote = replace(quote, market_data_type="FROZEN")
    elif problem == "zero":
        quote = replace(quote, bid=Decimal("0"))
    elif problem == "nan":
        quote = replace(quote, bid=Decimal("NaN"))
    elif problem == "delayed":
        quote = replace(quote, market_data_type="DELAYED")
    elif problem == "wrong-quantity":
        snapshot = replace(
            snapshot,
            position=replace(
                snapshot.position, quantity=snapshot.position.quantity + 1
            ),
        )
    elif problem == "stale-snapshot":
        snapshot = replace(snapshot, fresh=False)
    snapshot = replace(snapshot, quote=quote)
    monkeypatch.setattr(workbench._view_model, "latest_snapshot", lambda: snapshot)
    projection = to_xml(workbench._outcome_projection(workbench._projection_state()))
    if problem == "delayed":
        assert "delayed bid" in projection
        assert workbench._position_exit_bid() == quote.bid
    else:
        assert workbench._position_exit_bid() is None
        assert "Bid unavailable" in projection
        assert "Unknown" in projection


@pytest.mark.parametrize("realized", [Decimal("15.25"), None, "trailing-fill"])
def test_sell_all_bid_projection_includes_only_verified_realized_pnl(
    tmp_path, monkeypatch, realized
):
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    snapshot = replace(
        snapshot, quote=replace(snapshot.quote, fresh=True, market_data_type="LIVE")
    )
    monkeypatch.setattr(workbench._view_model, "latest_snapshot", lambda: snapshot)
    TestClient(workbench.app).post(
        workbench.path + "action", data={"action": "market-exit-selected"}
    )
    plan = workbench._armed_position_exit
    bid = workbench._position_exit_bid()
    assert plan is not None and bid is not None
    if realized == "trailing-fill":
        monkeypatch.setattr(
            workbench,
            "_trailing_entries",
            lambda: (SimpleNamespace(fills=("unresolved fill",)),),
        )
    monkeypatch.setattr(
        workbench,
        "_submission_outcomes",
        lambda: (
            (
                None,
                0,
                LayerOutcome(
                    "CLOSED_PROFIT",
                    filled_quantity=Decimal("1"),
                    realized_pnl=Decimal("15.25")
                    if realized == "trailing-fill"
                    else realized,
                    currency=plan.contract.currency,
                ),
            ),
        ),
    )
    projection = to_xml(workbench._position_exit_projection())
    if realized is None or realized == "trailing-fill":
        assert "Verified realised P&amp;L is incomplete" in projection
        assert "Unknown" in projection
    else:
        expected = (
            realized + (bid - plan.basis) * plan.contract.multiplier * plan.quantity
        )
        assert f"{'+' if expected >= 0 else '-'}${abs(expected):,.2f}" in projection


@pytest.mark.parametrize("scenario", ["convert", "working-trail"])
@pytest.mark.parametrize("change_quantity", [False, True])
def test_sell_all_review_and_confirm_accept_new_capture_counters(
    tmp_path, monkeypatch, scenario, change_quantity
):
    workbench = _trailing_scenario_workbench(tmp_path, scenario)
    broker = workbench._view_model._snapshots._broker
    original_capture = broker.capture
    epoch = 1

    def capture(request):
        nonlocal epoch
        result = original_capture(request)
        if isinstance(request, SnapshotRequest):
            epoch += 1
            result = replace(result, connection_epoch=epoch)
            if result.quote is not None:
                result = replace(
                    result, quote=replace(result.quote, market_data_type="LIVE")
                )
            if change_quantity:
                result = replace(
                    result,
                    positions=tuple(
                        replace(position, quantity=position.quantity + 1)
                        if position.contract.con_id == request.option_con_id
                        else position
                        for position in result.positions
                    ),
                )
        return result

    monkeypatch.setattr(broker, "capture", capture)
    service = workbench._paper_execution
    writes = []
    original_market = service._transport.submit_market

    def market(*args, **kwargs):
        writes.append(args[1].quantity)
        return original_market(*args, **kwargs)

    monkeypatch.setattr(service._transport, "submit_market", market)
    client = TestClient(workbench.app)
    client.post(workbench.path + "action", data={"action": "market-exit-selected"})
    assert workbench._armed_position_exit is not None
    quantity = workbench._armed_position_exit.quantity
    client.post(workbench.path + "action", data={"action": "active-action-execute"})
    if change_quantity:
        assert not workbench._active_action_verified
        assert writes == []
        assert "position quantity changed after review" in workbench._message
        assert workbench._toast.title == "Couldn't review market sell"
        return
    assert workbench._active_action_verified, workbench._message
    assert workbench._position_exit_bid() is not None
    assert writes == []
    client.post(workbench.path + "action", data={"action": "position-exit-confirm"})
    assert writes == [quantity], workbench._message
    assert epoch >= 4


@pytest.mark.parametrize(
    "failure", [None, "snapshot", "final-snapshot", "quantity", "ack"]
)
@pytest.mark.parametrize("exit_kind", ["market", "trail"])
def test_multi_bracket_exit_keeps_lease_and_reports_partial_failure(
    tmp_path, monkeypatch, failure, exit_kind
):
    from ibkr_options_manager.order_observation import _slot, borrow_client

    workbench = _trailing_scenario_workbench(tmp_path, "convert")
    service = workbench._paper_execution
    assert service is not None
    journal = service._journal
    entry = journal.submission_entries(
        account=DEMO_ACCOUNT, con_id=workbench._selected_con_id
    )[0]
    entry = replace(
        entry,
        expected_order_count=6,
        layers=tuple(
            replace(
                entry.layers[0],
                quantity=1,
                target_perm_id=810101 + 2 * index,
                stop_perm_id=810102 + 2 * index,
            )
            for index in range(3)
        ),
        order_ids=tuple(range(910101, 910107)),
        perm_ids=tuple(range(810101, 810107)),
    )
    journal._write((entry,))
    client = TestClient(workbench.app)
    client.post(workbench.path + "action", data={"action": "refresh"})
    client.post(
        workbench.path + "action",
        data=(
            {"action": "market-exit-selected"}
            if exit_kind == "market"
            else {
                "action": "trailing-convert-arm",
                "trail_value": "25",
                "trail_unit": "dollars",
            }
        ),
    )
    client.post(workbench.path + "action", data={"action": "active-action-execute"})
    assert workbench._active_action_verified, workbench._message
    trace_path = tmp_path / "position-exit.jsonl"
    monkeypatch.setenv("IBKR_OPTIONS_MANAGER_CANCEL_TRACE", str(trace_path))
    handoffs = []
    listener = SimpleNamespace(
        _pause=lambda: handoffs.append("pause"),
        _resume=lambda: handoffs.append("resume"),
    )
    slot = _slot("127.0.0.1", 7497, 17)
    monkeypatch.setattr(slot, "listener", listener)
    broker = workbench._view_model._snapshots._broker
    original_capture = broker.capture
    reads = 0

    def capture(request):
        nonlocal reads
        with borrow_client("127.0.0.1", 7497, 17):
            result = original_capture(request)
            if isinstance(request, SnapshotRequest):
                reads += 1
                if (failure == "snapshot" and reads == 3) or (
                    failure == "final-snapshot" and reads == 4
                ):
                    result = replace(
                        result,
                        errors=(
                            f"synthetic broker snapshot failure for {DEMO_ACCOUNT}",
                        ),
                    )
                if failure == "quantity" and reads == 3:
                    result = replace(
                        result,
                        positions=tuple(
                            replace(position, quantity=position.quantity + 1)
                            if position.contract.con_id == request.option_con_id
                            else position
                            for position in result.positions
                        ),
                    )
            return result

    monkeypatch.setattr(broker, "capture", capture)
    markets = []
    method = "submit_market" if exit_kind == "market" else "submit_trailing"
    original_market = getattr(service._transport, method)
    label = "Market sell" if exit_kind == "market" else "Trailing exit"

    def market(*args, **kwargs):
        markets.append(args[1].quantity)
        assert handoffs == ["pause"]
        if failure == "ack":
            raise ExecutionOutcomeUnknown("synthetic lost exit acknowledgement")
        return original_market(*args, **kwargs)

    monkeypatch.setattr(service._transport, method, market)
    client.post(
        workbench.path + "action",
        data={
            "action": "position-exit-confirm"
            if exit_kind == "market"
            else "trailing-convert-confirm"
        },
    )
    final_entry = journal.submission_entries(
        account=entry.account, con_id=entry.con_id
    )[0]
    if failure in {"snapshot", "final-snapshot", "quantity"}:
        assert markets == []
        expected_count = 3 if failure == "final-snapshot" else 2
        assert [layer.cancelled for layer in final_entry.layers] == [
            index < expected_count for index in range(3)
        ]
        assert (
            "quantity changed"
            if failure == "quantity"
            else "synthetic broker snapshot failure"
        ) in workbench._message
        assert f"{expected_count} of 3" in workbench._message
        assert f"{label} was not submitted" in workbench._protection_change_message
        assert workbench._toast.title == (
            "Market exit stopped" if exit_kind == "market" else "Trailing exit stopped"
        )
        assert DEMO_ACCOUNT not in workbench._message
    elif failure == "ack":
        assert markets == [Decimal("3")]
        assert all(layer.cancelled for layer in final_entry.layers)
        assert f"{label} status is unconfirmed" in workbench._message
        assert "was not submitted" not in workbench._message
        assert service.unresolved_management_entries(
            account=entry.account, con_id=entry.con_id
        )
        restarted = ExecutionJournal(journal._path)
        assert restarted.unresolved_management_entries(
            account=entry.account, con_id=entry.con_id
        )
    else:
        assert markets == [Decimal("3")], workbench._message
        assert all(layer.cancelled for layer in final_entry.layers)
    assert handoffs == ["pause", "resume"]
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert DEMO_ACCOUNT not in trace_path.read_text()
    assert len({event["run_id"] for event in events}) == 1
    stopped_event = (
        "position_exit_stopped"
        if exit_kind == "market"
        else "trailing_conversion_stopped"
    )
    assert any(event["event"] == stopped_event for event in events) is (
        failure is not None
    )


def test_mstr_four_brackets_convert_five_contracts_beside_external_exit(tmp_path):
    workbench = _trailing_scenario_workbench(tmp_path, "standard")
    service = workbench._paper_execution
    assert service is not None
    journal = service._journal
    journal._write(
        (
            *journal._entries(),
            JournalEntry(
                fingerprint="c" * 64,
                account=DEMO_ACCOUNT,
                con_id=1_001_500_251,
                state="SUBMITTED",
                order_ids=tuple(range(701, 709)),
                perm_ids=tuple(range(801, 809)),
                oca_prefix="mstr-four-brackets",
                layers=tuple(
                    JournalLayer(q, "4.00", "2.00", "GTC") for q in (2, 1, 1, 1)
                ),
            ),
        )
    )
    workbench._select_locked(1_001_500_251)
    before = workbench._view_model.latest_snapshot()
    assert before is not None
    external = tuple(
        o for o in before.working_orders if o.perm_id not in range(801, 809)
    )
    assert len(external) == 1
    client = TestClient(workbench.app)
    page = client.post(
        workbench.path + "action",
        data={
            "action": "trailing-convert-arm",
            "trail_value": "25",
            "trail_unit": "dollars",
        },
    )
    plan = workbench._armed_trailing
    assert plan is not None, workbench._message
    assert plan.quantity == 5
    assert len(plan.candidates) == 4
    dialog = to_xml(workbench._trailing_conversion_dialog())
    assert re.search(r'"quantity":\s*5', dialog)
    assert "5 contracts remain reserved by existing TWS orders" in page.text
    client.post(workbench.path + "action", data={"action": "active-action-execute"})
    assert workbench._active_action_verified, workbench._message
    client.post(workbench.path + "action", data={"action": "trailing-convert-confirm"})
    entries = service.trailing_entries(account=DEMO_ACCOUNT, con_id=1_001_500_251)
    assert len(entries) == 1, workbench._message
    assert entries[0].trailing_quantity == 5
    after = workbench._view_model.latest_snapshot()
    assert after is not None
    assert (
        tuple(o for o in after.working_orders if o.perm_id == external[0].perm_id)
        == external
    )
    assert not any(o.perm_id in range(801, 809) for o in after.working_orders)
    # The new partial-position trail remains editable and cancellable beside TWS exits.
    perm_id = entries[0].perm_ids[0]
    client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"trailing_stop_{perm_id}": "2.80",
            f"trailing_limit_{perm_id}": "",
        },
    )
    assert workbench._armed_trailing_change is not None, workbench._message
    client.post(workbench.path + "action", data={"action": "active-action-execute"})
    assert workbench._active_action_verified, workbench._message
    client.post(workbench.path + "action", data={"action": "trailing-change-confirm"})
    assert (
        service.trailing_entries(account=DEMO_ACCOUNT, con_id=1_001_500_251)[
            0
        ].trailing_stop
        == "2.80"
    )
    client.post(
        workbench.path + "action", data={"action": f"trailing-delete-arm:{perm_id}"}
    )
    assert workbench._armed_trailing_change is not None, workbench._message
    client.post(workbench.path + "action", data={"action": "active-action-execute"})
    assert workbench._active_action_verified, workbench._message
    client.post(workbench.path + "action", data={"action": "trailing-change-confirm"})
    assert (
        service.trailing_entries(account=DEMO_ACCOUNT, con_id=1_001_500_251)[0].state
        == "CANCELLED_CONFIRMED"
    )
    final = workbench._view_model.latest_snapshot()
    assert final is not None
    assert final.working_orders == external


def test_submission_review_dialog_exposes_unknown_transmission_reason():
    workbench = _demo_workbench()
    workbench._submission_review_required = True
    workbench._submission_review_error = (
        "TWS did not acknowledge every submitted order before the deadline"
    )
    page = to_xml(workbench._submission_review_dialog())
    assert "Transmission not verified" in page
    assert workbench._submission_review_error in page
    assert "Do not resubmit this draft" in page
    assert "Orders sent to TWS" not in page


def test_one_sided_bracket_modal_and_contract_lock_survive_refresh_and_restart(
    tmp_path,
):
    from support.orders import _plan, _snapshot

    journal = ExecutionJournal(tmp_path / "submission.json")
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    entry = journal.begin(snapshot, _plan(_snapshot()))
    journal.mark_unknown(
        entry.fingerprint,
        reason="Possible one-sided bracket · stop vanished after target",
    )
    workbench._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(journal), journal
    )
    client = TestClient(workbench.app)
    page = client.get(workbench.path).text
    assert "Possible one-sided bracket" in page
    assert "stop vanished after target" in page
    assert f"{entry.oca_prefix or entry.fingerprint[:12]}/tranche-1" in page
    assert "Tranche ID" in page and "LMT target" in page and "STP loss" in page
    assert "Verify bracket no longer exists" in page
    client.post(workbench.path + "action", data={"action": "trailing-convert-arm"})
    assert "before making changes" in workbench._message
    assert workbench._armed_trailing is None
    refreshed = client.post(workbench.path + "action", data={"action": "refresh"})
    assert "Possible one-sided bracket" in refreshed.text
    restored = _demo_workbench()
    restored._paper_execution = PaperExecutionService(
        DemoPaperExecutionTransport(journal),
        ExecutionJournal(tmp_path / "submission.json"),
    )
    restored.load_demo_data()
    assert restored._submission_review_error == ""
    restored_client = TestClient(restored.app)
    assert "stop vanished after target" in restored_client.get(restored.path).text
    recovery = restored_client.post(
        restored.path + "action",
        data={
            "action": f"verify-cancelled-bracket:{entry.fingerprint}:0",
        },
    )
    assert "data-cancelled-bracket-recovery-dialog" in recovery.text
    assert "I confirm neither order is working" in recovery.text


def test_filled_layer_with_a_surviving_leg_keeps_bracket_failure_lock(
    tmp_path, monkeypatch
):
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    from support.orders import _plan, _snapshot

    journal = ExecutionJournal(tmp_path / "failure.json")
    entry = journal.begin(snapshot, _plan(_snapshot()))
    entry = journal.mark_unknown(entry.fingerprint, reason="Possible one-sided bracket")
    monkeypatch.setattr(
        workbench,
        "_submission_outcomes",
        lambda: (
            (entry, 0, LayerOutcome("CLOSED_PROFIT", filled_quantity=Decimal("2"))),
        ),
    )
    orphan = WorkingOrder(
        perm_id=123,
        client_id=17,
        order_id=123,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("2"),
        status="Submitted",
        oca_group=f"{entry.oca_prefix or entry.fingerprint[:12]}/tranche-1",
    )
    monkeypatch.setattr(
        workbench._view_model,
        "latest_snapshot",
        lambda: replace(snapshot, executions_complete=True, working_orders=(orphan,)),
    )
    assert len(workbench._unresolved_bracket_submissions()) == 1
    monkeypatch.setattr(
        workbench._view_model,
        "latest_snapshot",
        lambda: replace(snapshot, executions_complete=True, working_orders=()),
    )
    assert workbench._unresolved_bracket_submissions() == ()


def test_bracket_failure_lock_clears_only_after_fresh_healthy_pair_reconciliation(
    tmp_path,
):
    workbench = _trailing_scenario_workbench(tmp_path, "convert")
    service = workbench._paper_execution
    snapshot = workbench._view_model.latest_snapshot()
    assert service is not None and snapshot is not None
    entry = service.submission_entries(
        account=snapshot.selected.account, con_id=snapshot.selected.con_id
    )[0]
    service._journal.mark_unknown(
        entry.fingerprint, reason="Possible one-sided bracket"
    )
    assert workbench._unresolved_bracket_submissions()
    service.reconcile_snapshot(snapshot)
    assert workbench._unresolved_bracket_submissions() == ()
    workbench._submission_review_required = False
    assert workbench._submission_review_dialog() is None


@pytest.mark.parametrize("failure", [None, "fill", "quantity", "expired"])
def test_market_layer_shows_acknowledged_order_and_cancel_race(
    tmp_path, monkeypatch, failure
):
    workbench = _trailing_scenario_workbench(tmp_path, "convert")
    client = TestClient(workbench.app)
    client.post(workbench.path + "action", data={"action": "market-exit-selected"})
    client.post(workbench.path + "action", data={"action": "active-action-execute"})
    submitted = client.post(
        workbench.path + "action", data={"action": "position-exit-confirm"}
    )
    assert 'data-layer-state="market"' in submitted.text, workbench._message
    assert "Working" in submitted.text
    assert "≈" in submitted.text
    assert "Confirm cancellation" not in submitted.text
    assert "Cancel this active market order" in submitted.text
    entry = workbench._market_entries()[0]
    assert entry.market_quantity == 3
    assert entry.state == "SUBMITTED"
    snapshot = workbench._view_model.latest_snapshot()
    order = next(o for o in snapshot.working_orders if o.order_type == "MKT")
    service = workbench._paper_execution
    calls = []
    original = service._transport.cancel_market

    def cancel(*args, **kwargs):
        calls.append(args[1].order.order_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(service._transport, "cancel_market", cancel)
    # Clicking the trash control only stages the existing sidebar flow.
    staged = client.post(
        workbench.path + "action", data={"action": f"market-cancel-arm:{order.perm_id}"}
    )
    assert calls == []
    sidebar = staged.text.split("ACTION REVIEW", maxsplit=1)[1]
    assert "CANCEL MARKET ORDER" in sidebar
    assert f"TWS order {order.order_id}" in sidebar
    assert "Review cancellation" in sidebar
    assert 'value="market-cancel-confirm"' not in sidebar
    assert workbench._active_action_verified is False
    assert "Outcome projection" not in sidebar
    # A direct confirm cannot bypass the fresh Review step.
    client.post(workbench.path + "action", data={"action": "market-cancel-confirm"})
    assert calls == []
    reviewed = client.post(
        workbench.path + "action", data={"action": "active-action-execute"}
    )
    assert workbench._active_action_verified, workbench._message
    assert 'value="market-cancel-confirm"' in reviewed.text
    assert calls == []
    if failure in {"fill", "quantity"}:
        orders = () if failure == "fill" else (replace(order, remaining=Decimal("1")),)
        monkeypatch.setattr(
            workbench,
            "_position_exit_snapshot_locked",
            lambda: replace(snapshot, working_orders=orders),
        )
    elif failure == "expired":
        workbench._armed_execution_deadline = 0
    result = client.post(
        workbench.path + "action", data={"action": "market-cancel-confirm"}
    )
    assert calls == ([order.order_id] if failure is None else [])
    if failure == "fill":
        assert "may have filled" in workbench._message
    elif failure == "quantity":
        assert "changed after review" in workbench._message
    elif failure == "expired":
        assert "Review expired" in workbench._message
    else:
        assert 'data-layer-state="cancelled-market"' in result.text
        assert workbench._market_entries()[0].state == "CANCELLED_CONFIRMED"


def test_filled_market_layer_shows_actual_fill_and_closed_position_history(
    tmp_path, monkeypatch
):
    from ibkr_options_manager.domain import ObservedExecution

    workbench = _trailing_scenario_workbench(tmp_path, "convert")
    client = TestClient(workbench.app)
    client.post(workbench.path + "action", data={"action": "market-exit-selected"})
    client.post(workbench.path + "action", data={"action": "active-action-execute"})
    client.post(workbench.path + "action", data={"action": "position-exit-confirm"})
    service = workbench._paper_execution
    entry = workbench._market_entries()[0]
    snapshot = workbench._view_model.latest_snapshot()
    filled = replace(
        snapshot,
        working_orders=(),
        executions=(
            ObservedExecution(
                exec_id="market.01",
                account=entry.account,
                con_id=entry.con_id,
                perm_id=entry.perm_ids[0],
                side="SLD",
                quantity=Decimal("3"),
                price=Decimal("9.11"),
                time="now",
                realized_pnl=Decimal("153"),
                currency="USD",
            ),
        ),
    )
    service.record_executions(filled)
    monkeypatch.setattr(workbench._view_model, "latest_snapshot", lambda: filled)
    entry = workbench._market_entries()[0]
    row = to_xml(workbench._market_layer_row(1, entry))
    assert 'data-layer-state="sold-market"' in row
    assert 'value="9.11"' in row
    assert "≈" not in row
    assert "Confirm cancellation" not in row
    position = next(p for p in workbench._state.positions if p.con_id == entry.con_id)
    workbench._session_closed_positions[entry.con_id] = position
    closed = to_xml(workbench._closed_session_workspace(entry.con_id))
    assert 'data-layer-state="sold-market"' in closed
    assert "153.00" in closed
    restored = ExecutionJournal(service._journal._path).market_entries(
        account=entry.account, con_id=entry.con_id
    )[0]
    assert restored.market_quantity == 3
    assert len(restored.fills) == 1
