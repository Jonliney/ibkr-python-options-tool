"""Evidence-based presentation of app-owned market exits, independent of the UI."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .domain import BrokerSnapshot, WorkingOrder
from .execution import JournalEntry


@dataclass(frozen=True, slots=True)
class MarketOutcome:
    status: str
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    average_price: Decimal | None = None
    realized_pnl: Decimal | None = None
    order: WorkingOrder | None = None


def classify_market(
    entry: JournalEntry, snapshot: BrokerSnapshot | None
) -> MarketOutcome:
    quantity = Decimal(entry.market_quantity)
    unknown = MarketOutcome("UNKNOWN", quantity)
    if (
        len(entry.perm_ids) != 1
        or len(entry.order_ids) != 1
        or min(*entry.perm_ids, *entry.order_ids) <= 0
    ):
        return unknown
    filled, proceeds, pnl = Decimal("0"), Decimal("0"), Decimal("0")
    pnl_complete = True
    seen = set()
    try:
        for fill in entry.fills:
            size, price = Decimal(fill.quantity), Decimal(fill.price)
            realized = (
                Decimal(fill.realized_pnl) if fill.realized_pnl is not None else None
            )
            if (
                not fill.exec_id
                or fill.exec_id in seen
                or fill.perm_id != entry.perm_ids[0]
                or fill.side.upper() not in {"SLD", "SELL"}
                or not size.is_finite()
                or size <= 0
                or not price.is_finite()
                or price <= 0
                or (realized is not None and not realized.is_finite())
            ):
                return unknown
            seen.add(fill.exec_id)
            filled += size
            proceeds += size * price
            if realized is None or fill.currency != "USD":
                pnl_complete = False
            else:
                pnl += realized
    except (InvalidOperation, ValueError):
        return unknown
    if quantity > 0 and filled > quantity:
        return unknown
    coherent = (
        snapshot is not None
        and snapshot.connected
        and snapshot.complete
        and snapshot.fresh
        and snapshot.paper_account_verified
        and not snapshot.errors
        and snapshot.executions_complete
        and snapshot.completed_orders_complete
        and snapshot.selected.account == entry.account
        and snapshot.selected.con_id == entry.con_id
    )
    working = (
        [
            o
            for o in snapshot.working_orders
            if o.key.account == entry.account
            and o.key.con_id == entry.con_id
            and o.perm_id == entry.perm_ids[0]
        ]
        if snapshot is not None
        else []
    )
    terminal = (
        [
            o
            for o in snapshot.completed_orders
            if o.account == entry.account
            and o.con_id == entry.con_id
            and o.perm_id == entry.perm_ids[0]
        ]
        if coherent and snapshot is not None
        else []
    )
    order = working[0] if len(working) == 1 else None
    if order is not None and (
        order.order_id != entry.order_ids[0]
        or order.action != "SELL"
        or order.order_type != "MKT"
        or order.oca_group
        or order.parent_id
        or order.remaining <= 0
    ):
        return unknown
    if not quantity:
        quantity = filled + (order.remaining if order is not None else 0)
    result = MarketOutcome(
        "UNKNOWN",
        quantity,
        filled,
        proceeds / filled if filled else None,
        pnl if filled and pnl_complete else None,
        order,
    )
    if len(working) > 1 or len(terminal) > 1 or (working and terminal):
        return result
    terminal_status = ""
    if terminal:
        item = terminal[0]
        if (
            item.order_id != entry.order_ids[0]
            or item.action != "SELL"
            or item.order_type != "MKT"
            or item.oca_group
        ):
            return result
        if (
            item.status
            and item.completed_status
            in {"Cancelled", "ApiCancelled", "Filled", "Inactive"}
            and item.status != item.completed_status
        ):
            return result
        terminal_status = item.status or item.completed_status
    status = "UNKNOWN"
    if order is not None:
        if (
            coherent
            and order.status in {"Submitted", "PreSubmitted"}
            and entry.state == "SUBMITTED"
            and (not entry.market_quantity or filled < entry.market_quantity)
        ):
            status = "PARTIAL" if filled else "WORKING"
    elif filled > 0 and (
        (entry.market_quantity > 0 and filled == entry.market_quantity)
        or terminal_status == "Filled"
    ):
        status = "FILLED"
    elif entry.state == "CANCELLED_CONFIRMED" or terminal_status in {
        "Cancelled",
        "ApiCancelled",
    }:
        status = "CANCELLED"
    return MarketOutcome(
        status,
        result.quantity,
        filled,
        result.average_price,
        result.realized_pnl,
        order,
    )
