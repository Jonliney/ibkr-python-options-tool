from dataclasses import replace
from decimal import Decimal

import pytest

from ibkr_options_manager.broker.execution import (
    PaperSubmission,
    _build_submission_contract,
)
from ibkr_options_manager.domain import (
    BrokerSnapshot,
    ContractKey,
    LayerRequest,
    MarketRule,
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
    require_paper_execution_snapshot,
)


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

    def cancel_pair_then_submit_market(
        self,
        _snapshot: BrokerSnapshot,
        candidate: MarketExitCandidate,
        **_kwargs: object,
    ) -> PaperSubmission:
        self.market_candidates.append(candidate)
        return PaperSubmission(order_ids=(301,), perm_ids=(401,))


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


def test_paper_execution_requires_read_only_api_to_have_been_explicitly_disabled(
) -> None:
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
        ExecutionBlocked, match="market-exit attempt is already journaled"
    ):
        service.cancel_pair_then_submit_market(
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
