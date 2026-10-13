from copy import copy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ibkr_options_manager.broker import execution as transport
from ibkr_options_manager.execution import ExecutionBlocked
from ibkr_options_manager.ibkr_probe import _IbapiImports
from test_execution import _plan, _snapshot


def _writer(monkeypatch, mode="normal", quantity=2, snapshot=None):
    if snapshot is None:
        snapshot = replace(_snapshot(), executions_complete=True)
        snapshot = replace(
            snapshot, position=replace(snapshot.position, quantity=Decimal(quantity))
        )
    c = snapshot.contract
    contract = SimpleNamespace(
        conId=c.con_id,
        secType=c.sec_type,
        lastTradeDateOrContractMonth=c.expiry,
        strike=c.strike,
        right=c.right,
        multiplier=str(c.multiplier),
        currency=c.currency,
        tradingClass=c.trading_class,
    )
    writes, reads = [], []

    class Wrapper:
        def __init__(self):
            pass

    class Client:
        def __init__(self, wrapper):
            self.wrapper = wrapper
            self.connected = False
            self.broker_orders = {}
            from ibapi.order import Order

            for prior in snapshot.working_orders:
                order = Order()
                order.permId, order.clientId = prior.perm_id, prior.client_id
                order.account, order.action = prior.key.account, prior.action
                order.orderType, order.totalQuantity = prior.order_type, prior.remaining
                order.ocaGroup, order.parentId = prior.oca_group or "", prior.parent_id
                order.tif = prior.tif
                if prior.limit_price is not None:
                    order.lmtPrice = float(prior.limit_price)
                if prior.stop_price is not None:
                    order.auxPrice = float(prior.stop_price)
                self.broker_orders[prior.order_id] = (order, prior.status)
            self.scan_number = 0
            self.previous_execution_request = None

        def connect(self, *_):
            self.connected = True
            self.wrapper.nextValidId(500)

        def run(self):
            pass

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False

        def placeOrder(self, oid, _contract, order):
            writes.append((oid, copy(order)))
            order = copy(order)
            order.permId = oid + 1000
            order.clientId = 17
            status = "Submitted" if order.orderType == "LMT" else "PreSubmitted"
            if mode == "stop-rejected" and order.orderType != "LMT":
                self.wrapper.error(oid, 109, "TWS price precaution")
                return
            if mode == "target-rejected" and order.orderType == "LMT":
                self.wrapper.error(oid, 109, "TWS price precaution")
                return
            if mode == "stop-held" and order.orderType != "LMT":
                status = "PendingSubmit"
            self.broker_orders[oid] = (order, status)
            self.wrapper.openOrder(oid, contract, order, SimpleNamespace(status=status))

        def reqAllOpenOrders(self):
            self.scan_number += 1
            reads.append(("orders", tuple(self.broker_orders)))
            for oid, (order, status) in self.broker_orders.items():
                if mode == "missing-stop-before" and self.scan_number == 2:
                    continue
                if (
                    mode == "missing-stop-after"
                    and self.scan_number == 3
                    and order.orderType != "LMT"
                ):
                    continue
                if (
                    mode == "missing-target-after"
                    and self.scan_number == 3
                    and order.orderType == "LMT"
                ):
                    continue
                reported = copy(order)
                reported_contract = copy(contract)
                if self.scan_number == 2:
                    if mode == "manual-edit":
                        reported.auxPrice += 0.05
                    if mode == "identity-change":
                        reported_contract.right = "P"
                    if mode == "permid-change":
                        reported.permId += 1
                    if mode == "cancelled-status":
                        status = "Cancelled"
                self.wrapper.openOrder(
                    oid, reported_contract, reported, SimpleNamespace(status=status)
                )
            if mode == "new-order" and self.scan_number == 2:
                other = copy(next(iter(self.broker_orders.values()))[0])
                other.permId = 9999
                self.wrapper.openOrder(
                    900, contract, other, SimpleNamespace(status="Submitted")
                )
            if not (mode == "missing-end" and self.scan_number == 2):
                self.wrapper.openOrderEnd()

        def reqPositions(self):
            qty = snapshot.position.quantity
            if mode == "position-change" and self.scan_number == 2:
                qty -= 1
            self.wrapper.position(
                snapshot.selected.account,
                contract,
                qty,
                snapshot.position.raw_average_cost,
            )
            self.wrapper.positionEnd()

        def cancelPositions(self):
            if mode == "cancel-race" and self.scan_number == 2:
                self.wrapper.orderStatus(500, "Cancelled", 0, 2)

        def reqExecutions(self, req_id, _filter):
            reads.append(("executions", self.scan_number))
            if mode == "late-history" and self.previous_execution_request is not None:
                self.wrapper.execDetails(
                    self.previous_execution_request,
                    contract,
                    SimpleNamespace(
                        acctNumber=snapshot.selected.account,
                        execId="late-prior-response",
                    ),
                )
                self.wrapper.execDetailsEnd(self.previous_execution_request)
            self.previous_execution_request = req_id
            if mode == "fill-after-target" and self.scan_number == 3:
                self.wrapper.execDetails(
                    req_id,
                    contract,
                    SimpleNamespace(
                        acctNumber=snapshot.selected.account, execId="racing-fill"
                    ),
                )
                self.wrapper.orderStatus(500, "Filled", snapshot.position.quantity, 0)
            if mode in {"fill", "partial-fill"} and self.scan_number == 2:
                self.wrapper.execDetails(
                    req_id,
                    contract,
                    SimpleNamespace(
                        acctNumber=snapshot.selected.account, execId="new-fill"
                    ),
                )
                self.wrapper.orderStatus(500, "PreSubmitted", 1, 1)
            if mode == "disconnect" and self.scan_number == 2:
                self.connected = False
            self.wrapper.execDetailsEnd(req_id)

    monkeypatch.setattr(
        transport,
        "_load_ibapi",
        lambda: _IbapiImports(Client, Wrapper, SimpleNamespace, SimpleNamespace),
    )
    return snapshot, writes, reads


def _submit(snapshot, plan=None):
    return transport.IbkrPaperExecutionBroker().submit(
        snapshot,
        plan or _plan(snapshot),
        host="127.0.0.1",
        port=7497,
        client_id=17,
        timeout_seconds=0.15,
    )


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


@pytest.mark.parametrize("fail_after_first", [False, True])
def test_four_brackets_are_sent_and_verified_in_sequence(monkeypatch, fail_after_first):
    from ibkr_options_manager.domain import (
        LayerRequest,
        PlanRequest,
        RemainderPolicy,
        build_exit_plan,
    )

    snapshot, writes, reads = _writer(
        monkeypatch, "missing-stop-after" if fail_after_first else "normal", quantity=4
    )
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
