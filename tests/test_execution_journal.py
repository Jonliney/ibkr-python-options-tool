"""Failure boundaries at the durable intent / transport seam."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ibkr_options_manager import execution
from ibkr_options_manager.domain import (
    ObservedCompletedOrder,
    ObservedExecution,
    WorkingOrder,
)
from ibkr_options_manager.execution import (
    ExecutionBlocked,
    ExecutionJournal,
    ExecutionOutcomeUnknown,
    PaperExecutionService,
)
from support.orders import _plan, _snapshot, _trailing_snapshot

SETTINGS = dict(host="127.0.0.1", port=7497, client_id=17, timeout_seconds=1)


def service(path):
    calls = []

    def submit(*args, **kwargs):
        calls.append("send")
        return SimpleNamespace(order_ids=(101, 102), perm_ids=(201, 202))

    return PaperExecutionService(
        SimpleNamespace(submit=submit), ExecutionJournal(path)
    ), calls


@pytest.mark.parametrize("payload", ["[", "{}", "[{}]", "[null]"])
def test_invalid_journal_blocks_before_transport(tmp_path, payload):
    path = tmp_path / "orders.json"
    path.write_text(payload)
    sender, calls = service(path)
    with pytest.raises(ExecutionBlocked, match="journal"):
        sender.submit(_snapshot(), _plan(_snapshot()), **SETTINGS)
    assert calls == []
    assert path.read_text() == payload


@pytest.mark.parametrize("failure", ["read", "write", "flush", "replace"])
def test_intent_storage_failure_never_calls_transport(tmp_path, monkeypatch, failure):
    path = tmp_path / "orders.json"
    path.write_text("[]")
    sender, calls = service(path)
    original_read = Path.read_text
    original_write = Path.write_text

    def read(file, *args, **kwargs):
        if file == path:
            raise PermissionError("simulated read denial")
        return original_read(file, *args, **kwargs)

    def write(file, *args, **kwargs):
        if file == path.with_suffix(".tmp"):
            raise OSError("simulated disk full")
        return original_write(file, *args, **kwargs)

    def replace(*args):
        raise OSError("simulated replacement failure")

    with monkeypatch.context() as patch:
        if failure == "read":
            patch.setattr(Path, "read_text", read)
        elif failure == "write":
            patch.setattr(Path, "write_text", write)
        elif failure == "flush":
            patch.setattr(execution.os, "fsync", replace)
        else:
            patch.setattr(execution.os, "replace", replace)
        with pytest.raises((ExecutionBlocked, OSError)):
            sender.submit(_snapshot(), _plan(_snapshot()), **SETTINGS)
    assert calls == []
    assert path.read_text() == "[]"


def test_receipt_storage_failure_keeps_intent_and_blocks_restart_retry(
    tmp_path, monkeypatch
):
    path = tmp_path / "orders.json"
    sender, calls = service(path)
    original_replace = execution.os.replace
    replacements = 0

    def replace(source, destination):
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("simulated receipt write failure")
        return original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(execution.os, "replace", replace)
        with pytest.raises(ExecutionOutcomeUnknown, match="journal"):
            sender.submit(_snapshot(), _plan(_snapshot()), **SETTINGS)
    assert calls == ["send"]
    assert (
        ExecutionJournal(path).find(_plan(_snapshot()).fingerprint).state == "PREPARED"
    )
    restarted, new_calls = service(path)
    with pytest.raises(ExecutionBlocked, match="already journaled"):
        restarted.submit(_snapshot(), _plan(_snapshot()), **SETTINGS)
    assert new_calls == []


def test_simultaneous_confirmations_send_once_across_journal_instances(
    tmp_path, monkeypatch
):
    path = tmp_path / "orders.json"
    first, first_calls = service(path)
    second, second_calls = service(path)
    pending_write, release_write, second_started = Event(), Event(), Event()
    guard = Lock()
    paused = False
    original_write = ExecutionJournal._write

    def pause_first_write(journal, entries):
        nonlocal paused
        with guard:
            pause = not paused
            paused = True
        if pause:
            pending_write.set()
            assert release_write.wait(3)
        return original_write(journal, entries)

    monkeypatch.setattr(ExecutionJournal, "_write", pause_first_write)

    def send(sender, started=None):
        if started is not None:
            started.set()
        try:
            sender.submit(_snapshot(), _plan(_snapshot()), **SETTINGS)
            return "sent"
        except ExecutionBlocked:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(send, first)
        try:
            assert pending_write.wait(3)
            two = pool.submit(send, second, second_started)
            assert second_started.wait(3)
            # Give the second caller a chance to pass an unprotected duplicate
            # check while the first is paused immediately before persistence.
            with suppress(TimeoutError):
                two.result(timeout=0.1)
        finally:
            release_write.set()
        assert sorted([one.result(timeout=3), two.result(timeout=3)]) == [
            "blocked",
            "sent",
        ]
    assert len(first_calls + second_calls) == 1
    restored = ExecutionJournal(path)
    assert len(restored.submission_entries(account="DU1234567", con_id=917864414)) == 1


def test_concurrent_intents_for_distinct_contracts_preserve_both_records(
    tmp_path, monkeypatch
):
    first_snapshot = _snapshot()
    other_key = replace(first_snapshot.selected, con_id=917864415)
    second_snapshot = replace(
        first_snapshot,
        selected=other_key,
        contract=replace(first_snapshot.contract, con_id=other_key.con_id),
        position=replace(first_snapshot.position, key=other_key),
    )
    path = tmp_path / "orders.json"
    first, second = ExecutionJournal(path), ExecutionJournal(path)
    pending, release, started = Event(), Event(), Event()
    original_write = first._write

    def pause(entries):
        pending.set()
        assert release.wait(3)
        return original_write(entries)

    monkeypatch.setattr(first, "_write", pause)

    def begin_second():
        started.set()
        return second.begin(second_snapshot, _plan(second_snapshot))

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first.begin, first_snapshot, _plan(first_snapshot))
        try:
            assert pending.wait(3)
            two = pool.submit(begin_second)
            assert started.wait(3)
            with suppress(TimeoutError):
                two.result(timeout=0.1)
        finally:
            release.set()
        entries = [one.result(timeout=3), two.result(timeout=3)]
    restored = ExecutionJournal(path)
    for entry in entries:
        assert restored.find(entry.fingerprint) == entry


def _journal_process(path, script):
    import subprocess
    import sys

    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import os, sys
from pathlib import Path
sys.path[:0] = sys.argv[2:]
from ibkr_options_manager.execution import ExecutionJournal, _journal_transaction
from support.orders import _plan, _snapshot
path = Path(sys.argv[1])
"""
            + script,
            str(path),
            str(Path(__file__).parents[1] / "src"),
            str(Path(__file__).parent),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_another_process_holding_journal_blocks_send_and_releases_on_exit(tmp_path):
    path = tmp_path / "orders.json"
    process = _journal_process(
        path,
        """
with _journal_transaction(path):
    print('locked', flush=True)
    sys.stdin.readline()
""",
    )
    try:
        # Bound process readiness as well as completion; a startup failure must
        # not leave the test hanging on a pipe read.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            ready = pool.submit(process.stdout.readline)
            try:
                assert ready.result(timeout=5) == "locked\n"
            except BaseException:
                process.kill()
                raise
        sender, calls = service(path)
        with pytest.raises(ExecutionBlocked, match="busy"):
            sender.submit(_snapshot(), _plan(_snapshot()), **SETTINGS)
        assert calls == []
    finally:
        if process.poll() is None:
            process.stdin.write("release\n")
            process.stdin.flush()
        _, errors = process.communicate(timeout=5)
        assert process.returncode == 0, errors
    sender.submit(_snapshot(), _plan(_snapshot()), **SETTINGS)
    assert calls == ["send"]


def test_process_crash_after_prepared_intent_releases_lock_but_blocks_resend(tmp_path):
    path = tmp_path / "orders.json"
    process = _journal_process(
        path,
        """
with _journal_transaction(path):
    ExecutionJournal(path).begin(_snapshot(), _plan(_snapshot()))
    os._exit(0)
""",
    )
    _, errors = process.communicate(timeout=5)
    assert process.returncode == 0, errors
    sender, calls = service(path)
    with pytest.raises(ExecutionBlocked, match="already journaled"):
        sender.submit(_snapshot(), _plan(_snapshot()), **SETTINGS)
    assert calls == []


@settings(max_examples=25, deadline=None)
@given(
    st.lists(
        st.sampled_from(
            [
                "restart",
                "disconnect",
                "partial-fill",
                "manual-edit",
                "rejection",
                "missing-orders",
                "retry",
            ]
        ),
        min_size=1,
        max_size=15,
    )
)
def test_unknown_submission_stays_non_retryable_through_unproven_lifecycle(actions):
    """No sequence of incomplete hints can substitute for exact recovery proof."""
    with TemporaryDirectory() as directory:
        path = Path(directory) / "orders.json"
        snapshot = _trailing_snapshot()
        plan = _plan(snapshot)
        sends = []

        def lost_ack(*args, **kwargs):
            sends.append("send")
            raise ExecutionOutcomeUnknown("lost acknowledgement")

        transport = SimpleNamespace(submit=lost_ack)
        journal = ExecutionJournal(path)
        sender = PaperExecutionService(transport, journal)
        with pytest.raises(ExecutionOutcomeUnknown):
            sender.submit(snapshot, plan, **SETTINGS)
        group = plan.pairs[0].target.logical_oca_group
        for index, action in enumerate(actions, start=1):
            later = replace(snapshot, captured_at=Decimal(index))
            if action == "restart":
                journal = ExecutionJournal(path)
                sender = PaperExecutionService(transport, journal)
            elif action == "disconnect":
                later = replace(later, connected=False, fresh=False)
            elif action == "partial-fill":
                later = replace(
                    later,
                    executions=(
                        ObservedExecution(
                            "partial.01",
                            snapshot.selected.account,
                            snapshot.selected.con_id,
                            201,
                            "SLD",
                            Decimal("1"),
                            Decimal("1.20"),
                            "now",
                        ),
                    ),
                )
            elif action == "manual-edit":
                later = replace(
                    later,
                    working_orders=(
                        WorkingOrder(
                            201,
                            17,
                            101,
                            snapshot.selected,
                            "SELL",
                            "LMT",
                            Decimal("1"),
                            "Submitted",
                            group,
                            limit_price=Decimal("1.30"),
                            tif="GTC",
                        ),
                    ),
                )
            elif action == "rejection":
                later = replace(
                    later,
                    completed_orders=(
                        ObservedCompletedOrder(
                            account=snapshot.selected.account,
                            con_id=snapshot.selected.con_id,
                            perm_id=201,
                            order_id=101,
                            client_id=17,
                            action="SELL",
                            order_type="LMT",
                            oca_group=group,
                            status="Inactive",
                        ),
                    ),
                )
            journal.record_executions(later)
            journal.reconcile_snapshot(later)
            # Even another complete, empty capture cannot justify a resend.
            with pytest.raises(ExecutionBlocked):
                sender.submit(
                    replace(snapshot, captured_at=Decimal(index + 1)), plan, **SETTINGS
                )
            assert sends == ["send"]
            assert journal.find(plan.fingerprint).state == "SUBMISSION_UNKNOWN"
