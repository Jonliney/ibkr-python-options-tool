from decimal import Decimal

from ibkr_options_manager.domain.outcome import (
    ExitScenario,
    project_position_outcome,
)


def test_whole_position_projection_adds_realized_and_open_scenarios() -> None:
    outcome = project_position_outcome(
        held_quantity=Decimal("3"),
        realized_pnl=Decimal("125"),
        exits=(
            ExitScenario(Decimal("2"), Decimal("600"), Decimal("-300")),
            ExitScenario(Decimal("1"), Decimal("200"), Decimal("-100")),
        ),
    )

    assert outcome.expected_gain == Decimal("925")
    assert outcome.max_loss == Decimal("-400")
    assert outcome.covered_loss == Decimal("-400")
    assert outcome.covered_quantity == Decimal("3")
    assert outcome.uncovered_quantity == 0


def test_deleted_or_unresolved_exit_never_reports_a_whole_position_total() -> None:
    layer = ExitScenario(Decimal("2"), Decimal("600"), Decimal("-300"))
    uncovered = project_position_outcome(
        held_quantity=Decimal("3"), realized_pnl=Decimal("125"), exits=(layer,)
    )
    unknown = project_position_outcome(
        held_quantity=Decimal("2"),
        realized_pnl=Decimal("125"),
        exits=(layer,),
        unresolved=True,
    )

    assert uncovered.expected_gain is None
    assert uncovered.max_loss is None
    assert uncovered.uncovered_quantity == 1
    assert uncovered.covered_gain == Decimal("725")
    assert unknown.expected_gain is None
    assert unknown.max_loss is None


def test_overallocated_exits_are_incomplete() -> None:
    outcome = project_position_outcome(
        held_quantity=Decimal("1"),
        realized_pnl=Decimal("0"),
        exits=(ExitScenario(Decimal("2"), Decimal("200"), Decimal("-100")),),
    )

    assert outcome.expected_gain is None
    assert outcome.max_loss is None
