"""Deterministic paper snapshot and plan builders shared by test layers."""

from dataclasses import replace
from decimal import Decimal

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
    build_exit_plan,
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


def _trailing_snapshot() -> BrokerSnapshot:
    return replace(
        _snapshot(), executions_complete=True, completed_orders_complete=True
    )


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
