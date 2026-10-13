from dataclasses import replace
from decimal import Decimal

import pytest

from ibkr_options_manager.domain import ObservedCompletedOrder
from ibkr_options_manager.execution import ExecutionJournal, JournalEntry, JournalFill
from ibkr_options_manager.trailing_outcome import classify_trailing

# Reuse the broker snapshot fixture with all freshness and history barriers.
from test_execution import _trailing_snapshot as _snapshot


def evidence():
    snapshot = _snapshot()
    entry = JournalEntry(
        fingerprint="trailing-conversion:" + "a" * 64,
        account=snapshot.selected.account,
        con_id=snapshot.selected.con_id,
        state="SUBMITTED",
        order_ids=(701,),
        perm_ids=(801,),
        trailing_quantity=2,
    )
    terminal = ObservedCompletedOrder(
        account=entry.account,
        con_id=entry.con_id,
        perm_id=801,
        order_id=701,
        client_id=17,
        action="SELL",
        order_type="TRAIL",
        oca_group="",
        status="Cancelled",
    )
    return entry, replace(snapshot, completed_orders=(terminal,)), terminal


@pytest.mark.parametrize(
    "change",
    [
        "absent",
        "disconnected",
        "stale",
        "history",
        "executions",
        "wrong_account",
        "wrong_id",
        "inactive",
        "duplicate",
        "working",
    ],
)
def test_cancellation_requires_exact_complete_unambiguous_evidence(change):
    entry, snapshot, terminal = evidence()
    from ibkr_options_manager.domain import WorkingOrder

    scenarios = {
        "absent": replace(snapshot, completed_orders=()),
        "disconnected": replace(snapshot, connected=False),
        "stale": replace(snapshot, fresh=False),
        "history": replace(snapshot, completed_orders_complete=False),
        "executions": replace(snapshot, executions_complete=False),
        "wrong_account": replace(
            snapshot, completed_orders=(replace(terminal, account="DU9999999"),)
        ),
        "wrong_id": replace(
            snapshot, completed_orders=(replace(terminal, order_id=702),)
        ),
        "inactive": replace(
            snapshot, completed_orders=(replace(terminal, status="Inactive"),)
        ),
        "duplicate": replace(snapshot, completed_orders=(terminal, terminal)),
        "working": replace(
            snapshot,
            working_orders=(
                WorkingOrder(
                    801,
                    17,
                    701,
                    snapshot.selected,
                    "SELL",
                    "TRAIL",
                    Decimal("2"),
                    "Submitted",
                ),
            ),
        ),
    }
    assert classify_trailing(entry, scenarios[change]).status in {"UNKNOWN", "CONFLICT"}


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("status", ["Cancelled", "ApiCancelled"])
def test_terminal_cancellation_persists_once_retains_fills_and_survives_restart(
    tmp_path, partial, status
):
    entry, snapshot, terminal = evidence()
    snapshot = replace(snapshot, completed_orders=(replace(terminal, status=status),))
    if partial:
        entry = replace(
            entry,
            fills=(
                JournalFill(
                    exec_id="fill.01",
                    perm_id=801,
                    side="SLD",
                    quantity="1",
                    price="1.00",
                    time="now",
                ),
            ),
        )
    assert classify_trailing(entry, snapshot).status == "CANCELLED"
    assert classify_trailing(entry, snapshot).filled_quantity == int(partial)
    path = tmp_path / "orders.json"
    journal = ExecutionJournal(path)
    journal._write((entry,))
    assert len(journal.record_trailing_cancellations(snapshot)) == 1
    assert journal.record_trailing_cancellations(snapshot) == ()
    restored = ExecutionJournal(path).trailing_entries(
        account=entry.account, con_id=entry.con_id
    )[0]
    assert restored.state == "CANCELLED_CONFIRMED"
    assert restored.fills == entry.fills


@pytest.mark.parametrize(
    ("status", "completed", "expected"),
    [
        ("", "Cancelled", "CANCELLED"),
        ("Submitted", "Cancelled", "CONFLICT"),
        ("", "Cancelled by user", "UNKNOWN"),
        ("Inactive", "", "UNKNOWN"),
    ],
)
def test_completed_status_fields_require_exact_consistent_terminal_evidence(
    status, completed, expected
):
    entry, snapshot, terminal = evidence()
    snapshot = replace(
        snapshot,
        completed_orders=(
            replace(terminal, status=status, completed_status=completed),
        ),
    )
    assert classify_trailing(entry, snapshot).status == expected
