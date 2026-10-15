from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ibkr_options_manager.broker.execution import PaperSubmission
from ibkr_options_manager.domain import ObservedExecution, WorkingOrder
from ibkr_options_manager.execution import (
    ExecutionBlocked,
    ExecutionJournal,
    ExecutionOutcomeUnknown,
    JournalEntry,
    PaperExecutionService,
)
from test_execution import _trailing_snapshot


def market_fixture(tmp_path):
    snapshot = _trailing_snapshot()
    order = WorkingOrder(
        901,
        17,
        501,
        snapshot.selected,
        "SELL",
        "MKT",
        Decimal("2"),
        "Submitted",
        tif="GTC",
    )
    entry = JournalEntry(
        "market-exit-position:" + "a" * 64,
        snapshot.selected.account,
        snapshot.selected.con_id,
        "SUBMITTED",
        order_ids=(501,),
        perm_ids=(901,),
        market_quantity=2,
        market_reference_bid="1.05",
    )
    journal = ExecutionJournal(tmp_path / "market.json")
    journal._write((entry,))
    return journal, entry, replace(snapshot, working_orders=(order,))


def test_market_submission_records_quantity_before_transport(tmp_path):
    snapshot = _trailing_snapshot()
    journal = ExecutionJournal(tmp_path / "market.json")

    def submit(snapshot, plan, **kwargs):
        intent = journal.market_entries(
            account=snapshot.selected.account, con_id=snapshot.selected.con_id
        )[0]
        assert intent.state == "PREPARED"
        assert intent.market_quantity == 2
        return PaperSubmission((501,), (901,))

    service = PaperExecutionService(SimpleNamespace(submit_market=submit), journal)
    plan = service.prepare_position_market_exit(
        snapshot, target_perm_ids=(), expected_client_id=17
    )
    service.submit_position_market_exit(
        snapshot, plan, host="127.0.0.1", port=7497, client_id=17, timeout_seconds=1
    )
    restored = ExecutionJournal(journal._path).market_entries(
        account=snapshot.selected.account, con_id=snapshot.selected.con_id
    )[0]
    assert restored.market_quantity == 2
    assert restored.perm_ids == (901,)


@pytest.mark.parametrize(
    "change",
    [
        None,
        "external",
        "client",
        "type",
        "filled",
        "partial",
        "stale",
        "history",
        "rejection",
        "ack",
    ],
)
def test_market_cancellation_is_owned_exact_fresh_and_durable(tmp_path, change):
    journal, entry, snapshot = market_fixture(tmp_path)
    writes = []

    def cancel(snapshot, candidate, **kwargs):
        writes.append(candidate)
        if change == "rejection":
            raise ExecutionOutcomeUnknown("rejected")
        return PaperSubmission(() if change == "ack" else (501,), ())

    service = PaperExecutionService(SimpleNamespace(cancel_market=cancel), journal)
    candidate = service.prepare_market_cancellation(
        snapshot, perm_id=901, expected_client_id=17
    )
    if change == "external":
        journal._write(())
    elif change == "client":
        snapshot = replace(
            snapshot, working_orders=(replace(candidate.order, client_id=99),)
        )
    elif change == "type":
        snapshot = replace(
            snapshot, working_orders=(replace(candidate.order, order_type="LMT"),)
        )
    elif change == "filled":
        snapshot = replace(snapshot, working_orders=())
    elif change == "partial":
        snapshot = replace(
            snapshot, working_orders=(replace(candidate.order, remaining=Decimal("1")),)
        )
    elif change == "stale":
        snapshot = replace(snapshot, fresh=False)
    elif change == "history":
        snapshot = replace(snapshot, executions_complete=False)
    if change:
        with pytest.raises((ExecutionBlocked, ExecutionOutcomeUnknown)):
            service.cancel_market(
                snapshot,
                candidate,
                host="127.0.0.1",
                port=7497,
                client_id=17,
                timeout_seconds=1,
            )
        assert bool(writes) is (change in {"rejection", "ack"})
        if writes:
            assert ExecutionJournal(journal._path).unresolved_management_entries(
                account=entry.account, con_id=entry.con_id
            )
    else:
        service.cancel_market(
            snapshot,
            candidate,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )
        assert (
            ExecutionJournal(journal._path).find(entry.fingerprint).state
            == "CANCELLED_CONFIRMED"
        )
        with pytest.raises(ExecutionBlocked):
            service.cancel_market(
                snapshot,
                candidate,
                host="127.0.0.1",
                port=7497,
                client_id=17,
                timeout_seconds=1,
            )
        assert len(writes) == 1


def test_market_fills_are_recorded_by_exact_identity_and_survive_restart(tmp_path):
    journal, entry, snapshot = market_fixture(tmp_path)
    fill = ObservedExecution(
        "market.01",
        entry.account,
        entry.con_id,
        901,
        "SLD",
        Decimal("2"),
        Decimal("1.07"),
        "now",
        realized_pnl=Decimal("12"),
        currency="USD",
    )
    journal.record_executions(replace(snapshot, working_orders=(), executions=(fill,)))
    restored = ExecutionJournal(journal._path).find(entry.fingerprint)
    assert len(restored.fills) == 1
    assert restored.fills[0].price == "1.07"
    assert restored.market_quantity == 2


@pytest.mark.parametrize(
    "event",
    [
        "status",
        "202",
        "filled",
        "rejected",
        "timeout",
        "wrong-id",
        "changed",
        "duplicate",
    ],
)
def test_market_cancel_transport_verifies_before_writing_and_handles_races(
    tmp_path, monkeypatch, event
):
    import ibkr_options_manager.broker.execution as transport_module
    from ibkr_options_manager.ibkr_probe import _IbapiImports

    journal, _entry, snapshot = market_fixture(tmp_path)
    service = PaperExecutionService(SimpleNamespace(), journal)
    candidate = service.prepare_market_cancellation(
        snapshot, perm_id=901, expected_client_id=17
    )
    writes = []

    class Wrapper:
        pass

    class Client:
        def __init__(self, wrapper):
            self.wrapper = wrapper
            self.connected = False

        def connect(self, *args):
            self.connected = True
            self.wrapper.nextValidId(502)

        def run(self):
            pass

        def disconnect(self):
            self.connected = False

        def isConnected(self):
            return self.connected

        def reqOpenOrders(self):
            order = SimpleNamespace(
                account=snapshot.selected.account,
                permId=902 if event == "changed" else 901,
                clientId=17,
                action="SELL",
                orderType="MKT",
                tif="GTC",
                ocaGroup="",
                parentId=0,
            )
            for _ in range(2 if event == "duplicate" else 1):
                self.wrapper.openOrder(
                    501,
                    SimpleNamespace(conId=snapshot.selected.con_id),
                    order,
                    SimpleNamespace(status="Submitted"),
                )
            self.wrapper.orderStatus(501, "Submitted", 0, 2)
            self.wrapper.openOrderEnd()

    def cancel(app, order_id):
        writes.append(order_id)
        if event == "status":
            app.orderStatus(order_id, "Cancelled", 0, 0)
        elif event == "202":
            app.error(order_id, 202, "Order Canceled")
        elif event == "filled":
            app.orderStatus(order_id, "Filled", 2, 0)
        elif event == "rejected":
            app.error(order_id, 201, "rejected")
        elif event == "wrong-id":
            app.error(order_id + 1, 202, "Order Canceled")

    monkeypatch.setattr(
        transport_module,
        "_load_ibapi",
        lambda: _IbapiImports(Client, Wrapper, SimpleNamespace, SimpleNamespace),
    )
    monkeypatch.setattr(transport_module, "_cancel_order", cancel)
    writer = transport_module.IbkrPaperExecutionBroker()
    kwargs = dict(host="127.0.0.1", port=7497, client_id=17, timeout_seconds=0.02)
    if event in {"changed", "duplicate"}:
        with pytest.raises(ExecutionBlocked):
            writer.cancel_market(snapshot, candidate, **kwargs)
        assert writes == []
    elif event in {"status", "202"}:
        assert writer.cancel_market(snapshot, candidate, **kwargs).order_ids == (501,)
        assert writes == [501]
    else:
        with pytest.raises(ExecutionOutcomeUnknown):
            writer.cancel_market(snapshot, candidate, **kwargs)
        assert writes == [501]


@pytest.mark.parametrize(
    "case, expected",
    [
        ("working", "WORKING"),
        ("partial", "PARTIAL"),
        ("filled", "FILLED"),
        ("absent", "UNKNOWN"),
        ("legacy-partial-absent", "UNKNOWN"),
        ("stale", "UNKNOWN"),
        ("duplicate", "UNKNOWN"),
        ("conflict", "UNKNOWN"),
        ("wrong-account-fill", "UNKNOWN"),
        ("cancelled", "CANCELLED"),
    ],
)
def test_market_outcome_requires_fill_or_order_evidence(tmp_path, case, expected):
    from ibkr_options_manager.execution import JournalFill
    from ibkr_options_manager.market_outcome import classify_market

    _journal, entry, snapshot = market_fixture(tmp_path)
    fill = JournalFill("fill.01", 901, "SLD", "2", "1.07", "now", "12", "USD")
    if case == "partial":
        entry = replace(entry, fills=(replace(fill, quantity="1"),))
        snapshot = replace(
            snapshot,
            working_orders=(
                replace(snapshot.working_orders[0], remaining=Decimal("1")),
            ),
        )
    elif case in {"filled", "conflict"}:
        entry = replace(entry, fills=(fill,))
        if case == "filled":
            snapshot = replace(snapshot, working_orders=())
    elif case in {"absent", "wrong-account-fill", "legacy-partial-absent"}:
        snapshot = replace(snapshot, working_orders=())
        if case == "legacy-partial-absent":
            entry = replace(
                entry, market_quantity=0, fills=(replace(fill, quantity="1"),)
            )
    elif case == "stale":
        snapshot = replace(snapshot, fresh=False)
    elif case == "duplicate":
        snapshot = replace(snapshot, working_orders=snapshot.working_orders * 2)
    elif case == "cancelled":
        entry = replace(entry, state="CANCELLED_CONFIRMED")
        snapshot = replace(snapshot, working_orders=())
    assert classify_market(entry, snapshot).status == expected


def test_legacy_market_quantity_recovered_only_with_terminal_filled_evidence(tmp_path):
    from ibkr_options_manager.domain import ObservedCompletedOrder
    from ibkr_options_manager.execution import JournalFill
    from ibkr_options_manager.market_outcome import classify_market

    journal, entry, snapshot = market_fixture(tmp_path)
    entry = replace(
        entry,
        market_quantity=0,
        fills=(JournalFill("fill.01", 901, "SLD", "2", "1.07", "now"),),
    )
    journal._write((entry,))
    absent = replace(snapshot, working_orders=())
    journal.record_market_outcomes(absent)
    assert journal.find(entry.fingerprint).market_quantity == 0
    terminal = ObservedCompletedOrder(
        entry.account, entry.con_id, 901, 501, 17, "SELL", "MKT", "", "Filled"
    )
    journal.record_market_outcomes(replace(absent, completed_orders=(terminal,)))
    restored = ExecutionJournal(journal._path).find(entry.fingerprint)
    assert restored.market_quantity == 2
    assert classify_market(restored, None).status == "FILLED"


@pytest.mark.parametrize("terminal", [False, True])
def test_lost_market_cancel_ack_reconciles_only_exact_terminal_cancellation(
    tmp_path, terminal
):
    from ibkr_options_manager.domain import ObservedCompletedOrder

    journal, entry, snapshot = market_fixture(tmp_path)

    def lost(*args, **kwargs):
        raise ExecutionOutcomeUnknown("lost acknowledgement")

    service = PaperExecutionService(SimpleNamespace(cancel_market=lost), journal)
    candidate = service.prepare_market_cancellation(
        snapshot, perm_id=901, expected_client_id=17
    )
    with pytest.raises(ExecutionOutcomeUnknown):
        service.cancel_market(
            snapshot,
            candidate,
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=1,
        )
    later = replace(
        snapshot,
        working_orders=(),
        captured_at=snapshot.captured_at + 1,
        completed_orders=(
            ObservedCompletedOrder(
                entry.account,
                entry.con_id,
                901,
                501,
                17,
                "SELL",
                "MKT",
                "",
                "Cancelled",
            ),
        )
        if terminal
        else (),
    )
    restored = ExecutionJournal(journal._path)
    restored.record_market_outcomes(later)
    assert (
        bool(
            restored.unresolved_management_entries(
                account=entry.account, con_id=entry.con_id
            )
        )
        is not terminal
    )
