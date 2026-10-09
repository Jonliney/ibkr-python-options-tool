import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ibkr_options_manager.broker.execution import (
    IbkrPaperExecutionBroker,
    PaperSubmission,
    _build_submission_contract,
    _cancel_order,
)
from ibkr_options_manager.domain import (
    BrokerSnapshot,
    ContractKey,
    LayerRequest,
    MarketRule,
    ObservedCompletedOrder,
    ObservedExecution,
    ObservedPosition,
    PlanRequest,
    PriceBand,
    Quote,
    RemainderPolicy,
    VerifiedOptionContract,
    WorkingOrder,
    build_exit_plan,
)
from ibkr_options_manager.execution import (
    ExecutionBlocked,
    ExecutionJournal,
    ExecutionOutcomeUnknown,
    JournalEntry,
    JournalFill,
    JournalLayer,
    MarketExitCandidate,
    PaperExecutionService,
    PriceUpdateCandidate,
    classify_journal_layer,
    require_paper_execution_snapshot,
)
from ibkr_options_manager.ibkr_probe import _IbapiImports
from ibkr_options_manager.trailing import TrailingRequest, plan_entire_position


def _snapshot(*, read_only_api: bool = False) -> BrokerSnapshot:
    key = ContractKey(account="DU1234567", con_id=917_864_414)
    return BrokerSnapshot(
        selected=key,
        connected=True,
        read_only_api=read_only_api,
        api_read_only_observed=True,
        localhost_only=True,
        paper_account_verified=True,
        complete=True,
        fresh=True,
        connection_epoch=1,
        errors=(),
        contract=VerifiedOptionContract(
            con_id=key.con_id,
            sec_type="OPT",
            expiry="20260916",
            strike=Decimal("7605"),
            right="C",
            multiplier=Decimal("100"),
            currency="USD",
            trading_class="SPXW",
            exchange="SMART",
            local_symbol="SPXW  260916C07605000",
        ),
        position=ObservedPosition(
            key=key,
            quantity=Decimal("2"),
            raw_average_cost=Decimal("100"),
            unit_basis=Decimal("1.00"),
        ),
        working_orders=(),
        quote=Quote(
            bid=Decimal("0.95"),
            ask=Decimal("1.05"),
            last=Decimal("1.00"),
            close=Decimal("0.90"),
            market_data_type="DELAYED",
            fresh=True,
        ),
        market_rule=MarketRule(
            exchange="SMART",
            bands=(PriceBand(Decimal("0"), Decimal("0.05")),),
        ),
    )


def _trailing_snapshot() -> BrokerSnapshot:
    return replace(
        _snapshot(), executions_complete=True, completed_orders_complete=True
    )


def test_trailing_plan_covers_all_unassigned_contracts_and_fixed_limit_offset() -> None:
    snapshot = _trailing_snapshot()
    plan = plan_entire_position(
        snapshot,
        (),
        TrailingRequest(
            trail_value=Decimal("10"),
            trail_unit="percent",
            limit_value=Decimal("10"),
            limit_unit="percent",
        ),
    )

    assert plan.quantity == 2
    assert plan.unassigned_quantity == 2
    assert plan.initial_stop == Decimal("0.85")
    assert plan.limit_offset == Decimal("0.10")


def test_trailing_plan_rejects_day_time_in_force() -> None:
    with pytest.raises(ExecutionBlocked, match="TIF must be GTC"):
        plan_entire_position(
            _trailing_snapshot(),
            (),
            TrailingRequest(Decimal("0.10"), "dollars", tif="DAY"),
        )


def test_trailing_baseline_accepts_new_capture_epoch_but_rejects_basis_change() -> None:
    snapshot = _trailing_snapshot()
    plan = plan_entire_position(
        snapshot, (), TrailingRequest(Decimal("0.10"), "dollars")
    )

    PaperExecutionService.verify_trailing_baseline(
        replace(snapshot, connection_epoch=snapshot.connection_epoch + 1), plan
    )
    with pytest.raises(ExecutionBlocked, match="position basis changed"):
        PaperExecutionService.verify_trailing_baseline(
            replace(
                snapshot,
                position=replace(
                    snapshot.position,
                    raw_average_cost=snapshot.position.raw_average_cost
                    + Decimal("1"),
                ),
            ),
            plan,
        )


def test_trailing_plan_rejects_external_order_and_stale_bid() -> None:
    snapshot = _trailing_snapshot()
    external = WorkingOrder(
        perm_id=99,
        client_id=1,
        order_id=9,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("1"),
        status="Submitted",
    )
    with pytest.raises(ExecutionBlocked, match="another working order"):
        plan_entire_position(
            replace(snapshot, working_orders=(external,)),
            (),
            TrailingRequest(Decimal("0.10"), "dollars"),
        )
    with pytest.raises(ExecutionBlocked, match="couldn't verify a current bid"):
        plan_entire_position(
            replace(snapshot, quote=replace(snapshot.quote, fresh=False)),
            (),
            TrailingRequest(Decimal("0.10"), "dollars"),
        )
    with pytest.raises(ExecutionBlocked, match="Check the bid in TWS"):
        plan_entire_position(
            replace(snapshot, quote=replace(snapshot.quote, bid=None)),
            (),
            TrailingRequest(Decimal("0.10"), "dollars"),
        )


def test_trailing_plan_rejects_invalid_tick_and_insufficient_stop_room() -> None:
    snapshot = _trailing_snapshot()
    with pytest.raises(ExecutionBlocked, match="valid price increment"):
        plan_entire_position(snapshot, (), TrailingRequest(Decimal("0.11"), "dollars"))
    with pytest.raises(ExecutionBlocked, match="at or below zero"):
        plan_entire_position(snapshot, (), TrailingRequest(Decimal("1.00"), "dollars"))


def test_trailing_submission_is_journaled_once_and_rejects_position_change(
    tmp_path,
) -> None:
    snapshot = _trailing_snapshot()
    plan = plan_entire_position(
        snapshot, (), TrailingRequest(Decimal("0.10"), "dollars")
    )

    class Transport:
        calls = 0

        def submit_trailing(self, *args, **kwargs):
            self.calls += 1
            return PaperSubmission((701,), (801,))

    transport = Transport()
    service = PaperExecutionService(
        transport, ExecutionJournal(tmp_path / "orders.json")
    )
    changed = replace(
        snapshot, position=replace(snapshot.position, quantity=Decimal("1"))
    )
    with pytest.raises(ExecutionBlocked, match="position quantity changed"):
        service.submit_entire_position_trailing(
            changed,
            plan,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )
    with pytest.raises(ExecutionBlocked, match="USD option"):
        plan_entire_position(
            replace(snapshot, contract=replace(snapshot.contract, currency="EUR")),
            (),
            TrailingRequest(Decimal("0.10"), "dollars"),
        )
    assert transport.calls == 0
    receipt = service.submit_entire_position_trailing(
        snapshot,
        plan,
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=1,
    )
    assert receipt.entry.perm_ids == (801,)
    assert (
        service.trailing_entries(
            account=snapshot.selected.account, con_id=snapshot.selected.con_id
        )[0].trailing_quantity
        == 2
    )
    with pytest.raises(ExecutionBlocked, match="already journaled"):
        service.submit_entire_position_trailing(
            snapshot,
            plan,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )
    assert transport.calls == 1


def test_unknown_trailing_submission_locks_contract_after_restart(tmp_path) -> None:
    snapshot = _trailing_snapshot()
    plan = plan_entire_position(
        snapshot, (), TrailingRequest(Decimal("0.10"), "dollars")
    )

    class RejectingTransport:
        def submit_trailing(self, *args, **kwargs):
            raise ExecutionOutcomeUnknown("no complete acknowledgement")

    path = tmp_path / "orders.json"
    service = PaperExecutionService(RejectingTransport(), ExecutionJournal(path))
    with pytest.raises(ExecutionOutcomeUnknown):
        service.submit_entire_position_trailing(
            snapshot,
            plan,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )
    restarted = PaperExecutionService(RejectingTransport(), ExecutionJournal(path))
    unresolved = restarted.unresolved_management_entries(
        account=snapshot.selected.account, con_id=snapshot.selected.con_id
    )
    assert len(unresolved) == 1
    assert unresolved[0].state == "SUBMISSION_UNKNOWN"
    with pytest.raises(ExecutionBlocked, match="locked"):
        restarted.submit_entire_position_trailing(
            snapshot,
            plan,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )


def test_trailing_submission_blocks_after_disconnect(tmp_path) -> None:
    snapshot = _trailing_snapshot()
    plan = plan_entire_position(
        snapshot, (), TrailingRequest(Decimal("0.10"), "dollars")
    )

    class Transport:
        def submit_trailing(self, *args, **kwargs):
            raise AssertionError("must not be called")

    service = PaperExecutionService(
        Transport(), ExecutionJournal(tmp_path / "orders.json")
    )
    with pytest.raises(ExecutionBlocked, match="complete, fresh connection"):
        service.submit_entire_position_trailing(
            replace(snapshot, connected=False),
            plan,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )


def test_trailing_submission_blocks_offsetting_manual_execution(tmp_path) -> None:
    snapshot = _trailing_snapshot()
    plan = plan_entire_position(
        snapshot, (), TrailingRequest(Decimal("0.10"), "dollars")
    )

    class Transport:
        def submit_trailing(self, *args, **kwargs):
            raise AssertionError("must not be called")

    manual = ObservedExecution(
        exec_id="manual.1",
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        perm_id=999,
        side="BOT",
        quantity=Decimal("1"),
        price=Decimal("0.95"),
        time="now",
    )
    service = PaperExecutionService(
        Transport(), ExecutionJournal(tmp_path / "orders.json")
    )
    with pytest.raises(ExecutionBlocked, match="execution changed"):
        service.submit_entire_position_trailing(
            replace(snapshot, executions=(manual,)),
            plan,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )


def test_unassigned_trailing_submission_rejects_account_switch(tmp_path) -> None:
    snapshot = _trailing_snapshot()
    plan = plan_entire_position(
        snapshot, (), TrailingRequest(Decimal("0.10"), "dollars")
    )

    class Transport:
        def submit_trailing(self, *args, **kwargs):
            raise AssertionError("must not be called")

    other_key = replace(snapshot.selected, account="DU7654321")
    switched = replace(
        snapshot,
        selected=other_key,
        position=replace(snapshot.position, key=other_key),
    )
    service = PaperExecutionService(
        Transport(), ExecutionJournal(tmp_path / "orders.json")
    )
    with pytest.raises(ExecutionBlocked, match="selected account"):
        service.submit_entire_position_trailing(
            switched,
            plan,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )


@pytest.mark.parametrize("with_limit", [False, True])
def test_paper_trailing_writer_sends_one_exact_order(monkeypatch, with_limit) -> None:
    from ibkr_options_manager.broker import execution as broker_execution

    snapshot = _trailing_snapshot()
    plan = plan_entire_position(
        snapshot,
        (),
        TrailingRequest(
            Decimal("10"),
            "percent",
            Decimal("0.05") if with_limit else None,
        ),
    )
    sent = []

    class FakeWrapper:
        def __init__(self) -> None:
            pass

    class FakeClient:
        def __init__(self, wrapper) -> None:
            self.wrapper = wrapper
            self.connected = False

        def connect(self, *_args) -> None:
            self.connected = True
            self.wrapper.nextValidId(500)

        def run(self) -> None:
            pass

        def isConnected(self) -> bool:
            return self.connected

        def disconnect(self) -> None:
            self.connected = False

        def placeOrder(self, order_id, _contract, order) -> None:
            sent.append(order)
            order.permId = 900
            self.wrapper.openOrder(order_id, None, order, None)

    monkeypatch.setattr(
        broker_execution,
        "_load_ibapi",
        lambda: _IbapiImports(
            FakeClient, FakeWrapper, SimpleNamespace, SimpleNamespace
        ),
    )
    result = IbkrPaperExecutionBroker().submit_trailing(
        snapshot,
        plan,
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=1,
    )
    assert result.order_ids == (500,)
    assert result.perm_ids == (900,)
    assert len(sent) == 1
    assert sent[0].orderType == ("TRAIL LIMIT" if with_limit else "TRAIL")
    assert sent[0].trailingPercent == 10
    assert sent[0].trailStopPrice == float(plan.initial_stop)
    assert sent[0].totalQuantity == 2
    assert sent[0].transmit is True
    if with_limit:
        assert sent[0].lmtPriceOffset == float(plan.limit_offset)


def test_cancel_order_supplies_the_required_empty_order_cancel_options() -> None:
    calls: list[tuple[int, object]] = []

    class RequiredCancelArguments:
        def cancelOrder(self, order_id: int, order_cancel: object) -> None:
            calls.append((order_id, order_cancel))

    _cancel_order(RequiredCancelArguments(), 701)

    assert calls[0][0] == 701
    assert vars(calls[0][1])["manualOrderCancelTime"] == ""
    assert vars(calls[0][1])["extOperator"] == ""


def _plan(
    snapshot: BrokerSnapshot,
    *,
    stop_order_type: str = "STP",
    stop_limit_offset: Decimal = Decimal("5"),
):
    return build_exit_plan(
        snapshot,
        PlanRequest(
            tranche_size=2,
            target_percentages=(Decimal("20"),),
            stop_loss_percentage=Decimal("25"),
            remainder_policy=RemainderPolicy.NEXT_RUNG,
            tif="GTC",
            layers=(
                LayerRequest(
                    quantity=2,
                    target_price=Decimal("1.20"),
                    stop_price=Decimal("0.75"),
                    tif="GTC",
                    target_percentage=Decimal("20"),
                ),
            ),
            paper_execution_mode=True,
            stop_order_type=stop_order_type,
            stop_limit_offset=stop_limit_offset,
        ),
    )


def test_outside_rth_defaults_on_only_for_documented_index_options() -> None:
    supported = _plan(_snapshot())
    assert supported.status.value == "VALID"
    assert all(
        pair.target.outside_rth and pair.stop.outside_rth for pair in supported.pairs
    )

    for changes in (
        {"trading_class": "AAPL"},
        {"exchange": "ISE"},
        {"currency": "EUR"},
    ):
        snapshot = _snapshot()
        contract = replace(snapshot.contract, **changes)
        unsupported = _plan(
            replace(
                snapshot,
                contract=contract,
                market_rule=replace(snapshot.market_rule, exchange=contract.exchange),
            )
        )
        assert unsupported.status.value == "VALID"
        assert all(
            not pair.target.outside_rth and not pair.stop.outside_rth
            for pair in unsupported.pairs
        )


def test_mismatched_outside_rth_pair_is_blocked_before_any_tws_write() -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    pair = plan.pairs[0]
    inconsistent = replace(
        plan, pairs=(replace(pair, stop=replace(pair.stop, outside_rth=False)),)
    )

    with pytest.raises(ExecutionBlocked, match="Outside RTH"):
        require_paper_execution_snapshot(snapshot, inconsistent)
    with pytest.raises(ExecutionBlocked, match="Outside RTH"):
        IbkrPaperExecutionBroker().submit(
            snapshot,
            inconsistent,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )


@pytest.mark.parametrize("supported", [True, False])
def test_paper_bracket_writer_sends_matching_outside_rth_flags(
    monkeypatch,
    supported: bool,
) -> None:
    from ibkr_options_manager.broker import execution as broker_execution

    snapshot = _snapshot()
    if not supported:
        snapshot = replace(
            snapshot,
            contract=replace(snapshot.contract, trading_class="AAPL"),
        )
    plan = _plan(snapshot)
    sent = []

    class FakeWrapper:
        def __init__(self) -> None:
            pass

    class FakeClient:
        def __init__(self, wrapper) -> None:
            self.wrapper = wrapper
            self.connected = False

        def connect(self, *_args) -> None:
            self.connected = True
            self.wrapper.nextValidId(500)

        def run(self) -> None:
            pass

        def isConnected(self) -> bool:
            return self.connected

        def disconnect(self) -> None:
            self.connected = False

        def placeOrder(self, order_id, _contract, order) -> None:
            sent.append((order.orderType, order.outsideRth, order.transmit))
            order.permId = order_id + 1000
            self.wrapper.openOrder(order_id, None, order, None)

    monkeypatch.setattr(
        broker_execution,
        "_load_ibapi",
        lambda: _IbapiImports(
            FakeClient, FakeWrapper, SimpleNamespace, SimpleNamespace
        ),
    )

    result = IbkrPaperExecutionBroker().submit(
        snapshot,
        plan,
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=1,
    )

    assert result.order_ids == (500, 501)
    assert sent == [("LMT", supported, False), ("STP", supported, True)]


@pytest.mark.parametrize("rejected", [False, True])
def test_paper_writer_sends_one_stop_limit_leg_with_trigger_limit_and_oca(
    monkeypatch,
    rejected: bool,
) -> None:
    from ibkr_options_manager.broker import execution as broker_execution

    snapshot = _snapshot()
    plan = _plan(snapshot, stop_order_type="STP LMT")
    assert plan.pairs[0].stop.limit_price == Decimal("0.70")
    sent = []

    class FakeWrapper:
        def __init__(self) -> None:
            pass

    class FakeClient:
        def __init__(self, wrapper) -> None:
            self.wrapper = wrapper
            self.connected = False

        def connect(self, *_args) -> None:
            self.connected = True
            self.wrapper.nextValidId(500)

        def run(self) -> None:
            pass

        def isConnected(self) -> bool:
            return self.connected

        def disconnect(self) -> None:
            self.connected = False

        def placeOrder(self, order_id, _contract, order) -> None:
            sent.append(
                (
                    order_id,
                    order.orderType,
                    order.auxPrice,
                    order.lmtPrice,
                    order.ocaGroup,
                    order.ocaType,
                    order.outsideRth,
                    order.transmit,
                )
            )
            if rejected and order.orderType == "STP LMT":
                self.wrapper.error(order_id, 109, "TWS price precaution")
                return
            order.permId = order_id + 1000
            self.wrapper.openOrder(order_id, None, order, None)

    monkeypatch.setattr(
        broker_execution,
        "_load_ibapi",
        lambda: _IbapiImports(
            FakeClient, FakeWrapper, SimpleNamespace, SimpleNamespace
        ),
    )
    if rejected:
        with pytest.raises(ExecutionBlocked, match="TWS price precaution"):
            IbkrPaperExecutionBroker().submit(
                snapshot,
                plan,
                host="127.0.0.1",
                port=7497,
                client_id=17,
                timeout_seconds=1,
            )
    else:
        receipt = IbkrPaperExecutionBroker().submit(
            snapshot,
            plan,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )
        assert receipt.order_ids == (500, 501)
    assert [(row[1], row[5], row[6], row[7]) for row in sent] == [
        ("LMT", 2, True, False),
        ("STP LMT", 2, True, True),
    ]
    assert sent[0][4] == sent[1][4]
    assert sent[1][2:4] == (0.75, 0.7)


def test_stop_limit_off_tick_price_is_blocked_before_writer_connects() -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot, stop_order_type="STP LMT")
    pair = plan.pairs[0]
    invalid = replace(
        plan,
        pairs=(replace(pair, stop=replace(pair.stop, limit_price=Decimal("0.72"))),),
    )
    with pytest.raises(ExecutionBlocked, match="invalid price increment"):
        require_paper_execution_snapshot(snapshot, invalid)
    with pytest.raises(ExecutionBlocked, match="invalid price increment"):
        IbkrPaperExecutionBroker().submit(
            snapshot,
            invalid,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )


def test_stop_limit_reconnect_requires_a_fresh_complete_snapshot() -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot, stop_order_type="STP LMT")
    for disconnected in (
        replace(snapshot, connected=False),
        replace(snapshot, fresh=False),
        replace(snapshot, complete=False),
    ):
        with pytest.raises(ExecutionBlocked):
            require_paper_execution_snapshot(disconnected, plan)


def _two_pair_plan(snapshot: BrokerSnapshot):
    return build_exit_plan(
        replace(
            snapshot,
            position=replace(snapshot.position, quantity=Decimal("7")),
        ),
        PlanRequest(
            tranche_size=4,
            target_percentages=(Decimal("20"), Decimal("40")),
            stop_loss_percentage=Decimal("25"),
            remainder_policy=RemainderPolicy.NEXT_RUNG,
            tif="GTC",
            layers=(
                LayerRequest(
                    quantity=4,
                    target_price=Decimal("1.20"),
                    stop_price=Decimal("0.75"),
                    tif="GTC",
                    target_percentage=Decimal("20"),
                ),
                LayerRequest(
                    quantity=3,
                    target_price=Decimal("1.40"),
                    stop_price=Decimal("0.75"),
                    tif="GTC",
                    target_percentage=Decimal("40"),
                ),
            ),
            paper_execution_mode=True,
        ),
    )


class _RecordingTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def submit(self, *_args: object, **_kwargs: object) -> PaperSubmission:
        self.calls += 1
        if self.fail:
            raise RuntimeError("socket failed after sending")
        return PaperSubmission(order_ids=(101, 102), perm_ids=(201, 202))


class _RecordingMarketTransport(_RecordingTransport):
    def __init__(self) -> None:
        super().__init__()
        self.market_candidates: list[MarketExitCandidate] = []
        self.cancelled_candidates: list[MarketExitCandidate] = []

    def cancel_pair(
        self,
        _snapshot: BrokerSnapshot,
        candidate: MarketExitCandidate,
        **_kwargs: object,
    ) -> PaperSubmission:
        self.cancelled_candidates.append(candidate)
        return PaperSubmission(
            order_ids=tuple(
                sorted((candidate.target_order_id, candidate.stop_order_id))
            ),
            perm_ids=(),
        )

    def cancel_pair_then_submit_market(
        self,
        _snapshot: BrokerSnapshot,
        candidate: MarketExitCandidate,
        **_kwargs: object,
    ) -> PaperSubmission:
        self.market_candidates.append(candidate)
        return PaperSubmission(order_ids=(301,), perm_ids=(401,))

    def cancel_pairs_then_submit_market(
        self,
        _snapshot: BrokerSnapshot,
        candidates: tuple[MarketExitCandidate, ...],
        **_kwargs: object,
    ) -> PaperSubmission:
        self.market_candidates.extend(candidates)
        return PaperSubmission(order_ids=(302,), perm_ids=(402,))

    def modify_prices(
        self,
        _snapshot: BrokerSnapshot,
        candidates: tuple[PriceUpdateCandidate, ...],
        **_kwargs: object,
    ) -> PaperSubmission:
        order_ids = tuple(
            order_id
            for candidate in candidates
            for order_id, price in (
                (candidate.layer.target_order_id, candidate.target_price),
                (candidate.layer.stop_order_id, candidate.stop_price),
            )
            if price is not None
        )
        perm_ids = tuple(400 + order_id for order_id in order_ids)
        return PaperSubmission(order_ids=order_ids, perm_ids=perm_ids)


class _EmptyContract:
    pass


class _ContractImports:
    Contract = _EmptyContract


def test_submission_uses_con_id_without_conflicting_spxw_contract_aliases() -> None:
    snapshot = _snapshot()

    contract = _build_submission_contract(_ContractImports, snapshot)

    assert contract.conId == snapshot.contract.con_id
    assert contract.exchange == snapshot.contract.exchange
    assert not hasattr(contract, "localSymbol")
    assert not hasattr(contract, "tradingClass")
    assert not hasattr(contract, "secType")
    assert not hasattr(contract, "strike")


def test_paper_execution_journals_before_and_after_one_acknowledged_submission(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    transport = _RecordingTransport()
    journal = ExecutionJournal(tmp_path / "journal.json")
    service = PaperExecutionService(transport, journal)

    receipt = service.submit(
        snapshot,
        plan,
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=1,
    )

    assert transport.calls == 1
    assert receipt.entry.state == "SUBMITTED"
    assert receipt.entry.order_ids == (101, 102)
    assert receipt.entry.perm_ids == (201, 202)
    with pytest.raises(ExecutionBlocked, match="already journaled"):
        service.submit(
            snapshot,
            plan,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )
    assert transport.calls == 1


@pytest.mark.parametrize("stop_order_type", ["STP", "STP LMT"])
def test_indeterminate_transport_outcome_is_durably_blocked_from_retry(
    tmp_path,
    stop_order_type: str,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot, stop_order_type=stop_order_type)
    transport = _RecordingTransport(fail=True)
    journal = ExecutionJournal(tmp_path / "journal.json")
    service = PaperExecutionService(transport, journal)

    with pytest.raises(ExecutionOutcomeUnknown, match="socket failed"):
        service.submit(
            snapshot,
            plan,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
            stop_limit_offset=(Decimal("5") if stop_order_type == "STP LMT" else None),
            stop_limit_unit="percent",
        )

    assert plan.fingerprint is not None
    assert journal.find(plan.fingerprint).state == "SUBMISSION_UNKNOWN"
    with pytest.raises(ExecutionBlocked, match="already journaled"):
        service.submit(
            snapshot,
            plan,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
            stop_limit_offset=(Decimal("5") if stop_order_type == "STP LMT" else None),
            stop_limit_unit="percent",
        )
    assert transport.calls == 1


def test_tws_transmit_confirmation_timeout_is_an_unknown_non_retryable_outcome(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)

    class _AwaitingTransmitTransport(_RecordingTransport):
        def submit(self, *_args: object, **_kwargs: object) -> PaperSubmission:
            raise ExecutionOutcomeUnknown("awaiting TWS Transmit confirmation")

    journal = ExecutionJournal(tmp_path / "journal.json")
    service = PaperExecutionService(_AwaitingTransmitTransport(), journal)

    with pytest.raises(ExecutionOutcomeUnknown, match="awaiting TWS"):
        service.submit(
            snapshot,
            plan,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )

    assert plan.fingerprint is not None
    assert journal.find(plan.fingerprint).state == "SUBMISSION_UNKNOWN"


def test_pending_submission_details_survive_restart_and_become_reconciled(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    path = tmp_path / "journal.json"
    journal = ExecutionJournal(path)
    journal.begin(snapshot, plan)
    journal.record_submission(
        plan.fingerprint, order_ids=(101, 102), perm_ids=(201, 202)
    )

    reopened = ExecutionJournal(path)
    entries = reopened.submission_entries(
        account=snapshot.selected.account, con_id=snapshot.selected.con_id
    )
    assert len(entries) == 1
    assert entries[0].state == "SUBMITTED"
    assert entries[0].layers[0].quantity == 2
    assert entries[0].layers[0].target_price == format(
        plan.pairs[0].target.rounded_price, "f"
    )
    assert entries[0].layers[0].target_percentage == "20"
    assert entries[0].layers[0].stop_percentage == "25"

    group = f"{plan.fingerprint[:12]}/tranche-1"
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
        tif="GTC",
    )
    stop = replace(target, perm_id=202, order_id=102, order_type="STP")
    reconciled = reopened.reconcile_snapshot(
        replace(snapshot, working_orders=(target, stop))
    )
    assert len(reconciled) == 1
    assert reconciled[0].state == "RECONCILED"
    assert reconciled[0].layers == entries[0].layers

    legacy_payload = json.loads(path.read_text())
    legacy_payload[0]["layers"][0].pop("target_percentage")
    legacy_payload[0]["layers"][0].pop("stop_percentage")
    path.write_text(json.dumps(legacy_payload))
    legacy = ExecutionJournal(path).submission_entries(
        account=snapshot.selected.account, con_id=snapshot.selected.con_id
    )
    assert legacy[0].layers[0].target_percentage == ""
    assert legacy[0].layers[0].stop_percentage == ""


def test_completed_tws_bracket_recovers_old_journal_ids_and_realized_profit(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _two_pair_plan(snapshot)
    assert plan.fingerprint is not None
    path = tmp_path / "journal.json"
    journal = ExecutionJournal(path)
    journal.begin(snapshot, plan)
    submitted = journal.record_submission(
        plan.fingerprint,
        order_ids=(101, 102, 103, 104),
        perm_ids=(201, 202, 203, 204),
    )
    # An older partial reconciliation retained only the surviving order IDs.
    journal._write(
        (
            replace(
                submitted,
                state="PARTIALLY_RECONCILED",
                order_ids=(103, 104),
                perm_ids=(203, 204),
                layers=tuple(
                    replace(layer, target_perm_id=0, stop_perm_id=0)
                    for layer in submitted.layers
                ),
            ),
        )
    )
    group = f"{plan.fingerprint[:12]}/tranche-1"
    completed = tuple(
        ObservedCompletedOrder(
            account=snapshot.selected.account,
            con_id=snapshot.selected.con_id,
            perm_id=perm_id,
            order_id=order_id,
            client_id=17,
            action="SELL",
            order_type=order_type,
            oca_group=group,
            status=status,
        )
        for perm_id, order_id, order_type, status in (
            (201, 101, "LMT", "Filled"),
            (202, 102, "STP", "Cancelled"),
        )
    )
    fill = ObservedExecution(
        exec_id="filled.01",
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        perm_id=201,
        side="SLD",
        quantity=Decimal("4"),
        price=Decimal("1.20"),
        time="20260925 12:00:00",
        realized_pnl=Decimal("75.50"),
        currency="USD",
    )
    active_target = WorkingOrder(
        perm_id=203,
        client_id=17,
        order_id=103,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("3"),
        status="Submitted",
        oca_group=f"{plan.fingerprint[:12]}/tranche-2",
    )
    active_stop = replace(active_target, perm_id=204, order_id=104, order_type="STP")
    observed = replace(
        snapshot,
        working_orders=(active_target, active_stop),
        completed_orders=completed,
        completed_orders_complete=True,
        executions=(fill,),
        executions_complete=True,
    )
    journal.record_completed_orders(observed)
    journal.record_executions(observed)
    restored = ExecutionJournal(path).find(plan.fingerprint)
    assert restored is not None
    assert restored.layers[0].target_perm_id == 201
    assert restored.layers[0].stop_perm_id == 202
    outcome = classify_journal_layer(
        restored,
        0,
        active_perm_ids=frozenset({203, 204}),
        observed_perm_ids=frozenset({203, 204}),
    )
    assert outcome.status == "CLOSED_PROFIT"
    assert outcome.realized_pnl == Decimal("75.50")
    assert outcome.exit_side == "Target"
    assert (
        classify_journal_layer(
            restored,
            1,
            active_perm_ids=frozenset({203, 204}),
            observed_perm_ids=frozenset({203, 204}),
        ).status
        == "ACTIVE"
    )


@pytest.mark.parametrize(
    ("perm_id", "pnl", "expected"),
    [
        (201, Decimal("12"), "CLOSED_PROFIT"),
        (202, Decimal("-18"), "CLOSED_LOSS"),
        (202, None, "CLOSED_PNL_UNKNOWN"),
    ],
)
def test_layer_outcome_uses_exact_tws_fill_and_commission_report(
    tmp_path,
    perm_id,
    pnl,
    expected,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.record_submission(
        plan.fingerprint, order_ids=(101, 102), perm_ids=(201, 202)
    )
    execution = ObservedExecution(
        exec_id="fill.01",
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        perm_id=perm_id,
        side="SLD",
        quantity=Decimal("2"),
        price=Decimal("1.20"),
        time="now",
        realized_pnl=pnl,
        currency="USD" if pnl is not None else "",
    )
    journal.record_executions(
        replace(
            snapshot,
            executions=(execution,),
            executions_complete=True,
        )
    )
    entry = journal.find(plan.fingerprint)
    assert entry is not None
    outcome = classify_journal_layer(
        entry,
        0,
        active_perm_ids=frozenset(),
        observed_perm_ids=frozenset(),
    )
    assert outcome.status == expected
    assert outcome.exit_side == ("Target" if perm_id == 201 else "Stop")


def test_execution_history_ignores_other_account_and_corrects_duplicate_fill(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.record_submission(
        plan.fingerprint, order_ids=(101, 102), perm_ids=(201, 202)
    )
    fill = ObservedExecution(
        exec_id="trade.1",
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        perm_id=201,
        side="SLD",
        quantity=Decimal("1"),
        price=Decimal("1.20"),
        time="now",
        realized_pnl=Decimal("10"),
        currency="USD",
    )
    journal.record_executions(
        replace(
            snapshot,
            executions=(replace(fill, account="DU-other"),),
            executions_complete=True,
        )
    )
    assert journal.find(plan.fingerprint).fills == ()
    journal.record_executions(
        replace(
            snapshot,
            executions=(fill,),
            executions_complete=True,
        )
    )
    corrected = replace(
        fill, exec_id="trade.2", quantity=Decimal("2"), realized_pnl=Decimal("25")
    )
    journal.record_executions(
        replace(
            snapshot,
            executions=(corrected,),
            executions_complete=True,
        )
    )
    entry = journal.find(plan.fingerprint)
    assert entry is not None
    assert len(entry.fills) == 1
    assert entry.fills[0].exec_id == "trade.2"
    outcome = classify_journal_layer(
        entry,
        0,
        active_perm_ids=frozenset(),
        observed_perm_ids=frozenset(),
    )
    assert outcome.status == "CLOSED_PROFIT"
    assert outcome.realized_pnl == Decimal("25")


def test_paper_execution_requires_read_only_api_to_have_been_explicitly_disabled() -> (
    None
):
    snapshot = _snapshot(read_only_api=True)
    plan = _plan(_snapshot())

    with pytest.raises(ExecutionBlocked, match="read-only mode disabled"):
        require_paper_execution_snapshot(snapshot, plan)


def test_paper_order_submission_can_recheck_without_a_quote() -> None:
    snapshot = _snapshot()
    orders_only = replace(
        snapshot,
        quote=replace(snapshot.quote, market_data_type="NOT_REQUESTED", fresh=False),
    )

    require_paper_execution_snapshot(orders_only, _plan(snapshot))


@pytest.mark.parametrize("oca_pair", [False, True])
def test_paper_execution_accepts_verified_external_reservation(
    oca_pair: bool,
) -> None:
    original = _snapshot()
    external = WorkingOrder(
        perm_id=501,
        client_id=0,
        order_id=0,
        key=original.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("1"),
        status="Submitted",
        oca_group="manual/pair" if oca_pair else None,
    )
    orders = (
        (
            external,
            replace(external, perm_id=502, order_type="STP"),
        )
        if oca_pair
        else (external,)
    )
    snapshot = replace(
        original,
        position=replace(original.position, quantity=Decimal("3")),
        working_orders=orders,
    )
    plan = _plan(snapshot)

    assert plan.allocated_quantity == 1
    assert plan.available_quantity == 2
    require_paper_execution_snapshot(snapshot, plan)

    changed = replace(
        snapshot,
        working_orders=tuple(
            replace(order, remaining=Decimal("2")) for order in orders
        ),
    )
    assert _plan(changed).status is not plan.status
    with pytest.raises(ExecutionBlocked, match="reservations changed"):
        require_paper_execution_snapshot(changed, plan)


def test_market_exit_cancels_only_a_fresh_complete_app_owned_oca_pair_then_submits_mkt(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.record_submission(
        plan.fingerprint,
        order_ids=(101, 102),
        perm_ids=(201, 202),
    )
    group = f"{plan.fingerprint[:12]}/tranche-1"
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
        tif="GTC",
    )
    stop = replace(
        target,
        perm_id=202,
        order_id=102,
        order_type="STP",
    )
    active_snapshot = replace(snapshot, working_orders=(target, stop))
    transport = _RecordingMarketTransport()
    service = PaperExecutionService(transport, journal)

    candidate = service.prepare_market_exit(
        active_snapshot,
        target_perm_id=201,
        expected_client_id=17,
    )
    receipt = service.cancel_pair_then_submit_market(
        active_snapshot,
        candidate,
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=1,
    )

    assert candidate.target_order_id == 101
    assert candidate.stop_order_id == 102
    assert candidate.quantity == Decimal("2")
    assert transport.market_candidates == [candidate]
    assert receipt.entry.order_ids == (301,)
    assert receipt.entry.perm_ids == (401,)
    with pytest.raises(
        ExecutionBlocked, match="management attempt is already journaled"
    ):
        service.cancel_pair_then_submit_market(
            active_snapshot,
            candidate,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )


def test_cancel_pair_removes_only_a_fresh_complete_app_owned_oca_bracket(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.record_submission(
        plan.fingerprint,
        order_ids=(101, 102),
        perm_ids=(201, 202),
    )
    target = WorkingOrder(
        perm_id=201,
        client_id=17,
        order_id=101,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("2"),
        status="Submitted",
        oca_group=f"{plan.fingerprint[:12]}/tranche-1",
        tif="GTC",
    )
    active_snapshot = replace(
        snapshot,
        working_orders=(
            target,
            replace(target, perm_id=202, order_id=102, order_type="STP"),
        ),
    )
    transport = _RecordingMarketTransport()
    service = PaperExecutionService(transport, journal)
    candidate = service.prepare_market_exit(
        active_snapshot,
        target_perm_id=201,
        expected_client_id=17,
    )

    receipt = service.cancel_pair(
        active_snapshot,
        candidate,
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=1,
    )

    assert transport.cancelled_candidates == [candidate]
    assert receipt.entry.state == "COMPLETED"
    assert receipt.entry.order_ids == (101, 102)
    assert receipt.entry.perm_ids == ()
    source = journal.submission_entries(
        account=snapshot.selected.account, con_id=snapshot.selected.con_id
    )[0]
    assert source.layers[0].cancelled is True
    assert (
        classify_journal_layer(
            source,
            0,
            active_perm_ids=frozenset(),
            observed_perm_ids=frozenset(),
        ).status
        == "CANCELLED"
    )
    assert (
        classify_journal_layer(
            source,
            0,
            active_perm_ids=frozenset(),
            observed_perm_ids=frozenset({201}),
        ).status
        == "UNKNOWN"
    )
    with pytest.raises(ExecutionBlocked, match="already journaled"):
        service.cancel_pair(
            active_snapshot,
            candidate,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )


def test_cancel_pair_trace_records_callback_waits_without_order_identity(
    monkeypatch, tmp_path
) -> None:
    from ibkr_options_manager.broker import execution as broker_execution

    trace_path = tmp_path / "bracket-cancellations.jsonl"
    monkeypatch.setenv("IBKR_OPTIONS_MANAGER_CANCEL_TRACE", str(trace_path))

    class FakeWrapper:
        pass

    class FakeClient:
        def __init__(self, wrapper) -> None:
            self.wrapper = wrapper
            self.connected = False

        def connect(self, *_args) -> None:
            self.connected = True
            self.wrapper.nextValidId(500)

        def run(self) -> None:
            pass

        def isConnected(self) -> bool:
            return self.connected

        def disconnect(self) -> None:
            self.connected = False

        def cancelOrder(self, order_id, _options) -> None:
            self.wrapper.orderStatus(order_id, "Cancelled")

        def reqOpenOrders(self) -> None:
            self.wrapper.openOrderEnd()

    monkeypatch.setattr(
        broker_execution,
        "_load_ibapi",
        lambda: _IbapiImports(
            FakeClient, FakeWrapper, SimpleNamespace, SimpleNamespace
        ),
    )
    snapshot = _snapshot()
    candidate = MarketExitCandidate(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        target_order_id=101,
        target_perm_id=201,
        client_id=17,
        quantity=Decimal("2"),
        tif="GTC",
        oca_group="app/tranche-1",
        stop_order_id=102,
        stop_perm_id=202,
    )
    result = IbkrPaperExecutionBroker().cancel_pair(
        snapshot,
        candidate,
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=1,
    )

    assert result.order_ids == (101, 102)
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "transport_connect_start",
        "transport_ready",
        "cancel_requests_sent",
        "cancel_acknowledged",
        "open_orders_check_start",
        "open_orders_check_complete",
        "transport_finished",
    ]
    assert all(event["run_id"] == events[0]["run_id"] for event in events)
    assert all(event["elapsed_ms"] >= 0 for event in events)
    assert all("order_id" not in event and "account" not in event for event in events)
    assert trace_path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(
        broker_execution,
        "record_cancellation_event",
        lambda *_args, **_kwargs: False,
    )
    assert IbkrPaperExecutionBroker().cancel_pair(
        snapshot,
        candidate,
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=1,
    ).order_ids == (101, 102)


def test_refresh_recovers_an_older_completed_bracket_cancellation(tmp_path) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.record_submission(
        plan.fingerprint,
        order_ids=(101, 102),
        perm_ids=(201, 202),
    )
    attempt = journal.begin_management(
        snapshot,
        operation="cancel-bracket",
        material=(101, 201, 102, 202),
        expected_order_count=2,
    )
    journal.record_management_completion(attempt.fingerprint, order_ids=(101, 102))

    journal.record_completed_orders(
        replace(snapshot, captured_at=snapshot.captured_at + 1)
    )

    source = journal.submission_entries(
        account=snapshot.selected.account, con_id=snapshot.selected.con_id
    )[0]
    assert source.layers[0].cancelled is True


def test_market_exit_rejects_a_layer_that_is_not_journal_owned(tmp_path) -> None:
    snapshot = _snapshot()
    target = WorkingOrder(
        perm_id=201,
        client_id=17,
        order_id=101,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("2"),
        status="Submitted",
        oca_group="external/tranche-1",
        tif="GTC",
    )
    stop = replace(target, perm_id=202, order_id=102, order_type="STP")
    service = PaperExecutionService(
        _RecordingMarketTransport(),
        ExecutionJournal(tmp_path / "journal.json"),
    )

    with pytest.raises(ExecutionBlocked, match="not created by this application"):
        service.prepare_market_exit(
            replace(snapshot, working_orders=(target, stop)),
            target_perm_id=201,
            expected_client_id=17,
        )


def test_new_bracket_is_allowed_for_quantity_unreserved_by_active_app_pairs(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    prior_plan = _plan(snapshot)
    assert prior_plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, prior_plan)
    journal.record_submission(
        prior_plan.fingerprint, order_ids=(101, 102), perm_ids=(201, 202)
    )
    active_target = WorkingOrder(
        perm_id=201,
        client_id=17,
        order_id=101,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("1"),
        status="Submitted",
        oca_group=f"{prior_plan.fingerprint[:12]}/tranche-1",
        tif="GTC",
    )
    active_snapshot = replace(
        snapshot,
        working_orders=(
            active_target,
            replace(active_target, perm_id=202, order_id=102, order_type="STP"),
        ),
    )
    replacement_plan = build_exit_plan(
        active_snapshot,
        PlanRequest(
            tranche_size=1,
            target_percentages=(Decimal("20"),),
            stop_loss_percentage=Decimal("25"),
            remainder_policy=RemainderPolicy.NEXT_RUNG,
            tif="GTC",
            layers=(
                LayerRequest(
                    quantity=1,
                    target_price=Decimal("1.20"),
                    stop_price=Decimal("0.75"),
                    tif="GTC",
                    target_percentage=Decimal("20"),
                ),
            ),
            paper_execution_mode=True,
        ),
    )
    assert replacement_plan.available_quantity == 1
    service = PaperExecutionService(_RecordingTransport(), journal)

    receipt = service.submit(
        active_snapshot,
        replacement_plan,
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=1,
    )

    assert receipt.entry.state == "SUBMITTED"


def test_selected_layers_cancel_as_a_set_then_submit_one_total_market_order(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = build_exit_plan(
        snapshot,
        PlanRequest(
            tranche_size=1,
            target_percentages=(Decimal("20"), Decimal("40")),
            stop_loss_percentage=Decimal("25"),
            remainder_policy=RemainderPolicy.NEXT_RUNG,
            tif="GTC",
            layers=(
                LayerRequest(
                    quantity=1,
                    target_price=Decimal("1.20"),
                    stop_price=Decimal("0.75"),
                    tif="GTC",
                    target_percentage=Decimal("20"),
                ),
                LayerRequest(
                    quantity=1,
                    target_price=Decimal("1.40"),
                    stop_price=Decimal("0.75"),
                    tif="GTC",
                    target_percentage=Decimal("40"),
                ),
            ),
            paper_execution_mode=True,
        ),
    )
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.record_submission(
        plan.fingerprint,
        order_ids=(101, 102, 103, 104),
        perm_ids=(201, 202, 203, 204),
    )
    first = WorkingOrder(
        perm_id=201,
        client_id=17,
        order_id=101,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("1"),
        status="Submitted",
        oca_group=f"{plan.fingerprint[:12]}/tranche-1",
        tif="GTC",
    )
    second = replace(
        first,
        perm_id=203,
        order_id=103,
        oca_group=f"{plan.fingerprint[:12]}/tranche-2",
    )
    active = replace(
        snapshot,
        working_orders=(
            first,
            replace(first, perm_id=202, order_id=102, order_type="STP"),
            second,
            replace(second, perm_id=204, order_id=104, order_type="STP"),
        ),
    )
    transport = _RecordingMarketTransport()
    service = PaperExecutionService(transport, journal)

    candidates = service.prepare_market_exits(
        active, target_perm_ids=(201, 203), expected_client_id=17
    )
    receipt = service.cancel_pairs_then_submit_market(
        active,
        candidates,
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=1,
    )

    assert transport.market_candidates == list(candidates)
    assert sum(candidate.quantity for candidate in candidates) == Decimal("2")
    assert receipt.entry.order_ids == (302,)
    with pytest.raises(ExecutionBlocked, match="already journaled"):
        service.cancel_pairs_then_submit_market(
            active,
            candidates,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )


def test_price_updates_amend_only_the_requested_app_owned_leg(tmp_path) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.record_submission(
        plan.fingerprint, order_ids=(101, 102), perm_ids=(201, 202)
    )
    group = f"{plan.fingerprint[:12]}/tranche-1"
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
        tif="GTC",
        limit_price=Decimal("1.20"),
    )
    stop = replace(
        target,
        perm_id=202,
        order_id=102,
        order_type="STP",
        limit_price=None,
        stop_price=Decimal("0.75"),
    )
    active = replace(snapshot, working_orders=(target, stop))
    service = PaperExecutionService(_RecordingMarketTransport(), journal)
    layer = service.prepare_market_exit(
        active, target_perm_id=201, expected_client_id=17
    )
    update = PriceUpdateCandidate(layer=layer, stop_price=Decimal("1.00"))

    receipt = service.modify_prices(
        active,
        service.prepare_price_updates(active, updates=(update,), expected_client_id=17),
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=1,
    )

    assert receipt.entry.order_ids == (102,)
    assert receipt.entry.perm_ids == (502,)


def test_unknown_price_amendment_needs_explicit_fresh_retry(tmp_path) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.record_submission(
        plan.fingerprint, order_ids=(101, 102), perm_ids=(201, 202)
    )
    group = f"{plan.fingerprint[:12]}/tranche-1"
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
        tif="GTC",
        limit_price=Decimal("1.20"),
    )
    stop = replace(
        target,
        perm_id=202,
        order_id=102,
        order_type="STP",
        limit_price=None,
        stop_price=Decimal("0.75"),
    )
    active = replace(snapshot, working_orders=(target, stop))

    class FlakyTransport(_RecordingMarketTransport):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def modify_prices(self, snapshot, candidates, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise ExecutionOutcomeUnknown("lost TWS acknowledgement")
            return super().modify_prices(snapshot, candidates, **kwargs)

    transport = FlakyTransport()
    service = PaperExecutionService(transport, journal)
    layer = service.prepare_market_exit(
        active, target_perm_id=201, expected_client_id=17
    )
    update = PriceUpdateCandidate(
        layer=layer,
        target_price=Decimal("1.40"),
        prior_target_price=Decimal("1.20"),
        prior_stop_price=Decimal("0.75"),
    )

    def amend(current, *, allow_unknown_retry=False):
        return service.modify_prices(
            current,
            (update,),
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
            allow_unknown_retry=allow_unknown_retry,
        )

    with pytest.raises(ExecutionOutcomeUnknown, match="lost TWS"):
        amend(active)
    with pytest.raises(ExecutionBlocked, match="contract is locked"):
        amend(active)
    with pytest.raises(ExecutionBlocked, match="contract is locked"):
        amend(active, allow_unknown_retry=True)
    with pytest.raises(ExecutionBlocked, match="contract is locked"):
        service.modify_prices(
            active,
            (replace(update, target_price=Decimal("1.50")),),
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )
    with pytest.raises(ExecutionBlocked, match="contract is locked"):
        journal.begin(active, plan)
    assert transport.attempts == 1

    refreshed = replace(
        active,
        captured_at=Decimal("1"),
        executions_complete=True,
        completed_orders_complete=True,
    )
    unknown = service.unresolved_management_entries(
        account=snapshot.selected.account, con_id=snapshot.selected.con_id
    )
    assert len(unknown) == 1
    assert not service.unresolved_management_entries(
        account=snapshot.selected.account, con_id=snapshot.selected.con_id + 1
    )
    with pytest.raises(ExecutionBlocked, match=r"later.*read"):
        service.confirm_unknown_management(
            active, unknown[0].fingerprint, confirmed_in_tws=True
        )
    with pytest.raises(ExecutionBlocked, match="confirm"):
        service.confirm_unknown_management(
            refreshed, unknown[0].fingerprint, confirmed_in_tws=False
        )
    with pytest.raises(ExecutionBlocked, match="stable working orders"):
        service.confirm_unknown_management(
            replace(
                refreshed,
                working_orders=(
                    replace(target, status="PendingSubmit"),
                    stop,
                ),
            ),
            unknown[0].fingerprint,
            confirmed_in_tws=True,
        )
    service.confirm_unknown_management(
        refreshed, unknown[0].fingerprint, confirmed_in_tws=True
    )
    assert not service.unresolved_management_entries(
        account=snapshot.selected.account, con_id=snapshot.selected.con_id
    )
    already_changed = replace(
        refreshed,
        working_orders=(replace(target, limit_price=Decimal("1.40")), stop),
    )
    with pytest.raises(ExecutionBlocked, match="changed since review"):
        amend(already_changed, allow_unknown_retry=True)
    assert transport.attempts == 1
    assert service.price_update_attempt_state(refreshed, (update,)) == "RESOLVED"
    receipt = amend(refreshed, allow_unknown_retry=True)
    assert receipt.entry.order_ids == (101,)
    assert transport.attempts == 2
    attempts = [
        entry
        for entry in journal._entries()
        if entry.fingerprint.startswith("price-update:")
    ]
    assert [entry.state for entry in attempts] == ["RESOLVED", "SUBMITTED"]
    assert attempts[0].fingerprint != attempts[1].fingerprint
    with pytest.raises(ExecutionBlocked, match="already journaled"):
        amend(replace(refreshed, captured_at=Decimal("2")), allow_unknown_retry=True)
    assert transport.attempts == 2


@pytest.mark.parametrize(
    ("post_price", "confirmed", "rejected"),
    [(29.1, False, False), (31.5, True, False), (29.1, False, True)],
)
def test_price_update_requires_fresh_post_write_order_price(
    monkeypatch,
    tmp_path,
    post_price: float,
    confirmed: bool,
    rejected: bool,
) -> None:
    from ibkr_options_manager.broker import execution as broker_execution

    trace_path = tmp_path / "price-amendments.jsonl"
    monkeypatch.setenv("IBKR_OPTIONS_MANAGER_PRICE_TRACE", str(trace_path))

    class FakeWrapper:
        def __init__(self) -> None:
            pass

    class FakeClient:
        def __init__(self, wrapper) -> None:
            self.wrapper = wrapper
            self.connected = False
            self.open_requests = 0

        def connect(self, *_args) -> None:
            self.connected = True
            self.wrapper.nextValidId(500)

        def isConnected(self) -> bool:
            return self.connected

        def disconnect(self) -> None:
            self.connected = False

        def run(self) -> None:
            pass

        def reqOpenOrders(self) -> None:
            self.open_requests += 1
            observed_price = 29.1 if self.open_requests == 1 else post_price
            self.wrapper.openOrder(
                101,
                None,
                SimpleNamespace(
                    permId=201,
                    action="SELL",
                    orderType="LMT",
                    lmtPrice=observed_price,
                    transmit=False,
                    volatility=0.0,
                    volatilityType=1,
                ),
                SimpleNamespace(status="Submitted"),
            )
            self.wrapper.openOrderEnd()

        def placeOrder(self, order_id, _contract, order) -> None:
            from ibapi.const import UNSET_DOUBLE, UNSET_INTEGER

            assert order_id == 101
            assert order.lmtPrice == 31.5
            assert order.transmit is True
            assert order.volatility == UNSET_DOUBLE
            assert order.volatilityType == UNSET_INTEGER
            if rejected:
                self.wrapper.error(order_id, 109, "TWS price precaution")
                return
            # The write callback reflects the requested value, but a separate
            # reqOpenOrders check above still sees the old working value.
            self.wrapper.openOrder(
                order_id, None, order, SimpleNamespace(status="Submitted")
            )

    monkeypatch.setattr(
        broker_execution,
        "_load_ibapi",
        lambda: _IbapiImports(
            FakeClient, FakeWrapper, SimpleNamespace, SimpleNamespace
        ),
    )
    snapshot = _snapshot()
    layer = MarketExitCandidate(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        target_order_id=101,
        target_perm_id=201,
        client_id=17,
        quantity=Decimal("2"),
        tif="GTC",
        oca_group="app/tranche-1",
        stop_order_id=102,
        stop_perm_id=202,
    )
    update = PriceUpdateCandidate(layer=layer, target_price=Decimal("31.5"))

    def amend() -> PaperSubmission:
        return IbkrPaperExecutionBroker().modify_prices(
            snapshot,
            (update,),
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )

    if confirmed:
        assert amend().order_ids == (101,)
    elif rejected:
        with pytest.raises(ExecutionBlocked, match="TWS price precaution"):
            amend()
    else:
        with pytest.raises(ExecutionOutcomeUnknown, match="post-update"):
            amend()
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert any(
        event["event"] == "place_order"
        and event["requested_price"] == "31.5"
        and event["submitted_transmit"] is True
        for event in events
    )
    if rejected:
        assert any(
            event["event"] == "tws_error"
            and event["code"] == 109
            and event["message"] == "TWS price precaution"
            for event in events
        )
    else:
        assert any(
            event["event"] == "open_order"
            and event["phase"] == "read_after"
            and event["observed_price"] == str(post_price)
            for event in events
        )
    outcomes = [event for event in events if event["event"] == "writer_outcome"]
    assert outcomes[-1]["outcome"] == (
        "blocked" if rejected else "verified" if confirmed else "unknown"
    )


def test_unknown_submission_reconciles_only_when_a_complete_oca_pair_is_observed(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.mark_unknown(plan.fingerprint)
    group = f"{plan.fingerprint[:12]}/tranche-1"
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
    )
    stop = replace(
        target,
        perm_id=202,
        order_id=102,
        order_type="STP",
    )

    assert journal.reconcile_snapshot(replace(snapshot, working_orders=(target,))) == ()

    reconciled = journal.reconcile_snapshot(
        replace(snapshot, working_orders=(target, stop))
    )

    assert len(reconciled) == 1
    assert reconciled[0].state == "RECONCILED"
    assert reconciled[0].order_ids == (101, 102)
    assert reconciled[0].perm_ids == (201, 202)
    assert journal.owned_perm_ids(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
    ) == frozenset({201, 202})


def test_stop_limit_unknown_submission_recovers_after_restart_only_with_exact_prices(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot, stop_order_type="STP LMT")
    assert plan.fingerprint is not None
    path = tmp_path / "journal.json"
    initial = ExecutionJournal(path)
    initial.begin(snapshot, plan)
    initial.mark_unknown(plan.fingerprint)
    journal = ExecutionJournal(path)
    group = f"{plan.fingerprint[:12]}/tranche-1"
    target = WorkingOrder(
        201,
        17,
        101,
        snapshot.selected,
        "SELL",
        "LMT",
        Decimal("1"),
        "Submitted",
        oca_group=group,
        limit_price=Decimal("1.20"),
    )
    stop = replace(
        target,
        perm_id=202,
        order_id=102,
        order_type="STP LMT",
        limit_price=Decimal("0.70"),
        stop_price=Decimal("0.75"),
    )
    assert (
        journal.reconcile_snapshot(
            replace(
                snapshot,
                working_orders=(target, replace(stop, limit_price=Decimal("0.65"))),
            )
        )
        == ()
    )
    assert (
        journal.reconcile_snapshot(replace(snapshot, working_orders=(target, stop)))[
            0
        ].state
        == "RECONCILED"
    )
    entry = journal.find(plan.fingerprint)
    assert entry is not None
    assert entry.layers[0].stop_order_type == "STP LMT"
    assert entry.layers[0].stop_limit_price == "0.70"
    assert journal.owned_perm_ids(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
    ) == frozenset({201, 202})


def test_active_stop_limit_pair_can_be_selected_for_cancel_but_not_price_amendment(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot, stop_order_type="STP LMT")
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.record_submission(
        plan.fingerprint,
        order_ids=(101, 102),
        perm_ids=(201, 202),
    )
    group = f"{plan.fingerprint[:12]}/tranche-1"
    target = WorkingOrder(
        201,
        17,
        101,
        snapshot.selected,
        "SELL",
        "LMT",
        Decimal("2"),
        "Submitted",
        oca_group=group,
        limit_price=Decimal("1.20"),
        tif="GTC",
    )
    stop = replace(
        target,
        perm_id=202,
        order_id=102,
        order_type="STP LMT",
        limit_price=Decimal("0.70"),
        stop_price=Decimal("0.75"),
    )
    active = replace(snapshot, working_orders=(target, stop))
    service = PaperExecutionService(_RecordingTransport(), journal)
    candidate = service.prepare_market_exit(
        active,
        target_perm_id=201,
        expected_client_id=17,
    )
    assert candidate.stop_perm_id == 202
    changed = replace(
        active,
        working_orders=(
            target,
            replace(stop, limit_price=Decimal("0.65")),
        ),
    )
    assert (
        service.prepare_market_exit(
            changed,
            target_perm_id=201,
            expected_client_id=17,
        )
        != candidate
    )
    with pytest.raises(ExecutionBlocked, match="no saved offset rule"):
        service.prepare_price_updates(
            active,
            updates=(
                PriceUpdateCandidate(
                    layer=candidate,
                    target_price=Decimal("1.25"),
                    prior_target_price=Decimal("1.20"),
                ),
            ),
            expected_client_id=17,
        )


def test_new_stop_limit_layer_saves_rule_and_amends_both_prices(tmp_path) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot, stop_order_type="STP LMT")
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(
        snapshot,
        plan,
        stop_limit_offset=Decimal("5"),
        stop_limit_unit="percent",
    )
    journal.record_submission(
        plan.fingerprint, order_ids=(101, 102), perm_ids=(201, 202)
    )
    group = f"{plan.fingerprint[:12]}/tranche-1"
    target = WorkingOrder(
        201,
        17,
        101,
        snapshot.selected,
        "SELL",
        "LMT",
        Decimal("2"),
        "Submitted",
        oca_group=group,
        limit_price=Decimal("1.20"),
        tif="GTC",
    )
    stop = replace(
        target,
        perm_id=202,
        order_id=102,
        order_type="STP LMT",
        stop_price=Decimal("0.75"),
        limit_price=Decimal("0.70"),
    )
    active = replace(snapshot, working_orders=(target, stop))
    service = PaperExecutionService(_RecordingTransport(), journal)
    layer = service.prepare_market_exit(
        active, target_perm_id=201, expected_client_id=17
    )
    assert service.stop_limit_rule(active, layer) == (Decimal("5"), "percent")
    retained = PriceUpdateCandidate(
        layer=layer,
        stop_price=Decimal("0.85"),
        stop_limit_price=Decimal("0.80"),
        prior_stop_price=Decimal("0.75"),
        prior_stop_limit_price=Decimal("0.70"),
    )
    assert service.prepare_price_updates(
        active, updates=(retained,), expected_client_id=17
    ) == (retained,)
    with pytest.raises(ExecutionBlocked, match="does not match"):
        service.prepare_price_updates(
            active,
            updates=(replace(retained, stop_limit_price=Decimal("0.75")),),
            expected_client_id=17,
        )
    with pytest.raises(ExecutionBlocked, match="invalid stop-limit offset"):
        service.prepare_price_updates(
            active,
            updates=(
                replace(
                    retained,
                    stop_limit_offset=Decimal("0"),
                    stop_limit_unit="dollars",
                ),
            ),
            expected_client_id=17,
        )
    overridden = replace(
        retained,
        stop_limit_price=Decimal("0.75"),
        stop_limit_offset=Decimal("0.10"),
        stop_limit_unit="dollars",
    )
    assert service.prepare_price_updates(
        active, updates=(overridden,), expected_client_id=17
    ) == (overridden,)
    offset_only = replace(
        overridden,
        stop_price=Decimal("0.75"),
        stop_limit_price=Decimal("0.65"),
    )
    assert service.prepare_price_updates(
        active, updates=(offset_only,), expected_client_id=17
    ) == (offset_only,)
    service.record_verified_price_updates(active, (overridden,), {201: ("20", "-15")})
    saved = journal.find(plan.fingerprint)
    assert saved is not None
    assert saved.layers[0].stop_limit_price == "0.75"
    assert saved.layers[0].stop_limit_offset == "0.10"
    assert saved.layers[0].stop_limit_unit == "dollars"


@pytest.mark.parametrize(("post_limit", "confirmed"), [(0.80, True), (0.70, False)])
def test_paper_broker_amends_and_verifies_both_stop_limit_prices(
    monkeypatch, tmp_path, post_limit: float, confirmed: bool
) -> None:
    from ibkr_options_manager.broker import execution as broker_execution

    monkeypatch.setenv(
        "IBKR_OPTIONS_MANAGER_PRICE_TRACE", str(tmp_path / "price-amendments.jsonl")
    )
    writes: list[tuple[int, float, float, bool]] = []

    class FakeWrapper:
        def __init__(self) -> None:
            pass

    class FakeClient:
        def __init__(self, wrapper) -> None:
            self.wrapper = wrapper
            self.connected = False
            self.open_requests = 0

        def connect(self, *_args) -> None:
            self.connected = True
            self.wrapper.nextValidId(500)

        def isConnected(self) -> bool:
            return self.connected

        def disconnect(self) -> None:
            self.connected = False

        def run(self) -> None:
            pass

        def _emit(self, trigger: float, limit: float) -> None:
            self.wrapper.openOrder(
                102,
                None,
                SimpleNamespace(
                    permId=202,
                    action="SELL",
                    orderType="STP LMT",
                    auxPrice=trigger,
                    lmtPrice=limit,
                    transmit=True,
                    volatility=0.0,
                    volatilityType=1,
                ),
                SimpleNamespace(status="Submitted"),
            )

        def reqOpenOrders(self) -> None:
            self.open_requests += 1
            self._emit(
                0.75 if self.open_requests == 1 else 0.85,
                0.70 if self.open_requests == 1 else post_limit,
            )
            self.wrapper.openOrderEnd()

        def placeOrder(self, order_id, _contract, order) -> None:
            writes.append((order_id, order.auxPrice, order.lmtPrice, order.transmit))
            self.wrapper.openOrder(
                order_id, None, order, SimpleNamespace(status="Submitted")
            )

    monkeypatch.setattr(
        broker_execution,
        "_load_ibapi",
        lambda: _IbapiImports(
            FakeClient, FakeWrapper, SimpleNamespace, SimpleNamespace
        ),
    )
    snapshot = _snapshot()
    layer = MarketExitCandidate(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        target_order_id=101,
        target_perm_id=201,
        client_id=17,
        quantity=Decimal("2"),
        tif="GTC",
        oca_group="app/tranche-1",
        stop_order_id=102,
        stop_perm_id=202,
        stop_order_type="STP LMT",
        stop_price=Decimal("0.75"),
        stop_limit_price=Decimal("0.70"),
    )
    update = PriceUpdateCandidate(
        layer=layer,
        stop_price=Decimal("0.85"),
        stop_limit_price=Decimal("0.80"),
        prior_stop_price=Decimal("0.75"),
        prior_stop_limit_price=Decimal("0.70"),
    )

    def amend() -> PaperSubmission:
        return IbkrPaperExecutionBroker().modify_prices(
            snapshot,
            (update,),
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )

    if confirmed:
        receipt = amend()
        assert receipt.order_ids == (102,)
        assert receipt.perm_ids == (202,)
    else:
        with pytest.raises(ExecutionOutcomeUnknown, match="post-update"):
            amend()
    assert writes == [(102, 0.85, 0.80, True)]


def test_stop_limit_partial_fill_remains_a_review_state() -> None:
    entry = JournalEntry(
        fingerprint="a" * 64,
        account="DU1234567",
        con_id=917_864_414,
        state="RECONCILED",
        perm_ids=(201, 202),
        layers=(
            JournalLayer(
                quantity=2,
                target_price="1.20",
                stop_price="0.75",
                tif="GTC",
                target_perm_id=201,
                stop_perm_id=202,
                stop_order_type="STP LMT",
                stop_limit_price="0.70",
            ),
        ),
        fills=(
            JournalFill(
                exec_id="fill-1",
                perm_id=202,
                side="SLD",
                quantity="1",
                price="0.70",
                time="now",
                realized_pnl="-5",
                currency="USD",
            ),
        ),
    )
    outcome = classify_journal_layer(
        entry,
        0,
        active_perm_ids=frozenset({201, 202}),
        observed_perm_ids=frozenset({201, 202}),
    )
    assert outcome.status == "PARTIAL"
    assert outcome.filled_quantity == Decimal("1")


def test_unknown_submission_recovers_a_surviving_complete_pair_after_sibling_cancel(
    tmp_path,
) -> None:
    """A manual TWS cancel must not orphan the remaining app-owned OCA pair."""
    snapshot = _snapshot()
    plan = _two_pair_plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.mark_unknown(plan.fingerprint)

    # TWS accepted both pairs, but the 4-contract first pair was cancelled
    # before the app had a chance to reconcile it. Only the transmitted,
    # still-working 3-contract second pair is visible now.
    group = f"{plan.fingerprint[:12]}/tranche-2"
    target = WorkingOrder(
        perm_id=203,
        client_id=17,
        order_id=103,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("3"),
        status="Submitted",
        oca_group=group,
    )
    stop = replace(
        target,
        perm_id=204,
        order_id=104,
        order_type="STP",
    )

    reconciled = journal.reconcile_snapshot(
        replace(snapshot, working_orders=(target, stop))
    )

    assert len(reconciled) == 1
    assert reconciled[0].state == "PARTIALLY_RECONCILED"
    assert reconciled[0].order_ids == (103, 104)
    assert reconciled[0].perm_ids == (203, 204)
    assert journal.owned_perm_ids(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
    ) == frozenset({203, 204})


def test_recreates_a_cancelled_partially_reconciled_draft_fingerprint(tmp_path) -> None:
    """A fresh snapshot may replace a known app attempt only after it is absent."""
    snapshot = _snapshot()
    plan = _two_pair_plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.record_submission(
        plan.fingerprint,
        order_ids=(101, 102, 103, 104),
        perm_ids=(201, 202, 203, 204),
    )

    # The previously-reconciled second pair has subsequently been cancelled in
    # TWS, so the exact current snapshot contains none of the prior perm IDs.
    surviving_target = WorkingOrder(
        perm_id=203,
        client_id=17,
        order_id=103,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("3"),
        status="Submitted",
        oca_group=f"{plan.fingerprint[:12]}/tranche-2",
        tif="GTC",
    )
    journal.reconcile_snapshot(
        replace(
            snapshot,
            working_orders=(
                surviving_target,
                replace(surviving_target, perm_id=204, order_id=104, order_type="STP"),
            ),
        )
    )

    replacement = journal.begin(
        replace(snapshot, captured_at=Decimal("1")),
        plan,
    )

    assert replacement.state == "PREPARED"
    entries = journal._entries()
    assert entries[-2].state == "SUPERSEDED"
    assert journal.find(plan.fingerprint) == replacement


def test_repeated_plan_uses_new_oca_groups_without_adopting_old_orders(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.record_submission(
        plan.fingerprint,
        order_ids=(101, 102),
        perm_ids=(201, 202),
    )
    original = journal.find(plan.fingerprint)
    assert original is not None
    journal._write(
        (
            replace(
                original,
                state="CANCELLED_CONFIRMED",
                resolution_captured_at="1",
            ),
        )
    )
    refreshed = replace(
        snapshot,
        captured_at=Decimal("2"),
        completed_orders_complete=True,
        executions_complete=True,
    )

    class CapturingTransport(_RecordingTransport):
        def __init__(self) -> None:
            super().__init__()
            self.groups: tuple[str, str] | None = None

        def submit(self, _snapshot, sent_plan, **_kwargs):
            pair = sent_plan.pairs[0]
            self.groups = (pair.target.logical_oca_group, pair.stop.logical_oca_group)
            return PaperSubmission(order_ids=(103, 104), perm_ids=(203, 204))

    transport = CapturingTransport()
    receipt = PaperExecutionService(transport, journal).submit(
        refreshed,
        plan,
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=1,
    )
    old_group = plan.pairs[0].target.logical_oca_group
    assert transport.groups is not None
    assert transport.groups[0] == transport.groups[1]
    assert transport.groups[0] != old_group
    assert receipt.entry.oca_prefix and transport.groups[0].startswith(
        receipt.entry.oca_prefix
    )

    old_target = ObservedCompletedOrder(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        perm_id=201,
        order_id=101,
        client_id=17,
        action="SELL",
        order_type="LMT",
        oca_group=old_group,
        status="Cancelled",
    )
    journal.record_completed_orders(
        replace(
            refreshed,
            completed_orders=(
                old_target,
                replace(old_target, perm_id=202, order_id=102, order_type="STP"),
            ),
        )
    )
    latest = journal.find(plan.fingerprint)
    assert latest is not None
    assert latest.layers[0].target_perm_id == 203
    assert latest.layers[0].stop_perm_id == 204
    new_target = WorkingOrder(
        perm_id=203,
        client_id=17,
        order_id=103,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("2"),
        status="Submitted",
        oca_group=transport.groups[0],
        tif="GTC",
    )
    assert (
        journal.reconcile_snapshot(
            replace(
                refreshed,
                working_orders=(
                    new_target,
                    replace(new_target, perm_id=204, order_id=104, order_type="STP"),
                ),
            )
        )[0].state
        == "RECONCILED"
    )
    journal.record_pair_cancellation(
        MarketExitCandidate(
            account=snapshot.selected.account,
            con_id=snapshot.selected.con_id,
            target_order_id=103,
            target_perm_id=203,
            client_id=17,
            quantity=Decimal("2"),
            tif="GTC",
            oca_group=transport.groups[0],
            stop_order_id=104,
            stop_perm_id=204,
        )
    )
    assert journal.find(plan.fingerprint).layers[0].cancelled


def test_legacy_reused_oca_group_cannot_adopt_older_working_pair(tmp_path) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    group = plan.pairs[0].target.logical_oca_group
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal._write(
        (
            JournalEntry(
                fingerprint=plan.fingerprint,
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                state="SUPERSEDED",
                order_ids=(101, 102),
                perm_ids=(201, 202),
                snapshot_captured_at="1",
                layers=(JournalLayer(2, "1.20", "0.75", "GTC", 201, 202),),
            ),
            JournalEntry(
                fingerprint=plan.fingerprint,
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                state="SUBMISSION_UNKNOWN",
                snapshot_captured_at="2",
                expected_order_count=2,
                layers=(JournalLayer(2, "1.20", "0.75", "GTC"),),
            ),
        )
    )
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
        tif="GTC",
    )
    observed = replace(
        snapshot,
        captured_at=Decimal("3"),
        working_orders=(
            target,
            replace(target, perm_id=202, order_id=102, order_type="STP"),
        ),
    )

    journal.record_completed_orders(observed)
    assert journal.reconcile_snapshot(observed) == ()
    latest = journal.find(plan.fingerprint)
    assert latest is not None
    assert latest.state == "SUBMISSION_UNKNOWN"
    assert latest.perm_ids == ()
    assert latest.layers[0].target_perm_id == 0


def test_repeated_two_layer_plan_uses_one_new_group_per_pair(tmp_path) -> None:
    base = _snapshot()
    snapshot = replace(base, position=replace(base.position, quantity=Decimal("7")))
    plan = _two_pair_plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    old = journal.begin(snapshot, plan)
    journal._write(
        (
            replace(
                old,
                state="CANCELLED_CONFIRMED",
                resolution_captured_at="1",
            ),
        )
    )

    class CapturingTransport(_RecordingTransport):
        def __init__(self) -> None:
            super().__init__()
            self.groups: tuple[tuple[str, str], ...] = ()

        def submit(self, _snapshot, sent_plan, **_kwargs):
            self.groups = tuple(
                (pair.target.logical_oca_group, pair.stop.logical_oca_group)
                for pair in sent_plan.pairs
            )
            return PaperSubmission(
                order_ids=(103, 104, 105, 106),
                perm_ids=(203, 204, 205, 206),
            )

    transport = CapturingTransport()
    PaperExecutionService(transport, journal).submit(
        replace(
            snapshot,
            captured_at=Decimal("2"),
            completed_orders_complete=True,
            executions_complete=True,
        ),
        plan,
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=1,
    )
    assert len(transport.groups) == 2
    assert all(target == stop for target, stop in transport.groups)
    assert len({target for target, _stop in transport.groups}) == 2
    assert all(
        sent[0] != planned.target.logical_oca_group
        for sent, planned in zip(transport.groups, plan.pairs, strict=True)
    )


def test_unknown_bracket_cancelled_in_tws_can_be_rebuilt(tmp_path) -> None:
    snapshot = _snapshot()
    plan = _two_pair_plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.mark_unknown(plan.fingerprint)
    completed = tuple(
        ObservedCompletedOrder(
            account=snapshot.selected.account,
            con_id=snapshot.selected.con_id,
            perm_id=201 + index * 2 + leg,
            order_id=101 + index * 2 + leg,
            client_id=17,
            action="SELL",
            order_type="LMT" if leg == 0 else "STP",
            oca_group=f"{plan.fingerprint[:12]}/tranche-{index + 1}",
            status="Cancelled",
        )
        for index in range(2)
        for leg in range(2)
    )
    refreshed = replace(
        snapshot,
        captured_at=snapshot.captured_at + 1,
        completed_orders=completed,
        completed_orders_complete=True,
        executions_complete=True,
    )

    assert journal.reconcile_snapshot(refreshed) == ()
    assert (
        journal.submission_entries(
            account=snapshot.selected.account, con_id=snapshot.selected.con_id
        )
        == ()
    )
    assert journal.begin(refreshed, plan).state == "PREPARED"


def test_immediately_filled_unknown_bracket_recovers_from_completed_history(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.mark_unknown(plan.fingerprint)
    group = f"{plan.fingerprint[:12]}/tranche-1"
    target = ObservedCompletedOrder(
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        perm_id=201,
        order_id=101,
        client_id=17,
        action="SELL",
        order_type="LMT",
        oca_group=group,
        status="Filled",
    )
    stop = replace(
        target, perm_id=202, order_id=102, order_type="STP", status="Cancelled"
    )
    fill = ObservedExecution(
        exec_id="fill.01",
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        perm_id=201,
        side="SLD",
        quantity=Decimal(str(plan.pairs[0].quantity)),
        price=Decimal("20"),
        time="now",
        realized_pnl=Decimal("125"),
        currency="USD",
    )
    history = replace(
        snapshot,
        complete=False,
        fresh=False,
        working_orders=(),
        completed_orders=(target, stop),
        completed_orders_complete=True,
        executions=(fill,),
        executions_complete=True,
    )
    journal.record_completed_orders(history)
    journal.record_executions(history)
    entry = journal.find(plan.fingerprint)
    assert entry is not None
    outcome = classify_journal_layer(
        entry, 0, active_perm_ids=frozenset(), observed_perm_ids=frozenset()
    )
    assert outcome.status == "CLOSED_PROFIT"
    assert outcome.realized_pnl == Decimal("125")


def test_unknown_bracket_without_cancel_evidence_stays_blocked(tmp_path) -> None:
    snapshot = _snapshot()
    plan = _two_pair_plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.mark_unknown(plan.fingerprint)

    with pytest.raises(ExecutionBlocked, match="already journaled"):
        journal.begin(replace(snapshot, captured_at=snapshot.captured_at + 1), plan)


def test_operator_confirmed_unknown_bracket_requires_clean_fresh_tws_read(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.mark_unknown(plan.fingerprint)
    clean = replace(
        snapshot,
        captured_at=snapshot.captured_at + 1,
        completed_orders=(
            ObservedCompletedOrder(
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                perm_id=201,
                order_id=101,
                client_id=17,
                action="SELL",
                order_type="LMT",
                oca_group=f"{plan.fingerprint[:12]}/tranche-1",
                status="Inactive",
            ),
        ),
        completed_orders_complete=True,
        executions_complete=True,
    )

    with pytest.raises(ExecutionBlocked, match="confirm both"):
        journal.confirm_cancelled_unknown(
            clean, plan.fingerprint, confirmed_in_tws=False
        )
    with pytest.raises(ExecutionBlocked, match="complete TWS"):
        journal.confirm_cancelled_unknown(
            replace(clean, executions_complete=False),
            plan.fingerprint,
            confirmed_in_tws=True,
        )
    working = WorkingOrder(
        perm_id=0,
        client_id=17,
        order_id=101,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("2"),
        status="PendingSubmit",
        oca_group=f"{plan.fingerprint[:12]}/tranche-1",
    )
    with pytest.raises(ExecutionBlocked, match="still working"):
        journal.confirm_cancelled_unknown(
            replace(clean, working_orders=(working,)),
            plan.fingerprint,
            confirmed_in_tws=True,
        )
    fill = ObservedExecution(
        exec_id="unknown.01",
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        perm_id=201,
        side="SLD",
        quantity=Decimal("1"),
        price=Decimal("1.20"),
        time="20260925 12:00:00",
    )
    with pytest.raises(ExecutionBlocked, match="201 may belong"):
        journal.confirm_cancelled_unknown(
            replace(clean, executions=(fill,)),
            plan.fingerprint,
            confirmed_in_tws=True,
        )
    with pytest.raises(ExecutionBlocked, match="999 may belong"):
        journal.confirm_cancelled_unknown(
            replace(clean, executions=(replace(fill, perm_id=999),)),
            plan.fingerprint,
            confirmed_in_tws=True,
        )
    with pytest.raises(ExecutionBlocked, match="conflicts"):
        journal.confirm_cancelled_unknown(
            replace(
                clean,
                completed_orders=(replace(clean.completed_orders[0], status="Filled"),),
            ),
            plan.fingerprint,
            confirmed_in_tws=True,
        )

    resolved = journal.confirm_cancelled_unknown(
        clean, plan.fingerprint, confirmed_in_tws=True
    )

    assert resolved.state == "CANCELLED_CONFIRMED"
    assert resolved.resolution_captured_at == str(clean.captured_at)
    assert (
        journal.submission_entries(
            account=snapshot.selected.account, con_id=snapshot.selected.con_id
        )
        == ()
    )
    with pytest.raises(ExecutionBlocked, match="already journaled"):
        journal.begin(clean, plan)
    assert (
        journal.begin(replace(clean, captured_at=clean.captured_at + 1), plan).state
        == "PREPARED"
    )


def test_cancelled_unknown_ignores_other_tranches_on_same_contract(tmp_path) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.mark_unknown(plan.fingerprint)
    other_group = "other-tranche/tranche-1"
    other_order = WorkingOrder(
        perm_id=901,
        client_id=17,
        order_id=801,
        key=snapshot.selected,
        action="SELL",
        order_type="STP",
        remaining=Decimal("2"),
        status="Submitted",
        oca_group=other_group,
    )
    other_fill = ObservedExecution(
        exec_id="other.01",
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        perm_id=902,
        side="SLD",
        quantity=Decimal("1"),
        price=Decimal("26.50"),
        time="20260925 12:00:00",
    )
    refreshed = replace(
        snapshot,
        captured_at=snapshot.captured_at + 1,
        working_orders=(other_order,),
        executions=(other_fill,),
        executions_complete=True,
        completed_orders=(
            ObservedCompletedOrder(
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                perm_id=902,
                order_id=802,
                client_id=17,
                action="SELL",
                order_type="STP",
                oca_group=other_group,
                status="Filled",
            ),
        ),
        completed_orders_complete=True,
    )

    resolved = journal.confirm_cancelled_unknown(
        refreshed, plan.fingerprint, confirmed_in_tws=True
    )
    assert resolved.state == "CANCELLED_CONFIRMED"
    assert (
        journal.begin(
            replace(refreshed, captured_at=refreshed.captured_at + 1), plan
        ).state
        == "PREPARED"
    )


def test_cancelled_unknown_uses_other_journal_ids_and_ignores_buy_fill(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.mark_unknown(plan.fingerprint)
    journal._write(
        (
            *journal._entries(),
            JournalEntry(
                fingerprint="f" * 64,
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                state="SUBMITTED",
                perm_ids=(902,),
            ),
        )
    )
    other_sell = ObservedExecution(
        exec_id="other.01",
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        perm_id=902,
        side="SLD",
        quantity=Decimal("1"),
        price=Decimal("26.50"),
        time="20260925 12:00:00",
    )
    buy = replace(other_sell, exec_id="buy.01", perm_id=903, side="BOT")
    refreshed = replace(
        snapshot,
        captured_at=snapshot.captured_at + 1,
        executions=(other_sell, buy),
        executions_complete=True,
        completed_orders_complete=True,
    )
    assert (
        journal.confirm_cancelled_unknown(
            refreshed, plan.fingerprint, confirmed_in_tws=True
        ).state
        == "CANCELLED_CONFIRMED"
    )
    assert (
        journal.begin(
            replace(refreshed, captured_at=refreshed.captured_at + 1), plan
        ).state
        == "PREPARED"
    )


def test_confirm_cancelled_layer_preserves_active_siblings(tmp_path) -> None:
    snapshot = _snapshot()
    fingerprint = "f" * 64
    layers = tuple(
        JournalLayer(
            quantity=1,
            target_price=str(i + 2),
            stop_price="1.00",
            tif="GTC",
            target_perm_id=200 + i * 2,
            stop_perm_id=201 + i * 2,
        )
        for i in range(4)
    )
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal._write(
        (
            JournalEntry(
                fingerprint=fingerprint,
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                state="RECONCILED",
                expected_order_count=8,
                snapshot_captured_at="1",
                perm_ids=tuple(range(200, 208)),
                order_ids=tuple(range(100, 108)),
                layers=layers,
            ),
        )
    )

    def working(index: int, stop: bool) -> WorkingOrder:
        return WorkingOrder(
            perm_id=200 + index * 2 + int(stop),
            client_id=17,
            order_id=100 + index * 2 + int(stop),
            key=snapshot.selected,
            action="SELL",
            order_type="STP" if stop else "LMT",
            remaining=Decimal("1"),
            status="Submitted",
            oca_group=f"{fingerprint[:12]}/tranche-{index + 1}",
        )

    clean = replace(
        snapshot,
        captured_at=Decimal("2"),
        position=replace(snapshot.position, quantity=Decimal("4")),
        working_orders=tuple(
            working(i, stop) for i in (1, 2, 3) for stop in (False, True)
        ),
        completed_orders_complete=True,
        executions_complete=True,
    )
    with pytest.raises(ExecutionBlocked, match="still working"):
        journal.confirm_cancelled_layer(clean, fingerprint, 1, confirmed_in_tws=True)
    with pytest.raises(ExecutionBlocked, match="complete TWS"):
        journal.confirm_cancelled_layer(
            replace(clean, executions_complete=False),
            fingerprint,
            0,
            confirmed_in_tws=True,
        )
    missing_fill = ObservedExecution(
        exec_id="missing.01",
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        perm_id=200,
        side="SLD",
        quantity=Decimal("1"),
        price=Decimal("2"),
        time="20260925 12:00:00",
    )
    with pytest.raises(ExecutionBlocked, match="execution may belong"):
        journal.confirm_cancelled_layer(
            replace(clean, executions=(missing_fill,)),
            fingerprint,
            0,
            confirmed_in_tws=True,
        )
    updated = journal.confirm_cancelled_layer(
        clean, fingerprint, 0, confirmed_in_tws=True
    )
    assert updated.state == "RECONCILED"
    assert [layer.cancelled for layer in updated.layers] == [True, False, False, False]
    assert (
        classify_journal_layer(
            updated,
            0,
            active_perm_ids=frozenset(range(202, 208)),
            observed_perm_ids=frozenset(range(202, 208)),
        ).status
        == "CANCELLED"
    )
    assert (
        classify_journal_layer(
            updated,
            1,
            active_perm_ids=frozenset(range(202, 208)),
            observed_perm_ids=frozenset(range(202, 208)),
        ).status
        == "ACTIVE"
    )
    with pytest.raises(ExecutionBlocked, match="already cleared"):
        journal.confirm_cancelled_layer(clean, fingerprint, 0, confirmed_in_tws=True)


def test_dismiss_cancelled_layer_keeps_journal_and_rejects_working_leg(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    journal = ExecutionJournal(tmp_path / "journal.json")
    fingerprint = "a" * 64
    entry = JournalEntry(
        fingerprint=fingerprint,
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        state="RECONCILED",
        snapshot_captured_at="101",
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
    )
    earlier = replace(
        entry,
        state="SUPERSEDED",
        snapshot_captured_at="100",
        order_ids=(301, 302),
        perm_ids=(401, 402),
        layers=(replace(entry.layers[0], target_perm_id=401, stop_perm_id=402),),
    )
    journal._write((earlier, entry))
    stale = replace(snapshot, complete=False, fresh=False)
    working = WorkingOrder(
        perm_id=202,
        client_id=17,
        order_id=102,
        key=snapshot.selected,
        action="SELL",
        order_type="STP",
        remaining=Decimal("2"),
        status="Submitted",
        oca_group=f"{fingerprint[:12]}/tranche-1",
    )
    with pytest.raises(ExecutionBlocked, match="working leg or fill"):
        journal.dismiss_cancelled_layer(
            replace(stale, working_orders=(working,)), fingerprint, "101", 0
        )
    newer_attempt_order = replace(working, perm_id=999, order_id=999)
    hidden = journal.dismiss_cancelled_layer(
        replace(stale, working_orders=(newer_attempt_order,)),
        fingerprint,
        "101",
        0,
    )
    assert hidden.layers[0].hidden_from_workspace
    assert journal.find(fingerprint) == hidden
    assert hidden.perm_ids == entry.perm_ids
    assert not journal._entries()[0].layers[0].hidden_from_workspace


def test_dismiss_missing_layer_does_not_match_surviving_sibling_ids(tmp_path) -> None:
    snapshot = _snapshot()
    fingerprint = "b" * 64
    layers = (
        JournalLayer(1, "3.02", "1.89", "GTC", stop_perm_id=201, cancelled=True),
        JournalLayer(1, "3.52", "1.89", "GTC", target_perm_id=202, stop_perm_id=203),
        JournalLayer(1, "4.02", "1.89", "GTC", target_perm_id=204, stop_perm_id=205),
        JournalLayer(1, "5.02", "1.89", "GTC", target_perm_id=206, stop_perm_id=207),
    )
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal._write(
        (
            JournalEntry(
                fingerprint=fingerprint,
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
                state="PARTIALLY_RECONCILED",
                expected_order_count=8,
                snapshot_captured_at="1",
                layers=layers,
                order_ids=(102, 103, 104, 105, 106, 107),
                perm_ids=(202, 203, 204, 205, 206, 207),
            ),
        )
    )
    sibling = WorkingOrder(
        perm_id=202,
        client_id=17,
        order_id=102,
        key=snapshot.selected,
        action="SELL",
        order_type="LMT",
        remaining=Decimal("1"),
        status="Submitted",
        oca_group=f"{fingerprint[:12]}/tranche-2",
    )
    refreshed = replace(snapshot, working_orders=(sibling,))
    hidden = journal.dismiss_cancelled_layer(refreshed, fingerprint, "1", 0)
    assert hidden.layers[0].hidden_from_workspace
    assert not hidden.layers[1].hidden_from_workspace
    with pytest.raises(ExecutionBlocked, match="working leg or fill"):
        journal.dismiss_cancelled_layer(
            replace(
                refreshed,
                working_orders=(
                    replace(sibling, oca_group=f"{fingerprint[:12]}/tranche-1"),
                ),
            ),
            fingerprint,
            "1",
            0,
        )


def test_cancelled_bracket_with_a_fill_cannot_be_rebuilt(tmp_path) -> None:
    snapshot = _snapshot()
    plan = _two_pair_plan(snapshot)
    assert plan.fingerprint is not None
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.begin(snapshot, plan)
    journal.mark_unknown(plan.fingerprint)
    completed = tuple(
        ObservedCompletedOrder(
            account=snapshot.selected.account,
            con_id=snapshot.selected.con_id,
            perm_id=201 + index * 2 + leg,
            order_id=101 + index * 2 + leg,
            client_id=17,
            action="SELL",
            order_type="LMT" if leg == 0 else "STP",
            oca_group=f"{plan.fingerprint[:12]}/tranche-{index + 1}",
            status="Cancelled",
        )
        for index in range(2)
        for leg in range(2)
    )
    fill = ObservedExecution(
        exec_id="fill.01",
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        perm_id=201,
        side="SLD",
        quantity=Decimal("1"),
        price=Decimal("1.20"),
        time="20260925 12:00:00",
    )
    refreshed = replace(
        snapshot,
        captured_at=snapshot.captured_at + 1,
        completed_orders=completed,
        completed_orders_complete=True,
        executions=(fill,),
        executions_complete=True,
    )

    assert journal.reconcile_snapshot(refreshed) == ()
    with pytest.raises(ExecutionBlocked, match="already journaled"):
        journal.begin(refreshed, plan)
