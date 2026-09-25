from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from threading import Event, Thread
from time import monotonic
from typing import Any

from ..ibkr_probe import (
    IbapiUnavailableError,
    _IbapiImports,
    _load_ibapi,
    _parse_error_arguments,
    _select_market_rule,
    extract_api_safety_settings,
)
from .read_only import (
    BrokerCapture,
    CapturedCompletedOrder,
    CapturedContract,
    CapturedExecution,
    CapturedMarketRule,
    CapturedOrder,
    CapturedPosition,
    CapturedQuote,
    PortfolioRequest,
    SnapshotRequest,
)

_INFORMATIONAL_ERROR_CODES = {2104, 2106, 2107, 2108, 2158}
_MARKET_DATA_TYPES = {
    1: "LIVE",
    2: "FROZEN",
    3: "DELAYED",
    4: "DELAYED_FROZEN",
}
_TICK_FIELDS = {
    1: "bid",
    2: "ask",
    4: "last",
    9: "close",
    66: "bid",
    67: "ask",
    68: "last",
    75: "close",
}


@dataclass(slots=True)
class _OrderDraft:
    perm_id: int
    client_id: int
    order_id: int
    account: str
    con_id: int
    action: str
    order_type: str
    oca_group: str | None
    parent_id: int
    limit_price: Decimal | None
    stop_price: Decimal | None
    tif: str


class IbkrSnapshotBroker:
    """Official TWS adapter exposing bounded read-only captures."""

    def __init__(self) -> None:
        self._connection_epoch = 0

    def capture(self, request: SnapshotRequest | PortfolioRequest) -> BrokerCapture:
        imports = _load_ibapi()
        self._connection_epoch += 1
        app = _build_capture_app(imports)
        deadline = monotonic() + request.timeout_seconds
        reader: Thread | None = None

        try:
            app.connect(request.host, request.port, request.client_id)
            reader = Thread(
                target=app.run,
                name="ibkr-snapshot-reader",
                daemon=True,
            )
            reader.start()
            if not _wait(app.handshake, deadline):
                app.errors.append("connection handshake timed out")
                return _capture(app, self._connection_epoch, connected=False)

            app.connected = bool(app.isConnected())
            app.server_version = int(app.serverVersion())
            app.reqCurrentTime()
            app.reqManagedAccts()
            app.reqPositions()
            # `reqAllOpenOrders` is intentionally retained for inspection of
            # outside orders, but it does not bind them to this API client.
            # First collect this application's own orders from the same client
            # ID that submitted them.  Those non-zero API order IDs are
            # required for the later, app-owned management flow.
            app.open_order_source = "client"
            app.reqOpenOrders()
            _request_configuration(app, imports)

            _await(app, "server_time", deadline, "server-time request timed out")
            _await(
                app,
                "managed_accounts",
                deadline,
                "managed-accounts request timed out",
            )
            _await(app, "positions", deadline, "position snapshot timed out")
            _await(
                app,
                "client_open_orders",
                deadline,
                "client open-order snapshot timed out",
            )
            app.open_order_source = "all"
            app.reqAllOpenOrders()
            _await(
                app,
                "all_open_orders",
                deadline,
                "all-open-order snapshot timed out",
            )
            app._complete("open_orders")
            _await(
                app,
                "configuration",
                deadline,
                "TWS configuration request timed out",
            )

            if isinstance(request, SnapshotRequest):
                try:
                    app.reqCompletedOrders(True)
                except (AttributeError, TypeError) as error:
                    app.completed_history_errors.append(
                        f"completed-order request unavailable: {error}"
                    )
                try:
                    from ibapi.execution import ExecutionFilter

                    execution_filter = ExecutionFilter()
                    execution_filter.acctCode = request.expected_account
                    app.reqExecutions(app.execution_request_id, execution_filter)
                except (ImportError, AttributeError, TypeError) as error:
                    app.execution_history_errors.append(
                        f"execution request unavailable: {error}"
                    )
                contract = imports.Contract()
                contract.conId = request.option_con_id
                contract.secType = "OPT"
                app.reqContractDetails(app.contract_request_id, contract)
                _await(
                    app,
                    "contract_details",
                    deadline,
                    "contract-details request timed out",
                )
                _request_quote_and_rule(app, deadline)
                # Execution history informs the UI only. Its absence must not
                # turn a coherent planning snapshot into a broker-write gate.
                _wait(app.events["executions"], deadline)
                _wait(app.events["completed_orders"], deadline)
            return _capture(app, self._connection_epoch, connected=app.connected)
        except (ConnectionError, OSError) as error:
            app.errors.append(f"connection failed: {error}")
            return _capture(app, self._connection_epoch, connected=False)
        finally:
            if getattr(app, "isConnected", lambda: False)():
                app.cancelPositions()
                app.disconnect()
            if reader is not None and reader.is_alive():
                reader.join(timeout=0.5)


def _build_capture_app(imports: _IbapiImports) -> Any:
    class CaptureApp(
        imports.EWrapper,  # type: ignore[misc, name-defined]
        imports.EClient,  # type: ignore[misc, name-defined]
    ):
        config_request_id = 9200
        contract_request_id = 9201
        quote_request_id = 9202
        execution_request_id = 9203

        def __init__(self) -> None:
            imports.EWrapper.__init__(self)
            imports.EClient.__init__(self, self)
            self.handshake = Event()
            self.events = {
                name: Event()
                for name in (
                    "server_time",
                    "managed_accounts",
                    "positions",
                    "open_orders",
                    "client_open_orders",
                    "all_open_orders",
                    "configuration",
                    "contract_details",
                    "quote",
                    "market_rule",
                    "executions",
                    "completed_orders",
                )
            }
            self.completion_times: dict[str, Decimal] = {}
            self.connected = False
            self.server_version: int | None = None
            self.server_time: int | None = None
            self.read_only_api: bool | None = None
            self.localhost_only: bool | None = None
            self.managed_accounts: tuple[str, ...] = ()
            self.positions: list[CapturedPosition] = []
            self.open_order_source = ""
            self.client_order_drafts: dict[int, _OrderDraft] = {}
            self.client_order_statuses: dict[int, tuple[str, Decimal]] = {}
            self.all_order_drafts: dict[int, _OrderDraft] = {}
            self.all_order_statuses: dict[int, tuple[str, Decimal]] = {}
            self.contract_details: list[CapturedContract] = []
            self.contract_details_raw: list[Any] = []
            self.quote_values: dict[str, Decimal] = {}
            self.market_data_type = "UNKNOWN"
            self.market_rule: CapturedMarketRule | None = None
            self.requested_market_rule_id: int | None = None
            self.requested_market_rule_exchange: str | None = None
            self.errors: list[str] = []
            self.execution_history_errors: list[str] = []
            self.completed_history_errors: list[str] = []
            self.execution_drafts: dict[str, CapturedExecution] = {}
            self.commission_reports: dict[str, tuple[Decimal | None, str]] = {}
            self.completed_order_drafts: dict[int, CapturedCompletedOrder] = {}

        def _complete(self, name: str) -> None:
            self.completion_times[name] = _now_decimal()
            self.events[name].set()

        def nextValidId(self, orderId: int) -> None:
            del orderId
            self.handshake.set()

        def currentTime(self, time: int) -> None:
            self.server_time = int(time)
            self._complete("server_time")

        def managedAccounts(self, accountsList: str) -> None:
            self.managed_accounts = tuple(
                account.strip()
                for account in accountsList.split(",")
                if account.strip()
            )
            self._complete("managed_accounts")

        def position(
            self, account: str, contract: Any, pos: Any, avgCost: float
        ) -> None:
            self.positions.append(
                CapturedPosition(
                    account=account,
                    contract=_capture_contract(contract),
                    quantity=_decimal(pos),
                    average_cost=_decimal(avgCost),
                )
            )

        def positionEnd(self) -> None:
            self._complete("positions")

        def openOrder(
            self, orderId: int, contract: Any, order: Any, orderState: Any
        ) -> None:
            del orderState
            draft = _OrderDraft(
                perm_id=int(getattr(order, "permId", 0) or 0),
                client_id=int(getattr(order, "clientId", 0) or 0),
                order_id=int(orderId),
                account=str(getattr(order, "account", "")),
                con_id=int(getattr(contract, "conId", 0) or 0),
                action=str(getattr(order, "action", "")),
                order_type=str(getattr(order, "orderType", "")),
                oca_group=str(getattr(order, "ocaGroup", "")) or None,
                parent_id=int(getattr(order, "parentId", 0) or 0),
                limit_price=_positive_decimal_or_none(getattr(order, "lmtPrice", 0)),
                stop_price=_positive_decimal_or_none(getattr(order, "auxPrice", 0)),
                tif=str(getattr(order, "tif", "")),
            )
            if self.open_order_source == "client":
                self.client_order_drafts[int(orderId)] = draft
            elif self.open_order_source == "all":
                self.all_order_drafts[int(orderId)] = draft

        def orderStatus(self, orderId: int, *args: Any) -> None:
            if len(args) < 3:
                self.errors.append(
                    f"order {orderId} returned an incomplete status callback"
                )
                return
            status = str(args[0])
            remaining = _decimal(args[2])
            if self.open_order_source == "client":
                self.client_order_statuses[int(orderId)] = (status, remaining)
            elif self.open_order_source == "all":
                self.all_order_statuses[int(orderId)] = (status, remaining)

        def openOrderEnd(self) -> None:
            if self.open_order_source == "client":
                self._complete("client_open_orders")
            elif self.open_order_source == "all":
                self._complete("all_open_orders")

        def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:
            if reqId != self.execution_request_id:
                return
            exec_id = str(getattr(execution, "execId", ""))
            if not exec_id:
                return
            self.execution_drafts[exec_id] = CapturedExecution(
                exec_id=exec_id,
                account=str(getattr(execution, "acctNumber", "")),
                con_id=int(getattr(contract, "conId", 0) or 0),
                perm_id=int(getattr(execution, "permId", 0) or 0),
                side=str(getattr(execution, "side", "")),
                quantity=_decimal(getattr(execution, "shares", "NaN")),
                price=_decimal(getattr(execution, "price", "NaN")),
                time=str(getattr(execution, "time", "")),
            )

        def completedOrder(self, contract: Any, order: Any, state: Any) -> None:
            perm_id = int(getattr(order, "permId", 0) or 0)
            if perm_id <= 0:
                return
            self.completed_order_drafts[perm_id] = CapturedCompletedOrder(
                account=str(getattr(order, "account", "")),
                con_id=int(getattr(contract, "conId", 0) or 0),
                perm_id=perm_id,
                order_id=int(getattr(order, "orderId", 0) or 0),
                client_id=int(getattr(order, "clientId", 0) or 0),
                action=str(getattr(order, "action", "")),
                order_type=str(getattr(order, "orderType", "")),
                oca_group=str(getattr(order, "ocaGroup", "")),
                status=str(getattr(state, "status", "")),
            )

        def completedOrdersEnd(self) -> None:
            self._complete("completed_orders")

        def execDetailsEnd(self, reqId: int) -> None:
            if reqId == self.execution_request_id:
                self._complete("executions")

        def commissionAndFeesReport(self, report: Any) -> None:
            self._record_commission(report)

        def commissionReport(self, report: Any) -> None:
            self._record_commission(report)

        def _record_commission(self, report: Any) -> None:
            exec_id = str(getattr(report, "execId", ""))
            if not exec_id:
                return
            value = _decimal(getattr(report, "realizedPNL", "NaN"))
            # IBAPI uses the largest float as an unset marker.
            pnl: Decimal | None = (
                value if value.is_finite() and abs(value) < Decimal("1e300") else None
            )
            self.commission_reports[exec_id] = (
                pnl,
                str(getattr(report, "currency", "")),
            )

        def contractDetails(self, reqId: int, contractDetails: Any) -> None:
            if reqId != self.contract_request_id:
                return
            self.contract_details.append(_capture_contract(contractDetails.contract))
            self.contract_details_raw.append(contractDetails)

        def contractDetailsEnd(self, reqId: int) -> None:
            if reqId == self.contract_request_id:
                self._complete("contract_details")

        def marketDataType(self, reqId: int, marketDataType: int) -> None:
            if reqId == self.quote_request_id:
                self.market_data_type = _MARKET_DATA_TYPES.get(
                    int(marketDataType), "UNKNOWN"
                )

        def tickPrice(
            self, reqId: int, tickType: int, price: float, attrib: Any
        ) -> None:
            del attrib
            field = _TICK_FIELDS.get(int(tickType))
            if reqId == self.quote_request_id and field and price > 0:
                self.quote_values[field] = _decimal(price)

        def tickSnapshotEnd(self, reqId: int) -> None:
            if reqId == self.quote_request_id:
                self._complete("quote")

        def marketRule(self, marketRuleId: int, priceIncrements: Any) -> None:
            if marketRuleId != self.requested_market_rule_id:
                return
            from ..domain import PriceBand

            self.market_rule = CapturedMarketRule(
                exchange=self.requested_market_rule_exchange or "",
                bands=tuple(
                    PriceBand(
                        low_edge=_decimal(increment.lowEdge),
                        increment=_decimal(increment.increment),
                    )
                    for increment in priceIncrements
                ),
            )
            self._complete("market_rule")

        def configResponseProtoBuf(self, configResponseProto: Any) -> None:
            self.read_only_api, self.localhost_only = extract_api_safety_settings(
                configResponseProto
            )
            self._complete("configuration")

        def error(self, reqId: int, *args: Any) -> None:
            code, message = _parse_error_arguments(args)
            if code not in _INFORMATIONAL_ERROR_CODES:
                target = (
                    self.execution_history_errors
                    if reqId == self.execution_request_id
                    else self.errors
                )
                target.append(f"IBKR error reqId={reqId} code={code}: {message}")

        def connectionClosed(self) -> None:
            self.connected = False

    return CaptureApp()


def _request_configuration(app: Any, imports: _IbapiImports) -> None:
    try:
        request = imports.ConfigRequestProto()
        request.reqId = app.config_request_id
        app.reqConfigProtoBuf(request)
    except (AttributeError, TypeError) as error:
        app.errors.append(f"installed API cannot request configuration: {error}")
        app._complete("configuration")


def _request_quote_and_rule(app: Any, deadline: float) -> None:
    if len(app.contract_details_raw) != 1:
        return
    details = app.contract_details_raw[0]
    app.reqMarketDataType(3)
    app.reqMktData(app.quote_request_id, details.contract, "", True, False, [])
    _await(app, "quote", deadline, "quote snapshot timed out")

    rule_id = _select_market_rule(details)
    if rule_id is None:
        app.errors.append("contract details had no unambiguous market rule")
        return
    app.requested_market_rule_id = rule_id
    app.requested_market_rule_exchange = _market_rule_exchange(details, rule_id)
    app.reqMarketRule(rule_id)
    _await(app, "market_rule", deadline, "market-rule request timed out")


def _market_rule_exchange(details: Any, rule_id: int) -> str:
    exchanges = str(getattr(details, "validExchanges", "")).split(",")
    rule_ids = str(getattr(details, "marketRuleIds", "")).split(",")
    for exchange, value in zip(exchanges, rule_ids, strict=False):
        if value.strip().isdigit() and int(value.strip()) == rule_id:
            return exchange.strip()
    return ""


def _capture(app: Any, epoch: int, *, connected: bool) -> BrokerCapture:
    # `reqAllOpenOrders` can report an API order ID of 0 for an otherwise
    # visible order.  Prefer the same-client `reqOpenOrders` version whenever
    # permanent IDs match, while retaining the all-orders view for external
    # coverage inspection.
    orders_by_perm_id: dict[int, CapturedOrder] = {}
    anonymous_orders: list[CapturedOrder] = []
    for drafts, statuses in (
        (app.all_order_drafts, app.all_order_statuses),
        (app.client_order_drafts, app.client_order_statuses),
    ):
        for order_id, draft in sorted(drafts.items()):
            status, remaining = statuses.get(
                order_id, ("MISSING_STATUS", Decimal("NaN"))
            )
            captured = CapturedOrder(
                perm_id=draft.perm_id,
                client_id=draft.client_id,
                order_id=draft.order_id,
                account=draft.account,
                con_id=draft.con_id,
                action=draft.action,
                order_type=draft.order_type,
                remaining=remaining,
                status=status,
                oca_group=draft.oca_group,
                parent_id=draft.parent_id,
                limit_price=draft.limit_price,
                stop_price=draft.stop_price,
                tif=draft.tif,
            )
            if captured.perm_id <= 0:
                anonymous_orders.append(captured)
                continue
            existing = orders_by_perm_id.get(captured.perm_id)
            if existing is None or (existing.order_id <= 0 < captured.order_id):
                orders_by_perm_id[captured.perm_id] = captured
    orders = [*orders_by_perm_id.values(), *anonymous_orders]
    executions_by_identity: dict[str, CapturedExecution] = {}
    for fill in getattr(app, "execution_drafts", {}).values():
        identity, _, revision = fill.exec_id.rpartition(".")
        if not revision.isdigit():
            identity = fill.exec_id
        prior = executions_by_identity.get(identity)
        prior_revision = prior.exec_id.rpartition(".")[2] if prior else ""
        if prior is None or (
            revision.isdigit()
            and prior_revision.isdigit()
            and int(revision) > int(prior_revision)
        ):
            executions_by_identity[identity] = fill
    executions = []
    for fill in executions_by_identity.values():
        pnl, currency = getattr(app, "commission_reports", {}).get(
            fill.exec_id, (None, "")
        )
        executions.append(
            CapturedExecution(
                exec_id=fill.exec_id,
                account=fill.account,
                con_id=fill.con_id,
                perm_id=fill.perm_id,
                side=fill.side,
                quantity=fill.quantity,
                price=fill.price,
                time=fill.time,
                realized_pnl=pnl,
                currency=currency,
            )
        )
    captured_at = _now_decimal()
    quote = None
    if app.events["quote"].is_set():
        quote = CapturedQuote(
            bid=app.quote_values.get("bid"),
            ask=app.quote_values.get("ask"),
            last=app.quote_values.get("last"),
            close=app.quote_values.get("close"),
            market_data_type=app.market_data_type,
            observed_at=app.completion_times["quote"],
        )
    return BrokerCapture(
        connection_epoch=epoch,
        connected=connected,
        server_version=app.server_version,
        server_time=app.server_time,
        read_only_api=app.read_only_api,
        localhost_only=app.localhost_only,
        managed_accounts=tuple(app.managed_accounts),
        positions=tuple(app.positions),
        orders=tuple(orders),
        contract_details=tuple(app.contract_details),
        quote=quote,
        market_rule=app.market_rule,
        completed=frozenset(
            name for name, event in app.events.items() if event.is_set()
        ),
        completion_times=tuple(sorted(app.completion_times.items())),
        errors=tuple(app.errors),
        captured_at=captured_at,
        executions=tuple(sorted(executions, key=lambda fill: fill.exec_id)),
        executions_complete=(
            app.events["executions"].is_set()
            and not getattr(app, "execution_history_errors", ())
        )
        if "executions" in app.events
        else False,
        completed_orders=tuple(
            sorted(
                getattr(app, "completed_order_drafts", {}).values(),
                key=lambda order: order.perm_id,
            )
        ),
        completed_orders_complete=(
            app.events["completed_orders"].is_set()
            and not getattr(app, "completed_history_errors", ())
        )
        if "completed_orders" in app.events
        else False,
    )


def _capture_contract(contract: Any) -> CapturedContract:
    return CapturedContract(
        con_id=int(getattr(contract, "conId", 0) or 0),
        sec_type=str(getattr(contract, "secType", "")),
        expiry=str(getattr(contract, "lastTradeDateOrContractMonth", "")),
        strike=_decimal(getattr(contract, "strike", "NaN")),
        right=str(getattr(contract, "right", "")),
        multiplier=_decimal(getattr(contract, "multiplier", "NaN")),
        currency=str(getattr(contract, "currency", "")),
        trading_class=str(getattr(contract, "tradingClass", "")),
        exchange=str(getattr(contract, "exchange", "")),
        local_symbol=str(getattr(contract, "localSymbol", "")),
    )


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("NaN")


def _positive_decimal_or_none(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed.is_finite() and parsed > 0 else None


def _await(app: Any, name: str, deadline: float, message: str) -> bool:
    complete = _wait(app.events[name], deadline)
    if not complete:
        app.errors.append(message)
    return complete


def _wait(event: Event, deadline: float) -> bool:
    return event.wait(max(0.0, deadline - monotonic()))


def _now_decimal() -> Decimal:
    return Decimal(str(monotonic()))


__all__ = ["IbapiUnavailableError", "IbkrSnapshotBroker"]
