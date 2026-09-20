from dataclasses import replace
from decimal import Decimal

from ibkr_options_manager.app.view_model import (
    ConnectionSelection,
    ConnectionSettings,
    DraftLayerForm,
    PlanForm,
    PlannerViewModel,
    UiStatus,
)
from ibkr_options_manager.broker import CapturedContract
from ibkr_options_manager.domain import (
    BrokerSnapshot,
    ContractKey,
    MarketRule,
    ObservedPosition,
    PriceBand,
    Quote,
    RemainderPolicy,
    VerifiedOptionContract,
    WorkingOrder,
)
from ibkr_options_manager.portfolio import (
    PortfolioPosition,
    PortfolioResult,
    PortfolioSnapshot,
    PortfolioStatus,
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


class FakePortfolioSource:
    def __init__(self, result: PortfolioResult) -> None:
        self.result = result
        self.refresh_count = 0

    def refresh(self, request: object) -> PortfolioResult:
        del request
        self.refresh_count += 1
        return self.result

    def current(self) -> PortfolioResult:
        return self.result


def ready_portfolio() -> PortfolioResult:
    snapshot = ready_snapshot()
    contract = snapshot.contract
    position = PortfolioPosition(
        key=snapshot.selected,
        contract=CapturedContract(
            con_id=contract.con_id,
            sec_type=contract.sec_type,
            expiry=contract.expiry,
            strike=contract.strike,
            right=contract.right,
            multiplier=contract.multiplier,
            currency=contract.currency,
            trading_class=contract.trading_class,
            exchange=contract.exchange,
            local_symbol=contract.local_symbol,
        ),
        quantity=snapshot.position.quantity,
        raw_average_cost=snapshot.position.raw_average_cost,
        unit_basis=snapshot.position.unit_basis,
        working_orders=snapshot.working_orders,
        eligible=True,
        eligibility="Eligible",
    )
    return PortfolioResult(
        PortfolioStatus.READY,
        PortfolioSnapshot(
            account=snapshot.selected.account,
            connected=True,
            read_only_api=True,
            localhost_only=True,
            paper_account_verified=True,
            connection_epoch=2,
            server_version=223,
            server_time=snapshot.server_time,
            captured_at=Decimal("100"),
            positions=(position,),
        ),
        (),
    )


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
    assert state.allocation == ("Position 5", "Reserved 0", "Bracketed 5 · Open 0")
    assert [pair.quantity for pair in state.pairs] == [2, 2, 1]
    assert state.preview_headers == (
        "Pair",
        "Qty",
        "Target",
        "Stop",
        "TIF",
        "Logical OCA",
    )
    assert state.pairs[0].target_price == Decimal("28.6")
    assert state.pairs[0].stop_price == Decimal("19.1")
    assert state.quote_calculator is not None
    assert state.quote_calculator.bid == Decimal("23.3")
    assert state.quote_calculator.ask == Decimal("23.4")
    assert state.quote_calculator.bands == ready_snapshot().market_rule.bands
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


def test_explicit_layer_form_maps_each_price_into_the_preview() -> None:
    source = FakeSnapshotSource(
        SnapshotResult(SnapshotStatus.READY, ready_snapshot(), ())
    )
    view_model = PlannerViewModel(source, clock=lambda: Decimal("101"))
    layer_form = PlanForm(
        layers=(
            DraftLayerForm("3", "28.6", "19.1", "20", "GTC"),
            DraftLayerForm("2", "33.4", "17.9", "40", "DAY", True),
        )
    )

    state = view_model.refresh(selection(), layer_form)

    assert state.status is UiStatus.READY
    prices = [
        (pair.quantity, pair.target_price, pair.stop_price) for pair in state.pairs
    ]
    assert prices == [
        (3, Decimal("28.6"), Decimal("19.1")),
        (2, Decimal("33.4"), Decimal("17.9")),
    ]
    assert state.pairs[1].runner is True
    assert state.available_quantity == 5


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


def test_portfolio_refresh_lists_positions_before_loading_contract_detail() -> None:
    snapshots = FakeSnapshotSource(
        SnapshotResult(SnapshotStatus.READY, ready_snapshot(), ())
    )
    portfolio = FakePortfolioSource(ready_portfolio())
    view_model = PlannerViewModel(
        snapshots,
        portfolio=portfolio,
        clock=lambda: Decimal("101"),
    )

    state = view_model.refresh_portfolio(ConnectionSettings(account="DU1234567"))

    assert state.status is UiStatus.READY
    assert state.selected_con_id is None
    assert [position.con_id for position in state.positions] == [917864414]
    assert state.positions[0].quantity == "5"
    assert state.positions[0].unit_basis == "$23.816303"
    assert state.account == "***4567"
    assert portfolio.refresh_count == 1
    assert snapshots.refresh_count == 0


def test_selecting_a_position_loads_its_default_bracket_preview() -> None:
    snapshots = FakeSnapshotSource(
        SnapshotResult(SnapshotStatus.READY, ready_snapshot(), ())
    )
    view_model = PlannerViewModel(
        snapshots,
        portfolio=FakePortfolioSource(ready_portfolio()),
        clock=lambda: Decimal("101"),
    )
    view_model.refresh_portfolio(ConnectionSettings(account="DU1234567"))

    state = view_model.select_position(917864414)

    assert state.selected_con_id == 917864414
    assert [row.values[1] for row in state.preview_rows] == ["2", "2", "1"]
    assert state.positions[0].con_id == 917864414
    assert snapshots.refresh_count == 1


def test_bracket_preview_reserves_existing_closing_orders() -> None:
    key = ContractKey("DU1234567", 917864414)
    external = WorkingOrder(
        51,
        4,
        101,
        key,
        "SELL",
        "LMT",
        Decimal("2"),
        "Submitted",
    )
    snapshot = replace(ready_snapshot(), working_orders=(external,))
    source = FakeSnapshotSource(SnapshotResult(SnapshotStatus.READY, snapshot, ()))
    view_model = PlannerViewModel(
        source,
        portfolio=FakePortfolioSource(ready_portfolio()),
        clock=lambda: Decimal("101"),
    )
    view_model.refresh_portfolio(ConnectionSettings(account="DU1234567"))
    view_model.select_position(917864414)

    state = view_model.preview_action(form())

    assert state.status is UiStatus.READY
    assert state.allocation == ("Position 5", "Reserved 2", "Bracketed 3 · Open 0")
    assert [row.values[1] for row in state.preview_rows] == ["2", "1"]
    assert state.working_orders[0].action == "SELL"
    assert state.working_orders[0].status == "Submitted"
    assert state.validations == ()


def test_preview_action_builds_an_oca_preview() -> None:
    source = FakeSnapshotSource(
        SnapshotResult(SnapshotStatus.READY, ready_snapshot(), ())
    )
    view_model = PlannerViewModel(
        source,
        portfolio=FakePortfolioSource(ready_portfolio()),
        clock=lambda: Decimal("101"),
    )
    view_model.refresh_portfolio(ConnectionSettings(account="DU1234567"))
    view_model.select_position(917864414)

    bracket = view_model.preview_action(form())

    assert bracket.status is UiStatus.READY
    assert bracket.preview_headers[0] == "Pair"
    assert [row.values[1] for row in bracket.preview_rows] == ["2", "2", "1"]


def test_bracket_form_is_remembered_for_the_selected_contract() -> None:
    source = FakeSnapshotSource(
        SnapshotResult(SnapshotStatus.READY, ready_snapshot(), ())
    )
    view_model = PlannerViewModel(
        source,
        portfolio=FakePortfolioSource(ready_portfolio()),
        clock=lambda: Decimal("101"),
    )
    view_model.refresh_portfolio(ConnectionSettings(account="DU1234567"))
    view_model.select_position(917864414)
    custom = replace(
        form(),
        target_percentages="35, 70, 105",
        stop_loss_percentage="12",
    )
    view_model.preview_action(custom)

    restored = view_model.select_position(917864414)

    assert restored.bracket_form == custom
