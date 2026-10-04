from dataclasses import replace

from ibkr_options_manager.app.position_observation import VerifiedPositionChanges
from ibkr_options_manager.app.view_model import PortfolioPositionLine


def position(
    con_id: int, quantity: str = "4", *, eligible: bool = True
) -> PortfolioPositionLine:
    return PortfolioPositionLine(
        con_id=con_id,
        local_symbol=f"OPT {con_id}",
        quantity=quantity,
        unit_basis="2.00",
        working_order_count=0,
        eligible=eligible,
        eligibility="Eligible" if eligible else "Unverified",
    )


def test_verified_inventory_sets_a_baseline_then_marks_newly_verified_options() -> None:
    changes = VerifiedPositionChanges()
    first = position(101)
    pending = position(202, eligible=False)

    changes.observe("DU1", (first, pending), selected_con_id=101)
    assert changes.new_ids == frozenset()

    changes.observe(
        "DU1", (first, replace(pending, eligible=True)), selected_con_id=101
    )
    assert changes.new_ids == frozenset({202})
    changes.select(202)
    assert changes.new_ids == frozenset()

    changes.observe("DU1", (first, position(303)), selected_con_id=101)
    assert changes.new_ids == frozenset({303})
    changes.observe("DU2", (first, position(404)), selected_con_id=101)
    assert changes.new_ids == frozenset()


def test_selected_quantity_notice_keeps_original_baseline_until_acknowledged() -> None:
    changes = VerifiedPositionChanges()
    changes.observe("DU1", (position(101),), selected_con_id=101)

    changes.observe(
        "DU1",
        (position(101, "3"),),
        selected_con_id=101,
        track_selected_quantity=True,
    )
    assert changes.selected_change == (101, 4, 3)
    changes.observe(
        "DU1",
        (position(101, "2"),),
        selected_con_id=101,
        track_selected_quantity=True,
    )
    assert changes.selected_change == (101, 4, 2)

    changes.clear_selected_change()
    changes.observe(
        "DU1",
        (position(101, "1"),),
        selected_con_id=101,
        track_selected_quantity=True,
    )
    assert changes.selected_change == (101, 2, 1)


def test_invalid_quantity_and_account_change_cannot_create_a_notice() -> None:
    changes = VerifiedPositionChanges()
    changes.observe("DU1", (position(101),), selected_con_id=101)

    changes.observe(
        "DU1",
        (position(101, "1.5"),),
        selected_con_id=101,
        track_selected_quantity=True,
    )
    assert changes.selected_change is None

    changes.observe(
        "DU2",
        (position(101, "2"),),
        selected_con_id=101,
        track_selected_quantity=True,
    )
    assert changes.selected_change is None
