from __future__ import annotations

from ..domain import ContractKey, WorkingOrder
from .read_only import BrokerCapture


def working_orders_from_capture(
    capture: BrokerCapture,
    *,
    selected: ContractKey | None = None,
) -> tuple[WorkingOrder, ...]:
    """Normalize captured orders into one deterministic domain representation."""
    orders = (
        order
        for order in capture.orders
        if selected is None
        or (order.account == selected.account and order.con_id == selected.con_id)
    )
    return tuple(
        WorkingOrder(
            perm_id=order.perm_id,
            client_id=order.client_id,
            order_id=order.order_id,
            key=ContractKey(order.account, order.con_id),
            action=order.action,
            order_type=order.order_type,
            remaining=order.remaining,
            status=order.status,
            oca_group=order.oca_group,
            parent_id=order.parent_id,
            observed_at=capture.captured_at,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            tif=order.tif,
        )
        for order in sorted(
            orders,
            key=lambda item: (
                item.account,
                item.con_id,
                item.perm_id,
                item.client_id,
                item.order_id,
            ),
        )
    )


__all__ = ["working_orders_from_capture"]
