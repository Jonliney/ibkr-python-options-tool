from __future__ import annotations

import hashlib
import json
from decimal import ROUND_CEILING, Decimal

from .model import (
    BrokerSnapshot,
    ExitPair,
    LayerRequest,
    OrderIntent,
    PlanRequest,
    PlanResult,
    PlanStatus,
    PriceBand,
    ReferencePricePreview,
    RemainderPolicy,
    Validation,
    WorkingOrder,
)


def build_exit_plan(snapshot: BrokerSnapshot, request: PlanRequest) -> PlanResult:
    """Build a deterministic preview; this module has no broker side effects."""

    state_validations = (
        *_validate_snapshot_state(snapshot, request.paper_execution_mode),
        *_validate_contract_position(snapshot),
        *_validate_basis_and_quote(snapshot),
        *_validate_market_rule(snapshot),
    )
    if state_validations:
        return _blocked(state_validations)
    allocated, allocation_validations = _allocated_quantity(snapshot)
    position_quantity = _positive_whole(snapshot.position.quantity) or 0
    available = position_quantity - allocated
    validations = (
        *allocation_validations,
        *_validate_request(request, available, snapshot.market_rule.bands),
    )
    if validations:
        return _blocked(validations)

    pair_quantities = (
        tuple(layer.quantity for layer in request.layers)
        if request.layers
        else _pair_quantities(
            available,
            request.tranche_size,
            request.remainder_policy,
            layer_limit=len(request.target_percentages),
        )
    )
    fingerprint = _fingerprint(snapshot, request, allocated, pair_quantities)
    pairs: list[ExitPair] = []

    for index, quantity in enumerate(pair_quantities):
        if request.layers:
            layer = request.layers[index]
            percentage = layer.target_percentage or _percentage_from_basis(
                layer.target_price, snapshot.position.unit_basis
            )
            target_raw = layer.target_price
            target_rounded = layer.target_price
            stop_raw = layer.stop_price
            stop_rounded = layer.stop_price
            tif = layer.tif
        else:
            percentage = request.target_percentages[index]
            target_raw = snapshot.position.unit_basis * (
                Decimal("1") + percentage / Decimal("100")
            )
            target_rounded = _round_up(target_raw, snapshot.market_rule.bands)
            stop_raw = snapshot.position.unit_basis * (
                Decimal("1") - request.stop_loss_percentage / Decimal("100")
            )
            stop_rounded = _round_up(stop_raw, snapshot.market_rule.bands)
            tif = request.tif
        group = f"{fingerprint[:12]}/tranche-{index + 1}"
        target = OrderIntent(
            account=snapshot.selected.account,
            con_id=snapshot.selected.con_id,
            action="SELL",
            order_type="LMT",
            quantity=quantity,
            raw_price=target_raw,
            rounded_price=target_rounded,
            tif=tif,
            logical_oca_group=group,
            oca_type=2,
        )
        stop = OrderIntent(
            account=snapshot.selected.account,
            con_id=snapshot.selected.con_id,
            action="SELL",
            order_type="STP",
            quantity=quantity,
            raw_price=stop_raw,
            rounded_price=stop_rounded,
            tif=tif,
            logical_oca_group=group,
            oca_type=2,
        )
        pairs.append(
            ExitPair(
                index=index + 1,
                target_percentage=percentage,
                quantity=quantity,
                target=target,
                stop=stop,
                runner=request.layers[index].runner if request.layers else False,
            )
        )

    return PlanResult(
        status=PlanStatus.VALID,
        fingerprint=fingerprint,
        allocated_quantity=allocated,
        available_quantity=available,
        planned_quantity=sum(pair_quantities),
        pairs=tuple(pairs),
        validations=(),
    )


def preview_reference_prices(
    reference_price: Decimal,
    target_percentage: Decimal,
    stop_loss_percentage: Decimal,
    bands: tuple[PriceBand, ...],
) -> ReferencePricePreview:
    """Calculate tick-rounded illustrative target and stop prices from a reference."""

    if (
        not reference_price.is_finite()
        or reference_price <= 0
        or not target_percentage.is_finite()
        or target_percentage <= 0
        or not stop_loss_percentage.is_finite()
        or stop_loss_percentage <= 0
        or stop_loss_percentage > 100
        or not bands
    ):
        raise ValueError("reference-price inputs must be positive and complete")
    target_raw = reference_price * (Decimal("1") + target_percentage / Decimal("100"))
    stop_raw = reference_price * (Decimal("1") - stop_loss_percentage / Decimal("100"))
    return ReferencePricePreview(
        reference_price=reference_price,
        target_percentage=target_percentage,
        target_price=_round_up(target_raw, bands),
        stop_loss_percentage=stop_loss_percentage,
        stop_price=_round_up(stop_raw, bands),
    )


def round_up_price(value: Decimal, bands: tuple[PriceBand, ...]) -> Decimal:
    """Round a verified positive price upward to its applicable IBKR increment."""
    if not value.is_finite() or value <= 0 or not bands:
        raise ValueError("price and market-rule bands must be positive and complete")
    return _round_up(value, bands)


def _validate_snapshot_state(
    snapshot: BrokerSnapshot,
    paper_execution_mode: bool,
) -> tuple[Validation, ...]:
    failures: list[Validation] = []

    def require(condition: bool, code: str, message: str) -> None:
        if not condition:
            failures.append(Validation(code, message, True))

    require(snapshot.connected, "CONNECTION_REQUIRED", "TWS is disconnected")
    if paper_execution_mode:
        require(
            snapshot.api_read_only_observed and not snapshot.read_only_api,
            "PAPER_EXECUTION_API_REQUIRED",
            "TWS API must explicitly report non-read-only mode for paper execution",
        )
    else:
        require(
            snapshot.read_only_api,
            "READ_ONLY_REQUIRED",
            "TWS API read-only mode was not verified",
        )
    require(
        snapshot.localhost_only,
        "LOCALHOST_REQUIRED",
        "TWS localhost-only mode was not verified",
    )
    require(
        snapshot.paper_account_verified,
        "PAPER_ACCOUNT_REQUIRED",
        "the selected paper account was not verified",
    )
    require(
        snapshot.complete,
        "SNAPSHOT_INCOMPLETE",
        "the broker snapshot is incomplete",
    )
    require(snapshot.fresh, "SNAPSHOT_STALE", "the broker snapshot is stale")
    require(
        snapshot.connection_epoch > 0,
        "CONNECTION_EPOCH_INVALID",
        "snapshot has no valid connection epoch",
    )
    failures.extend(
        Validation("BROKER_ERROR", message, True) for message in snapshot.errors
    )
    return tuple(failures)


def _validate_contract_position(
    snapshot: BrokerSnapshot,
) -> tuple[Validation, ...]:
    failures: list[Validation] = []
    contract = snapshot.contract
    if not snapshot.selected.account.strip() or snapshot.selected.con_id <= 0:
        failures.append(
            Validation(
                "SELECTED_KEY_INVALID",
                "selected account and conId must be explicit",
                True,
            )
        )
    if contract.con_id != snapshot.selected.con_id:
        failures.append(
            Validation(
                "CONTRACT_ID_MISMATCH",
                "contract details do not match the selected conId",
                True,
            )
        )
    if snapshot.position.key != snapshot.selected:
        failures.append(
            Validation(
                "POSITION_ID_MISMATCH",
                "position account or conId does not match the selection",
                True,
            )
        )
    if contract.sec_type != "OPT":
        failures.append(
            Validation(
                "OPTION_REQUIRED",
                "only verified single-leg option contracts are eligible",
                True,
            )
        )
    quantity = snapshot.position.quantity
    if _positive_whole(quantity) is None:
        failures.append(
            Validation(
                "INTEGRAL_LONG_POSITION_REQUIRED",
                "position quantity must be a positive whole number",
                True,
            )
        )
    if (
        not contract.expiry.isdigit()
        or len(contract.expiry) != 8
        or not contract.strike.is_finite()
        or contract.strike <= 0
        or contract.right not in {"C", "P"}
        or not contract.multiplier.is_finite()
        or contract.multiplier <= 0
        or contract.multiplier != contract.multiplier.to_integral_value()
        or not contract.currency.strip()
        or not contract.trading_class.strip()
        or not contract.exchange.strip()
        or not contract.local_symbol.strip()
    ):
        failures.append(
            Validation(
                "CONTRACT_INVALID",
                "verified option identity contains an invalid or missing field",
                True,
            )
        )
    return tuple(failures)


def _allocated_quantity(
    snapshot: BrokerSnapshot,
) -> tuple[int, tuple[Validation, ...]]:
    """Return contracts reserved by coherent closing orders for this position."""

    failures: list[Validation] = []
    relevant_perm_ids = [
        order.perm_id
        for order in snapshot.working_orders
        if order.key == snapshot.selected and order.action == "SELL"
    ]
    if len(relevant_perm_ids) != len(set(relevant_perm_ids)):
        failures.append(
            Validation(
                "DUPLICATE_ORDER_IDENTITY",
                "a permanent order ID appears more than once",
                True,
            )
        )
    ungrouped = 0
    all_groups: dict[str, list[WorkingOrder]] = {}
    for order in snapshot.working_orders:
        if order.key != snapshot.selected:
            if order.oca_group:
                all_groups.setdefault(order.oca_group, []).append(order)
            continue
        if order.action == "BUY":
            failures.append(
                Validation(
                    "OPENING_ORDER_CONFLICT",
                    (
                        f"order {order.perm_id} may change the position; cancel or "
                        "fill it in TWS, then refresh before planning exits"
                    ),
                    True,
                )
            )
        elif order.action != "SELL":
            failures.append(
                Validation(
                    "ORDER_ACTION_UNSUPPORTED",
                    f"order {order.perm_id} has unknown action {order.action!r}",
                    True,
                )
            )
        if order.action == "SELL":
            remaining = _positive_whole(order.remaining)
            if remaining is None:
                failures.append(
                    Validation(
                        "ORDER_REMAINING_INVALID",
                        (
                            f"order {order.perm_id} has a nonpositive or "
                            "fractional remainder"
                        ),
                        True,
                    )
                )
            if order.status not in {
                "PendingSubmit",
                "PreSubmitted",
                "Submitted",
            }:
                failures.append(
                    Validation(
                        "ORDER_STATUS_UNSUPPORTED",
                        (
                            f"order {order.perm_id} has unsupported status "
                            f"{order.status!r}"
                        ),
                        True,
                    )
                )
        if order.oca_group:
            all_groups.setdefault(order.oca_group, []).append(order)
        elif order.action == "SELL":
            ungrouped += _positive_whole(order.remaining) or 0

    grouped_allocated = 0
    for name, orders in all_groups.items():
        selected_orders = [
            order
            for order in orders
            if order.key == snapshot.selected and order.action == "SELL"
        ]
        if not selected_orders:
            continue
        mixed_contract = any(order.key != snapshot.selected for order in orders)
        if mixed_contract:
            failures.append(
                Validation(
                    "OCA_MIXED_CONTRACT",
                    f"OCA group {name!r} contains another account or contract",
                    True,
                )
            )
        elif (
            len(orders) != 2
            or len(selected_orders) != 2
            or {order.order_type for order in selected_orders} != {"LMT", "STP"}
        ):
            failures.append(
                Validation(
                    "OCA_SHAPE_INVALID",
                    f"OCA group {name!r} is not one target/stop SELL pair",
                    True,
                )
            )
        quantities = [
            remaining
            for order in selected_orders
            if (remaining := _positive_whole(order.remaining)) is not None
        ]
        if not quantities:
            continue
        if len(set(quantities)) != 1:
            failures.append(
                Validation(
                    "OCA_QUANTITY_MISMATCH",
                    f"OCA group {name!r} has inconsistent remaining quantities",
                    True,
                )
            )
        grouped_allocated += max(quantities)

    allocated = ungrouped + grouped_allocated
    position_quantity = _positive_whole(snapshot.position.quantity)
    if position_quantity is None:
        return allocated, tuple(failures)
    if allocated > position_quantity:
        failures.append(
            Validation(
                "ALLOCATION_EXCEEDS_POSITION",
                "existing closing exposure exceeds the current position",
                True,
            )
        )
    elif allocated == position_quantity:
        failures.append(
            Validation(
                "POSITION_FULLY_ALLOCATED",
                "existing closing exposure already covers the whole position",
                True,
            )
        )
    return allocated, tuple(failures)


def _validate_market_rule(
    snapshot: BrokerSnapshot,
) -> tuple[Validation, ...]:
    rule = snapshot.market_rule
    failures: list[Validation] = []
    if rule.exchange != snapshot.contract.exchange:
        failures.append(
            Validation(
                "MARKET_RULE_EXCHANGE_MISMATCH",
                "market rule exchange does not match the verified contract",
                True,
            )
        )
    if not rule.bands:
        failures.append(
            Validation(
                "MARKET_RULE_EMPTY",
                "market rule has no price-increment bands",
                True,
            )
        )
        return tuple(failures)
    edges = [band.low_edge for band in rule.bands]
    if (
        edges[0] != 0
        or edges != sorted(set(edges))
        or any(
            not band.low_edge.is_finite()
            or not band.increment.is_finite()
            or band.low_edge < 0
            or band.increment <= 0
            for band in rule.bands
        )
    ):
        failures.append(
            Validation(
                "MARKET_RULE_INVALID",
                "market-rule bands must be ordered from zero with positive increments",
                True,
            )
        )
    return tuple(failures)


def _validate_basis_and_quote(
    snapshot: BrokerSnapshot,
) -> tuple[Validation, ...]:
    failures: list[Validation] = []
    position = snapshot.position
    contract = snapshot.contract
    basis_valid = (
        position.unit_basis.is_finite()
        and position.unit_basis > 0
        and position.raw_average_cost.is_finite()
        and position.raw_average_cost > 0
        and contract.multiplier.is_finite()
        and contract.multiplier > 0
    )
    if not basis_valid:
        failures.append(
            Validation(
                "BASIS_INVALID",
                "average cost and unit premium must be finite and positive",
                True,
            )
        )
    elif position.unit_basis * contract.multiplier != position.raw_average_cost:
        failures.append(
            Validation(
                "BASIS_MISMATCH",
                "unit premium does not match average cost and contract multiplier",
                True,
            )
        )
    quote = snapshot.quote
    if quote.bid is None or quote.ask is None:
        failures.append(
            Validation(
                "QUOTE_MISSING",
                "both option bid and ask are required",
                True,
            )
        )
    quote_values = (
        value
        for value in (quote.bid, quote.ask, quote.last, quote.close)
        if value is not None
    )
    if any(not value.is_finite() or value <= 0 for value in quote_values):
        failures.append(
            Validation(
                "QUOTE_NONPOSITIVE",
                "available quote prices must be finite and positive",
                True,
            )
        )
    if quote.bid is not None and quote.ask is not None and quote.bid > quote.ask:
        failures.append(
            Validation(
                "QUOTE_CROSSED",
                "option bid is greater than its ask",
                True,
            )
        )
    if not quote.fresh:
        failures.append(Validation("QUOTE_STALE", "option quote is stale", True))
    if quote.market_data_type not in {
        "LIVE",
        "FROZEN",
        "DELAYED",
        "DELAYED_FROZEN",
    }:
        failures.append(
            Validation(
                "QUOTE_TYPE_UNSUPPORTED",
                "market-data type is unknown",
                True,
            )
        )
    return tuple(failures)


def _validate_request(
    request: PlanRequest,
    available: int,
    bands: tuple[PriceBand, ...],
) -> tuple[Validation, ...]:
    if request.layers:
        return _validate_layers(request.layers, available, bands)
    if request.tranche_size <= 0:
        return (
            Validation(
                "TRANCHE_SIZE_INVALID",
                "tranche size must be a positive whole number",
                True,
            ),
        )
    failures: list[Validation] = []
    if (
        request.remainder_policy is RemainderPolicy.ADD_TO_LAST
        and available < request.tranche_size
    ):
        failures.append(
            Validation(
                "REMAINDER_POLICY_INVALID",
                "ADD_TO_LAST requires at least one full tranche",
                True,
            )
        )
    if not request.target_percentages:
        failures.append(
            Validation(
                "TARGET_PERCENTAGES_REQUIRED",
                "provide at least one target percentage to create a bracket layer",
                True,
            )
        )
    if any(
        not percentage.is_finite() or percentage <= 0
        for percentage in request.target_percentages
    ) or any(
        current <= previous
        for previous, current in zip(
            request.target_percentages,
            request.target_percentages[1:],
            strict=False,
        )
    ):
        failures.append(
            Validation(
                "TARGET_PERCENTAGES_INVALID",
                "target percentages must be positive and strictly increasing",
                True,
            )
        )
    if (
        not request.stop_loss_percentage.is_finite()
        or request.stop_loss_percentage <= 0
        or request.stop_loss_percentage >= 100
    ):
        failures.append(
            Validation(
                "STOP_LOSS_INVALID",
                "stop-loss percentage must be greater than zero and below 100",
                True,
            )
        )
    if request.tif not in {"DAY", "GTC"}:
        failures.append(
            Validation(
                "TIF_UNSUPPORTED",
                "only DAY and GTC time-in-force values are supported",
                True,
            )
        )
    return tuple(failures)


def _validate_layers(
    layers: tuple[LayerRequest, ...],
    available: int,
    bands: tuple[PriceBand, ...],
) -> tuple[Validation, ...]:
    failures: list[Validation] = []
    if not layers:
        return (
            Validation(
                "LAYERS_REQUIRED",
                "provide at least one explicitly priced OCA layer",
                True,
            ),
        )
    total = 0
    for index, layer in enumerate(layers, start=1):
        if layer.quantity <= 0:
            failures.append(
                Validation(
                    "LAYER_QUANTITY_INVALID",
                    f"layer {index} quantity must be a positive whole number",
                    True,
                )
            )
        total += layer.quantity
        for label, price in (
            ("target", layer.target_price),
            ("stop", layer.stop_price),
        ):
            if not price.is_finite() or price <= 0:
                failures.append(
                    Validation(
                        "LAYER_PRICE_INVALID",
                        f"layer {index} {label} price must be finite and positive",
                        True,
                    )
                )
            elif _round_up(price, bands) != price:
                failures.append(
                    Validation(
                        "LAYER_PRICE_INCREMENT_INVALID",
                        f"layer {index} {label} price is not on the verified tick",
                        True,
                    )
                )
        if layer.tif not in {"DAY", "GTC"}:
            failures.append(
                Validation(
                    "TIF_UNSUPPORTED",
                    f"layer {index} time-in-force must be DAY or GTC",
                    True,
                )
            )
        if layer.stop_price >= layer.target_price:
            failures.append(
                Validation(
                    "LAYER_PRICE_RELATION_INVALID",
                    f"layer {index} stop price must be below its target price",
                    True,
                )
            )
    if total > available:
        failures.append(
            Validation(
                "LAYER_QUANTITY_EXCEEDS_AVAILABLE",
                "draft layers exceed the verified available quantity",
                True,
            )
        )
    return tuple(failures)


def _blocked(validations: tuple[Validation, ...]) -> PlanResult:
    return PlanResult(
        status=PlanStatus.BLOCKED,
        fingerprint=None,
        allocated_quantity=0,
        available_quantity=0,
        planned_quantity=0,
        pairs=(),
        validations=validations,
    )


def _pair_quantities(
    available: int,
    tranche_size: int,
    remainder_policy: RemainderPolicy,
    *,
    layer_limit: int | None = None,
) -> tuple[int, ...]:
    full, remainder = divmod(available, tranche_size)
    full_limit = full if layer_limit is None else min(full, layer_limit)
    quantities = [tranche_size] * full_limit
    has_room_for_remainder = layer_limit is None or len(quantities) < layer_limit
    if remainder and has_room_for_remainder:
        if remainder_policy is RemainderPolicy.ADD_TO_LAST and quantities:
            quantities[-1] += remainder
        else:
            quantities.append(remainder)
    return tuple(quantities)


def _round_up(value: Decimal, bands: tuple[PriceBand, ...]) -> Decimal:
    candidate = value
    for _ in range(len(bands) + 1):
        band = max(
            (band for band in bands if band.low_edge <= candidate),
            key=lambda item: item.low_edge,
        )
        rounded = (value / band.increment).to_integral_value(
            rounding=ROUND_CEILING
        ) * band.increment
        rounded_band = max(
            (item for item in bands if item.low_edge <= rounded),
            key=lambda item: item.low_edge,
        )
        if rounded_band == band:
            return rounded
        candidate = rounded
    raise ValueError("market-rule rounding did not converge")


def _percentage_from_basis(price: Decimal, basis: Decimal) -> Decimal:
    return ((price / basis) - Decimal("1")) * Decimal("100")


def _fingerprint(
    snapshot: BrokerSnapshot,
    request: PlanRequest,
    allocated: int,
    pair_quantities: tuple[int, ...],
) -> str:
    payload = {
        "account": snapshot.selected.account,
        "con_id": snapshot.selected.con_id,
        "contract": {
            "sec_type": snapshot.contract.sec_type,
            "expiry": snapshot.contract.expiry,
            "strike": _decimal_text(snapshot.contract.strike),
            "right": snapshot.contract.right,
            "multiplier": _decimal_text(snapshot.contract.multiplier),
            "currency": snapshot.contract.currency,
            "trading_class": snapshot.contract.trading_class,
            "exchange": snapshot.contract.exchange,
            "local_symbol": snapshot.contract.local_symbol,
        },
        "position_quantity": _decimal_text(snapshot.position.quantity),
        "raw_average_cost": _decimal_text(snapshot.position.raw_average_cost),
        "unit_basis": _decimal_text(snapshot.position.unit_basis),
        "allocated_quantity": allocated,
        "tranche_size": request.tranche_size,
        "target_percentages": [
            _decimal_text(value) for value in request.target_percentages
        ],
        "stop_loss_percentage": _decimal_text(request.stop_loss_percentage),
        "remainder_policy": request.remainder_policy.value,
        "tif": request.tif,
        "paper_execution_mode": request.paper_execution_mode,
        "layers": [
            {
                "quantity": layer.quantity,
                "target_price": _decimal_text(layer.target_price),
                "stop_price": _decimal_text(layer.stop_price),
                "tif": layer.tif,
                "target_percentage": (
                    None
                    if layer.target_percentage is None
                    else _decimal_text(layer.target_percentage)
                ),
                "runner": layer.runner,
            }
            for layer in request.layers
        ],
        "pair_quantities": pair_quantities,
        "working_orders": [
            {
                "perm_id": order.perm_id,
                "account": order.key.account,
                "con_id": order.key.con_id,
                "action": order.action,
                "order_type": order.order_type,
                "remaining": _decimal_text(order.remaining),
                "status": order.status,
                "oca_group": order.oca_group,
                "parent_id": order.parent_id,
            }
            for order in sorted(
                (
                    order
                    for order in snapshot.working_orders
                    if order.key == snapshot.selected and order.action == "SELL"
                ),
                key=lambda order: (
                    order.perm_id,
                    order.client_id,
                    order.order_id,
                ),
            )
        ],
        "market_rule": [
            (
                _decimal_text(band.low_edge),
                _decimal_text(band.increment),
            )
            for band in snapshot.market_rule.bands
        ],
        "market_rule_exchange": snapshot.market_rule.exchange,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _positive_whole(value: Decimal) -> int | None:
    if not value.is_finite() or value <= 0:
        return None
    if value != value.to_integral_value():
        return None
    return int(value)
