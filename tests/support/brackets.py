from copy import copy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from ibkr_options_manager.broker import execution as transport
from ibkr_options_manager.ibkr_probe import _IbapiImports
from support.orders import _plan, _snapshot


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
            if (
                mode.startswith("outside-rth-warning")
                and mode != "outside-rth-warning-wrong-order"
            ):
                self.wrapper.error(
                    oid,
                    2109,
                    "Order Event Warning:Attribute 'Outside Regular Trading Hours' "
                    "is ignored based on the order type and destination. "
                    "PlaceOrder is now being processed.",
                )
                if mode == "outside-rth-warning-no-ack":
                    return
                if mode == "outside-rth-warning-rejected":
                    self.wrapper.error(oid, 109, "TWS price precaution")
                    return
            order = copy(order)
            if mode == "outside-rth-warning-normalized":
                order.outsideRth = False
                if order.orderType == "STP":
                    order.lmtPrice = 0.0
                else:
                    order.auxPrice = 0.0
            if mode == "outside-rth-without-warning":
                order.outsideRth = False
            if mode == "unused-price-zero":
                if order.orderType == "STP":
                    order.lmtPrice = 0.0
                else:
                    order.auxPrice = 0.0
            if mode == "outside-rth-warning-wrong-order":
                self.wrapper.error(oid + 100, 2109, "Outside RTH ignored")
                order.outsideRth = False
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
