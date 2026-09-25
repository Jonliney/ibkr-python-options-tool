from decimal import Decimal
from threading import Event
from types import SimpleNamespace

from ibkr_options_manager.broker.ibkr import _capture, _OrderDraft
from ibkr_options_manager.broker.read_only import (
    CapturedCompletedOrder,
    CapturedExecution,
)


def _order(*, order_id: int) -> _OrderDraft:
    return _OrderDraft(
        perm_id=201,
        client_id=17,
        order_id=order_id,
        account="DU1234567",
        con_id=917_864_414,
        action="SELL",
        order_type="LMT",
        oca_group="aabbccddeeff/tranche-1",
        parent_id=0,
        limit_price=Decimal("1.20"),
        stop_price=None,
        tif="GTC",
    )


def test_capture_prefers_client_bound_order_over_nonbinding_all_order_view() -> None:
    app = SimpleNamespace(
        all_order_drafts={0: _order(order_id=0)},
        all_order_statuses={0: ("Submitted", Decimal("2"))},
        client_order_drafts={101: _order(order_id=101)},
        client_order_statuses={101: ("Submitted", Decimal("2"))},
        events={"quote": Event()},
        quote_values={},
        market_data_type="UNKNOWN",
        completion_times={},
        positions=[],
        contract_details=[],
        server_version=None,
        server_time=None,
        read_only_api=False,
        localhost_only=True,
        managed_accounts=(),
        market_rule=None,
        errors=[],
    )

    capture = _capture(app, epoch=1, connected=True)

    assert len(capture.orders) == 1
    assert capture.orders[0].perm_id == 201
    assert capture.orders[0].order_id == 101


def test_capture_reports_completed_orders_and_latest_corrected_execution() -> None:
    executed = Event()
    executed.set()
    completed = Event()
    completed.set()
    fill = CapturedExecution(
        exec_id="trade.2",
        account="DU1234567",
        con_id=917_864_414,
        perm_id=201,
        side="SLD",
        quantity=Decimal("2"),
        price=Decimal("1.20"),
        time="now",
    )
    app = SimpleNamespace(
        all_order_drafts={},
        all_order_statuses={},
        client_order_drafts={},
        client_order_statuses={},
        events={
            "quote": Event(),
            "executions": executed,
            "completed_orders": completed,
        },
        quote_values={},
        market_data_type="UNKNOWN",
        completion_times={},
        positions=[],
        contract_details=[],
        server_version=None,
        server_time=None,
        read_only_api=False,
        localhost_only=True,
        managed_accounts=(),
        market_rule=None,
        errors=[],
        history_errors=[],
        execution_drafts={
            "trade.1": CapturedExecution(
                exec_id="trade.1",
                account=fill.account,
                con_id=fill.con_id,
                perm_id=fill.perm_id,
                side=fill.side,
                quantity=Decimal("1"),
                price=fill.price,
                time=fill.time,
            ),
            "trade.2": fill,
        },
        commission_reports={"trade.2": (Decimal("25.40"), "USD")},
        completed_order_drafts={
            201: CapturedCompletedOrder(
                account=fill.account,
                con_id=fill.con_id,
                perm_id=201,
                order_id=101,
                client_id=17,
                action="SELL",
                order_type="LMT",
                oca_group="aabbccddeeff/tranche-1",
                status="Filled",
            )
        },
    )

    capture = _capture(app, epoch=1, connected=True)

    assert capture.executions_complete
    assert len(capture.executions) == 1
    assert capture.executions[0].exec_id == "trade.2"
    assert capture.executions[0].quantity == Decimal("2")
    assert capture.executions[0].realized_pnl == Decimal("25.40")
    assert capture.completed_orders_complete
    assert capture.completed_orders[0].status == "Filled"
