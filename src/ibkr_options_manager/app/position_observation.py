"""Presentation state derived only from complete, verified position inventories.

This module never reads TWS and never makes a broker snapshot actionable. A
callback hint must first pass through the portfolio coordinator; callers then
publish a complete verified inventory here to update badges and quantity notices.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .view_model import PortfolioPositionLine


class VerifiedPositionChanges:
    """Track account-scoped position changes across verified observations."""

    def __init__(self) -> None:
        self._account: str | None = None
        self._ids: set[int] = set()
        self._quantities: dict[int, int] = {}
        self._new_ids: set[int] = set()
        self._selected_change: tuple[int, int, int] | None = None

    @property
    def new_ids(self) -> frozenset[int]:
        return frozenset(self._new_ids)

    @property
    def selected_change(self) -> tuple[int, int, int] | None:
        return self._selected_change

    def observe(
        self,
        account: str,
        positions: tuple[PortfolioPositionLine, ...],
        *,
        selected_con_id: int | None,
        track_selected_quantity: bool = False,
    ) -> None:
        """Publish a complete verified inventory, retaining the original baseline."""
        same_account = self._account == account
        if not same_account:
            self._new_ids.clear()
            self._selected_change = None

        quantities = _eligible_quantities(positions)
        if (
            track_selected_quantity
            and same_account
            and selected_con_id is not None
            and selected_con_id in self._quantities
            and selected_con_id in quantities
        ):
            baseline = (
                self._selected_change[1]
                if self._selected_change is not None
                and self._selected_change[0] == selected_con_id
                else self._quantities[selected_con_id]
            )
            current = quantities[selected_con_id]
            self._selected_change = (
                (selected_con_id, baseline, current) if current != baseline else None
            )

        ids = {position.con_id for position in positions if position.eligible}
        if same_account:
            self._new_ids.update(ids - self._ids)
        self._new_ids.intersection_update(ids)
        self._ids = ids
        self._quantities = quantities
        self._account = account

    def select(self, con_id: int) -> None:
        """Mark a position as viewed; selection does not change broker evidence."""
        self._new_ids.discard(con_id)

    def clear_selected_change(self) -> None:
        self._selected_change = None


def _eligible_quantities(
    positions: tuple[PortfolioPositionLine, ...],
) -> dict[int, int]:
    quantities: dict[int, int] = {}
    for position in positions:
        if not position.eligible:
            continue
        try:
            quantity = Decimal(position.quantity)
        except InvalidOperation:
            continue
        if (
            quantity.is_finite()
            and quantity > 0
            and quantity == quantity.to_integral_value()
        ):
            quantities[position.con_id] = int(quantity)
    return quantities
