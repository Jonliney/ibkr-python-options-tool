"""Paper-TWS order submission for app-owned OCA pairs only."""

from __future__ import annotations

from copy import copy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

from ..cancellation_trace import (
    current_cancellation_context,
    record_cancellation_event,
)
from ..domain import BrokerSnapshot, PlanResult, supports_outside_rth
from ..execution import (
    ExecutionBlocked,
    ExecutionOutcomeUnknown,
    MarketExitCandidate,
    PositionMarketExitPlan,
    PriceUpdateCandidate,
    TrailingChangeCandidate,
    require_paper_execution_snapshot,
    require_paper_management_snapshot,
)
from ..ibkr_probe import (
    _INFORMATIONAL_ERROR_CODES,
    _load_ibapi,
    _parse_error_arguments,
)
from ..order_observation import leased_client
from ..price_update_trace import record_price_update_event
from ..redaction import redact_account
from ..trailing import TrailingPlan, plan_entire_position


@dataclass(frozen=True, slots=True)
class PaperSubmission:
    order_ids: tuple[int, ...]
    perm_ids: tuple[int, ...]


def _cancel_order(app: Any, order_id: int) -> None:
    """Cancel one app-owned order using the current official IBKR API shape."""
    from ibapi.order_cancel import OrderCancel

    app.cancelOrder(order_id, OrderCancel())


def _observed_trailing_offset(value: Any) -> Decimal | None:
    """Normalize the TWS unset sentinel while rejecting malformed gap values."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("the trailing limit offset is invalid") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("the trailing limit offset is invalid")
    return None if parsed == 0 or parsed >= Decimal("1e100") else parsed


class IbkrPaperExecutionBroker:
    """Bounded paper writer for creation and exact app-owned order changes."""

    @leased_client
    def change_trailing(
        self,
        snapshot: BrokerSnapshot,
        candidate: TrailingChangeCandidate,
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmission:
        """Change or cancel one same-client trail after exact prewrite verification."""
        require_paper_management_snapshot(snapshot)
        prior = candidate.order
        if prior.client_id != client_id or prior.key != snapshot.selected:
            raise ExecutionBlocked("the selected trailing order does not match TWS")
        imports = _load_ibapi()

        class App(imports.EWrapper, imports.EClient):  # type: ignore[name-defined, misc]
            def __init__(self) -> None:
                imports.EWrapper.__init__(self)
                imports.EClient.__init__(self, self)
                self.ready = Event()
                self.orders_done = Event()
                self.acknowledged = Event()
                self.order: Any | None = None
                self.status = ""
                self.remaining: Decimal | None = None
                self.errors: list[str] = []
                self.sent = False

            def nextValidId(self, _order_id: int) -> None:
                self.ready.set()

            def openOrder(
                self, order_id: int, contract: Any, order: Any, state: Any
            ) -> None:
                if int(order_id) != prior.order_id:
                    return
                if int(getattr(order, "permId", 0) or 0) != prior.perm_id:
                    self.errors.append("the trailing permanent ID changed")
                    return
                if int(getattr(contract, "conId", 0) or 0) != snapshot.selected.con_id:
                    self.errors.append("the trailing contract changed")
                    return
                if self.sent and not candidate.cancel:
                    try:
                        stop = Decimal(str(order.trailStopPrice))
                        offset = _observed_trailing_offset(order.lmtPriceOffset)
                        value = _observed_trailing_offset(
                            order.trailingPercent
                            if candidate.trail_unit == "percent"
                            else order.auxPrice
                        )
                        unused_value = _observed_trailing_offset(
                            order.auxPrice
                            if candidate.trail_unit == "percent"
                            else order.trailingPercent
                        )
                    except (InvalidOperation, ValueError, AttributeError):
                        return
                    if (
                        (
                            candidate.trail_value is None
                            or (value == candidate.trail_value and unused_value is None)
                        )
                        and stop == candidate.stop_price
                        and offset == candidate.limit_offset
                        and str(getattr(state, "status", ""))
                        in {"Submitted", "PreSubmitted"}
                        and str(order.orderType)
                        == (
                            "TRAIL LIMIT"
                            if candidate.limit_offset is not None
                            else "TRAIL"
                        )
                    ):
                        self.acknowledged.set()
                else:
                    self.order = order
                    self.status = str(getattr(state, "status", ""))

            def openOrderEnd(self) -> None:
                self.orders_done.set()

            def orderStatus(self, order_id: int, status: str, *args: Any) -> None:
                if not self.sent and int(order_id) == prior.order_id:
                    self.status = status
                    try:
                        self.remaining = Decimal(str(args[1]))
                    except (IndexError, InvalidOperation, ValueError):
                        self.errors.append("TWS returned an invalid trailing quantity")
                if (
                    self.sent
                    and candidate.cancel
                    and int(order_id) == prior.order_id
                    and status in {"Cancelled", "ApiCancelled"}
                ):
                    self.acknowledged.set()

            def error(self, req_id: int, *args: Any) -> None:
                code, message = _parse_error_arguments(args)
                if (
                    self.sent
                    and candidate.cancel
                    and req_id == prior.order_id
                    and code == 202
                ):
                    self.acknowledged.set()
                    return
                if code not in _INFORMATIONAL_ERROR_CODES:
                    self.errors.append(
                        f"IBKR error reqId={req_id} code={code}: {message}"
                    )

        app = App()
        reader: Thread | None = None
        deadline = monotonic() + timeout_seconds
        try:
            app.connect(host, port, client_id)
            reader = Thread(
                target=app.run, name="ibkr-paper-trail-change-reader", daemon=True
            )
            reader.start()
            if not app.ready.wait(max(0, deadline - monotonic())):
                raise ExecutionBlocked("TWS did not issue a next valid order ID")
            app.reqOpenOrders()
            if not app.orders_done.wait(max(0, deadline - monotonic())):
                raise ExecutionBlocked("same-client open orders were not verified")
            order = app.order
            if app.errors or order is None:
                raise ExecutionBlocked("the app-owned trailing order was not available")
            try:
                observed_stop = Decimal(str(order.trailStopPrice))
                observed_amount = Decimal(str(order.auxPrice))
                observed_percent = Decimal(str(order.trailingPercent))
                observed_offset = _observed_trailing_offset(order.lmtPriceOffset)
                observed_quantity = Decimal(str(order.totalQuantity))
            except (InvalidOperation, ValueError, AttributeError) as error:
                raise ExecutionBlocked(
                    "the trailing order fields are incomplete"
                ) from error
            if (
                str(order.account) != snapshot.selected.account
                or int(getattr(order, "clientId", -1)) != client_id
                or str(order.action) != "SELL"
                or str(order.orderType) != prior.order_type
                or str(order.tif) != "GTC"
                or str(getattr(order, "ocaGroup", "")) != ""
                or int(getattr(order, "parentId", -1)) != 0
                or app.status not in {"Submitted", "PreSubmitted"}
                or app.remaining != prior.remaining
                or observed_stop != prior.stop_price
                or observed_quantity != prior.remaining
                or (
                    prior.trailing_amount is not None
                    and observed_amount != prior.trailing_amount
                )
                or (
                    prior.trailing_percent is not None
                    and observed_percent != prior.trailing_percent
                )
                or observed_offset != prior.limit_offset
            ):
                raise ExecutionBlocked("the trailing order changed in TWS before write")
            app.sent = True
            if candidate.cancel:
                _cancel_order(app, prior.order_id)
            else:
                assert candidate.stop_price is not None
                from ibapi.const import UNSET_DOUBLE

                if candidate.trail_value is not None:
                    order.auxPrice = (
                        float(candidate.trail_value)
                        if candidate.trail_unit == "dollars"
                        else UNSET_DOUBLE
                    )
                    order.trailingPercent = (
                        float(candidate.trail_value)
                        if candidate.trail_unit == "percent"
                        else UNSET_DOUBLE
                    )
                order.trailStopPrice = float(candidate.stop_price)
                order.orderType = (
                    "TRAIL LIMIT" if candidate.limit_offset is not None else "TRAIL"
                )
                if candidate.limit_offset is None:
                    from ibapi.const import UNSET_DOUBLE

                    order.lmtPriceOffset = UNSET_DOUBLE
                else:
                    order.lmtPriceOffset = float(candidate.limit_offset)
                # Preserve all other broker fields, including TWS precautions.
                app.placeOrder(
                    prior.order_id, _build_submission_contract(imports, snapshot), order
                )
            while (
                not app.acknowledged.is_set()
                and not app.errors
                and monotonic() < deadline
            ):
                sleep(0.02)
            if app.errors or not app.acknowledged.is_set():
                raise ExecutionOutcomeUnknown(
                    "; ".join(app.errors)
                    or "TWS did not acknowledge the trailing change"
                )
            return PaperSubmission(
                (prior.order_id,), () if candidate.cancel else (prior.perm_id,)
            )
        finally:
            if app.isConnected():
                app.disconnect()
            if reader is not None:
                reader.join(timeout=0.5)

    @leased_client
    def submit_market(
        self,
        snapshot: BrokerSnapshot,
        plan: PositionMarketExitPlan,
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmission:
        require_paper_management_snapshot(snapshot)
        if (
            snapshot.selected != plan.selected
            or snapshot.contract != plan.contract
            or snapshot.position.quantity != plan.quantity
        ):
            raise ExecutionBlocked("the market exit no longer matches the position")
        imports = _load_ibapi()
        from ibapi.execution import ExecutionFilter
        from ibapi.order import Order

        class App(imports.EWrapper, imports.EClient):  # type: ignore[name-defined, misc]
            def __init__(self) -> None:
                imports.EWrapper.__init__(self)
                imports.EClient.__init__(self, self)
                (
                    self.ready,
                    self.orders_done,
                    self.positions_done,
                    self.fills_done,
                    self.ack,
                ) = (Event() for _ in range(5))
                self.next_id = self.order_id = self.perm_id = 0
                self.quantity = Decimal("0")
                self.positions = 0
                self.execution_ids: set[str] = set()
                self.errors: list[str] = []

            def nextValidId(self, order_id: int) -> None:
                self.next_id = int(order_id)
                self.ready.set()

            def position(
                self, account: str, contract: Any, position: Any, average_cost: Any
            ) -> None:
                if (
                    account == plan.selected.account
                    and int(contract.conId) == plan.selected.con_id
                ):
                    self.positions += 1
                    self.quantity = Decimal(str(position))
                    if Decimal(str(average_cost)) != plan.raw_average_cost:
                        self.errors.append(
                            "the position cost changed before market submission"
                        )

            def positionEnd(self) -> None:
                self.positions_done.set()

            def execDetails(self, req_id: int, contract: Any, execution: Any) -> None:
                if (
                    str(execution.acctNumber) == plan.selected.account
                    and int(contract.conId) == plan.selected.con_id
                ):
                    self.execution_ids.add(str(execution.execId))

            def execDetailsEnd(self, req_id: int) -> None:
                self.fills_done.set()

            def openOrder(
                self, order_id: int, contract: Any, order: Any, state: Any
            ) -> None:
                if (
                    int(contract.conId) != plan.selected.con_id
                    or str(order.account) != plan.selected.account
                ):
                    return
                if int(order_id) != self.order_id or self.order_id == 0:
                    self.errors.append(
                        "a closing order appeared before market submission"
                    )
                    return
                if (
                    str(order.orderType) == "MKT"
                    and str(order.action) == "SELL"
                    and Decimal(str(order.totalQuantity)) == plan.quantity
                    and str(order.tif) == "GTC"
                    and int(getattr(order, "clientId", -1)) == client_id
                    and str(getattr(state, "status", ""))
                    in {"Submitted", "PreSubmitted", "Filled"}
                    and int(order.permId) > 0
                ):
                    self.perm_id = int(order.permId)
                    self.ack.set()

            def openOrderEnd(self) -> None:
                self.orders_done.set()

            def error(self, req_id: int, *args: Any) -> None:
                code, message = _parse_error_arguments(args)
                if code not in _INFORMATIONAL_ERROR_CODES:
                    self.errors.append(f"IBKR error {code}: {message}")

        app = App()
        reader: Thread | None = None
        deadline = monotonic() + timeout_seconds
        sent = False
        try:
            app.connect(host, port, client_id)
            reader = Thread(
                target=app.run, name="ibkr-paper-position-market-reader", daemon=True
            )
            reader.start()
            if not app.ready.wait(max(0, deadline - monotonic())):
                raise ExecutionBlocked("TWS did not issue an order ID")
            app.reqAllOpenOrders()
            app.reqPositions()
            app.reqExecutions(71, ExecutionFilter())
            for event in (app.orders_done, app.positions_done, app.fills_done):
                if not event.wait(max(0, deadline - monotonic())):
                    raise ExecutionBlocked(
                        "the final market-exit verification is incomplete"
                    )
            if (
                app.errors
                or app.positions != 1
                or app.quantity != plan.quantity
                or frozenset(app.execution_ids) != plan.execution_ids
            ):
                raise ExecutionBlocked(
                    "; ".join(app.errors)
                    or "the position or fills changed before market submission"
                )
            order = Order()
            order.account, order.action, order.orderType, order.tif = (
                plan.selected.account,
                "SELL",
                "MKT",
                "GTC",
            )
            order.totalQuantity, order.transmit = plan.quantity, True
            app.order_id = app.next_id
            sent = True
            app.placeOrder(
                app.order_id, _build_submission_contract(imports, snapshot), order
            )
            while not app.ack.is_set() and not app.errors and monotonic() < deadline:
                sleep(0.02)
            if app.errors or not app.ack.is_set():
                raise ExecutionOutcomeUnknown(
                    "; ".join(app.errors) or "TWS did not acknowledge the market exit"
                )
            return PaperSubmission((app.order_id,), (app.perm_id,))
        except Exception as error:
            if sent and not isinstance(error, ExecutionOutcomeUnknown):
                raise ExecutionOutcomeUnknown(str(error)) from error
            raise
        finally:
            if app.isConnected():
                app.disconnect()
            if reader is not None:
                reader.join(timeout=0.5)

    @leased_client
    def submit_trailing(
        self,
        snapshot: BrokerSnapshot,
        plan: TrailingPlan,
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmission:
        """Submit one standalone paper trailing exit after the service's final check."""
        require_paper_management_snapshot(snapshot)
        fresh = plan_entire_position(snapshot, (), plan.request)
        if (
            plan.candidates
            or plan.quantity != fresh.quantity
            or plan.position_quantity != snapshot.position.quantity
            or plan.retained_orders != fresh.retained_orders
            or plan.outside_rth != supports_outside_rth(snapshot)
            or plan.request.tif != "GTC"
        ):
            raise ExecutionBlocked("the trailing plan no longer matches TWS")
        imports = _load_ibapi()
        from ibapi.order import Order

        class App(imports.EWrapper, imports.EClient):  # type: ignore[name-defined, misc]
            def __init__(self) -> None:
                imports.EWrapper.__init__(self)
                imports.EClient.__init__(self, self)
                self.ready = Event()
                self.acknowledged = Event()
                self.next_order_id = 0
                self.order_id = 0
                self.perm_id = 0
                self.errors: list[str] = []

            def nextValidId(self, order_id: int) -> None:
                self.next_order_id = int(order_id)
                self.ready.set()

            def openOrder(
                self, order_id: int, contract: Any, order: Any, state: Any
            ) -> None:
                del contract, state
                if int(order_id) != self.order_id:
                    return
                if str(getattr(order, "orderType", "")) != (
                    "TRAIL LIMIT" if plan.limit_offset is not None else "TRAIL"
                ):
                    self.errors.append("TWS returned a different trailing order type")
                    return
                self.perm_id = int(getattr(order, "permId", 0) or 0)
                if self.perm_id:
                    self.acknowledged.set()

            def error(self, req_id: int, *args: Any) -> None:
                code, message = _parse_error_arguments(args)
                if code not in _INFORMATIONAL_ERROR_CODES:
                    self.errors.append(
                        f"IBKR error reqId={req_id} code={code}: {message}"
                    )

        app = App()
        reader: Thread | None = None
        deadline = monotonic() + timeout_seconds
        try:
            app.connect(host, port, client_id)
            reader = Thread(
                target=app.run, name="ibkr-paper-trailing-reader", daemon=True
            )
            reader.start()
            if not app.ready.wait(max(0, deadline - monotonic())):
                raise ExecutionBlocked("TWS did not issue a next valid order ID")
            order = Order()
            order.action = "SELL"
            order.orderType = (
                "TRAIL LIMIT" if plan.limit_offset is not None else "TRAIL"
            )
            order.totalQuantity = plan.quantity
            order.account = snapshot.selected.account
            order.tif = plan.request.tif
            order.trailStopPrice = float(plan.initial_stop)
            order.outsideRth = plan.outside_rth
            order.transmit = True
            if plan.request.trail_unit == "percent":
                order.trailingPercent = float(plan.request.trail_value)
            else:
                order.auxPrice = float(plan.request.trail_value)
            if plan.limit_offset is not None:
                order.lmtPriceOffset = float(plan.limit_offset)
            app.order_id = app.next_order_id
            app.placeOrder(
                app.order_id, _build_submission_contract(imports, snapshot), order
            )
            while (
                not app.acknowledged.is_set()
                and not app.errors
                and monotonic() < deadline
            ):
                sleep(0.02)
            if app.errors:
                raise ExecutionOutcomeUnknown("; ".join(app.errors))
            if not app.acknowledged.is_set():
                raise ExecutionOutcomeUnknown(
                    "TWS did not acknowledge the trailing order"
                )
            return PaperSubmission((app.order_id,), (app.perm_id,))
        finally:
            if app.isConnected():
                app.disconnect()
            if reader is not None:
                reader.join(timeout=0.5)

    @leased_client
    def submit(
        self,
        snapshot: BrokerSnapshot,
        plan: PlanResult,
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmission:
        require_paper_execution_snapshot(snapshot, plan)
        if not snapshot.executions_complete:
            raise ExecutionBlocked(
                "A complete execution baseline is required for stop-first brackets"
            )
        imports = _load_ibapi()
        from ibapi.order import Order

        submitted: dict[int, Any] = {}

        # ibapi is loaded lazily so that planning remains usable without TWS.
        # Its runtime classes cannot be named in static type checking.
        class App(imports.EWrapper, imports.EClient):  # type: ignore[name-defined, misc]
            def __init__(self) -> None:
                imports.EWrapper.__init__(self)
                imports.EClient.__init__(self, self)
                self.ready = Event()
                self.acks: dict[int, int] = {}
                self.errors: list[str] = []
                self.next_order_id = 0
                self.outside_rth_ignored: set[int] = set()
                self.orders: dict[int, tuple[Any, Any, str]] = {}
                self.scan: dict[int, tuple[Any, Any, str]] | None = None
                self.orders_done, self.positions_done, self.fills_done = (
                    Event() for _ in range(3)
                )
                self.positions: list[tuple[Any, Decimal, Decimal]] = []
                self.execution_ids: set[str] = set()
                self.has_fill = False
                self.execution_request_id = 80

            def nextValidId(self, order_id: int) -> None:
                self.next_order_id = int(order_id)
                self.ready.set()

            def openOrder(
                self,
                order_id: int,
                contract: Any,
                order: Any,
                state: Any,
            ) -> None:
                self.next_order_id = max(self.next_order_id, int(order_id) + 1)
                if int(order_id) in submitted:
                    differences = list(
                        _bracket_order_mismatches(
                            order,
                            submitted[int(order_id)],
                            outside_rth_ignored=int(order_id)
                            in self.outside_rth_ignored,
                        )
                    )
                    if not _same_option_identity(contract, snapshot):
                        differences.append(
                            "contract identity differs from verified option"
                        )
                    if int(getattr(order, "clientId", -1)) != client_id:
                        differences.append(
                            f"clientId expected={client_id!r} "
                            f"observed={getattr(order, 'clientId', None)!r}"
                        )
                    if differences:
                        self.errors.append(
                            "The acknowledged bracket does not match "
                            "the submitted order: " + "; ".join(differences)
                        )
                perm_id = int(getattr(order, "permId", 0) or 0)
                if (
                    self.acks.get(int(order_id), 0) > 0
                    and self.acks[int(order_id)] != perm_id
                ):
                    self.errors.append("A bracket permanent order ID changed")
                self.acks[int(order_id)] = perm_id
                record = (
                    copy(contract),
                    copy(order),
                    str(getattr(state, "status", "")),
                )
                self.orders[int(order_id)] = record
                if self.scan is not None:
                    self.scan[int(order_id)] = record

            def openOrderEnd(self) -> None:
                self.orders_done.set()

            def orderStatus(
                self,
                order_id: int,
                status: str,
                filled: Any,
                remaining: Any,
                *args: Any,
            ) -> None:
                del args
                if order_id in self.orders:
                    contract, order, _ = self.orders[order_id]
                    self.orders[order_id] = (contract, order, status)
                if order_id in submitted:
                    if Decimal(str(filled)) > 0:
                        self.has_fill = True
                    if status in {
                        "Cancelled",
                        "ApiCancelled",
                        "PendingCancel",
                        "Inactive",
                        "Filled",
                    }:
                        self.errors.append(f"Order {order_id} reported {status}")

            def position(
                self, account: str, contract: Any, quantity: Any, cost: Any
            ) -> None:
                if (
                    account == snapshot.selected.account
                    and int(contract.conId) == snapshot.selected.con_id
                ):
                    self.positions.append(
                        (copy(contract), Decimal(str(quantity)), Decimal(str(cost)))
                    )

            def positionEnd(self) -> None:
                self.positions_done.set()

            def execDetails(self, req_id: int, contract: Any, execution: Any) -> None:
                if (
                    req_id == self.execution_request_id
                    and str(execution.acctNumber) == snapshot.selected.account
                    and int(contract.conId) == snapshot.selected.con_id
                ):
                    self.execution_ids.add(str(execution.execId))

            def execDetailsEnd(self, req_id: int) -> None:
                if req_id == self.execution_request_id:
                    self.fills_done.set()

            def error(self, req_id: int, *args: Any) -> None:
                code, message = _parse_error_arguments(args)
                if code == 2109 and int(req_id) in submitted:
                    self.outside_rth_ignored.add(int(req_id))
                if code not in _INFORMATIONAL_ERROR_CODES:
                    self.errors.append(
                        f"IBKR error reqId={req_id} code={code}: {message}"
                    )

        app = App()
        reader: Thread | None = None
        deadline = monotonic() + timeout_seconds
        active_group = ""
        phase = "connection"
        try:
            app.connect(host, port, client_id)
            reader = Thread(
                target=app.run,
                name="ibkr-paper-execution-reader",
                daemon=True,
            )
            reader.start()
            if not app.ready.wait(max(0, deadline - monotonic())):
                raise ExecutionBlocked("TWS did not issue a next valid order ID")

            contract = _build_submission_contract(imports, snapshot)
            ids: list[int] = []
            sent_ids: list[int] = []
            active_group = ""
            phase = "before first stop"
            _verify_bracket_snapshot(
                app, snapshot, submitted, sent_ids, client_id, deadline
            )
            base_order_id = app.next_order_id
            for index, pair in enumerate(plan.pairs):
                active_group = pair.stop.logical_oca_group
                # Allocate IDs in send order. Receipts retain target/stop order
                # for journal layer matching, even though the stop has the lower ID.
                stop_id = base_order_id + index * 2
                target_id = stop_id + 1
                ids.extend((target_id, stop_id))
                for intent, order_id in (
                    (pair.stop, stop_id),
                    (pair.target, target_id),
                ):
                    phase = (
                        "stop acceptance"
                        if order_id == stop_id
                        else "target acceptance"
                    )
                    order = Order()
                    order.action = intent.action
                    order.orderType = intent.order_type
                    order.totalQuantity = intent.quantity
                    order.account = intent.account
                    order.tif = intent.tif
                    order.ocaGroup = intent.logical_oca_group
                    order.ocaType = intent.oca_type
                    order.outsideRth = intent.outside_rth
                    order.transmit = True
                    if intent.order_type == "LMT":
                        order.lmtPrice = float(intent.rounded_price)
                    elif intent.order_type == "STP LMT":
                        if intent.limit_price is None:
                            raise ExecutionBlocked("the stop-limit price is missing")
                        order.auxPrice = float(intent.rounded_price)
                        order.lmtPrice = float(intent.limit_price)
                    else:
                        order.auxPrice = float(intent.rounded_price)
                    _wait_owned_orders(app, submitted, sent_ids, deadline)
                    submitted[order_id] = copy(order)
                    sent_ids.append(order_id)
                    app.placeOrder(order_id, contract, order)
                    _wait_owned_orders(app, submitted, sent_ids, deadline)
                    phase = "before target" if order_id == stop_id else "after target"
                    _verify_bracket_snapshot(
                        app, snapshot, submitted, sent_ids, client_id, deadline
                    )
                    # A callback can arrive as a fresh read finishes. Check again
                    # before sending any next leg; post-target reads catch races.
                    _wait_owned_orders(app, submitted, sent_ids, deadline)
            return PaperSubmission(
                tuple(ids),
                tuple(app.acks[order_id] for order_id in ids),
            )
        except Exception as error:
            if submitted:
                details = "; ".join(
                    f"{submitted[oid].orderType} {oid}: "
                    f"{app.orders.get(oid, (None, None, 'missing'))[2]}, "
                    f"permId={app.acks.get(oid, 0)}"
                    for oid in submitted
                )
                raise ExecutionOutcomeUnknown(
                    f"Possible one-sided bracket · {active_group} · {phase}: {error}. "
                    f"Observed orders: {details}. Check TWS; "
                    "no further tranches were sent"
                ) from error
            raise
        finally:
            if app.isConnected():
                app.disconnect()
            if reader is not None:
                reader.join(timeout=0.5)

    @leased_client
    def cancel_pair(
        self,
        snapshot: BrokerSnapshot,
        candidate: MarketExitCandidate,
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmission:
        """Cancel one exact owned OCA pair and verify both legs disappeared."""
        if (
            snapshot.selected.account != candidate.account
            or snapshot.selected.con_id != candidate.con_id
            or candidate.client_id != client_id
            or candidate.quantity <= 0
            or not candidate.oca_group
        ):
            raise ExecutionBlocked("the cancellation candidate does not match TWS")
        imports = _load_ibapi()
        selected_ids = {candidate.target_order_id, candidate.stop_order_id}

        class App(imports.EWrapper, imports.EClient):  # type: ignore[name-defined, misc]
            def __init__(self) -> None:
                imports.EWrapper.__init__(self)
                imports.EClient.__init__(self, self)
                self.ready = Event()
                self.cancelled: set[int] = set()
                self.open_orders_done = Event()
                self.active_order_ids: set[int] = set()
                self.errors: list[str] = []

            def nextValidId(self, _order_id: int) -> None:
                self.ready.set()

            def openOrder(
                self, order_id: int, contract: Any, order: Any, state: Any
            ) -> None:
                del contract, order, state
                self.active_order_ids.add(int(order_id))

            def openOrderEnd(self) -> None:
                self.open_orders_done.set()

            def orderStatus(self, order_id: int, *args: Any) -> None:
                current_id = int(order_id)
                status = str(args[0]) if args else ""
                if current_id not in selected_ids:
                    return
                if status in {"Cancelled", "ApiCancelled"}:
                    self.cancelled.add(current_id)
                elif status in {"Filled", "Inactive"}:
                    self.errors.append(
                        f"selected OCA order {current_id} changed to {status} "
                        "while cancelling"
                    )

            def error(self, req_id: int, *args: Any) -> None:
                code, message = _parse_error_arguments(args)
                if code == 202 and req_id in selected_ids:
                    self.cancelled.add(int(req_id))
                    return
                if code not in _INFORMATIONAL_ERROR_CODES:
                    self.errors.append(
                        f"IBKR error reqId={req_id} code={code}: {message}"
                    )

        app = App()
        reader: Thread | None = None
        started = monotonic()
        context = current_cancellation_context.get()
        run_id, bracket_number = context if context is not None else (uuid4().hex, 1)

        def trace(event: str, **fields: Any) -> None:
            record_cancellation_event(
                event,
                run_id=run_id,
                bracket_number=bracket_number,
                elapsed_ms=round((monotonic() - started) * 1000, 1),
                **fields,
            )

        deadline = started + timeout_seconds
        outcome = "unknown"
        try:
            trace("transport_connect_start", timeout_seconds=timeout_seconds)
            app.connect(host, port, client_id)
            reader = Thread(
                target=app.run,
                name="ibkr-paper-oca-cancellation-reader",
                daemon=True,
            )
            reader.start()
            if not app.ready.wait(max(0, deadline - monotonic())):
                raise ExecutionBlocked("TWS did not issue a next valid order ID")
            trace("transport_ready")
            for order_id in sorted(selected_ids):
                _cancel_order(app, order_id)
            trace("cancel_requests_sent", leg_count=len(selected_ids))
            while (
                len(app.cancelled) != len(selected_ids)
                and not app.errors
                and monotonic() < deadline
            ):
                sleep(0.02)
            if app.errors:
                raise ExecutionBlocked("; ".join(app.errors))
            if len(app.cancelled) != len(selected_ids):
                raise ExecutionOutcomeUnknown(
                    "TWS did not acknowledge cancellation of both selected OCA legs "
                    "before the deadline"
                )
            trace("cancel_acknowledged", leg_count=len(app.cancelled))
            app.active_order_ids.clear()
            app.open_orders_done.clear()
            trace("open_orders_check_start")
            app.reqOpenOrders()
            while (
                not app.open_orders_done.is_set()
                and not app.errors
                and monotonic() < deadline
            ):
                sleep(0.02)
            if app.errors:
                raise ExecutionBlocked("; ".join(app.errors))
            if not app.open_orders_done.is_set():
                raise ExecutionOutcomeUnknown(
                    "TWS did not complete the post-cancellation open-order check"
                )
            if selected_ids & app.active_order_ids:
                raise ExecutionBlocked(
                    "the selected OCA pair remains active after cancellation"
                )
            trace("open_orders_check_complete", selected_pair_absent=True)
            outcome = "confirmed"
            return PaperSubmission(order_ids=tuple(sorted(selected_ids)), perm_ids=())
        except (ExecutionBlocked, ExecutionOutcomeUnknown) as error:
            outcome = type(error).__name__
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            trace("transport_finished", outcome=outcome)
            if app.isConnected():
                app.disconnect()
            if reader is not None:
                reader.join(timeout=0.5)

    @leased_client
    def cancel_pair_then_submit_market(
        self,
        snapshot: BrokerSnapshot,
        candidate: MarketExitCandidate,
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmission:
        """Cancel one owned OCA pair, verify it, then submit a standalone MKT.

        The MKT is intentionally not assigned to an OCA group.  Altering an
        existing LMT into MKT is not a supported safe modification and proved
        capable of affecting another layer in paper TWS.
        """
        if (
            snapshot.selected.account != candidate.account
            or snapshot.selected.con_id != candidate.con_id
            or candidate.client_id != client_id
            or candidate.quantity <= 0
            or candidate.tif != "GTC"
            or not candidate.oca_group
        ):
            raise ExecutionBlocked("the market-exit candidate does not match TWS")
        imports = _load_ibapi()
        from ibapi.order import Order

        class App(imports.EWrapper, imports.EClient):  # type: ignore[name-defined, misc]
            def __init__(self) -> None:
                imports.EWrapper.__init__(self)
                imports.EClient.__init__(self, self)
                self.ready = Event()
                self.cancelled: set[int] = set()
                self.open_orders_done = Event()
                self.active_order_ids: set[int] = set()
                self.market_order_id = 0
                self.market_perm_id = 0
                self.market_acknowledged = Event()
                self.next_order_id = 0
                self.errors: list[str] = []

            def nextValidId(self, order_id: int) -> None:
                self.next_order_id = int(order_id)
                self.ready.set()

            def openOrder(
                self,
                order_id: int,
                contract: Any,
                order: Any,
                state: Any,
            ) -> None:
                del contract, state
                current_id = int(order_id)
                self.active_order_ids.add(current_id)
                if current_id != self.market_order_id:
                    return
                perm_id = int(getattr(order, "permId", 0) or 0)
                if not perm_id or str(getattr(order, "orderType", "")) != "MKT":
                    self.errors.append("TWS did not acknowledge a standalone MKT order")
                    return
                self.market_perm_id = perm_id
                self.market_acknowledged.set()

            def openOrderEnd(self) -> None:
                self.open_orders_done.set()

            def orderStatus(self, order_id: int, *args: Any) -> None:
                current_id = int(order_id)
                status = str(args[0]) if args else ""
                if current_id in {candidate.target_order_id, candidate.stop_order_id}:
                    if status in {"Cancelled", "ApiCancelled"}:
                        self.cancelled.add(current_id)
                    elif status in {"Filled", "Inactive"}:
                        self.errors.append(
                            f"selected OCA order {current_id} changed to {status} "
                            "while cancelling"
                        )

            def error(self, req_id: int, *args: Any) -> None:
                code, message = _parse_error_arguments(args)
                if code == 202 and req_id in {
                    candidate.target_order_id,
                    candidate.stop_order_id,
                }:
                    self.cancelled.add(int(req_id))
                    return
                if code not in _INFORMATIONAL_ERROR_CODES:
                    self.errors.append(
                        f"IBKR error reqId={req_id} code={code}: {message}"
                    )

        app = App()
        reader: Thread | None = None
        deadline = monotonic() + timeout_seconds
        try:
            app.connect(host, port, client_id)
            reader = Thread(
                target=app.run,
                name="ibkr-paper-market-exit-reader",
                daemon=True,
            )
            reader.start()
            if not app.ready.wait(max(0, deadline - monotonic())):
                raise ExecutionBlocked("TWS did not issue a next valid order ID")

            _cancel_order(app, candidate.target_order_id)
            _cancel_order(app, candidate.stop_order_id)
            while len(app.cancelled) != 2 and not app.errors and monotonic() < deadline:
                sleep(0.02)
            if app.errors:
                raise ExecutionBlocked("; ".join(app.errors))
            if len(app.cancelled) != 2:
                raise ExecutionOutcomeUnknown(
                    "TWS did not acknowledge cancellation of both selected OCA legs "
                    "before the deadline"
                )
            app.active_order_ids.clear()
            app.open_orders_done.clear()
            app.reqOpenOrders()
            while (
                not app.open_orders_done.is_set()
                and not app.errors
                and monotonic() < deadline
            ):
                sleep(0.02)
            if app.errors:
                raise ExecutionBlocked("; ".join(app.errors))
            if not app.open_orders_done.is_set():
                raise ExecutionOutcomeUnknown(
                    "TWS did not complete the post-cancellation open-order check"
                )
            selected_pair_ids = {
                candidate.target_order_id,
                candidate.stop_order_id,
            }
            if selected_pair_ids & app.active_order_ids:
                raise ExecutionBlocked(
                    "the selected OCA pair remains active after cancellation"
                )

            order = Order()
            order.action = "SELL"
            order.orderType = "MKT"
            order.totalQuantity = candidate.quantity
            order.account = candidate.account
            order.tif = "GTC"
            order.transmit = True
            app.market_order_id = app.next_order_id
            app.placeOrder(
                app.market_order_id,
                _build_submission_contract(imports, snapshot),
                order,
            )
            while (
                not app.market_acknowledged.is_set()
                and not app.errors
                and monotonic() < deadline
            ):
                sleep(0.02)
            if app.errors:
                raise ExecutionBlocked("; ".join(app.errors))
            if not app.market_acknowledged.is_set():
                raise ExecutionOutcomeUnknown(
                    "TWS did not acknowledge the standalone MKT order before "
                    "the deadline"
                )
            return PaperSubmission(
                order_ids=(app.market_order_id,),
                perm_ids=(app.market_perm_id,),
            )
        finally:
            if app.isConnected():
                app.disconnect()
            if reader is not None:
                reader.join(timeout=0.5)

    @leased_client
    def cancel_pairs_then_submit_market(
        self,
        snapshot: BrokerSnapshot,
        candidates: tuple[MarketExitCandidate, ...],
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmission:
        """Cancel all selected owned pairs, then submit one total MKT exit."""
        if not candidates or (
            len({candidate.oca_group for candidate in candidates}) != len(candidates)
        ):
            raise ExecutionBlocked(
                "selected market-exit layers are missing or duplicate"
            )
        if any(
            candidate.account != snapshot.selected.account
            or candidate.con_id != snapshot.selected.con_id
            or candidate.client_id != client_id
            or candidate.quantity <= 0
            or candidate.tif != "GTC"
            for candidate in candidates
        ):
            raise ExecutionBlocked("a selected market-exit layer does not match TWS")
        imports = _load_ibapi()
        from ibapi.order import Order

        selected_ids = {
            order_id
            for candidate in candidates
            for order_id in (candidate.target_order_id, candidate.stop_order_id)
        }

        class App(imports.EWrapper, imports.EClient):  # type: ignore[name-defined, misc]
            def __init__(self) -> None:
                imports.EWrapper.__init__(self)
                imports.EClient.__init__(self, self)
                self.ready = Event()
                self.cancelled: set[int] = set()
                self.open_orders_done = Event()
                self.active_order_ids: set[int] = set()
                self.market_order_id = 0
                self.market_perm_id = 0
                self.market_acknowledged = Event()
                self.next_order_id = 0
                self.errors: list[str] = []

            def nextValidId(self, order_id: int) -> None:
                self.next_order_id = int(order_id)
                self.ready.set()

            def openOrder(
                self, order_id: int, contract: Any, order: Any, state: Any
            ) -> None:
                del contract, state
                current_id = int(order_id)
                self.active_order_ids.add(current_id)
                if current_id != self.market_order_id:
                    return
                perm_id = int(getattr(order, "permId", 0) or 0)
                if not perm_id or str(getattr(order, "orderType", "")) != "MKT":
                    self.errors.append("TWS did not acknowledge a standalone MKT order")
                    return
                self.market_perm_id = perm_id
                self.market_acknowledged.set()

            def openOrderEnd(self) -> None:
                self.open_orders_done.set()

            def orderStatus(self, order_id: int, *args: Any) -> None:
                current_id = int(order_id)
                status = str(args[0]) if args else ""
                if current_id not in selected_ids:
                    return
                if status in {"Cancelled", "ApiCancelled"}:
                    self.cancelled.add(current_id)
                elif status in {"Filled", "Inactive"}:
                    self.errors.append(
                        f"selected OCA order {current_id} changed to {status} "
                        "while cancelling"
                    )

            def error(self, req_id: int, *args: Any) -> None:
                code, message = _parse_error_arguments(args)
                if code == 202 and req_id in selected_ids:
                    self.cancelled.add(int(req_id))
                    return
                if code not in _INFORMATIONAL_ERROR_CODES:
                    self.errors.append(
                        f"IBKR error reqId={req_id} code={code}: {message}"
                    )

        app = App()
        reader: Thread | None = None
        deadline = monotonic() + timeout_seconds
        try:
            app.connect(host, port, client_id)
            reader = Thread(
                target=app.run,
                name="ibkr-paper-bulk-market-exit-reader",
                daemon=True,
            )
            reader.start()
            if not app.ready.wait(max(0, deadline - monotonic())):
                raise ExecutionBlocked("TWS did not issue a next valid order ID")
            for order_id in sorted(selected_ids):
                _cancel_order(app, order_id)
            while (
                len(app.cancelled) != len(selected_ids)
                and not app.errors
                and monotonic() < deadline
            ):
                sleep(0.02)
            if app.errors:
                raise ExecutionBlocked("; ".join(app.errors))
            if len(app.cancelled) != len(selected_ids):
                raise ExecutionOutcomeUnknown(
                    "TWS did not acknowledge cancellation of every selected OCA leg"
                )
            app.active_order_ids.clear()
            app.open_orders_done.clear()
            app.reqOpenOrders()
            while (
                not app.open_orders_done.is_set()
                and not app.errors
                and monotonic() < deadline
            ):
                sleep(0.02)
            if app.errors:
                raise ExecutionBlocked("; ".join(app.errors))
            if not app.open_orders_done.is_set():
                raise ExecutionOutcomeUnknown(
                    "TWS did not complete the post-cancellation open-order check"
                )
            if selected_ids & app.active_order_ids:
                raise ExecutionBlocked(
                    "a selected OCA leg remains active after cancellation"
                )
            order = Order()
            order.action = "SELL"
            order.orderType = "MKT"
            order.totalQuantity = sum(candidate.quantity for candidate in candidates)
            order.account = candidates[0].account
            order.tif = "GTC"
            order.transmit = True
            app.market_order_id = app.next_order_id
            app.placeOrder(
                app.market_order_id,
                _build_submission_contract(imports, snapshot),
                order,
            )
            while (
                not app.market_acknowledged.is_set()
                and not app.errors
                and monotonic() < deadline
            ):
                sleep(0.02)
            if app.errors:
                raise ExecutionBlocked("; ".join(app.errors))
            if not app.market_acknowledged.is_set():
                raise ExecutionOutcomeUnknown(
                    "TWS did not acknowledge the standalone MKT order before "
                    "the deadline"
                )
            return PaperSubmission(
                order_ids=(app.market_order_id,), perm_ids=(app.market_perm_id,)
            )
        finally:
            if app.isConnected():
                app.disconnect()
            if reader is not None:
                reader.join(timeout=0.5)

    @leased_client
    def modify_prices(
        self,
        snapshot: BrokerSnapshot,
        candidates: tuple[PriceUpdateCandidate, ...],
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmission:
        """Amend only price fields on exact, same-client app-owned OCA orders."""
        if not candidates:
            raise ExecutionBlocked("no app-owned OCA price updates were supplied")
        if any(
            candidate.layer.account != snapshot.selected.account
            or candidate.layer.con_id != snapshot.selected.con_id
            or candidate.layer.client_id != client_id
            or (candidate.target_price is None and candidate.stop_price is None)
            for candidate in candidates
        ):
            raise ExecutionBlocked("a selected price update does not match TWS")
        if any(
            candidate.stop_price is not None
            and candidate.layer.stop_order_type == "STP LMT"
            and (
                candidate.stop_limit_price is None
                or candidate.prior_stop_limit_price is None
            )
            for candidate in candidates
        ):
            raise ExecutionBlocked("both stop-limit prices are required for amendment")
        selected_ids = {
            order_id
            for candidate in candidates
            for order_id, price in (
                (candidate.layer.target_order_id, candidate.target_price),
                (candidate.layer.stop_order_id, candidate.stop_price),
            )
            if price is not None
        }
        if len(selected_ids) != sum(
            int(candidate.target_price is not None)
            + int(candidate.stop_price is not None)
            for candidate in candidates
        ):
            raise ExecutionBlocked("selected price amendments are ambiguous")
        imports = _load_ibapi()

        expected: dict[
            int, tuple[str, Decimal, int, Decimal | None, Decimal | None]
        ] = {}
        from ibapi.const import UNSET_DOUBLE, UNSET_INTEGER

        for candidate in candidates:
            if candidate.target_price is not None:
                expected[candidate.layer.target_order_id] = (
                    "LMT",
                    candidate.target_price,
                    candidate.layer.target_perm_id,
                    candidate.prior_target_price,
                    None,
                )
            if candidate.stop_price is not None:
                expected[candidate.layer.stop_order_id] = (
                    candidate.layer.stop_order_type,
                    candidate.stop_price,
                    candidate.layer.stop_perm_id,
                    candidate.prior_stop_price,
                    candidate.stop_limit_price,
                )

        attempt_id = uuid4().hex[:12]
        if not record_price_update_event(
            "writer_attempt",
            attempt_id=attempt_id,
            con_id=snapshot.selected.con_id,
            client_id=client_id,
            expected={
                order_id: {"type": kind, "price": str(price), "perm_id": perm_id}
                for order_id, (kind, price, perm_id, _prior, limit) in expected.items()
            },
        ):
            raise ExecutionBlocked("price update diagnostic file is unavailable")

        class App(imports.EWrapper, imports.EClient):  # type: ignore[name-defined, misc]
            def __init__(self) -> None:
                imports.EWrapper.__init__(self)
                imports.EClient.__init__(self, self)
                self.ready = Event()
                self.open_orders_done = Event()
                self.orders: dict[int, Any] = {}
                self.acks: dict[int, int] = {}
                self.errors: list[str] = []
                self.phase = "connect"
                self.trace_failed = False

            def trace(self, event: str, **fields: Any) -> None:
                if not record_price_update_event(
                    event, attempt_id=attempt_id, phase=self.phase, **fields
                ):
                    self.trace_failed = True

            def nextValidId(self, _order_id: int) -> None:
                self.trace("next_valid_id", next_order_id=int(_order_id))
                self.ready.set()

            def openOrder(
                self, order_id: int, contract: Any, order: Any, state: Any
            ) -> None:
                del contract
                current_id = int(order_id)
                if current_id not in expected:
                    return
                order_type, price, expected_perm_id, _prior, limit = expected[
                    current_id
                ]
                perm_id = int(getattr(order, "permId", 0) or 0)
                received_type = str(getattr(order, "orderType", ""))
                raw_price = (
                    getattr(order, "lmtPrice", None)
                    if order_type == "LMT"
                    else getattr(order, "auxPrice", None)
                )
                self.trace(
                    "open_order",
                    order_id=current_id,
                    perm_id=perm_id,
                    order_type=received_type,
                    observed_price=str(raw_price),
                    expected_price=str(price),
                    transmit=getattr(order, "transmit", None),
                    status=str(getattr(state, "status", "")),
                    warning=str(getattr(state, "warningText", "")),
                )
                try:
                    received_price = Decimal(str(raw_price))
                except (InvalidOperation, ValueError):
                    self.errors.append(
                        f"TWS returned an invalid price for selected order {current_id}"
                    )
                    return
                if (
                    str(getattr(order, "action", "")) != "SELL"
                    or received_type != order_type
                    or perm_id != expected_perm_id
                ):
                    self.errors.append(
                        f"selected order {current_id} no longer matches its "
                        "app-owned OCA leg"
                    )
                    return
                self.orders[current_id] = order
                try:
                    received_limit = Decimal(str(getattr(order, "lmtPrice", None)))
                except (InvalidOperation, ValueError):
                    received_limit = None
                if received_price == price and (
                    limit is None or received_limit == limit
                ):
                    self.acks[current_id] = perm_id

            def openOrderEnd(self) -> None:
                self.trace(
                    "open_order_end",
                    observed_order_ids=sorted(self.orders),
                    matching_price_ids=sorted(self.acks),
                )
                self.open_orders_done.set()

            def orderStatus(self, order_id: int, status: str, *args: Any) -> None:
                if int(order_id) not in expected:
                    return
                self.trace(
                    "order_status",
                    order_id=int(order_id),
                    status=status,
                    remaining=str(args[1]) if len(args) > 1 else "",
                    perm_id=str(args[3]) if len(args) > 3 else "",
                    why_held=str(args[7]) if len(args) > 7 else "",
                )

            def error(self, req_id: int, *args: Any) -> None:
                code, message = _parse_error_arguments(args)
                self.trace(
                    "tws_error",
                    req_id=req_id,
                    code=code,
                    message=message,
                    advanced_rejection=str(args[2]) if len(args) > 2 else "",
                )
                if code not in _INFORMATIONAL_ERROR_CODES:
                    self.errors.append(
                        f"IBKR error reqId={req_id} code={code}: {message}"
                    )

        app = App()
        reader: Thread | None = None
        deadline = monotonic() + timeout_seconds
        try:
            app.trace("connect_requested", host=host, port=port, client_id=client_id)
            app.connect(host, port, client_id)
            reader = Thread(
                target=app.run,
                name="ibkr-paper-price-update-reader",
                daemon=True,
            )
            reader.start()
            if not app.ready.wait(max(0, deadline - monotonic())):
                raise ExecutionBlocked("TWS did not issue a next valid order ID")
            app.phase = "read_before"
            app.trace("request_open_orders")
            app.reqOpenOrders()
            while (
                not app.open_orders_done.is_set()
                and not app.errors
                and monotonic() < deadline
            ):
                sleep(0.02)
            if app.errors:
                raise ExecutionBlocked("; ".join(app.errors))
            if not app.open_orders_done.is_set() or set(app.orders) != selected_ids:
                raise ExecutionBlocked(
                    "selected app-owned OCA orders are no longer open"
                )
            for order_id, (kind, _price, _perm, prior, limit) in expected.items():
                order = app.orders[order_id]
                try:
                    observed = Decimal(
                        str(order.lmtPrice if kind == "LMT" else order.auxPrice)
                    )
                except (InvalidOperation, ValueError) as error:
                    raise ExecutionBlocked(
                        "selected OCA price is invalid before write"
                    ) from error
                if prior is not None and observed != prior:
                    raise ExecutionBlocked("selected OCA price changed before write")
                if kind == "STP LMT":
                    candidate = next(
                        item
                        for item in candidates
                        if item.layer.stop_order_id == order_id
                    )
                    try:
                        prior_limit = Decimal(str(order.lmtPrice))
                    except (InvalidOperation, ValueError):
                        prior_limit = None
                    if (
                        candidate.prior_stop_limit_price is None
                        or prior_limit != candidate.prior_stop_limit_price
                        or limit is None
                    ):
                        raise ExecutionBlocked(
                            "selected stop-limit price changed before write"
                        )
            app.trace("prewrite_orders_verified", order_ids=sorted(app.orders))

            contract = _build_submission_contract(imports, snapshot)
            app.phase = "submit"
            for order_id in sorted(selected_ids):
                order = app.orders[order_id]
                order_type, price, _perm_id, _prior, limit = expected[order_id]
                prior_price = (
                    getattr(order, "lmtPrice", None)
                    if order_type == "LMT"
                    else getattr(order, "auxPrice", None)
                )
                prior_transmit = getattr(order, "transmit", None)
                if order_type == "LMT":
                    order.lmtPrice = float(price)
                else:
                    order.auxPrice = float(price)
                    if order_type == "STP LMT" and limit is not None:
                        order.lmtPrice = float(limit)
                # TWS may populate VOL-only fields on an openOrder callback
                # even for a plain app-owned LMT/STP. Re-sending those values
                # makes placeOrder fail validation with code 321.
                order.volatility = UNSET_DOUBLE
                order.volatilityType = UNSET_INTEGER
                # The target was originally staged with transmit=False while
                # its OCA stop transmitted the pair. An amendment of that
                # target must itself be sent through TWS precautions.
                order.transmit = True
                app.trace(
                    "place_order",
                    order_id=order_id,
                    order_type=order_type,
                    prior_price=str(prior_price),
                    requested_price=str(price),
                    prior_transmit=prior_transmit,
                    submitted_transmit=order.transmit,
                )
                app.placeOrder(order_id, contract, order)
            while (
                len(app.acks) != len(selected_ids)
                and not app.errors
                and monotonic() < deadline
            ):
                sleep(0.02)
            if app.errors:
                raise ExecutionBlocked("; ".join(app.errors))
            if len(app.acks) != len(selected_ids):
                raise ExecutionOutcomeUnknown(
                    "TWS did not acknowledge every selected price amendment"
                )
            app.trace("write_callbacks_complete", matching_price_ids=sorted(app.acks))
            acknowledged = dict(app.acks)
            app.acks.clear()
            app.orders.clear()
            app.open_orders_done.clear()
            app.phase = "read_after"
            app.trace("request_open_orders")
            app.reqOpenOrders()
            while (
                not app.open_orders_done.is_set()
                and not app.errors
                and monotonic() < deadline
            ):
                sleep(0.02)
            if app.errors:
                raise ExecutionBlocked("; ".join(app.errors))
            if (
                not app.open_orders_done.is_set()
                or set(app.orders) != selected_ids
                or set(app.acks) != selected_ids
            ):
                raise ExecutionOutcomeUnknown(
                    "TWS post-update check did not show every requested price"
                )
            if app.trace_failed:
                raise ExecutionOutcomeUnknown(
                    "price update diagnostic file stopped accepting events"
                )
            app.trace(
                "writer_outcome",
                outcome="verified",
                order_ids=sorted(selected_ids),
            )
            if app.trace_failed:
                raise ExecutionOutcomeUnknown(
                    "price update diagnostic file stopped accepting events"
                )
            return PaperSubmission(
                order_ids=tuple(sorted(selected_ids)),
                perm_ids=tuple(
                    acknowledged[order_id] for order_id in sorted(selected_ids)
                ),
            )
        except Exception as error:
            app.trace(
                "writer_outcome",
                outcome=(
                    "unknown"
                    if isinstance(error, ExecutionOutcomeUnknown)
                    else "blocked"
                ),
                error_type=type(error).__name__,
                reason=str(error),
            )
            raise
        finally:
            if app.isConnected():
                app.trace("disconnect")
                app.disconnect()
            if reader is not None:
                reader.join(timeout=0.5)


def _build_submission_contract(imports: Any, snapshot: BrokerSnapshot) -> Any:
    """Build the minimal IBKR order contract from a freshly verified conId.

    The snapshot has already verified every option identity field.  Re-sending
    local-symbol and trading-class aliases can conflict with IBKR's canonical
    underlying symbol (for example, SPXW option aliases resolve to SPX).
    ``conId`` plus the verified destination exchange is the unambiguous order
    contract and avoids that secondary-resolution path.
    """
    contract = imports.Contract()
    contract.conId = snapshot.contract.con_id
    contract.exchange = snapshot.contract.exchange
    return contract


def _wait_owned_orders(
    app: Any, submitted: dict[int, Any], ids: list[int], deadline: float
) -> None:
    while app.isConnected() and not app.errors and monotonic() < deadline:
        if app.has_fill:
            break
        if all(
            oid in app.orders
            and app.orders[oid][2] in {"Submitted", "PreSubmitted"}
            and app.acks.get(oid, 0) > 0
            for oid in ids
        ):
            return
        sleep(0.02)
    raise ExecutionOutcomeUnknown(
        "; ".join(app.errors)
        or "Missing order acceptance, fill, timeout, or disconnect"
    )


def _same_option_identity(contract: Any, snapshot: BrokerSnapshot) -> bool:
    c = snapshot.contract
    return (
        int(getattr(contract, "conId", 0)) == c.con_id
        and str(getattr(contract, "secType", "")) == c.sec_type
        and str(getattr(contract, "lastTradeDateOrContractMonth", "")) == c.expiry
        and Decimal(str(getattr(contract, "strike", "NaN"))) == c.strike
        and str(getattr(contract, "right", "")) == c.right
        and Decimal(str(getattr(contract, "multiplier", "NaN"))) == c.multiplier
        and str(getattr(contract, "currency", "")) == c.currency
        and str(getattr(contract, "tradingClass", "")) == c.trading_class
    )


def _bracket_order_fields(order: Any) -> dict[str, Any]:
    fields = dict(
        (field, getattr(order, field, None))
        for field in (
            "account",
            "action",
            "orderType",
            "totalQuantity",
            "tif",
            "ocaGroup",
            "ocaType",
            "parentId",
            "lmtPrice",
            "auxPrice",
            "outsideRth",
        )
    )
    # TWS represents an unused price as zero instead of the API unset sentinel.
    # Only inactive price fields are normalized; stop/limit prices remain exact.
    from ibapi.const import UNSET_DOUBLE

    unused = (
        "lmtPrice"
        if fields["orderType"] == "STP"
        else "auxPrice"
        if fields["orderType"] == "LMT"
        else None
    )
    if unused and fields[unused] in {None, 0, UNSET_DOUBLE}:
        fields[unused] = None
    return fields


def _bracket_order_mismatches(
    order: Any, expected: Any, *, outside_rth_ignored: bool = False
) -> tuple[str, ...]:
    observed_fields = _bracket_order_fields(order)
    expected_fields = _bracket_order_fields(expected)
    differences = []
    for field, value in expected_fields.items():
        observed = observed_fields[field]
        if value == observed:
            continue
        if (
            field == "outsideRth"
            and outside_rth_ignored
            and value is True
            and observed is False
        ):
            continue
        if field == "account":
            value, observed = redact_account(str(value)), redact_account(str(observed))
        differences.append(f"{field} expected={value!r} observed={observed!r}")
    return tuple(differences)


def _verify_bracket_snapshot(
    app: Any,
    snapshot: BrokerSnapshot,
    submitted: dict[int, Any],
    ids: list[int],
    client_id: int,
    deadline: float,
) -> None:
    from ibapi.execution import ExecutionFilter

    if not snapshot.executions_complete:
        raise ExecutionOutcomeUnknown(
            "Execution baseline is incomplete; check TWS before transmitting"
        )
    app.scan = {}
    app.orders_done.clear()
    app.positions_done.clear()
    app.fills_done.clear()
    app.positions.clear()
    app.execution_ids.clear()
    app.reqAllOpenOrders()
    app.reqPositions()
    execution_filter = ExecutionFilter()
    execution_filter.acctCode = snapshot.selected.account
    app.execution_request_id += 1
    app.reqExecutions(app.execution_request_id, execution_filter)
    try:
        while (
            not all(
                event.is_set()
                for event in (app.orders_done, app.positions_done, app.fills_done)
            )
            and app.isConnected()
            and not app.errors
            and monotonic() < deadline
        ):
            sleep(0.02)
        if (
            not app.isConnected()
            or app.errors
            or app.has_fill
            or not all(
                event.is_set()
                for event in (app.orders_done, app.positions_done, app.fills_done)
            )
        ):
            raise ExecutionOutcomeUnknown("Fresh bracket checks failed; check TWS")
        if len(app.positions) != 1:
            raise ExecutionOutcomeUnknown(
                "The position is missing or ambiguous; check TWS"
            )
        contract, quantity, cost = app.positions[0]
        if (
            not _same_option_identity(contract, snapshot)
            or quantity != snapshot.position.quantity
            or cost != snapshot.position.raw_average_cost
        ):
            raise ExecutionOutcomeUnknown(
                "The position changed during bracket verification; check TWS"
            )
        if app.execution_ids != {
            e.exec_id
            for e in snapshot.executions
            if e.account == snapshot.selected.account
            and e.con_id == snapshot.selected.con_id
        }:
            raise ExecutionOutcomeUnknown(
                "Executions changed during bracket verification; check TWS"
            )
        selected_orders = {
            oid: record
            for oid, record in app.scan.items()
            if str(getattr(record[1], "account", "")) == snapshot.selected.account
            and int(getattr(record[0], "conId", 0)) == snapshot.selected.con_id
        }
        # Existing outside reservations are not managed by this call. Compare
        # their observed safety fields, and reject new/missing orders.
        expected_existing = {
            o.order_id: o for o in snapshot.working_orders if o.key == snapshot.selected
        }
        if set(selected_orders) != set(ids) | set(expected_existing):
            raise ExecutionOutcomeUnknown(
                "Working order set changed during bracket verification; check TWS"
            )
        for oid in ids:
            contract, order, status = selected_orders[oid]
            if (
                not _same_option_identity(contract, snapshot)
                or _bracket_order_mismatches(
                    order,
                    submitted[oid],
                    outside_rth_ignored=oid in app.outside_rth_ignored,
                )
                or int(getattr(order, "clientId", -1)) != client_id
                or int(order.permId) != app.acks[oid]
                or int(order.permId) <= 0
                or status not in {"Submitted", "PreSubmitted"}
            ):
                raise ExecutionOutcomeUnknown(
                    "App-owned bracket changed during verification; check TWS"
                )
        for oid, prior in expected_existing.items():
            _, order, status = selected_orders[oid]
            if (
                int(order.permId),
                int(order.clientId),
                str(order.action),
                str(order.orderType),
                Decimal(str(order.totalQuantity)),
                str(order.tif),
                str(order.ocaGroup),
                int(order.parentId),
                status,
            ) != (
                prior.perm_id,
                prior.client_id,
                prior.action,
                prior.order_type,
                prior.remaining,
                prior.tif,
                prior.oca_group or "",
                prior.parent_id,
                prior.status,
            ):
                raise ExecutionOutcomeUnknown(
                    "An existing order changed during bracket verification; check TWS"
                )
            for field, price in (
                ("lmtPrice", prior.limit_price),
                ("auxPrice", prior.stop_price),
            ):
                if price is not None and Decimal(str(getattr(order, field))) != price:
                    raise ExecutionOutcomeUnknown(
                        "Existing order price changed during verification; check TWS"
                    )
        _wait_owned_orders(app, submitted, ids, deadline)
    finally:
        app.cancelPositions()
        app.scan = None
