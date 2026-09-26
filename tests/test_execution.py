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
    MarketExitCandidate,
    PaperExecutionService,
    PriceUpdateCandidate,
    classify_journal_layer,
    require_paper_execution_snapshot,
)
from ibkr_options_manager.ibkr_probe import _IbapiImports


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


def test_cancel_order_supplies_the_required_empty_order_cancel_options() -> None:
    calls: list[tuple[int, object]] = []

    class RequiredCancelArguments:
        def cancelOrder(self, order_id: int, order_cancel: object) -> None:
            calls.append((order_id, order_cancel))

    _cancel_order(RequiredCancelArguments(), 701)

    assert calls[0][0] == 701
    assert vars(calls[0][1])["manualOrderCancelTime"] == ""
    assert vars(calls[0][1])["extOperator"] == ""


def _plan(snapshot: BrokerSnapshot):
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
        ),
    )


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


def test_indeterminate_transport_outcome_is_durably_blocked_from_retry(
    tmp_path,
) -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    transport = _RecordingTransport(fail=True)
    journal = ExecutionJournal(tmp_path / "journal.json")
    service = PaperExecutionService(transport, journal)

    with pytest.raises(RuntimeError, match="socket failed"):
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
    with pytest.raises(ExecutionBlocked, match="already journaled"):
        service.cancel_pair(
            active_snapshot,
            candidate,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )


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
        perm_id=201, client_id=17, order_id=101, key=snapshot.selected,
        action="SELL", order_type="LMT", remaining=Decimal("2"),
        status="Submitted", oca_group=group, tif="GTC",
        limit_price=Decimal("1.20"),
    )
    stop = replace(
        target, perm_id=202, order_id=102, order_type="STP",
        limit_price=None, stop_price=Decimal("0.75"),
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
        layer=layer, target_price=Decimal("1.40"),
        prior_target_price=Decimal("1.20"),
        prior_stop_price=Decimal("0.75"),
    )

    def amend(current, *, allow_unknown_retry=False):
        return service.modify_prices(
            current, (update,), host="127.0.0.1", port=7497,
            client_id=17, timeout_seconds=1,
            allow_unknown_retry=allow_unknown_retry,
        )

    with pytest.raises(ExecutionOutcomeUnknown, match="lost TWS"):
        amend(active)
    with pytest.raises(ExecutionBlocked, match="already journaled"):
        amend(active)
    with pytest.raises(ExecutionBlocked, match=r"fresh.*later"):
        amend(active, allow_unknown_retry=True)
    assert transport.attempts == 1

    refreshed = replace(active, captured_at=Decimal("1"))
    already_changed = replace(
        refreshed,
        working_orders=(replace(target, limit_price=Decimal("1.40")), stop),
    )
    with pytest.raises(ExecutionBlocked, match="prices changed"):
        amend(already_changed, allow_unknown_retry=True)
    assert transport.attempts == 1
    assert (
        service.price_update_attempt_state(refreshed, (update,))
        == "SUBMISSION_UNKNOWN"
    )
    receipt = amend(refreshed, allow_unknown_retry=True)
    assert receipt.entry.order_ids == (101,)
    assert transport.attempts == 2
    attempts = [
        entry for entry in journal._entries()
        if entry.fingerprint.startswith("price-update:")
    ]
    assert [entry.state for entry in attempts] == ["SUBMISSION_UNKNOWN", "SUBMITTED"]
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
                ),
                SimpleNamespace(status="Submitted"),
            )
            self.wrapper.openOrderEnd()

        def placeOrder(self, order_id, _contract, order) -> None:
            assert order_id == 101
            assert order.lmtPrice == 31.5
            assert order.transmit is True
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
