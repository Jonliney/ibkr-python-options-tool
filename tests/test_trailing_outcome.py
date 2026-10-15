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
        trailing_value="0.10",
        trailing_unit="dollars",
        trailing_stop="0.80",
        trailing_tif="GTC",
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


@pytest.mark.parametrize("already_confirmed", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_lost_trailing_cancel_ack_reconciles_exact_attempt_after_restart(
    tmp_path, already_confirmed, partial
):
    from types import SimpleNamespace

    from ibkr_options_manager.domain import ObservedExecution, WorkingOrder
    from ibkr_options_manager.execution import (
        ExecutionOutcomeUnknown,
        PaperExecutionService,
    )

    entry, terminal_snapshot, _terminal = evidence()
    order = WorkingOrder(
        801,
        17,
        701,
        terminal_snapshot.selected,
        "SELL",
        "TRAIL",
        Decimal("2"),
        "Submitted",
        stop_price=Decimal("0.80"),
        tif="GTC",
        trailing_amount=Decimal("0.10"),
    )
    before = replace(terminal_snapshot, working_orders=(order,), completed_orders=())
    path = tmp_path / "orders.json"
    journal = ExecutionJournal(path)
    journal._write((entry,))

    def lost_ack(*args, **kwargs):
        intent = journal.unresolved_management_entries(
            account=entry.account, con_id=entry.con_id
        )[0]
        assert intent.state == "PREPARED"
        assert intent.cancellation_source == entry.fingerprint
        assert intent.cancellation_order_id == 701
        assert intent.cancellation_perm_id == 801
        raise ExecutionOutcomeUnknown("lost cancellation acknowledgement")

    service = PaperExecutionService(SimpleNamespace(change_trailing=lost_ack), journal)
    candidate = service.prepare_trailing_change(
        before, perm_id=801, expected_client_id=17, cancel=True
    )
    with pytest.raises(ExecutionOutcomeUnknown):
        service.submit_trailing_change(
            before,
            candidate,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )
    pending = journal.unresolved_management_entries(
        account=entry.account, con_id=entry.con_id
    )[0]
    if already_confirmed:
        journal._write(
            tuple(
                replace(item, state="CANCELLED_CONFIRMED")
                if item.fingerprint == entry.fingerprint
                else item
                for item in journal._entries()
            )
        )
    later = replace(terminal_snapshot, captured_at=before.captured_at + 1)
    if partial:
        later = replace(
            later,
            executions=(
                ObservedExecution(
                    exec_id="partial.01",
                    perm_id=801,
                    account=entry.account,
                    con_id=entry.con_id,
                    side="SLD",
                    quantity=Decimal("1"),
                    price=Decimal("1"),
                    time="now",
                ),
            ),
        )
    restored = ExecutionJournal(path)
    restored.record_trailing_cancellations(later)
    assert not restored.unresolved_management_entries(
        account=entry.account, con_id=entry.con_id
    )
    assert restored.find(pending.fingerprint).state == "COMPLETED"
    assert restored.find(entry.fingerprint).state == "CANCELLED_CONFIRMED"
    assert len(restored.find(entry.fingerprint).fills) == int(partial)
    assert (
        restored.find(pending.fingerprint).submission_error
        == "lost cancellation acknowledgement"
    )
    assert not ExecutionJournal(path).unresolved_management_entries(
        account=entry.account, con_id=entry.con_id
    )


@pytest.mark.parametrize(
    "change",
    [
        "absent",
        "stale",
        "disconnected",
        "errors",
        "history",
        "executions",
        "wrong-source",
        "wrong-order",
        "wrong-perm",
        "wrong-account",
        "wrong-contract",
        "duplicate-source",
        "legacy",
        "not-later",
        "invalid-time",
        "filled",
        "working",
        "duplicate-terminal",
        "unrelated-attempt",
    ],
)
def test_uncertain_trailing_cancel_stays_locked_without_exact_terminal_proof(
    tmp_path, change
):
    from ibkr_options_manager.domain import WorkingOrder

    entry, snapshot, terminal = evidence()
    pending = JournalEntry(
        fingerprint="cancel-trailing:" + "b" * 64,
        account=entry.account,
        con_id=entry.con_id,
        state="SUBMISSION_UNKNOWN",
        snapshot_captured_at=str(snapshot.captured_at - 1),
        cancellation_source=entry.fingerprint,
        cancellation_order_id=701,
        cancellation_perm_id=801,
    )
    if change == "absent":
        snapshot = replace(snapshot, completed_orders=())
    elif change == "stale":
        snapshot = replace(snapshot, fresh=False)
    elif change == "disconnected":
        snapshot = replace(snapshot, connected=False)
    elif change == "errors":
        snapshot = replace(snapshot, errors=("unknown broker failure",))
    elif change == "history":
        snapshot = replace(snapshot, completed_orders_complete=False)
    elif change == "executions":
        snapshot = replace(snapshot, executions_complete=False)
    elif change == "wrong-source":
        pending = replace(
            pending, cancellation_source="trailing-conversion:" + "c" * 64
        )
    elif change == "wrong-order":
        pending = replace(pending, cancellation_order_id=702)
    elif change == "wrong-perm":
        pending = replace(pending, cancellation_perm_id=802)
    elif change == "wrong-account":
        snapshot = replace(
            snapshot, completed_orders=(replace(terminal, account="DU9999999"),)
        )
    elif change == "wrong-contract":
        snapshot = replace(
            snapshot, completed_orders=(replace(terminal, con_id=entry.con_id + 1),)
        )
    elif change == "legacy":
        pending = replace(
            pending,
            cancellation_source="",
            cancellation_order_id=0,
            cancellation_perm_id=0,
        )
    elif change == "not-later":
        pending = replace(pending, snapshot_captured_at=str(snapshot.captured_at))
    elif change == "invalid-time":
        pending = replace(pending, snapshot_captured_at="NaN")
    elif change == "filled":
        snapshot = replace(
            snapshot, completed_orders=(replace(terminal, status="Filled"),)
        )
    elif change == "working":
        snapshot = replace(
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
        )
    elif change == "duplicate-terminal":
        snapshot = replace(snapshot, completed_orders=(terminal, terminal))
    elif change == "unrelated-attempt":
        pending = replace(pending, fingerprint="amend-trailing:" + "b" * 64)
    path = tmp_path / "orders.json"
    journal = ExecutionJournal(path)
    journal._write(
        (entry, pending, entry) if change == "duplicate-source" else (entry, pending)
    )
    journal.record_trailing_cancellations(snapshot)
    assert journal.unresolved_management_entries(
        account=entry.account, con_id=entry.con_id
    )
    assert journal.find(pending.fingerprint).state == "SUBMISSION_UNKNOWN"


@pytest.mark.parametrize("partial", [False, True])
def test_hide_cancelled_trail_persists_without_erasing_fills(tmp_path, partial):
    entry, snapshot, _terminal = evidence()
    entry = replace(entry, state="CANCELLED_CONFIRMED", snapshot_captured_at="98")
    if partial:
        entry = replace(
            entry, fills=(JournalFill("fill.01", 801, "SLD", "1", "1.00", "now"),)
        )
    path = tmp_path / "orders.json"
    journal = ExecutionJournal(path)
    journal._write((entry,))
    hidden = journal.dismiss_cancelled_trailing(snapshot, entry.fingerprint, "98")
    assert hidden.hidden_from_workspace
    assert replace(hidden, hidden_from_workspace=False) == entry
    assert ExecutionJournal(path).find(entry.fingerprint) == hidden


@pytest.mark.parametrize(
    "change", ["working", "stale", "absent", "wrong-attempt", "active", "ambiguous"]
)
def test_hide_trail_requires_exact_confirmed_cancellation(tmp_path, change):
    from ibkr_options_manager.domain import WorkingOrder
    from ibkr_options_manager.execution import ExecutionBlocked

    entry, snapshot, _terminal = evidence()
    entry = replace(entry, state="CANCELLED_CONFIRMED", snapshot_captured_at="98")
    if change == "working":
        snapshot = replace(
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
        )
    elif change == "stale":
        snapshot = replace(snapshot, fresh=False)
    elif change == "absent":
        snapshot = replace(snapshot, completed_orders=())
    elif change == "active":
        entry = replace(entry, state="SUBMITTED")
    journal = ExecutionJournal(tmp_path / "orders.json")
    journal._write((entry, entry) if change == "ambiguous" else (entry,))
    with pytest.raises(ExecutionBlocked):
        journal.dismiss_cancelled_trailing(
            snapshot, entry.fingerprint, "99" if change == "wrong-attempt" else "98"
        )
    assert not journal.find(entry.fingerprint).hidden_from_workspace
