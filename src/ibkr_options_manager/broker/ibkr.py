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
    CapturedContract,
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
            app.reqAllOpenOrders()
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
                "open_orders",
                deadline,
                "open-order snapshot timed out",
            )
            _await(
                app,
                "configuration",
                deadline,
                "TWS configuration request timed out",
            )

            if isinstance(request, SnapshotRequest):
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
                    "configuration",
                    "contract_details",
                    "quote",
                    "market_rule",
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
            self.order_drafts: dict[int, _OrderDraft] = {}
            self.order_statuses: dict[int, tuple[str, Decimal]] = {}
            self.contract_details: list[CapturedContract] = []
            self.contract_details_raw: list[Any] = []
            self.quote_values: dict[str, Decimal] = {}
            self.market_data_type = "UNKNOWN"
            self.market_rule: CapturedMarketRule | None = None
            self.requested_market_rule_id: int | None = None
            self.requested_market_rule_exchange: str | None = None
            self.errors: list[str] = []

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
            self.order_drafts[int(orderId)] = _OrderDraft(
                perm_id=int(getattr(order, "permId", 0) or 0),
                client_id=int(getattr(order, "clientId", 0) or 0),
                order_id=int(orderId),
                account=str(getattr(order, "account", "")),
                con_id=int(getattr(contract, "conId", 0) or 0),
                action=str(getattr(order, "action", "")),
                order_type=str(getattr(order, "orderType", "")),
                oca_group=str(getattr(order, "ocaGroup", "")) or None,
                parent_id=int(getattr(order, "parentId", 0) or 0),
            )

        def orderStatus(self, orderId: int, *args: Any) -> None:
            if len(args) < 3:
                self.errors.append(
                    f"order {orderId} returned an incomplete status callback"
                )
                return
            status = str(args[0])
            remaining = _decimal(args[2])
            self.order_statuses[int(orderId)] = (status, remaining)

        def openOrderEnd(self) -> None:
            self._complete("open_orders")

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
                self.errors.append(f"IBKR error reqId={reqId} code={code}: {message}")

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
    orders: list[CapturedOrder] = []
    for order_id, draft in sorted(app.order_drafts.items()):
        status, remaining = app.order_statuses.get(
            order_id, ("MISSING_STATUS", Decimal("NaN"))
        )
        orders.append(
            CapturedOrder(
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
