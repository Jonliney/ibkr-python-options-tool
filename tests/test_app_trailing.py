import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread

import pytest
from starhtml import to_xml
from starlette.testclient import TestClient

from ibkr_options_manager.app.demo import (
    DEMO_ACCOUNT,
    DEMO_SCENARIOS,
    DemoPaperExecutionTransport,
    DemoReadOnlyBroker,
)
from ibkr_options_manager.app.web import StarUIWorkbench
from ibkr_options_manager.app.web.surface import (
    _action_notice,
    _money,
    _trailing_fill_summary,
)
from ibkr_options_manager.broker import SnapshotRequest
from ibkr_options_manager.domain import (
    round_down_price,
)
from ibkr_options_manager.execution import (
    ExecutionBlocked,
    ExecutionJournal,
    ExecutionOutcomeUnknown,
    JournalEntry,
    JournalFill,
    JournalLayer,
    LayerOutcome,
    PaperExecutionService,
)
from ibkr_options_manager.portfolio import PortfolioCoordinator
from ibkr_options_manager.snapshot import SnapshotCoordinator
from support.workbench import _demo_workbench, _trailing_scenario_workbench


def test_closed_trail_keeps_row_but_withholds_pnl_without_report() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    con_id = 1_004_470_201
    workbench._session_closed_positions[con_id] = next(
        position for position in workbench._state.positions if position.con_id == con_id
    )
    workbench._selected_closed_con_id = con_id
    workbench._selected_con_id = None
    trailing = JournalEntry(
        fingerprint="trailing-conversion:" + "a" * 64,
        account=DEMO_ACCOUNT,
        con_id=con_id,
        state="SUBMITTED",
        perm_ids=(90210,),
        trailing_quantity=3,
        trailing_stop="8.80",
        trailing_value="0.25",
        trailing_unit="dollars",
        trailing_limit_offset="0.10",
        trailing_tif="GTC",
        fills=(JournalFill("trail.01", 90210, "SLD", "3", "9.10", "now"),),
    )

    class History:
        def submission_entries(self, **_kwargs):
            return ()

        def trailing_entries(self, **_kwargs):
            return (trailing,)

    workbench._paper_execution = History()  # type: ignore[assignment]
    page = to_xml(workbench._page())
    assert 'data-layer-state="sold-trailing"' in page
    assert "P&amp;L pending" in page or "P&L pending" in page
    assert "Realised P&amp;L" in page or "Realised P&L" in page
    assert "+$0.00" not in page


@pytest.mark.parametrize("reported_pnl", ["147.75", "0"])
def test_closed_trail_uses_raw_tws_reported_pnl(reported_pnl: str) -> None:
    entry = JournalEntry(
        fingerprint="trailing-conversion:" + "d" * 64,
        account=DEMO_ACCOUNT,
        con_id=1_004_470_201,
        state="SUBMITTED",
        perm_ids=(90210,),
        trailing_quantity=3,
        fills=(
            JournalFill(
                "trail.01", 90210, "SLD", "3", "9.10", "now", reported_pnl, "USD"
            ),
        ),
    )
    summary = _trailing_fill_summary(entry)
    assert summary is not None
    assert summary.quantity == Decimal("3")
    assert summary.realized_pnl == Decimal(reported_pnl)


def test_trailing_pnl_sums_matching_partial_fill_reports() -> None:
    entry = JournalEntry(
        fingerprint="trailing-conversion:" + "e" * 64,
        account=DEMO_ACCOUNT,
        con_id=1_004_470_201,
        state="SUBMITTED",
        perm_ids=(90210,),
        trailing_quantity=3,
        fills=(
            JournalFill("trail.01", 90210, "SLD", "1", "9.00", "first", "39", "USD"),
            JournalFill("trail.02", 90210, "SLD", "2", "9.20", "second", "118", "USD"),
        ),
    )
    summary = _trailing_fill_summary(entry)
    assert summary is not None
    assert summary.quantity == Decimal("3")
    assert summary.realized_pnl == Decimal("157")


def test_named_trailing_demo_scenarios_expose_distinct_states(tmp_path: Path) -> None:
    assert set(DEMO_SCENARIOS) == {
        "standard",
        "convert",
        "working-trail",
        "partial-fill",
        "manual-cancel",
        "closed-trail",
        "no-bid",
    }
    convert = _trailing_scenario_workbench(tmp_path / "convert", "convert")
    assert convert._planning_available_quantity() == 1
    convert_page = TestClient(convert.app).get(convert.path).text
    assert 'data-layer-state="working"' in convert_page
    conversion_review = TestClient(convert.app).post(
        convert.path + "action",
        data={
            "action": "trailing-convert-arm",
            "trail_value": "25",
            "trail_unit": "dollars",
            "trail_limit_value": "10",
            "trail_limit_unit": "dollars",
        },
    )
    assert conversion_review.status_code == 200
    sidebar = conversion_review.text.split("ACTION REVIEW", maxsplit=1)[1]
    assert sidebar.index("CANCEL BRACKET") < sidebar.index("TRAIL-1")
    assert "OCA-1" in sidebar
    assert "2 contracts · GTC" in sidebar
    assert sidebar.index("TRAIL AMT") < sidebar.index("SELL STP")
    assert "$25" in sidebar
    assert "SELL STP LMT" in sidebar
    assert convert._armed_trailing is not None
    assert convert._armed_trailing.request.trail_value == Decimal("0.25")
    assert convert._armed_trailing.request.limit_value == Decimal("0.10")
    assert convert._armed_trailing.limit_offset == Decimal("0.10")
    assert "Limit offset" not in sidebar
    assert "Initial limit estimate" not in sidebar
    assert "Fill risk" not in sidebar
    assert "$8.70" in conversion_review.text
    percent_review = TestClient(convert.app).post(
        convert.path + "action",
        data={
            "action": "trailing-convert-arm",
            "trail_value": "25",
            "trail_unit": "dollars",
            "trail_limit_value": "1",
            "trail_limit_unit": "percent",
        },
    )
    assert percent_review.status_code == 200
    assert convert._armed_trailing is not None
    assert convert._armed_trailing.request.limit_value == Decimal("1")
    assert convert._armed_trailing.request.limit_unit == "percent"
    assert convert._armed_trailing.limit_offset == Decimal("0.09")
    stop_only = replace(convert._armed_trailing, limit_offset=None)
    stop_only_review = "".join(map(str, convert._review_trailing_plan(stop_only)))
    assert "TRAIL-1" in stop_only_review
    assert "2 contracts · GTC" in stop_only_review
    assert "TRAIL AMT" in stop_only_review
    assert "SELL STP" in stop_only_review
    assert "SELL STP LMT</span>" not in stop_only_review
    assert stop_only_review.index("CANCEL BRACKET") < stop_only_review.index("TRAIL-1")

    working = _trailing_scenario_workbench(tmp_path / "working", "working-trail")
    working_page = TestClient(working.app).get(working.path).text
    assert 'data-layer-state="trailing"' in working_page
    assert "TRAIL 1</span>" in working_page
    assert "TRAIL 1 · TRAIL STP LMT" not in working_page
    assert "Active" in working_page
    recorded = working._paper_execution.trailing_entries(
        account=DEMO_ACCOUNT, con_id=1_004_470_201
    )
    assert recorded[0].trailing_last_stop == "8.80"
    assert recorded[0].trailing_last_limit == "8.70"

    partial = _trailing_scenario_workbench(tmp_path / "partial", "partial-fill")
    assert partial._state.positions[-1].quantity == "1"
    assert partial._state.working_orders[0].remaining == "1"
    snapshot = partial._view_model.latest_snapshot()
    assert snapshot is not None
    assert snapshot.executions[0].quantity == Decimal("2")

    cancelled = _trailing_scenario_workbench(tmp_path / "cancel", "manual-cancel")
    cancelled_page = TestClient(cancelled.app).get(cancelled.path).text
    assert 'data-layer-state="trailing"' in cancelled_page
    assert "CHECK TWS" in cancelled_page

    no_bid = _trailing_scenario_workbench(tmp_path / "no-bid", "no-bid")
    snapshot = no_bid._view_model.latest_snapshot()
    assert snapshot is not None
    assert snapshot.quote.bid is None
    no_bid_dialog = to_xml(no_bid._trailing_conversion_dialog())
    assert "data-trail-stop-preview" in no_bid_dialog
    blocked_review = TestClient(no_bid.app).post(
        no_bid.path + "action",
        data={
            "action": "trailing-convert-arm",
            "trail_value": "25",
            "trail_unit": "dollars",
        },
    )
    assert blocked_review.status_code == 200
    assert "couldn't verify a current bid" in no_bid._message
    assert no_bid._feedback is not None
    assert "couldn't verify a current bid" in no_bid._feedback.description


def test_working_trail_limit_review_update_and_delete(tmp_path: Path) -> None:
    workbench = _trailing_scenario_workbench(tmp_path / "change", "working-trail")
    client = TestClient(workbench.app)
    entry = workbench._trailing_entries()[0]
    perm_id = entry.perm_ids[0]
    page = client.get(workbench.path).text
    assert f'name="trailing_value_{perm_id}"' in page
    assert f'name="trailing_offset_{perm_id}"' in page
    assert f'value="trailing-delete-arm:{perm_id}"' in page
    assert 'aria-label="Delete trail 1"' in page
    assert 'aria-label="Review trail 1 changes"' not in page
    assert 'value="active-update-arm"' in page
    assert "data-trailing-review-row" in page
    trailing_row = page.split(f'data-trailing-perm-id="{perm_id}"', 1)[1].split(
        "data-layer-state=", 1
    )[0]
    stop_input = re.search(
        rf'<input[^>]+name="trailing_value_{perm_id}"[^>]*>', trailing_row
    )
    gap_input = re.search(
        rf'<input[^>]+name="trailing_offset_{perm_id}"[^>]*>', trailing_row
    )
    quantity_input = re.search(
        rf'<input[^>]+id="trailing-quantity-{perm_id}"[^>]*>', trailing_row
    )
    assert stop_input is not None
    assert re.search(r"\sdisabled(?:\s|>)", stop_input.group()) is None
    assert gap_input is not None
    assert re.search(r"\sdisabled(?:\s|>)", gap_input.group()) is None
    assert quantity_input is not None
    assert re.search(r"\sreadonly(?:\s|>)", quantity_input.group())
    assert "Last recorded STP" not in page
    assert "Last recorded LMT" not in page

    reviewed = client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"trailing_stop_{perm_id}": "8.80",
            f"trailing_limit_{perm_id}": "8.65",
        },
    )
    assert reviewed.status_code == 200
    assert "TRAIL-1" in reviewed.text
    assert "SELL STP LMT</span>" in reviewed.text
    assert workbench._armed_trailing_change is not None
    staged_candidate = workbench._armed_trailing_change
    stale_snapshot = workbench._view_model.latest_snapshot()
    assert stale_snapshot is not None
    client.post(workbench.path + "action", data={"action": "active-action-execute"})
    assert workbench._active_action_verified
    client.post(workbench.path + "action", data={"action": "trailing-change-confirm"})
    assert workbench._trailing_entries()[0].trailing_limit_offset == "0.15", (
        workbench._message
    )
    with pytest.raises(ExecutionBlocked, match="limit gap changed"):
        workbench._paper_execution.submit_trailing_change(
            stale_snapshot,
            staged_candidate,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )

    deleted = client.post(
        workbench.path + "action",
        data={"action": f"trailing-delete-arm:{perm_id}"},
    )
    assert "TRAIL-1" in deleted.text
    client.post(workbench.path + "action", data={"action": "active-action-execute"})
    assert workbench._active_action_verified
    client.post(workbench.path + "action", data={"action": "trailing-change-confirm"})
    assert workbench._trailing_entries()[0].state == "CANCELLED_CONFIRMED"


def test_trail_gap_can_be_added_and_removed_but_stop_is_required(
    tmp_path: Path,
) -> None:
    workbench = _trailing_scenario_workbench(tmp_path, "convert")
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
    assert workbench._active_action_verified, workbench._message
    client.post(workbench.path + "action", data={"action": "trailing-convert-confirm"})
    entry = workbench._trailing_entries()[0]
    perm_id = entry.perm_ids[0]
    assert workbench._state.working_orders[0].order_type == "TRAIL"
    page = client.get(workbench.path).text
    for field in ("value", "offset"):
        input_markup = re.search(
            rf'<input[^>]+name="trailing_{field}_{perm_id}"[^>]*>', page
        )
        assert input_markup is not None
        assert re.search(r"\sdisabled(?:\s|>)", input_markup.group()) is None

    blank_stop = client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"trailing_stop_{perm_id}": "",
            f"trailing_limit_{perm_id}": "8.70",
        },
    )
    assert "A STP is required before entering a LMT." in blank_stop.text
    assert workbench._armed_trailing_change is None

    for limit, expected_type in (("8.70", "TRAIL LIMIT"), ("", "TRAIL")):
        reviewed = client.post(
            workbench.path + "action",
            data={
                "action": "active-update-arm",
                f"trailing_stop_{perm_id}": "8.80",
                f"trailing_limit_{perm_id}": limit,
            },
        )
        assert "TRAIL-1" in reviewed.text
        assert workbench._armed_trailing_change is not None, workbench._message
        client.post(workbench.path + "action", data={"action": "active-action-execute"})
        assert workbench._active_action_verified, workbench._message
        client.post(
            workbench.path + "action", data={"action": "trailing-change-confirm"}
        )
        assert workbench._state.working_orders[0].order_type == expected_type
        assert workbench._trailing_entries()[0].trailing_limit_offset == (
            "0.10" if limit else ""
        )


def test_trail_change_rejects_manual_gap_change(tmp_path: Path) -> None:
    workbench = _trailing_scenario_workbench(tmp_path / "manual", "working-trail")
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    entry = workbench._trailing_entries()[0]
    changed = replace(
        snapshot,
        working_orders=tuple(
            replace(order, limit_offset=Decimal("0.20"))
            if order.perm_id == entry.perm_ids[0]
            else order
            for order in snapshot.working_orders
        ),
    )
    with pytest.raises(ExecutionBlocked, match="limit gap changed"):
        workbench._paper_execution.prepare_trailing_change(
            changed,
            perm_id=entry.perm_ids[0],
            expected_client_id=17,
            stop_price=Decimal("8.80"),
            limit_offset=Decimal("0.15"),
        )


def test_trail_change_rejects_partial_fill_disconnect_and_stale_review(
    tmp_path: Path,
) -> None:
    partial = _trailing_scenario_workbench(tmp_path / "partial", "partial-fill")
    partial_snapshot = partial._view_model.latest_snapshot()
    assert partial_snapshot is not None
    with pytest.raises(ExecutionBlocked, match="no longer safely modifiable"):
        partial._paper_execution.prepare_trailing_change(
            partial_snapshot,
            perm_id=partial._trailing_entries()[0].perm_ids[0],
            expected_client_id=17,
            stop_price=Decimal("8.80"),
            limit_offset=Decimal("0.15"),
        )

    workbench = _trailing_scenario_workbench(tmp_path / "stale", "working-trail")
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    service = workbench._paper_execution
    perm_id = workbench._trailing_entries()[0].perm_ids[0]
    candidate = service.prepare_trailing_change(
        snapshot,
        perm_id=perm_id,
        expected_client_id=17,
        stop_price=Decimal("8.80"),
        limit_offset=Decimal("0.15"),
    )
    with pytest.raises(ExecutionBlocked, match="connection snapshot"):
        service.submit_trailing_change(
            replace(snapshot, connected=False),
            candidate,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )
    changed = replace(
        snapshot,
        working_orders=tuple(
            replace(order, stop_price=Decimal("8.75"))
            if order.perm_id == perm_id
            else order
            for order in snapshot.working_orders
        ),
    )
    with pytest.raises(ExecutionBlocked, match="changed after review"):
        service.submit_trailing_change(
            changed,
            candidate,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )
    with pytest.raises(ExecutionBlocked, match="price increments"):
        service.prepare_trailing_change(
            snapshot,
            perm_id=perm_id,
            expected_client_id=17,
            stop_price=Decimal("8.801"),
            limit_offset=Decimal("0.15"),
        )


def test_unknown_trail_change_locks_followup_after_restart(tmp_path: Path) -> None:
    workbench = _trailing_scenario_workbench(tmp_path / "unknown", "working-trail")
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    service = workbench._paper_execution
    candidate = service.prepare_trailing_change(
        snapshot,
        perm_id=workbench._trailing_entries()[0].perm_ids[0],
        expected_client_id=17,
        stop_price=Decimal("8.80"),
        limit_offset=Decimal("0.15"),
    )

    class NoAcknowledgement:
        def change_trailing(self, *_args, **_kwargs):
            raise ExecutionOutcomeUnknown("simulated missing acknowledgement")

    service._transport = NoAcknowledgement()
    with pytest.raises(ExecutionOutcomeUnknown, match="missing acknowledgement"):
        service.submit_trailing_change(
            snapshot,
            candidate,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )
    assert service.unresolved_management_entries(
        account=snapshot.selected.account, con_id=snapshot.selected.con_id
    )
    reopened = ExecutionJournal(service._journal._path)
    restarted = PaperExecutionService(DemoPaperExecutionTransport(reopened), reopened)
    with pytest.raises(ExecutionBlocked, match="uncertain order outcome"):
        restarted.submit_trailing_change(
            snapshot,
            candidate,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )


def test_closed_trailing_demo_moves_position_to_session_history_on_refresh(
    tmp_path: Path,
) -> None:
    workbench = _trailing_scenario_workbench(tmp_path, "closed-trail")
    client = TestClient(workbench.app)
    initial = client.get(workbench.path).text
    assert 1_004_470_201 in workbench._session_closed_positions
    assert workbench._selected_closed_con_id == 1_004_470_201
    assert 'data-layer-state="sold-trailing"' in initial
    assert initial.count('data-layer-state="sold"') == 2
    assert "+$340.00" in initial
    assert "-$110.00" in initial
    assert "+$150.00" in initial
    assert 'for="trailing-stop-810201"' in initial
    assert 'for="trailing-limit-810201"' in initial
    assert "How was" not in initial
    assert 'value="8.80"' in initial
    assert 'value="8.70"' in initial
    assert "WORKING" not in initial
    trail = workbench._paper_execution.trailing_entries(
        account=DEMO_ACCOUNT, con_id=1_004_470_201
    )[0]
    closed_row = to_xml(workbench._trailing_layer_row(3, trail, closed_position=True))
    assert ">Closed<" in closed_row
    assert ">CLOSED<" not in closed_row
    assert "TRAIL LIMIT" not in closed_row
    assert ">STP<" in closed_row
    assert ">LMT<" in closed_row
    assert "<details" not in closed_row
    assert "Last observed in TWS" not in closed_row
    assert "Derived from recorded stop and offset" not in closed_row
    assert "trail</p>" not in closed_row
    assert "Average fill" not in closed_row


def test_trailing_review_failure_uses_trailing_feedback_title() -> None:
    notice = _action_notice("Trailing exit blocked: the quote is unavailable")
    assert notice is not None
    assert notice.title == "Couldn't review trailing exit"


def test_submitted_trailing_exit_appears_as_central_layer_row() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    entry = JournalEntry(
        fingerprint="trailing-conversion:" + "a" * 64,
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        state="SUBMITTED",
        perm_ids=(90210,),
        trailing_quantity=2,
        trailing_stop="0.80",
        trailing_value="0.10",
        trailing_unit="dollars",
        trailing_limit_offset="0.05",
        trailing_tif="GTC",
    )

    class History:
        def owned_perm_ids(self, **_kwargs):
            return frozenset()

        def submission_entries(self, **_kwargs):
            return ()

        def trailing_entries(self, **_kwargs):
            return (entry,)

    workbench._paper_execution = History()  # type: ignore[assignment]
    page = to_xml(workbench._page())
    assert 'data-layer-state="trailing"' in page
    assert "TRAIL 1</span>" in page
    assert "LMT offset</label>" in page
    assert "CHECK TWS" in page
    trailing_row = to_xml(workbench._trailing_layer_row(1, entry))
    assert 'class="layer-row-grid ' in trailing_row
    assert ">TIF<" not in trailing_row
    assert "GTC" not in trailing_row
    assert "font-mono" not in trailing_row
    assert "disabled" in trailing_row
    assert "STP" in trailing_row
    assert "Quantity" in trailing_row
    filled = replace(
        entry,
        fills=(JournalFill("trail.01", 90210, "SLD", "2", "0.95", "now"),),
    )
    closed_row = to_xml(workbench._trailing_layer_row(1, filled, closed_position=True))
    assert 'data-layer-state="sold-trailing"' in closed_row
    assert "Average fill" not in closed_row
    assert "P&amp;L pending" in closed_row or "P&L pending" in closed_row
    incomplete = replace(
        entry,
        fills=(JournalFill("trail.01", 90210, "SLD", "1", "0.95", "now"),),
    )
    unresolved_row = to_xml(
        workbench._trailing_layer_row(1, incomplete, closed_position=True)
    )
    assert 'data-layer-state="sold-trailing"' not in unresolved_row
    assert "VERIFY IN TWS" in unresolved_row


def test_trailing_dialog_has_one_clickable_trigger_button() -> None:
    workbench = _demo_workbench()
    dialog_markup = to_xml(workbench._trailing_conversion_dialog())
    trigger_markup = dialog_markup.split("<dialog", 1)[0]
    assert trigger_markup.count("<button") == 1
    assert 'aria-label="Set a trailing exit for this position"' in trigger_markup
    assert "evt.currentTarget.blur()" in trigger_markup
    assert "Set a trailing exit for this position" in dialog_markup
    assert "Set a trailing exit" in dialog_markup
    assert "Choose a trailing stop or trailing limit" in dialog_markup
    assert dialog_markup.index(">Cancel<") < dialog_markup.index("Review trailing exit")
    assert 'd="M9 19h8.5' in trigger_markup
    assert 'aria-label="TRAIL AMT unit"' in dialog_markup
    assert 'aria-label="Trailing limit offset unit"' in dialog_markup
    assert 'name="trail_unit" data-bind="trailing_amount_unit"' in dialog_markup
    assert 'name="trail_limit_unit" data-bind="trailing_limit_unit"' in dialog_markup
    assert 'name="trail_tif"' not in dialog_markup
    assert ">DAY<" not in dialog_markup
    assert dialog_markup.index('aria-label="TRAIL AMT unit"') < dialog_markup.index(
        'name="trail_value"'
    )
    assert dialog_markup.index(
        'aria-label="Trailing limit offset unit"'
    ) < dialog_markup.index('name="trail_limit_value"')


def test_trailing_conversion_dialog_shows_demo_position_prices() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    markup = to_xml(workbench._trailing_conversion_dialog())
    assert "Average position price" in markup
    assert "Latest bid" in markup
    assert "Latest ask" not in markup
    assert 'value="25"' in markup
    assert "TRAIL AMT</label>" in markup
    assert "LMT offset (optional)</label>" in markup
    assert snapshot.quote.bid is not None
    expected_stop = round_down_price(
        snapshot.quote.bid - Decimal("25") / snapshot.contract.multiplier,
        snapshot.market_rule.bands,
    )
    assert f"${expected_stop:,.2f}" in markup
    expected_change = (
        (expected_stop - snapshot.position.unit_basis)
        * snapshot.contract.multiplier
        * Decimal("5")
    )
    expected_outcome = (
        f"+${expected_change:,.2f} gain"
        if expected_change >= 0
        else f"-${abs(expected_change):,.2f} estimated loss at stop"
    )
    assert expected_outcome in markup
    assert "data-trail-stop-preview" in markup
    assert "data-trail-outcome-preview" in markup
    assert "data-trail-limit-price-preview" in markup
    assert "data-trail-limit-outcome-preview" in markup
    assert "Enter the dollar move for one contract" not in markup
    assert "A percentage limit offset becomes" not in markup
    assert "mt-4" in markup
    assert 'class="grid gap-4"' in markup
    assert markup.index('name="trail_limit_value"') < markup.index(
        "Average position price"
    )
    assert markup.index("Latest bid") < markup.index("Review trailing exit")
    assert f"${snapshot.position.unit_basis:,.2f}" in markup
    assert f"${snapshot.quote.bid:,.2f}" in markup


@pytest.mark.parametrize("with_bracket", [False, True])
@pytest.mark.parametrize("bid_changes_before_review", [False, True])
@pytest.mark.parametrize("epoch_changes_before_review", [False, True])
def test_demo_entire_position_trailing_conversion_includes_unassigned_contracts(
    tmp_path,
    with_bracket,
    bid_changes_before_review,
    epoch_changes_before_review,
) -> None:
    def clock() -> Decimal:
        return Decimal("100")

    from ibkr_options_manager.app.view_model import PlannerViewModel

    journal = ExecutionJournal(tmp_path / "demo-trailing.json")
    if with_bracket:
        journal._write(
            (
                JournalEntry(
                    fingerprint="b" * 64,
                    account=DEMO_ACCOUNT,
                    con_id=1_004_470_201,
                    state="SUBMITTED",
                    order_ids=(701, 702),
                    perm_ids=(801, 802),
                    oca_prefix="demo-trailing-test",
                    layers=(JournalLayer(1, "12.00", "7.00", "GTC"),),
                ),
            )
        )

    class MovingBidBroker(DemoReadOnlyBroker):
        bid_adjustment = Decimal("0")
        epoch_adjustment = 0
        advance_epoch_each_capture = False

        def capture(self, request):
            captured = super().capture(request)
            if isinstance(request, SnapshotRequest):
                if self.bid_adjustment:
                    captured = replace(
                        captured,
                        quote=replace(
                            captured.quote,
                            bid=captured.quote.bid + self.bid_adjustment,
                        ),
                    )
                if self.advance_epoch_each_capture:
                    self.epoch_adjustment += 1
                    captured = replace(
                        captured,
                        connection_epoch=captured.connection_epoch
                        + self.epoch_adjustment,
                    )
            return captured

    broker = MovingBidBroker(clock=clock, paper_execution_enabled=True)
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
        initial_con_id=1_004_470_201,
        demo_mode=True,
        paper_execution=PaperExecutionService(
            DemoPaperExecutionTransport(journal), journal
        ),
    )
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    review = client.post(
        workbench.path + "action",
        data={
            "action": "trailing-convert-arm",
            "trail_value": "25",
            "trail_unit": "dollars",
            "trail_limit_value": "10",
            "trail_limit_unit": "dollars",
            "trail_tif": "DAY",
        },
    )
    assert review.status_code == 200
    assert "TRAIL-1" in review.text
    assert "3 contracts · GTC" in review.text
    assert ("CANCEL BRACKET" in review.text) is with_bracket
    assert "TRAIL AMT" in review.text
    assert "SELL STP LMT" in review.text
    if bid_changes_before_review:
        broker.bid_adjustment = Decimal("0.05")
    if epoch_changes_before_review:
        broker.advance_epoch_each_capture = True
    verified = client.post(
        workbench.path + "action", data={"action": "active-action-execute"}
    )
    assert 'value="trailing-convert-confirm"' in verified.text
    assert "Set trailing exit" in verified.text
    if not with_bracket:
        assert "Confirm cancels every reviewed app-owned bracket" not in verified.text
    if bid_changes_before_review:
        assert workbench._armed_trailing is not None
        assert workbench._armed_trailing.reference_price == Decimal("9.10")
        assert workbench._armed_trailing.initial_stop == Decimal("8.85")
        assert "$8.85" in verified.text
    result = client.post(
        workbench.path + "action", data={"action": "trailing-convert-confirm"}
    )
    assert result.status_code == 200
    assert 'data-layer-state="trailing"' in result.text
    assert re.search(r"TRAIL [12]</span>", result.text)
    assert "Active" in result.text
    for field in ("value", "offset"):
        input_markup = re.search(
            rf'<input[^>]+name="trailing_{field}_[0-9]+"[^>]*>', result.text
        )
        assert input_markup is not None
        assert re.search(r"\sdisabled(?:\s|>)", input_markup.group()) is None
    assert (
        journal.trailing_entries(account=DEMO_ACCOUNT, con_id=1_004_470_201)[
            -1
        ].trailing_tif
        == "GTC"
    )
    if with_bracket:
        original = journal.find("b" * 64)
        assert original is not None and original.layers[0].cancelled


@pytest.mark.parametrize("limit", ["8.65", "8.10", ""])
def test_trail_projection_uses_limit_estimate_in_staged_review(
    tmp_path: Path, limit: str
) -> None:
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    entry = workbench._trailing_entries()[0]
    perm_id = entry.perm_ids[0]
    client = TestClient(workbench.app)
    baseline, observed, _ = workbench._projection_state()
    assert baseline == observed
    assert baseline.covered_quantity == Decimal("3")
    assert baseline.expected_gain is not None
    initial_projection = to_xml(
        workbench._outcome_projection(workbench._projection_state())
    )
    assert 'data-live-metric="gain"' in initial_projection
    assert 'data-live-metric="loss"' not in initial_projection
    assert "P&L at STP" not in initial_projection
    reviewed = client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"trailing_stop_{perm_id}": "8.80",
            f"trailing_limit_{perm_id}": limit,
        },
    )
    candidate = workbench._armed_trailing_change
    assert candidate is not None, workbench._message
    before, after, config = workbench._projection_state()
    basis, multiplier = workbench._state.unit_basis, workbench._state.multiplier
    assert basis is not None and multiplier is not None
    expected = (Decimal(limit or "8.80") - basis) * multiplier * 3
    assert before.expected_gain == baseline.expected_gain
    assert after.expected_gain == expected
    assert after.max_loss == (Decimal("8.80") - basis) * multiplier * 3
    assert config["staged"]
    review = to_xml(workbench._review_trailing_change(candidate))
    assert "TRAIL-1" in review
    assert "3 contracts · GTC" in review
    assert "SELL STP LMT</span>" in review
    assert "border-l-2 border-border pl-3" in review
    assert "Remove limit gap" not in review
    assert "trails use the shown LMT" in reviewed.text
    assert _money(expected) in reviewed.text
    projection_markup = to_xml(
        workbench._outcome_projection(workbench._projection_state())
    )
    assert 'data-live-metric="loss"' not in projection_markup
    assert "grid-cols-1" in projection_markup
    assert "Stops can slip; limits may not fill" in projection_markup
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    workbench._view_model._latest_snapshot = replace(snapshot, fresh=False)
    assert workbench._projection_state()[1].expected_gain is None


@pytest.mark.parametrize("extra", ["bracket", "draft", "pending"])
def test_trail_with_other_bracket_work_keeps_both_projection_cards(
    tmp_path, monkeypatch, extra
):
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    if extra == "bracket":
        from ibkr_options_manager.app.view_model import WorkingOrderLine

        workbench._state = replace(
            workbench._state,
            working_orders=(
                *workbench._state.working_orders,
                WorkingOrderLine(
                    perm_id=900,
                    action="SELL",
                    order_type="LMT",
                    remaining="1",
                    status="Submitted",
                ),
            ),
        )
    elif extra == "draft":
        from ibkr_options_manager.app.view_model import DraftLayerForm

        workbench._drafts[workbench._selected_con_id] = (
            DraftLayerForm(
                quantity="1",
                target_price="12.04",
                stop_price="6.88",
                target_percentage="40",
                stop_percentage="20",
            ),
        )
    else:
        monkeypatch.setattr(
            workbench,
            "_pending_submissions",
            lambda: ((None, 0, LayerOutcome("UNKNOWN")),),
        )
    projection = to_xml(workbench._outcome_projection(workbench._projection_state()))
    assert 'data-live-metric="gain"' in projection
    assert 'data-live-metric="loss"' in projection
    assert "grid-cols-2" in projection


def test_trail_conversion_review_replaces_two_cards_with_one(tmp_path):
    workbench = _trailing_scenario_workbench(tmp_path, "convert")
    client = TestClient(workbench.app)
    assert 'data-live-metric="loss"' in to_xml(
        workbench._outcome_projection(workbench._projection_state())
    )
    client.post(
        workbench.path + "action",
        data={
            "action": "trailing-convert-arm",
            "trail_value": "25",
            "trail_unit": "dollars",
        },
    )
    assert workbench._armed_trailing is not None, workbench._message
    projection = to_xml(workbench._outcome_projection(workbench._projection_state()))
    assert 'data-live-metric="gain"' in projection
    assert 'data-live-metric="loss"' not in projection


def test_trail_absolute_limit_matches_expected_gain(tmp_path: Path) -> None:
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    perm_id = workbench._trailing_entries()[0].perm_ids[0]
    client = TestClient(workbench.app)
    page = client.get(workbench.path).text
    assert f'name="trailing_offset_{perm_id}"' in page
    response = client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"trailing_stop_{perm_id}": "8.30",
            f"trailing_limit_{perm_id}": "8.10",
        },
    )
    candidate = workbench._armed_trailing_change
    assert candidate is not None, workbench._message
    assert candidate.limit_offset == Decimal("0.20")
    assert workbench._projection_state()[1].expected_gain == Decimal("-150.00")
    assert "-$150.00" in response.text
    review = to_xml(workbench._review_trailing_change(candidate))
    assert "$8.10" in review
    assert "$0.20" not in review


@pytest.mark.parametrize(
    "stop, limit, message",
    [
        ("8.30", "8.40", "LMT must be lower than STP."),
        ("8.30", "8.30", "LMT must be lower than STP."),
        ("", "8.10", "A STP is required before entering a LMT."),
        ("", "", "A STP is required."),
        ("0", "8.10", "Enter a positive STP price."),
        ("8.30", "-1", "Enter a positive LMT price, or leave it blank."),
    ],
)
def test_invalid_trail_prices_cannot_stage(
    tmp_path: Path, stop: str, limit: str, message: str
) -> None:
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    perm_id = workbench._trailing_entries()[0].perm_ids[0]
    response = TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"trailing_stop_{perm_id}": stop,
            f"trailing_limit_{perm_id}": limit,
        },
    )
    assert workbench._armed_trailing_change is None
    assert message in response.text


def test_outdated_trail_gap_form_does_not_remove_limit(tmp_path: Path) -> None:
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    perm_id = workbench._trailing_entries()[0].perm_ids[0]
    response = TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"trailing_stop_{perm_id}": "8.30",
            f"trailing_gap_{perm_id}": "0.20",
        },
    )
    assert workbench._armed_trailing_change is None
    assert "Refresh and enter the STP and LMT prices again." in response.text
    assert workbench._trailing_entries()[0].trailing_limit_offset == "0.10"


@pytest.mark.parametrize("limit", ["8.10", ""])
def test_review_trail_prices_goes_directly_to_confirm_and_retains_inputs(
    tmp_path: Path, limit: str
) -> None:
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    client = TestClient(workbench.app)
    entry = workbench._trailing_entries()[0]
    perm_id = entry.perm_ids[0]
    reviewed = client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"trailing_value_{perm_id}": "75",
            f"trailing_value_unit_{perm_id}": "dollars",
            f"trailing_offset_unit_{perm_id}": "dollars",
            f"trailing_offset_{perm_id}": "20" if limit else "",
        },
    )
    assert workbench._armed_trailing_change is not None, workbench._message
    assert workbench._active_action_verified
    assert workbench._armed_execution_deadline is not None
    assert 'value="trailing-change-confirm"' in reviewed.text
    assert 'value="active-action-execute"' not in reviewed.text
    for field, value in (("value", "75"), ("offset", "20" if limit else "")):
        node = re.search(
            rf'<input[^>]*name="trailing_{field}_{perm_id}"[^>]*>', reviewed.text
        )
        assert node is not None
        assert f'value="{value}"' in node.group()
        assert re.search(r"\sreadonly(?:\s|>)", node.group())
    # Review has no broker write. Cancel restores the observed order values.
    assert workbench._trailing_entries()[0].trailing_limit_offset == "0.10"
    cancelled = client.post(workbench.path + "action", data={"action": "cancel-staged"})
    assert workbench._armed_trailing_change is None
    assert not workbench._active_action_verified
    assert 'value="trailing-change-confirm"' not in cancelled.text
    node = re.search(
        rf'<input[^>]*name="trailing_value_{perm_id}"[^>]*>', cancelled.text
    )
    assert node is not None and 'value="25.00"' in node.group()


@pytest.mark.parametrize("failure", ["disconnect", "manual-change"])
def test_direct_trail_review_fails_closed_when_verification_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    perm_id = workbench._trailing_entries()[0].perm_ids[0]
    original = workbench._view_model.select_position
    captures = 0

    def capture(*args, **kwargs):
        nonlocal captures
        state = original(*args, **kwargs)
        captures += 1
        if captures == 2:
            snapshot = workbench._view_model.latest_snapshot()
            assert snapshot is not None
            workbench._view_model._latest_snapshot = (
                replace(snapshot, connected=False)
                if failure == "disconnect"
                else replace(
                    snapshot,
                    working_orders=tuple(
                        replace(order, stop_price=Decimal("8.75"))
                        if order.perm_id == perm_id
                        else order
                        for order in snapshot.working_orders
                    ),
                )
            )
        return state

    monkeypatch.setattr(workbench._view_model, "select_position", capture)
    reviewed = TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"trailing_stop_{perm_id}": "8.30",
            f"trailing_limit_{perm_id}": "8.10",
        },
    )
    assert captures == 2
    assert workbench._armed_trailing_change is None
    assert not workbench._active_action_verified
    assert workbench._armed_execution_deadline is None
    assert 'value="trailing-change-confirm"' not in reviewed.text
    assert "blocked" in workbench._message
    assert workbench._trailing_entries()[0].trailing_limit_offset == "0.10"


@pytest.mark.parametrize(
    ("value", "unit", "offset", "offset_unit", "stop", "gap"),
    [
        ("75", "dollars", "20", "dollars", "8.30", "0.20"),
        ("10", "percent", "10", "percent", "8.14", "0.82"),
        ("25", "dollars", "25", "dollars", "8.80", "0.25"),
    ],
)
def test_trail_distance_review_and_confirmation_record_exact_rule(
    tmp_path: Path,
    value: str,
    unit: str,
    offset: str,
    offset_unit: str,
    stop: str,
    gap: str,
) -> None:
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    client = TestClient(workbench.app)
    perm_id = workbench._trailing_entries()[0].perm_ids[0]
    reviewed = client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"trailing_value_{perm_id}": value,
            f"trailing_value_unit_{perm_id}": unit,
            f"trailing_offset_{perm_id}": offset,
            f"trailing_offset_unit_{perm_id}": offset_unit,
        },
    )
    candidate = workbench._armed_trailing_change
    assert candidate is not None, workbench._message
    assert candidate.stop_price == Decimal(stop)
    assert candidate.limit_offset == Decimal(gap)
    assert candidate.trail_unit == unit
    assert candidate.trail_value == (
        Decimal(value) / 100 if unit == "dollars" else Decimal(value)
    )
    assert 'value="trailing-change-confirm"' in reviewed.text
    assert workbench._trailing_entries()[0].trailing_value == "0.25"
    for name, expected in (
        (f"trailing_value_{perm_id}", value),
        (f"trailing_offset_{perm_id}", offset),
        (f"trailing_offset_unit_{perm_id}", offset_unit),
    ):
        node = re.search(rf'<input[^>]*name="{name}"[^>]*>', reviewed.text)
        assert node is not None and f'value="{expected}"' in node.group()
    client.post(workbench.path + "action", data={"action": "trailing-change-confirm"})
    entry = workbench._trailing_entries()[0]
    assert entry.trailing_value == format(candidate.trail_value, "f")
    assert entry.trailing_unit == unit
    assert Decimal(entry.trailing_limit_offset) == Decimal(gap)
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    order = next(order for order in snapshot.working_orders if order.perm_id == perm_id)
    assert (
        order.trailing_amount if unit == "dollars" else order.trailing_percent
    ) == candidate.trail_value
    # The updated durable rule is accepted by the next management review.
    assert workbench._paper_execution is not None
    snapshot = workbench._view_model.latest_snapshot()
    assert snapshot is not None
    workbench._paper_execution.prepare_trailing_change(
        snapshot,
        perm_id=perm_id,
        expected_client_id=workbench._settings.client_id,
        cancel=True,
    )


@pytest.mark.parametrize("action", ["cancel-all-active", "market-exit-selected"])
def test_trail_toolbar_cancel_and_position_exit_require_review(
    tmp_path, monkeypatch, action
):
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    client = TestClient(workbench.app)
    service = workbench._paper_execution
    assert service is not None
    writes = []
    original = service._transport.change_trailing

    def cancel(*args, **kwargs):
        writes.append("cancel-trail")
        return original(*args, **kwargs)

    monkeypatch.setattr(service._transport, "change_trailing", cancel)
    original_market = service._transport.submit_market

    def market(*args, **kwargs):
        writes.append(("market", args[1].quantity))
        return original_market(*args, **kwargs)

    monkeypatch.setattr(service._transport, "submit_market", market)
    page = client.get(workbench.path).text
    for label in (
        "Set all stops…",
        "Raise stops to B/E",
        "Cancel all exits",
        "Sell all brackets now",
    ):
        assert f'aria-label="{label}"' in page
    reviewed = client.post(workbench.path + "action", data={"action": action})
    assert writes == []
    assert 'value="active-action-execute"' in reviewed.text
    client.post(workbench.path + "action", data={"action": "active-action-execute"})
    confirm = (
        "trailing-change-confirm"
        if action == "cancel-all-active"
        else "position-exit-confirm"
    )
    assert workbench._active_action_verified, workbench._message
    assert writes == []
    client.post(workbench.path + "action", data={"action": confirm})
    assert writes == (
        ["cancel-trail"]
        if action == "cancel-all-active"
        else ["cancel-trail", ("market", Decimal("3"))]
    )
    assert workbench._trailing_entries()[0].state == "CANCELLED_CONFIRMED"


def test_set_trail_trigger_uses_fresh_bid_and_preserves_limit_gap(tmp_path):
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    perm_id = workbench._trailing_entries()[0].perm_ids[0]
    client = TestClient(workbench.app)
    client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"trailing_value_{perm_id}": "55",
            f"trailing_value_unit_{perm_id}": "dollars",
            f"trailing_offset_{perm_id}": "10",
            f"trailing_offset_unit_{perm_id}": "dollars",
            f"trailing_exact_stop_{perm_id}": "8.50",
        },
    )
    candidate = workbench._armed_trailing_change
    assert candidate is not None, workbench._message
    assert candidate.stop_price == Decimal("8.50")
    assert candidate.trail_value == Decimal("0.55")
    assert candidate.limit_offset == Decimal("0.10")
    assert candidate.stop_price - candidate.limit_offset == Decimal("8.40")


def test_raise_trail_stop_to_be_does_not_lower_a_higher_fresh_stop(tmp_path):
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    perm_id = workbench._trailing_entries()[0].perm_ids[0]
    client = TestClient(workbench.app)
    client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            "raise_stops_only": "yes",
            f"trailing_value_{perm_id}": "45",
            f"trailing_value_unit_{perm_id}": "dollars",
            f"trailing_offset_{perm_id}": "10",
            f"trailing_offset_unit_{perm_id}": "dollars",
            f"trailing_exact_stop_{perm_id}": "8.60",
        },
    )
    assert workbench._armed_trailing_change is None
    assert "already at or above B/E" in workbench._message
    assert workbench._trailing_entries()[0].trailing_stop == "8.80"


def test_position_exit_stops_if_position_changes_after_trail_cancellation(
    tmp_path, monkeypatch
):
    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    service = workbench._paper_execution
    assert service is not None
    client = TestClient(workbench.app)
    client.post(workbench.path + "action", data={"action": "market-exit-selected"})
    client.post(workbench.path + "action", data={"action": "active-action-execute"})
    assert workbench._active_action_verified
    original = workbench._position_exit_snapshot_locked
    reads = 0

    def capture():
        nonlocal reads
        snapshot = original()
        reads += 1
        return (
            replace(
                snapshot, position=replace(snapshot.position, quantity=Decimal("2"))
            )
            if reads == 2
            else snapshot
        )

    monkeypatch.setattr(workbench, "_position_exit_snapshot_locked", capture)
    markets = []
    monkeypatch.setattr(
        service._transport,
        "submit_market",
        lambda *args, **kwargs: markets.append(args),
    )
    client.post(workbench.path + "action", data={"action": "position-exit-confirm"})
    assert markets == []
    assert workbench._trailing_entries()[0].state == "CANCELLED_CONFIRMED"
    assert "1 of 1 exits confirmed cancelled" in workbench._message
    assert "Market sell was not submitted" in workbench._message


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("pending_cancel", [False, True])
def test_order_hint_reconciles_manual_trail_cancellation_without_position_change(
    tmp_path, terminal, pending_cancel
):
    from ibkr_options_manager.broker.read_only import CapturedCompletedOrder

    workbench = _trailing_scenario_workbench(tmp_path, "working-trail")
    service = workbench._paper_execution
    assert service is not None
    entry = workbench._trailing_entries()[0]
    broker = workbench._view_model._snapshots._broker
    original_capture = broker.capture

    def cancelled_capture(request):
        captured = original_capture(request)
        return replace(
            captured,
            orders=tuple(o for o in captured.orders if o.perm_id != entry.perm_ids[0]),
            completed_orders=(
                CapturedCompletedOrder(
                    entry.account,
                    entry.con_id,
                    entry.perm_ids[0],
                    entry.order_ids[0],
                    17,
                    "SELL",
                    "TRAIL LIMIT",
                    "",
                    "Cancelled",
                ),
            )
            if terminal
            else (),
        )

    drafts = dict(workbench._drafts)
    initial_quantity = workbench._view_model.latest_snapshot().position.quantity
    # Stage an edit first; a new callback must invalidate it before confirmation.
    client = TestClient(workbench.app)
    client.post(
        workbench.path + "action",
        data={
            "action": "active-update-arm",
            f"trailing_stop_{entry.perm_ids[0]}": "8.70",
            f"trailing_limit_{entry.perm_ids[0]}": "8.60",
        },
    )
    assert workbench._armed_trailing_change is not None
    if pending_cancel:
        before = workbench._view_model.latest_snapshot()
        before = replace(before, captured_at=before.captured_at - 1)
        candidate = service.prepare_trailing_change(
            before, perm_id=entry.perm_ids[0], expected_client_id=17, cancel=True
        )

        class LostCancellationAck:
            def change_trailing(self, *_args, **_kwargs):
                raise ExecutionOutcomeUnknown("lost cancellation acknowledgement")

        service._transport = LostCancellationAck()
        with pytest.raises(ExecutionOutcomeUnknown):
            service.submit_trailing_change(
                before,
                candidate,
                host="127.0.0.1",
                port=7497,
                client_id=17,
                timeout_seconds=1,
            )
    broker.capture = cancelled_capture
    # Queue the event after review, without changing the held quantity.
    workbench._pending_order_observation = True
    workbench._pending_observation = True
    worker = Thread(target=workbench._observation_loop, daemon=True)
    worker.start()
    workbench._observer_signal.set()
    try:
        for _ in range(100):
            if workbench._protection_change_message:
                break
            Event().wait(0.01)
        assert workbench._protection_change_message
        assert (
            workbench._view_model.latest_snapshot().position.quantity
            == initial_quantity
        )
        assert workbench._drafts == drafts
        assert workbench._armed_trailing_change is None
        assert workbench._active_action_verified is False
        fragment = client.get(workbench.path + "inventory-fragment")
        assert fragment.headers["X-Protection-Change"]
        page = client.get(workbench.path).text
        assert ('data-layer-state="cancelled-trailing"' in page) is terminal
        assert ("CHECK TWS" in page) is not terminal
        assert workbench._trailing_entries()[0].state == (
            "CANCELLED_CONFIRMED" if terminal else "SUBMITTED"
        )
        assert ("data-contract-lockdown" in page) is (pending_cancel and not terminal)
        if terminal:
            assert 'data-result-tone="cancelled"' in page
            assert "Remove this cancelled layer from view" in page
            hidden = client.post(
                workbench.path + "action",
                data={
                    "action": (
                        f"dismiss-cancelled-trail:{entry.fingerprint}:"
                        f"{entry.snapshot_captured_at}"
                    )
                },
            )
            assert 'data-layer-state="cancelled-trailing"' not in hidden.text
            assert workbench._trailing_entries()[0].hidden_from_workspace
            assert workbench._trailing_entries()[0].perm_ids == entry.perm_ids
            refreshed = client.post(
                workbench.path + "action", data={"action": "refresh"}
            )
            assert 'data-layer-state="cancelled-trailing"' not in refreshed.text
            assert (
                ExecutionJournal(service._journal._path)
                .find(entry.fingerprint)
                .hidden_from_workspace
            )
    finally:
        workbench.close()
        worker.join(timeout=2)


def test_trailing_review_survives_pending_order_hint(tmp_path):
    workbench = _trailing_scenario_workbench(tmp_path, "convert")
    client = TestClient(workbench.app)
    client.post(
        workbench.path + "action",
        data={
            "action": "trailing-convert-arm",
            "trail_value": "25",
            "trail_unit": "dollars",
            "trail_limit_value": "10",
            "trail_limit_unit": "dollars",
        },
    )
    assert workbench._armed_trailing is not None
    workbench._pending_order_observation = True
    reviewed = client.post(
        workbench.path + "action", data={"action": "active-action-execute"}
    )
    assert workbench._armed_trailing is not None, workbench._message
    assert workbench._active_action_verified, workbench._message
    assert 'value="trailing-convert-confirm"' in reviewed.text
    assert not workbench._paper_execution.trailing_entries(
        account=DEMO_ACCOUNT, con_id=workbench._selected_con_id
    )
    # A further callback between review and confirmation is still subject to
    # the confirmation's fresh bracket/position/fill checks.
    workbench._pending_order_observation = True
    client.post(workbench.path + "action", data={"action": "trailing-convert-confirm"})
    entries = workbench._paper_execution.trailing_entries(
        account=DEMO_ACCOUNT, con_id=workbench._selected_con_id
    )
    assert len(entries) == 1, workbench._message
    assert entries[0].state == "SUBMITTED"


@pytest.mark.parametrize("verified", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_background_order_hint_preserves_trailing_review_only_when_verified(
    tmp_path, monkeypatch, changed, verified
):
    workbench = _trailing_scenario_workbench(tmp_path, "convert")
    client = TestClient(workbench.app)
    client.post(
        workbench.path + "action",
        data={
            "action": "trailing-convert-arm",
            "trail_value": "25",
            "trail_unit": "dollars",
        },
    )
    if verified:
        client.post(workbench.path + "action", data={"action": "active-action-execute"})
    plan = workbench._armed_trailing
    deadline = workbench._armed_execution_deadline
    assert plan is not None
    assert workbench._active_action_verified is verified
    if changed:
        broker = workbench._view_model._snapshots._broker
        capture = broker.capture

        def changed_capture(request):
            result = capture(request)
            return replace(
                result,
                orders=tuple(
                    replace(order, limit_price=order.limit_price + Decimal("0.01"))
                    if order.limit_price is not None
                    else order
                    for order in result.orders
                ),
            )

        monkeypatch.setattr(broker, "capture", changed_capture)
    workbench._pending_order_observation = True
    workbench._pending_observation = True
    revision = workbench._inventory_revision
    worker = Thread(target=workbench._observation_loop, daemon=True)
    worker.start()
    workbench._observer_signal.set()
    try:
        for _ in range(100):
            if workbench._inventory_revision > revision:
                break
            Event().wait(0.01)
        assert workbench._inventory_revision > revision
        if changed:
            assert workbench._armed_trailing is None
            assert "Trailing exit review invalidated" in workbench._message
        else:
            assert workbench._armed_trailing == plan
            assert workbench._active_action_verified is verified
            assert workbench._armed_execution_deadline == deadline
    finally:
        workbench.close()
        worker.join(timeout=2)
