from dataclasses import replace
from decimal import Decimal

from ibkr_options_manager.app.view_model import (
    ConnectionSelection,
    PlanForm,
    PlannerViewModel,
    UiStatus,
)
from ibkr_options_manager.domain import (
    BrokerSnapshot,
    ContractKey,
    MarketRule,
    ObservedPosition,
    PriceBand,
    Quote,
    RemainderPolicy,
    TriggerMethod,
    VerifiedOptionContract,
)
from ibkr_options_manager.snapshot import SnapshotResult, SnapshotStatus


def ready_snapshot() -> BrokerSnapshot:
    key = ContractKey("DU1234567", 917864414)
    return BrokerSnapshot(
        selected=key,
        connected=True,
        read_only_api=True,
        localhost_only=True,
        paper_account_verified=True,
        complete=True,
        fresh=True,
        connection_epoch=3,
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
            quantity=Decimal("5"),
            raw_average_cost=Decimal("2381.6303"),
            unit_basis=Decimal("23.816303"),
        ),
        working_orders=(),
        quote=Quote(
            bid=Decimal("23.3"),
            ask=Decimal("23.4"),
            last=Decimal("23.3"),
            close=Decimal("21.25"),
            market_data_type="LIVE",
            fresh=True,
            observed_at=Decimal("100"),
        ),
        market_rule=MarketRule(
            exchange="SMART",
            bands=(
                PriceBand(Decimal("0"), Decimal("0.05")),
                PriceBand(Decimal("3"), Decimal("0.1")),
            ),
        ),
        server_version=223,
        server_time=1_789_566_365,
        captured_at=Decimal("100"),
    )


class FakeSnapshotSource:
    def __init__(self, result: SnapshotResult) -> None:
        self.result = result
        self.refresh_count = 0

    def refresh(self, request: object) -> SnapshotResult:
        del request
        self.refresh_count += 1
        return self.result

    def current(self) -> SnapshotResult:
        return self.result


def selection() -> ConnectionSelection:
    return ConnectionSelection(
        account="DU1234567",
        con_id=917864414,
        port=7497,
        client_id=17,
        timeout_seconds=20,
    )


def form() -> PlanForm:
    return PlanForm(
        tranche_size="2",
        target_percentages="20, 40, 60",
        stop_loss_percentage="20",
        remainder_policy=RemainderPolicy.NEXT_RUNG,
        tif="GTC",
        trigger_method=TriggerMethod.DOUBLE_BID_ASK,
    )


def test_ready_snapshot_maps_to_a_redacted_complete_preview() -> None:
    source = FakeSnapshotSource(
        SnapshotResult(SnapshotStatus.READY, ready_snapshot(), ())
    )
    view_model = PlannerViewModel(source, clock=lambda: Decimal("101"))

    state = view_model.refresh(selection(), form())

    assert state.status is UiStatus.READY
    assert state.account == "***4567"
    assert state.snapshot_age == "1.0 s"
    assert state.position_title == "SPXW  260916C07605000"
    assert state.allocation == ("Position 5", "Allocated 0", "Planned 5")
    assert [pair.quantity for pair in state.pairs] == [2, 2, 1]
    assert state.pairs[0].target_price == Decimal("28.6")
    assert state.pairs[0].stop_price == Decimal("19.1")
    assert state.validations == ()
    assert state.can_preview is True
    assert "DU1234567" not in repr(state)


def test_malformed_plan_input_blocks_without_discarding_broker_evidence() -> None:
    source = FakeSnapshotSource(
        SnapshotResult(SnapshotStatus.READY, ready_snapshot(), ())
    )
    view_model = PlannerViewModel(source, clock=lambda: Decimal("101"))
    view_model.refresh(selection(), form())

    state = view_model.preview(replace(form(), target_percentages="20, nope"))

    assert state.status is UiStatus.BLOCKED
    assert state.position_title == "SPXW  260916C07605000"
    assert state.pairs == ()
    assert state.validations[0].code == "INPUT_INVALID"
    assert state.can_preview is True


def test_failed_refresh_replaces_the_previous_snapshot_and_redacts_errors() -> None:
    source = FakeSnapshotSource(
        SnapshotResult(SnapshotStatus.READY, ready_snapshot(), ())
    )
    view_model = PlannerViewModel(source, clock=lambda: Decimal("101"))
    assert view_model.refresh(selection(), form()).status is UiStatus.READY
    source.result = SnapshotResult(
        SnapshotStatus.BLOCKED,
        None,
        ("account DU1234567 position request timed out",),
    )

    state = view_model.refresh(selection(), form())

    assert state.status is UiStatus.BLOCKED
    assert state.position_title == "No verified position"
    assert state.pairs == ()
    assert state.can_preview is False
    assert state.validations[0].message == (
        "account ***4567 position request timed out"
    )
    assert "DU1234567" not in repr(state)


def test_preview_rechecks_snapshot_freshness_without_refreshing_transport() -> None:
    source = FakeSnapshotSource(
        SnapshotResult(SnapshotStatus.READY, ready_snapshot(), ())
    )
    view_model = PlannerViewModel(source, clock=lambda: Decimal("101"))
    view_model.refresh(selection(), form())
    source.result = SnapshotResult(
        SnapshotStatus.STALE,
        None,
        ("snapshot exceeded its freshness limit",),
    )

    state = view_model.preview(form())

    assert state.status is UiStatus.STALE
    assert state.can_preview is False
    assert source.refresh_count == 1
