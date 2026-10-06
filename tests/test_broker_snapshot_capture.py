import json
from decimal import Decimal
from threading import Event
from time import monotonic
from types import SimpleNamespace

from ibkr_options_manager.broker.ibkr import (
    _await,
    _capture,
    _OrderDraft,
    _request_quote_and_rule,
)
from ibkr_options_manager.broker.read_only import (
    CapturedCompletedOrder,
    CapturedExecution,
)
from ibkr_options_manager.cancellation_trace import current_snapshot_context


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


def test_snapshot_wait_trace_is_correlated_and_omits_order_identity(
    monkeypatch, tmp_path
) -> None:
    trace_path = tmp_path / "bracket-cancellations.jsonl"
    monkeypatch.setenv("IBKR_OPTIONS_MANAGER_CANCEL_TRACE", str(trace_path))
    complete = Event()
    complete.set()
    app = SimpleNamespace(events={"quote": complete}, errors=[])
    context_token = current_snapshot_context.set(("test-run", "before_pair", 2))
    try:
        assert _await(app, "quote", monotonic() + 1, "quote timed out")
    finally:
        current_snapshot_context.reset(context_token)

    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert len(events) == 1
    assert events[0]["event"] == "snapshot_request_wait_complete"
    assert events[0]["run_id"] == "test-run"
    assert events[0]["snapshot_role"] == "before_pair"
    assert events[0]["bracket_number"] == 2
    assert events[0]["phase"] == "quote"
    assert events[0]["complete"] is True
    assert events[0]["duration_ms"] >= 0
    assert "account" not in events[0]
    assert "order_id" not in events[0]


def test_orders_only_capture_requests_market_rule_without_quote(monkeypatch) -> None:
    from ibkr_options_manager.broker import ibkr

    monkeypatch.setattr(ibkr, "_select_market_rule", lambda _details: 123)
    monkeypatch.setattr(
        ibkr, "_market_rule_exchange", lambda _details, _rule_id: "SMART"
    )
    requested: list[int] = []
    completed = Event()
    completed.set()
    app = SimpleNamespace(
        contract_details_raw=[SimpleNamespace(contract=object())],
        events={"market_rule": completed},
        errors=[],
        reqMarketRule=requested.append,
        reqMktData=lambda *_args: (_ for _ in ()).throw(
            AssertionError("quote request must be skipped")
        ),
    )

    _request_quote_and_rule(app, monotonic() + 1, include_quote=False)

    assert requested == [123]
    assert app.errors == []


def test_quote_capture_finishes_from_streaming_bid_and_ask(monkeypatch) -> None:
    from ibkr_options_manager.broker import ibkr

    monkeypatch.setattr(ibkr, "_select_market_rule", lambda _details: 123)
    monkeypatch.setattr(ibkr, "_market_rule_exchange", lambda *_args: "SMART")
    quote_ready = Event()
    market_rule_done = Event()
    market_rule_done.set()
    requests = []
    cancellations = []

    def request_quote(*args):
        requests.append(args)
        quote_ready.set()

    app = SimpleNamespace(
        contract_details_raw=[SimpleNamespace(contract=object())],
        quote_request_id=9202,
        quote_ready=quote_ready,
        quote_any_price=quote_ready,
        quote_values={"bid": Decimal("1"), "ask": Decimal("1")},
        events={"market_rule": market_rule_done},
        errors=[],
        reqMarketDataType=lambda _value: None,
        reqMktData=request_quote,
        cancelMktData=cancellations.append,
        reqMarketRule=lambda _value: None,
        _complete=lambda name: completed.append(name),
    )
    completed = []

    _request_quote_and_rule(app, monotonic() + 1)

    assert len(requests) == 1
    assert requests[0][3] is False
    assert cancellations == [9202]
    assert completed == ["quote"]


def test_quote_capture_keeps_partial_stream_quote_without_waiting_for_snapshot(
    monkeypatch,
) -> None:
    from ibkr_options_manager.broker import ibkr

    monkeypatch.setattr(ibkr, "_select_market_rule", lambda _details: 123)
    monkeypatch.setattr(ibkr, "_market_rule_exchange", lambda *_args: "SMART")
    monkeypatch.setattr(ibkr, "_QUOTE_SIDE_GRACE_SECONDS", 0)
    any_price = Event()
    any_price.set()
    market_rule_done = Event()
    market_rule_done.set()
    requests = []
    completed = []
    app = SimpleNamespace(
        contract_details_raw=[SimpleNamespace(contract=object())],
        quote_request_id=9202,
        quote_ready=Event(),
        quote_any_price=any_price,
        quote_values={"last": Decimal("1")},
        events={"market_rule": market_rule_done},
        errors=[],
        reqMarketDataType=lambda _value: None,
        reqMktData=lambda *args: requests.append(args),
        cancelMktData=lambda _value: None,
        reqMarketRule=lambda _value: None,
        _complete=completed.append,
    )

    _request_quote_and_rule(app, monotonic() + 1)

    assert [request[3] for request in requests] == [False]
    assert completed == ["quote"]
    assert app.quote_values == {"last": Decimal("1")}


def test_quote_capture_falls_back_when_stream_has_no_positive_price(
    monkeypatch,
) -> None:
    from ibkr_options_manager.broker import ibkr

    monkeypatch.setattr(ibkr, "_select_market_rule", lambda _details: 123)
    monkeypatch.setattr(ibkr, "_market_rule_exchange", lambda *_args: "SMART")
    monkeypatch.setattr(ibkr, "_QUOTE_STREAM_WAIT_SECONDS", 0)
    requests = []
    cancellations = []
    quote_done = Event()
    market_rule_done = Event()
    market_rule_done.set()

    def request_quote(*args):
        requests.append(args)
        if args[3] is True:
            quote_done.set()

    app = SimpleNamespace(
        contract_details_raw=[SimpleNamespace(contract=object())],
        quote_request_id=9202,
        quote_ready=Event(),
        quote_any_price=Event(),
        quote_values={"bid": Decimal("1")},
        market_data_type="LIVE",
        events={"quote": quote_done, "market_rule": market_rule_done},
        errors=[],
        reqMarketDataType=lambda _value: None,
        reqMktData=request_quote,
        cancelMktData=cancellations.append,
        reqMarketRule=lambda _value: None,
    )

    _request_quote_and_rule(app, monotonic() + 1)

    assert [request[3] for request in requests] == [False, True]
    assert cancellations == [9202]
    assert app.quote_request_id == 9204
    assert app.quote_values == {}
    assert app.market_data_type == "UNKNOWN"
    assert app.errors == []
