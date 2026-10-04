"""Pure, deterministic whole-position exit scenarios.

Only verified fills contribute realized P&L. An uncovered or unresolved held
contract makes both exit scenarios incomplete; partial totals remain available
for an explicitly labelled breakdown, never as the headline outcome.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ExitScenario:
    quantity: Decimal
    target_pnl: Decimal
    stop_pnl: Decimal


@dataclass(frozen=True, slots=True)
class PositionOutcome:
    held_quantity: Decimal
    expected_gain: Decimal | None
    max_loss: Decimal | None
    covered_gain: Decimal
    covered_loss: Decimal
    realized_pnl: Decimal
    covered_quantity: Decimal
    uncovered_quantity: Decimal


def project_position_outcome(
    *,
    held_quantity: Decimal,
    realized_pnl: Decimal,
    exits: tuple[ExitScenario, ...],
    unresolved: bool = False,
) -> PositionOutcome:
    covered_quantity = sum((item.quantity for item in exits), Decimal("0"))
    covered_gain = realized_pnl + sum((item.target_pnl for item in exits), Decimal("0"))
    covered_loss = sum((item.stop_pnl for item in exits), Decimal("0"))
    uncovered_quantity = max(held_quantity - covered_quantity, Decimal("0"))
    complete = (
        not unresolved
        and held_quantity >= 0
        and covered_quantity == held_quantity
        and all(item.quantity > 0 for item in exits)
    )
    return PositionOutcome(
        held_quantity=held_quantity,
        expected_gain=covered_gain if complete else None,
        max_loss=covered_loss if complete else None,
        covered_gain=covered_gain,
        covered_loss=covered_loss,
        realized_pnl=realized_pnl,
        covered_quantity=covered_quantity,
        uncovered_quantity=uncovered_quantity,
    )
