"""Pure terminal-state evidence for one journal-owned trailing exit."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .domain import BrokerSnapshot
from .execution import JournalEntry


@dataclass(frozen=True, slots=True)
class TrailingOutcome:
    status: str
    filled_quantity: Decimal = Decimal("0")


def classify_trailing(entry: JournalEntry, snapshot: BrokerSnapshot) -> TrailingOutcome:
    """Absence alone never proves cancellation; terminal evidence retains fills."""
    if (
        not snapshot.connected
        or not snapshot.complete
        or not snapshot.fresh
        or snapshot.errors
        or snapshot.position.key != snapshot.selected
        or snapshot.contract.con_id != entry.con_id
        or snapshot.contract.sec_type != "OPT"
        or not snapshot.paper_account_verified
        or snapshot.selected.account != entry.account
        or snapshot.selected.con_id != entry.con_id
        or len(entry.perm_ids) != 1
        or len(entry.order_ids) != 1
        or entry.perm_ids[0] <= 0
        or entry.trailing_quantity <= 0
        or not snapshot.executions_complete
        or not snapshot.completed_orders_complete
    ):
        return TrailingOutcome("UNKNOWN")
    perm_id = entry.perm_ids[0]
    working = [
        order
        for order in snapshot.working_orders
        if order.key == snapshot.selected and order.perm_id == perm_id
    ]
    terminal = [
        order
        for order in snapshot.completed_orders
        if order.account == entry.account
        and order.con_id == entry.con_id
        and order.perm_id == perm_id
    ]
    if len(working) > 1 or len(terminal) > 1 or (working and terminal):
        return TrailingOutcome("CONFLICT")
    try:
        fills = [fill for fill in entry.fills if fill.perm_id == perm_id]
        if any(
            fill.side.upper() not in {"SLD", "SELL"} or not fill.exec_id
            for fill in fills
        ):
            return TrailingOutcome("CONFLICT")
        quantities = [Decimal(fill.quantity) for fill in fills]
        if any(not value.is_finite() or value <= 0 for value in quantities):
            return TrailingOutcome("CONFLICT")
        filled = sum(quantities, Decimal("0"))
    except InvalidOperation:
        return TrailingOutcome("CONFLICT")
    if filled > entry.trailing_quantity:
        return TrailingOutcome("CONFLICT")
    if working:
        return TrailingOutcome("WORKING", filled)
    if terminal:
        order = terminal[0]
        if (
            order.order_id != entry.order_ids[0]
            or order.action != "SELL"
            or order.order_type not in {"TRAIL", "TRAIL LIMIT"}
            or order.oca_group
        ):
            return TrailingOutcome("CONFLICT", filled)
        # Retain both broker fields. Recognize only exact API status values;
        # descriptive completedStatus strings cannot establish cancellation.
        status = order.status or order.completed_status
        if (
            order.status
            and order.completed_status
            and order.completed_status
            in {"Cancelled", "ApiCancelled", "Filled", "Inactive"}
            and order.status != order.completed_status
        ):
            return TrailingOutcome("CONFLICT", filled)
        if status in {"Cancelled", "ApiCancelled"} and filled < entry.trailing_quantity:
            return TrailingOutcome("CANCELLED", filled)
        if status == "Filled" and filled == entry.trailing_quantity:
            return TrailingOutcome("FILLED", filled)
        return TrailingOutcome("UNKNOWN", filled)
    return TrailingOutcome(
        "FILLED" if filled == entry.trailing_quantity else "UNKNOWN", filled
    )
