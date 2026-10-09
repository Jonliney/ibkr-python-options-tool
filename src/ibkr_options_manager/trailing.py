"""Pure plan for replacing all app-owned exits with one paper trailing exit."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256

from .domain import (
    BrokerSnapshot,
    ContractKey,
    VerifiedOptionContract,
    round_down_price,
    round_up_price,
    supports_outside_rth,
)
from .execution import ExecutionBlocked, MarketExitCandidate


@dataclass(frozen=True, slots=True)
class TrailingRequest:
    trail_value: Decimal
    trail_unit: str  # dollars or percent
    limit_value: Decimal | None = None
    limit_unit: str = "dollars"
    tif: str = "GTC"


@dataclass(frozen=True, slots=True)
class TrailingPlan:
    request: TrailingRequest
    candidates: tuple[MarketExitCandidate, ...]
    quantity: int
    unassigned_quantity: int
    reference_price: Decimal
    initial_stop: Decimal
    limit_offset: Decimal | None
    outside_rth: bool
    fingerprint: str
    contract: VerifiedOptionContract
    selected: ContractKey
    position_basis: Decimal
    position_raw_average_cost: Decimal
    connection_epoch: int
    execution_ids: frozenset[str]


def plan_entire_position(
    snapshot: BrokerSnapshot,
    candidates: tuple[MarketExitCandidate, ...],
    request: TrailingRequest,
) -> TrailingPlan:
    """Plan the entire held position from owned pairs and free contracts."""
    position = snapshot.position.quantity
    if (
        not position.is_finite()
        or position <= 0
        or position != position.to_integral_value()
        or snapshot.position.key != snapshot.selected
        or snapshot.contract.con_id != snapshot.selected.con_id
        or snapshot.contract.sec_type != "OPT"
    ):
        raise ExecutionBlocked("the selected long option position is invalid")
    if snapshot.contract.currency != "USD":
        raise ExecutionBlocked(
            "this dollar-based trailing workflow requires a USD option"
        )
    if not snapshot.executions_complete or not snapshot.completed_orders_complete:
        raise ExecutionBlocked("complete order and fill history is required")
    if request.tif != "GTC":
        raise ExecutionBlocked("trailing order TIF must be GTC")
    if (
        request.trail_unit not in {"dollars", "percent"}
        or not request.trail_value.is_finite()
        or request.trail_value <= 0
        or (request.trail_unit == "percent" and request.trail_value >= 100)
    ):
        raise ExecutionBlocked("enter a positive trail amount below 100%")
    if request.limit_value is not None and (
        request.limit_unit not in {"dollars", "percent"}
        or not request.limit_value.is_finite()
        or request.limit_value <= 0
        or (request.limit_unit == "percent" and request.limit_value >= 100)
    ):
        raise ExecutionBlocked("enter a positive limit offset below 100%")
    if not snapshot.quote.fresh or snapshot.quote.bid is None:
        raise ExecutionBlocked("a fresh option bid is required for a trailing order")
    reference = snapshot.quote.bid
    if not reference.is_finite() or reference <= 0:
        raise ExecutionBlocked("the option bid is invalid")
    selected = {
        id_ for item in candidates for id_ in (item.target_perm_id, item.stop_perm_id)
    }
    if len(selected) != 2 * len(candidates):
        raise ExecutionBlocked("the selected bracket identities are ambiguous")
    matching_orders = tuple(
        order for order in snapshot.working_orders if order.key == snapshot.selected
    )
    if {order.perm_id for order in matching_orders} != selected:
        raise ExecutionBlocked(
            "another working order exists for this option; review it in TWS"
        )
    covered = sum((item.quantity for item in candidates), Decimal("0"))
    if covered > position:
        raise ExecutionBlocked("bracket quantities exceed the position")
    trail = (
        request.trail_value
        if request.trail_unit == "dollars"
        else reference * request.trail_value / Decimal("100")
    )
    raw_stop = reference - trail
    if raw_stop <= 0:
        raise ExecutionBlocked("the trail would put the initial stop at or below zero")
    # Round a SELL trigger conservatively below the bid, using the verified rule.
    bands = snapshot.market_rule.bands
    try:
        if snapshot.market_rule.exchange != snapshot.contract.exchange:
            raise ValueError("market rule exchange does not match contract")
        if (
            request.trail_unit == "dollars"
            and round_up_price(request.trail_value, bands) != request.trail_value
        ):
            raise ExecutionBlocked("the dollar trail is not on a valid price increment")
        initial_stop = round_down_price(raw_stop, bands)
    except (ValueError, TypeError) as error:
        raise ExecutionBlocked("the option market rule is invalid") from error
    if initial_stop <= 0 or initial_stop >= reference:
        raise ExecutionBlocked("the initial trailing stop cannot use a valid tick")
    limit_offset = None
    if request.limit_value is not None:
        raw_offset = (
            request.limit_value
            if request.limit_unit == "dollars"
            else initial_stop * request.limit_value / Decimal("100")
        )
        limit_offset = round_up_price(raw_offset, bands)
        if limit_offset <= 0 or initial_stop - limit_offset <= 0:
            raise ExecutionBlocked("the limit offset leaves no positive limit price")
    identity = (
        snapshot.selected.account,
        snapshot.selected.con_id,
        position,
        reference,
        snapshot.connection_epoch,
        tuple((c.target_perm_id, c.stop_perm_id, c.quantity) for c in candidates),
        request,
    )
    return TrailingPlan(
        request=request,
        candidates=candidates,
        quantity=int(position),
        unassigned_quantity=int(position - covered),
        reference_price=reference,
        initial_stop=initial_stop,
        limit_offset=limit_offset,
        outside_rth=supports_outside_rth(snapshot),
        fingerprint=sha256(repr(identity).encode()).hexdigest(),
        contract=snapshot.contract,
        selected=snapshot.selected,
        position_basis=snapshot.position.unit_basis,
        position_raw_average_cost=snapshot.position.raw_average_cost,
        connection_epoch=snapshot.connection_epoch,
        execution_ids=frozenset(
            execution.exec_id
            for execution in snapshot.executions
            if execution.account == snapshot.selected.account
            and execution.con_id == snapshot.selected.con_id
        ),
    )
