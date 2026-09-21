"""Paper-TWS order submission for app-owned OCA pairs only."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

from ..domain import BrokerSnapshot, PlanResult
from ..execution import ExecutionBlocked, ExecutionOutcomeUnknown, MarketExitCandidate
from ..ibkr_probe import _load_ibapi, _parse_error_arguments


@dataclass(frozen=True, slots=True)
class PaperSubmission:
    order_ids: tuple[int, ...]
    perm_ids: tuple[int, ...]


class IbkrPaperExecutionBroker:
    """Bounded paper writer for creation and exact app-owned order changes."""

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
        if (
            not snapshot.selected.account.upper().startswith("DU")
            or plan.fingerprint is None
        ):
            raise ExecutionBlocked("a fingerprinted paper-account plan is required")
        imports = _load_ibapi()
        from ibapi.order import Order

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
                self.acks[int(order_id)] = int(getattr(order, "permId", 0) or 0)

            def error(self, req_id: int, *args: Any) -> None:
                code, message = _parse_error_arguments(args)
                if code not in {2104, 2106, 2107, 2108, 2158}:
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
                name="ibkr-paper-execution-reader",
                daemon=True,
            )
            reader.start()
            if not app.ready.wait(max(0, deadline - monotonic())):
                raise ExecutionBlocked("TWS did not issue a next valid order ID")

            contract = _build_submission_contract(imports, snapshot)
            ids: list[int] = []
            for pair in plan.pairs:
                for intent, transmit in ((pair.target, False), (pair.stop, True)):
                    order = Order()
                    order.action = intent.action
                    order.orderType = intent.order_type
                    order.totalQuantity = intent.quantity
                    order.account = intent.account
                    order.tif = intent.tif
                    order.ocaGroup = intent.logical_oca_group
                    order.ocaType = intent.oca_type
                    order.transmit = transmit
                    if intent.order_type == "LMT":
                        order.lmtPrice = float(intent.rounded_price)
                    else:
                        order.auxPrice = float(intent.rounded_price)
                    order_id = app.next_order_id + len(ids)
                    app.placeOrder(order_id, contract, order)
                    ids.append(order_id)

            while (
                len(app.acks) < len(ids)
                and not app.errors
                and monotonic() < deadline
            ):
                sleep(0.02)
            if app.errors:
                raise ExecutionBlocked("; ".join(app.errors))
            if len(app.acks) != len(ids) or any(
                not app.acks.get(order_id) for order_id in ids
            ):
                raise ExecutionOutcomeUnknown(
                    "TWS did not acknowledge every submitted order before the "
                    "deadline; "
                    "it may still be awaiting a TWS Transmit confirmation"
                )
            return PaperSubmission(
                tuple(ids),
                tuple(app.acks[order_id] for order_id in ids),
            )
        finally:
            if app.isConnected():
                app.disconnect()
            if reader is not None:
                reader.join(timeout=0.5)

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
            or not candidate.tif
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
                if code not in {2104, 2106, 2107, 2108, 2158}:
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

            app.cancelOrder(candidate.target_order_id)
            app.cancelOrder(candidate.stop_order_id)
            while (
                len(app.cancelled) != 2
                and not app.errors
                and monotonic() < deadline
            ):
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
            order.tif = candidate.tif
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
