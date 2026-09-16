from dataclasses import replace
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ibkr_options_manager.domain import (
    BrokerSnapshot,
    ContractKey,
    MarketRule,
    ObservedPosition,
    PlanRequest,
    PlanStatus,
    PriceBand,
    Quote,
    RemainderPolicy,
    TriggerMethod,
    VerifiedOptionContract,
    WorkingOrder,
    build_exit_plan,
)


def complete_snapshot(
    *, quantity: Decimal = Decimal("10"), working_orders: tuple = ()
) -> BrokerSnapshot:
    key = ContractKey(account="DU1234567", con_id=917864414)
    return BrokerSnapshot(
        selected=key,
        connected=True,
        read_only_api=True,
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
            quantity=quantity,
            raw_average_cost=Decimal("100"),
            unit_basis=Decimal("1.00"),
        ),
        working_orders=working_orders,
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


def canonical_request(
    *, remainder_policy: RemainderPolicy = RemainderPolicy.NEXT_RUNG
) -> PlanRequest:
    return PlanRequest(
        tranche_size=2,
        target_percentages=tuple(
            Decimal(value) for value in ("20", "40", "60", "80", "100")
        ),
        stop_loss_percentage=Decimal("20"),
        remainder_policy=remainder_policy,
        tif="GTC",
        trigger_method=TriggerMethod.DOUBLE_BID_ASK,
    )


def test_builds_five_equal_target_stop_pairs_for_ten_contracts() -> None:
    result = build_exit_plan(complete_snapshot(), canonical_request())

    assert result.status is PlanStatus.VALID
    assert result.allocated_quantity == 0
    assert result.available_quantity == 10
    assert result.planned_quantity == 10
    assert len(result.pairs) == 5
    assert len(result.fingerprint or "") == 64

    first = result.pairs[0]
    assert first.quantity == 2
    assert first.target.action == first.stop.action == "SELL"
    assert first.target.order_type == "LMT"
    assert first.stop.order_type == "STP"
    assert first.target.raw_price == first.target.rounded_price == Decimal("1.20")
    assert first.stop.raw_price == first.stop.rounded_price == Decimal("0.80")
    assert first.target.oca_type == first.stop.oca_type == 2
    assert first.target.logical_oca_group == first.stop.logical_oca_group
    assert first.target.account == first.stop.account == "DU1234567"
    assert first.target.con_id == first.stop.con_id == 917864414


def test_blocks_an_unverified_or_incomplete_snapshot() -> None:
    snapshot = replace(
        complete_snapshot(),
        connected=False,
        read_only_api=False,
        localhost_only=False,
        paper_account_verified=False,
        complete=False,
        fresh=False,
        errors=("position request timed out",),
    )

    result = build_exit_plan(snapshot, canonical_request())

    assert result.status is PlanStatus.BLOCKED
    assert result.fingerprint is None
    assert result.pairs == ()
    assert {validation.code for validation in result.validations} == {
        "CONNECTION_REQUIRED",
        "READ_ONLY_REQUIRED",
        "LOCALHOST_REQUIRED",
        "PAPER_ACCOUNT_REQUIRED",
        "SNAPSHOT_INCOMPLETE",
        "SNAPSHOT_STALE",
        "BROKER_ERROR",
    }


def test_blocks_a_non_option_or_fractional_position() -> None:
    snapshot = complete_snapshot(quantity=Decimal("1.5"))
    snapshot = replace(
        snapshot,
        contract=replace(snapshot.contract, sec_type="STK"),
    )

    result = build_exit_plan(snapshot, canonical_request())

    assert result.status is PlanStatus.BLOCKED
    assert {validation.code for validation in result.validations} == {
        "OPTION_REQUIRED",
        "INTEGRAL_LONG_POSITION_REQUIRED",
    }


def test_allocates_ungrouped_sells_plus_one_equal_oca_pair_quantity() -> None:
    selected = ContractKey("DU1234567", 917864414)
    orders = (
        WorkingOrder(1, 8, 101, selected, "SELL", "LMT", Decimal("1"), "Submitted"),
        WorkingOrder(
            2,
            8,
            102,
            selected,
            "SELL",
            "LMT",
            Decimal("2"),
            "Submitted",
            "existing-pair",
        ),
        WorkingOrder(
            3,
            8,
            103,
            selected,
            "SELL",
            "STP",
            Decimal("2"),
            "Submitted",
            "existing-pair",
        ),
        WorkingOrder(
            4,
            8,
            104,
            ContractKey("DU1234567", 999),
            "SELL",
            "LMT",
            Decimal("9"),
            "Submitted",
        ),
    )

    result = build_exit_plan(
        complete_snapshot(working_orders=orders), canonical_request()
    )

    assert result.status is PlanStatus.VALID
    assert result.allocated_quantity == 3
    assert result.available_quantity == 7
    assert result.planned_quantity == 7
    assert [pair.quantity for pair in result.pairs] == [2, 2, 2, 1]


def test_blocks_an_ambiguous_existing_oca_group() -> None:
    selected = ContractKey("DU1234567", 917864414)
    orders = (
        WorkingOrder(
            1,
            8,
            101,
            selected,
            "SELL",
            "LMT",
            Decimal("2"),
            "Submitted",
            "ambiguous",
        ),
        WorkingOrder(
            2,
            8,
            102,
            selected,
            "SELL",
            "STP",
            Decimal("1"),
            "Submitted",
            "ambiguous",
        ),
        WorkingOrder(
            3,
            8,
            103,
            ContractKey("DU1234567", 999),
            "SELL",
            "STP",
            Decimal("2"),
            "Submitted",
            "ambiguous",
        ),
    )

    result = build_exit_plan(
        complete_snapshot(working_orders=orders), canonical_request()
    )

    assert result.status is PlanStatus.BLOCKED
    assert {validation.code for validation in result.validations} == {
        "OCA_MIXED_CONTRACT",
        "OCA_QUANTITY_MISMATCH",
    }


def test_add_to_last_remainder_policy_enlarges_the_last_full_pair() -> None:
    result = build_exit_plan(
        complete_snapshot(quantity=Decimal("5")),
        canonical_request(remainder_policy=RemainderPolicy.ADD_TO_LAST),
    )

    assert result.status is PlanStatus.VALID
    assert [pair.quantity for pair in result.pairs] == [2, 3]
    assert [pair.target_percentage for pair in result.pairs] == [
        Decimal("20"),
        Decimal("40"),
    ]
    assert result.planned_quantity == 5


def test_add_to_last_blocks_when_no_full_tranche_exists() -> None:
    result = build_exit_plan(
        complete_snapshot(quantity=Decimal("1")),
        canonical_request(remainder_policy=RemainderPolicy.ADD_TO_LAST),
    )

    assert result.status is PlanStatus.BLOCKED
    assert [validation.code for validation in result.validations] == [
        "REMAINDER_POLICY_INVALID"
    ]
    assert result.pairs == ()


def test_zero_tranche_size_is_a_blocker_not_an_exception() -> None:
    request = replace(canonical_request(), tranche_size=0)

    result = build_exit_plan(complete_snapshot(), request)

    assert result.status is PlanStatus.BLOCKED
    assert [validation.code for validation in result.validations] == [
        "TRANCHE_SIZE_INVALID"
    ]


def test_insufficient_target_rungs_blocks_the_plan() -> None:
    request = replace(
        canonical_request(),
        target_percentages=(Decimal("20"), Decimal("40")),
    )

    result = build_exit_plan(complete_snapshot(), request)

    assert result.status is PlanStatus.BLOCKED
    assert [validation.code for validation in result.validations] == [
        "TARGET_RUNGS_INSUFFICIENT"
    ]


@pytest.mark.parametrize(
    ("remaining", "expected_code"),
    [
        (Decimal("10"), "POSITION_FULLY_ALLOCATED"),
        (Decimal("11"), "ALLOCATION_EXCEEDS_POSITION"),
    ],
)
def test_existing_closing_exposure_can_never_leave_a_zero_or_negative_plan(
    remaining: Decimal, expected_code: str
) -> None:
    order = WorkingOrder(
        1,
        8,
        101,
        ContractKey("DU1234567", 917864414),
        "SELL",
        "LMT",
        remaining,
        "Submitted",
    )

    result = build_exit_plan(
        complete_snapshot(working_orders=(order,)), canonical_request()
    )

    assert result.status is PlanStatus.BLOCKED
    assert expected_code in {validation.code for validation in result.validations}
    assert result.planned_quantity == 0


def test_invalid_remaining_quantity_or_status_blocks_allocation() -> None:
    order = WorkingOrder(
        1,
        8,
        101,
        ContractKey("DU1234567", 917864414),
        "SELL",
        "LMT",
        Decimal("1.5"),
        "Inactive",
    )

    result = build_exit_plan(
        complete_snapshot(working_orders=(order,)), canonical_request()
    )

    assert result.status is PlanStatus.BLOCKED
    assert {validation.code for validation in result.validations} == {
        "ORDER_REMAINING_INVALID",
        "ORDER_STATUS_UNSUPPORTED",
    }


def test_prices_round_up_using_the_active_market_rule_band() -> None:
    snapshot = complete_snapshot(quantity=Decimal("1"))
    snapshot = replace(
        snapshot,
        position=replace(
            snapshot.position,
            raw_average_cost=Decimal("251"),
            unit_basis=Decimal("2.51"),
        ),
        market_rule=MarketRule(
            exchange="SMART",
            bands=(
                PriceBand(Decimal("0"), Decimal("0.05")),
                PriceBand(Decimal("3"), Decimal("0.10")),
            ),
        ),
    )

    result = build_exit_plan(snapshot, canonical_request())

    assert result.status is PlanStatus.VALID
    assert result.pairs[0].target.raw_price == Decimal("3.012")
    assert result.pairs[0].target.rounded_price == Decimal("3.10")
    assert result.pairs[0].stop.raw_price == Decimal("2.008")
    assert result.pairs[0].stop.rounded_price == Decimal("2.05")


@pytest.mark.parametrize(
    ("rule", "expected_code"),
    [
        (MarketRule("SMART", ()), "MARKET_RULE_EMPTY"),
        (
            MarketRule("SMART", (PriceBand(Decimal("0"), Decimal("0")),)),
            "MARKET_RULE_INVALID",
        ),
        (
            MarketRule("CBOE", (PriceBand(Decimal("0"), Decimal("0.05")),)),
            "MARKET_RULE_EXCHANGE_MISMATCH",
        ),
    ],
)
def test_invalid_or_ambiguous_market_rules_block_before_rounding(
    rule: MarketRule, expected_code: str
) -> None:
    snapshot = replace(complete_snapshot(), market_rule=rule)

    result = build_exit_plan(snapshot, canonical_request())

    assert result.status is PlanStatus.BLOCKED
    assert expected_code in {validation.code for validation in result.validations}
    assert result.pairs == ()


def test_inconsistent_basis_or_crossed_stale_quote_blocks() -> None:
    snapshot = complete_snapshot()
    snapshot = replace(
        snapshot,
        position=replace(snapshot.position, unit_basis=Decimal("1.01")),
        quote=replace(
            snapshot.quote,
            bid=Decimal("1.10"),
            ask=Decimal("1.00"),
            fresh=False,
        ),
    )

    result = build_exit_plan(snapshot, canonical_request())

    assert result.status is PlanStatus.BLOCKED
    assert {validation.code for validation in result.validations} == {
        "BASIS_MISMATCH",
        "QUOTE_CROSSED",
        "QUOTE_STALE",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expiry", ""),
        ("strike", Decimal("0")),
        ("right", "X"),
        ("multiplier", Decimal("0")),
        ("currency", ""),
        ("trading_class", ""),
        ("exchange", ""),
        ("local_symbol", ""),
    ],
)
def test_incomplete_contract_identity_blocks(field: str, value: str | Decimal) -> None:
    snapshot = complete_snapshot()
    snapshot = replace(
        snapshot,
        contract=replace(snapshot.contract, **{field: value}),
    )

    result = build_exit_plan(snapshot, canonical_request())

    assert result.status is PlanStatus.BLOCKED
    assert "CONTRACT_INVALID" in {validation.code for validation in result.validations}


def test_selected_contract_key_must_match_position_and_contract_details() -> None:
    snapshot = complete_snapshot()
    snapshot = replace(
        snapshot,
        contract=replace(snapshot.contract, con_id=999),
        position=replace(
            snapshot.position,
            key=ContractKey("DU7654321", 888),
        ),
    )

    result = build_exit_plan(snapshot, canonical_request())

    assert result.status is PlanStatus.BLOCKED
    assert {validation.code for validation in result.validations} == {
        "CONTRACT_ID_MISMATCH",
        "POSITION_ID_MISMATCH",
    }


def test_invalid_price_policy_inputs_are_aggregated_as_blockers() -> None:
    request = replace(
        canonical_request(),
        target_percentages=tuple(
            Decimal(value) for value in ("20", "20", "60", "80", "100")
        ),
        stop_loss_percentage=Decimal("100"),
        tif="IOC",
        trigger_method=TriggerMethod.DEFAULT,
    )

    result = build_exit_plan(complete_snapshot(), request)

    assert result.status is PlanStatus.BLOCKED
    assert {validation.code for validation in result.validations} == {
        "TARGET_PERCENTAGES_INVALID",
        "STOP_LOSS_INVALID",
        "TIF_UNSUPPORTED",
        "TRIGGER_METHOD_UNSUPPORTED",
    }


def test_missing_nonpositive_or_unknown_quote_data_blocks() -> None:
    snapshot = complete_snapshot()
    snapshot = replace(
        snapshot,
        quote=replace(
            snapshot.quote,
            bid=Decimal("0"),
            ask=None,
            market_data_type="UNKNOWN",
        ),
    )

    result = build_exit_plan(snapshot, canonical_request())

    assert result.status is PlanStatus.BLOCKED
    assert {validation.code for validation in result.validations} == {
        "QUOTE_MISSING",
        "QUOTE_NONPOSITIVE",
        "QUOTE_TYPE_UNSUPPORTED",
    }


def test_nonpositive_cost_basis_blocks_the_plan() -> None:
    snapshot = complete_snapshot()
    snapshot = replace(
        snapshot,
        position=replace(
            snapshot.position,
            raw_average_cost=Decimal("0"),
            unit_basis=Decimal("0"),
        ),
    )

    result = build_exit_plan(snapshot, canonical_request())

    assert result.status is PlanStatus.BLOCKED
    assert [validation.code for validation in result.validations] == ["BASIS_INVALID"]


def test_semantically_equal_decimal_inputs_have_the_same_fingerprint() -> None:
    first_snapshot = complete_snapshot()
    second_snapshot = replace(
        first_snapshot,
        position=replace(
            first_snapshot.position,
            quantity=Decimal("10.0"),
            raw_average_cost=Decimal("100.0"),
            unit_basis=Decimal("1.0"),
        ),
        market_rule=MarketRule("SMART", (PriceBand(Decimal("0.0"), Decimal("0.050")),)),
    )
    first_request = canonical_request()
    second_request = replace(
        first_request,
        target_percentages=tuple(
            Decimal(value) for value in ("20.0", "40.0", "60.0", "80.0", "100.0")
        ),
        stop_loss_percentage=Decimal("20.0"),
    )

    first = build_exit_plan(first_snapshot, first_request)
    second = build_exit_plan(second_snapshot, second_request)

    assert first.status is second.status is PlanStatus.VALID
    assert first.fingerprint == second.fingerprint
    assert first.pairs == second.pairs


@given(
    quantity=st.integers(min_value=1, max_value=100),
    tranche_size=st.integers(min_value=1, max_value=100),
    remainder_policy=st.sampled_from(tuple(RemainderPolicy)),
)
def test_every_valid_plan_preserves_quantity_and_pair_invariants(
    quantity: int,
    tranche_size: int,
    remainder_policy: RemainderPolicy,
) -> None:
    tranche_size = min(tranche_size, quantity)
    snapshot = complete_snapshot(quantity=Decimal(quantity))
    request = PlanRequest(
        tranche_size=tranche_size,
        target_percentages=tuple(Decimal(index * 10) for index in range(1, 101)),
        stop_loss_percentage=Decimal("20"),
        remainder_policy=remainder_policy,
        tif="GTC",
        trigger_method=TriggerMethod.DOUBLE_BID_ASK,
    )

    result = build_exit_plan(snapshot, request)

    assert result.status is PlanStatus.VALID
    assert 0 < result.planned_quantity <= quantity
    assert sum(pair.quantity for pair in result.pairs) == result.planned_quantity
    assert len({pair.target.logical_oca_group for pair in result.pairs}) == len(
        result.pairs
    )
    assert all(pair.target.quantity == pair.stop.quantity for pair in result.pairs)
    assert all(
        pair.target.action == pair.stop.action == "SELL" for pair in result.pairs
    )
    assert all(
        pair.target.account == pair.stop.account == "DU1234567"
        and pair.target.con_id == pair.stop.con_id == 917864414
        for pair in result.pairs
    )
    assert all(
        pair.target.rounded_price % Decimal("0.05") == 0
        and pair.stop.rounded_price % Decimal("0.05") == 0
        for pair in result.pairs
    )
    assert build_exit_plan(snapshot, request).fingerprint == result.fingerprint


def test_unrecognizable_existing_oca_shape_blocks_allocation() -> None:
    order = WorkingOrder(
        1,
        8,
        101,
        ContractKey("DU1234567", 917864414),
        "SELL",
        "LMT",
        Decimal("2"),
        "Submitted",
        "incomplete-pair",
    )

    result = build_exit_plan(
        complete_snapshot(working_orders=(order,)), canonical_request()
    )

    assert result.status is PlanStatus.BLOCKED
    assert "OCA_SHAPE_INVALID" in {validation.code for validation in result.validations}


def test_order_callback_sequence_and_unrelated_orders_do_not_change_fingerprint() -> (
    None
):
    selected = ContractKey("DU1234567", 917864414)
    target = WorkingOrder(
        11,
        8,
        101,
        selected,
        "SELL",
        "LMT",
        Decimal("2"),
        "Submitted",
        "existing-pair",
    )
    stop = replace(target, perm_id=12, order_id=102, order_type="STP")
    unrelated = replace(
        target,
        perm_id=13,
        order_id=103,
        key=ContractKey("DU1234567", 999),
        oca_group=None,
    )

    first = build_exit_plan(
        complete_snapshot(working_orders=(target, stop)), canonical_request()
    )
    second = build_exit_plan(
        complete_snapshot(working_orders=(unrelated, stop, target)),
        canonical_request(),
    )

    assert first.status is second.status is PlanStatus.VALID
    assert first.fingerprint == second.fingerprint
    assert first.pairs == second.pairs


def test_duplicate_permanent_order_identity_blocks_allocation() -> None:
    first = WorkingOrder(
        11,
        8,
        101,
        ContractKey("DU1234567", 917864414),
        "SELL",
        "LMT",
        Decimal("1"),
        "Submitted",
    )
    duplicate = replace(first, order_id=102)

    result = build_exit_plan(
        complete_snapshot(working_orders=(first, duplicate)),
        canonical_request(),
    )

    assert result.status is PlanStatus.BLOCKED
    assert "DUPLICATE_ORDER_IDENTITY" in {
        validation.code for validation in result.validations
    }


def test_nonfinite_order_quantity_blocks_instead_of_raising() -> None:
    order = WorkingOrder(
        11,
        8,
        101,
        ContractKey("DU1234567", 917864414),
        "SELL",
        "LMT",
        Decimal("NaN"),
        "Submitted",
    )

    result = build_exit_plan(
        complete_snapshot(working_orders=(order,)), canonical_request()
    )

    assert result.status is PlanStatus.BLOCKED
    assert "ORDER_REMAINING_INVALID" in {
        validation.code for validation in result.validations
    }


def test_verified_contract_identity_is_part_of_the_fingerprint() -> None:
    first_snapshot = complete_snapshot()
    second_snapshot = replace(
        first_snapshot,
        contract=replace(
            first_snapshot.contract,
            trading_class="SPX",
            local_symbol="SPX   260916C07605000",
        ),
    )

    first = build_exit_plan(first_snapshot, canonical_request())
    second = build_exit_plan(second_snapshot, canonical_request())

    assert first.status is second.status is PlanStatus.VALID
    assert first.fingerprint != second.fingerprint


def test_unknown_action_on_selected_contract_blocks_allocation() -> None:
    order = WorkingOrder(
        11,
        8,
        101,
        ContractKey("DU1234567", 917864414),
        "",
        "LMT",
        Decimal("1"),
        "Submitted",
    )

    result = build_exit_plan(
        complete_snapshot(working_orders=(order,)), canonical_request()
    )

    assert result.status is PlanStatus.BLOCKED
    assert "ORDER_ACTION_UNSUPPORTED" in {
        validation.code for validation in result.validations
    }


def test_selected_account_and_contract_id_must_be_explicit() -> None:
    snapshot = complete_snapshot()
    invalid_key = ContractKey("", 0)
    snapshot = replace(
        snapshot,
        selected=invalid_key,
        contract=replace(snapshot.contract, con_id=0),
        position=replace(snapshot.position, key=invalid_key),
    )

    result = build_exit_plan(snapshot, canonical_request())

    assert result.status is PlanStatus.BLOCKED
    assert "SELECTED_KEY_INVALID" in {
        validation.code for validation in result.validations
    }


def test_missing_connection_epoch_blocks_the_snapshot() -> None:
    snapshot = replace(complete_snapshot(), connection_epoch=0)

    result = build_exit_plan(snapshot, canonical_request())

    assert result.status is PlanStatus.BLOCKED
    assert "CONNECTION_EPOCH_INVALID" in {
        validation.code for validation in result.validations
    }


@given(
    basis_cents=st.integers(min_value=1, max_value=10_000),
    target_percentage=st.integers(min_value=1, max_value=500),
    stop_percentage=st.integers(min_value=1, max_value=99),
    increment=st.sampled_from((Decimal("0.01"), Decimal("0.05"), Decimal("0.10"))),
)
def test_sell_prices_never_round_below_the_requested_price(
    basis_cents: int,
    target_percentage: int,
    stop_percentage: int,
    increment: Decimal,
) -> None:
    basis = Decimal(basis_cents) / Decimal("100")
    snapshot = complete_snapshot(quantity=Decimal("1"))
    snapshot = replace(
        snapshot,
        position=replace(
            snapshot.position,
            raw_average_cost=basis * Decimal("100"),
            unit_basis=basis,
        ),
        market_rule=MarketRule("SMART", (PriceBand(Decimal("0"), increment),)),
    )
    request = replace(
        canonical_request(),
        target_percentages=(Decimal(target_percentage),),
        stop_loss_percentage=Decimal(stop_percentage),
    )

    result = build_exit_plan(snapshot, request)

    assert result.status is PlanStatus.VALID
    pair = result.pairs[0]
    assert pair.target.rounded_price >= pair.target.raw_price
    assert pair.stop.rounded_price >= pair.stop.raw_price
    assert pair.target.rounded_price % increment == 0
    assert pair.stop.rounded_price % increment == 0
