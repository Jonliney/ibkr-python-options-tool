from copy import copy
from dataclasses import replace
from decimal import Decimal

import pytest

from ibkr_options_manager.broker import execution as transport
from ibkr_options_manager.execution import ExecutionBlocked
from support.brackets import _submit, _writer
from support.orders import _plan, _snapshot


def test_stop_first_with_fresh_before_and_after_pair_verification(monkeypatch):
    snapshot, writes, reads = _writer(monkeypatch)
    receipt = _submit(snapshot)
    assert receipt.order_ids == (501, 500)  # journal remains target/stop ordered
    assert [(oid, order.orderType, order.transmit) for oid, order in writes] == [
        (500, "STP", True),
        (501, "LMT", True),
    ]
    assert [r[1] for r in reads if r[0] == "orders"] == [(), (500,), (500, 501)]
    assert writes[0][1].ocaGroup == writes[1][1].ocaGroup


@pytest.mark.parametrize(
    "mode",
    [
        "stop-held",
        "stop-rejected",
        "manual-edit",
        "identity-change",
        "permid-change",
        "position-change",
        "fill",
        "partial-fill",
        "disconnect",
        "missing-end",
        "missing-stop-before",
        "cancel-race",
        "cancelled-status",
        "new-order",
    ],
)
def test_no_target_is_sent_when_stop_or_fresh_safety_check_fails(monkeypatch, mode):
    snapshot, writes, _ = _writer(monkeypatch, mode)
    with pytest.raises(ExecutionBlocked) as failure:
        _submit(snapshot)
    assert len(writes) == 1
    assert "tranche-1" in str(failure.value)
    assert "one-sided bracket" in str(failure.value)


@pytest.mark.parametrize(
    "mode",
    [
        "target-rejected",
        "missing-stop-after",
        "missing-target-after",
        "fill-after-target",
    ],
)
def test_post_target_failure_is_unknown_and_stops_next_tranche(monkeypatch, mode):
    snapshot, writes, _ = _writer(monkeypatch, mode)
    with pytest.raises(ExecutionBlocked) as failure:
        _submit(snapshot)
    assert len(writes) == 2
    assert "tranche-1" in str(failure.value)
    assert "one-sided bracket" in str(failure.value)


def test_incomplete_history_is_blocked_before_any_order(monkeypatch):
    snapshot, writes, _ = _writer(monkeypatch)
    with pytest.raises(ExecutionBlocked, match="execution baseline"):
        _submit(replace(snapshot, executions_complete=False))
    assert writes == []


@pytest.mark.parametrize(
    "mode",
    ["outside-rth-warning", "outside-rth-warning-normalized", "unused-price-zero"],
)
def test_outside_rth_warning_does_not_abort_accepted_bracket(monkeypatch, mode):
    snapshot, writes, reads = _writer(monkeypatch, mode)
    receipt = _submit(snapshot)
    assert receipt.order_ids == (501, 500)
    assert [order.orderType for _, order in writes] == ["STP", "LMT"]
    assert len([read for read in reads if read[0] == "orders"]) == 3


@pytest.mark.parametrize(
    "mode", ["outside-rth-without-warning", "outside-rth-warning-wrong-order"]
)
def test_changed_outside_rth_requires_warning_for_that_order(monkeypatch, mode):
    snapshot, writes, _ = _writer(monkeypatch, mode)
    with pytest.raises(ExecutionBlocked, match="outsideRth"):
        _submit(snapshot)
    assert len(writes) == 1


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("STP", "auxPrice", 22.5),
        ("LMT", "lmtPrice", 35.0),
        ("STP LMT", "lmtPrice", 0.0),
        ("STP LMT", "auxPrice", 0.0),
        ("STP", "totalQuantity", 2),
        ("STP", "ocaGroup", "other/tranche-1"),
        ("STP", "ocaType", 1),
        ("STP", "parentId", 1),
        ("STP", "tif", "DAY"),
    ],
)
def test_normalization_keeps_active_prices_and_bracket_fields_exact(kind, field, value):
    from ibapi.order import Order

    expected = Order()
    expected.orderType = kind
    expected.auxPrice = 23.0
    expected.lmtPrice = 36.8
    expected.totalQuantity = 1
    expected.ocaGroup = "app/tranche-1"
    expected.ocaType = 2
    expected.parentId = 0
    expected.tif = "GTC"
    expected.outsideRth = True
    observed = copy(expected)
    observed.outsideRth = False
    setattr(observed, field, value)
    differences = transport._bracket_order_mismatches(
        observed, expected, outside_rth_ignored=True
    )
    assert len(differences) == 1
    assert differences[0].startswith(field + " expected=")


@pytest.mark.parametrize(
    "mode", ["outside-rth-warning-no-ack", "outside-rth-warning-rejected"]
)
def test_outside_rth_warning_is_not_acceptance_and_does_not_hide_rejection(
    monkeypatch, mode
):
    snapshot, writes, _ = _writer(monkeypatch, mode)
    with pytest.raises(ExecutionBlocked, match="one-sided bracket"):
        _submit(snapshot)
    assert len(writes) == 1


@pytest.mark.parametrize(
    "fail_after_first,mode",
    [
        (False, "normal"),
        (True, "missing-stop-after"),
        (False, "outside-rth-warning"),
        (False, "outside-rth-warning-normalized"),
    ],
)
def test_four_brackets_are_sent_and_verified_in_sequence(
    monkeypatch, fail_after_first, mode
):
    from ibkr_options_manager.domain import (
        LayerRequest,
        PlanRequest,
        RemainderPolicy,
        build_exit_plan,
    )

    snapshot, writes, reads = _writer(monkeypatch, mode, quantity=4)
    plan = build_exit_plan(
        snapshot,
        PlanRequest(
            tranche_size=1,
            target_percentages=(Decimal("20"),),
            stop_loss_percentage=Decimal("25"),
            remainder_policy=RemainderPolicy.NEXT_RUNG,
            tif="GTC",
            paper_execution_mode=True,
            layers=tuple(
                LayerRequest(
                    quantity=1,
                    target_price=Decimal("1.20") + Decimal(i) / 10,
                    stop_price=Decimal(".75"),
                    tif="GTC",
                    target_percentage=Decimal("20"),
                )
                for i in range(4)
            ),
        ),
    )
    if fail_after_first:
        with pytest.raises(ExecutionBlocked, match="one-sided bracket"):
            _submit(snapshot, plan)
        assert [oid for oid, _ in writes] == [500, 501]
        return
    receipt = _submit(snapshot, plan)
    assert receipt.order_ids == (501, 500, 503, 502, 505, 504, 507, 506)
    assert [oid for oid, _ in writes] == list(range(500, 508))
    assert all(order.transmit for _, order in writes)
    assert len([r for r in reads if r[0] == "orders"]) == 9


def test_failed_stop_first_attempt_persists_reason_and_blocks_retry(
    monkeypatch, tmp_path
):
    from ibkr_options_manager.execution import ExecutionJournal, PaperExecutionService

    snapshot, writes, _ = _writer(monkeypatch, "missing-stop-after")
    path = tmp_path / "paper.json"
    service = PaperExecutionService(
        transport.IbkrPaperExecutionBroker(), ExecutionJournal(path)
    )

    def submit():
        return service.submit(
            snapshot,
            _plan(snapshot),
            host="127.0.0.1",
            port=7497,
            client_id=17,
            timeout_seconds=0.15,
        )

    with pytest.raises(ExecutionBlocked, match="one-sided bracket"):
        submit()
    entry = ExecutionJournal(path).submission_entries(
        account=snapshot.selected.account, con_id=snapshot.selected.con_id
    )[0]
    assert entry.state == "SUBMISSION_UNKNOWN"
    assert "tranche-1" in entry.submission_error
    assert "after target" in entry.submission_error
    with pytest.raises(ExecutionBlocked):
        submit()
    assert len(writes) == 2


def test_stop_first_preserves_coherent_external_reservations(monkeypatch):
    from ibkr_options_manager.domain import WorkingOrder

    snapshot = replace(_snapshot(), executions_complete=True)
    orders = tuple(
        WorkingOrder(
            perm_id=1200 + i,
            client_id=42,
            order_id=200 + i,
            key=snapshot.selected,
            action="SELL",
            order_type=kind,
            remaining=Decimal("2"),
            status="Submitted",
            oca_group="external",
            tif="GTC",
            limit_price=Decimal("2") if kind == "LMT" else None,
            stop_price=Decimal(".50") if kind == "STP" else None,
        )
        for i, kind in enumerate(("LMT", "STP"))
    )
    snapshot = replace(
        snapshot,
        working_orders=orders,
        position=replace(snapshot.position, quantity=Decimal("4")),
    )
    snapshot, writes, _ = _writer(monkeypatch, snapshot=snapshot)
    _submit(snapshot)
    assert [oid for oid, _ in writes] == [500, 501]
    assert all(o.ocaGroup != "external" for _, o in writes)


def test_late_execution_response_cannot_contaminate_next_verification(monkeypatch):
    snapshot, writes, _ = _writer(monkeypatch, "late-history")
    _submit(snapshot)
    assert len(writes) == 2
