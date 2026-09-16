from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from time import monotonic
from typing import Any

from .capability import ProbeConfig, ProbeObservation

_INFORMATIONAL_ERROR_CODES = {
    2104,  # Market data farm connection is OK.
    2106,  # Historical data farm connection is OK.
    2107,  # Historical data farm is inactive but available on demand.
    2108,  # Market data farm is inactive but available on demand.
    2158,  # Sec-def data farm connection is OK.
}

_PRICE_TICK_TYPES = {
    1,  # bid
    2,  # ask
    4,  # last
    9,  # close
    66,  # delayed bid
    67,  # delayed ask
    68,  # delayed last
    75,  # delayed close
}


class IbapiUnavailableError(RuntimeError):
    pass


def extract_api_safety_settings(
    config_response: Any,
) -> tuple[bool | None, bool | None]:
    """Read fail-closed API safety flags from an official config response."""

    api_config = _first_attribute(config_response, "api", "apiConfig", "api_config")
    settings = _first_attribute(api_config, "settings")
    return (
        _optional_bool(settings, "readOnlyApi", "read_only_api"),
        _optional_bool(settings, "allowLocalhostOnly", "allow_localhost_only"),
    )


@dataclass(frozen=True, slots=True)
class _Position:
    account: str
    con_id: int
    sec_type: str
    quantity: str


class IbkrReadOnlyBroker:
    """Collects capability evidence without exposing an order-write interface."""

    def observe(self, config: ProbeConfig) -> ProbeObservation:
        imports = _load_ibapi()
        app = _build_app(imports)
        deadline = monotonic() + config.timeout_seconds
        reader: Thread | None = None

        try:
            app.connect(config.host, config.port, config.client_id)
            reader = Thread(target=app.run, name="ibkr-probe-reader", daemon=True)
            reader.start()

            if not _wait(app.handshake, deadline):
                app.blocking_errors.append("connection handshake timed out")
                return _observation(app, connected=False)

            app.connected = bool(app.isConnected())
            app.server_version = int(app.serverVersion())

            app.reqCurrentTime()
            app.reqManagedAccts()
            app.reqPositions()
            app.reqAllOpenOrders()
            _request_configuration(app, imports)

            _wait_or_record(
                app.server_time_end,
                deadline,
                app,
                "server-time request timed out",
            )
            _wait_or_record(
                app.managed_accounts_end,
                deadline,
                app,
                "managed-accounts request timed out",
            )
            _wait_or_record(
                app.positions_end,
                deadline,
                app,
                "position snapshot timed out",
            )
            _wait_or_record(
                app.open_orders_end,
                deadline,
                app,
                "open-order snapshot timed out",
            )
            _wait_or_record(
                app.config_end,
                deadline,
                app,
                "TWS configuration request timed out",
            )

            target = _select_option_position(app.positions, config)
            if target is not None:
                app.option_con_id = target.con_id
                contract = imports.Contract()
                contract.conId = target.con_id
                contract.secType = "OPT"
                app.reqContractDetails(app.contract_request_id, contract)
                _wait_or_record(
                    app.contract_details_end,
                    deadline,
                    app,
                    "contract-details request timed out",
                )
                _request_quote_and_market_rule(app, deadline)

            return _observation(app, connected=app.connected)
        except (ConnectionError, OSError) as error:
            app.blocking_errors.append(f"connection failed: {error}")
            return _observation(app, connected=False)
        finally:
            if getattr(app, "isConnected", lambda: False)():
                app.cancelPositions()
                app.disconnect()
            if reader is not None and reader.is_alive():
                reader.join(timeout=0.5)


@dataclass(frozen=True, slots=True)
class _IbapiImports:
    EClient: type[Any]
    EWrapper: type[Any]
    Contract: type[Any]
    ConfigRequestProto: type[Any]


def _load_ibapi() -> _IbapiImports:
    try:
        from ibapi.client import EClient
        from ibapi.contract import Contract
        from ibapi.protobuf.ConfigRequest_pb2 import (
            ConfigRequest as ConfigRequestProto,
        )
        from ibapi.wrapper import EWrapper
    except ImportError as error:
        raise IbapiUnavailableError(
            "The official IBKR Python TWS API is not installed. Install it "
            "from the matching official TWS API download; do not use the "
            "unofficial PyPI package."
        ) from error
    return _IbapiImports(EClient, EWrapper, Contract, ConfigRequestProto)


def _build_app(imports: _IbapiImports) -> Any:
    class ProbeApp(
        imports.EWrapper,  # type: ignore[misc, name-defined]
        imports.EClient,  # type: ignore[misc, name-defined]
    ):
        contract_request_id = 9101
        quote_request_id = 9102

        def __init__(self) -> None:
            imports.EWrapper.__init__(self)
            imports.EClient.__init__(self, self)
            self.handshake = Event()
            self.server_time_end = Event()
            self.managed_accounts_end = Event()
            self.positions_end = Event()
            self.open_orders_end = Event()
            self.config_end = Event()
            self.contract_details_end = Event()
            self.quote_end = Event()
            self.market_rule_end = Event()
            self.connected = False
            self.server_version: int | None = None
            self.server_time_received = False
            self.read_only_api: bool | None = None
            self.localhost_only: bool | None = None
            self.managed_accounts: tuple[str, ...] = ()
            self.positions: list[_Position] = []
            self.option_con_id: int | None = None
            self.contract_details: list[Any] = []
            self.quote_received = False
            self.market_rule_received = False
            self.requested_market_rule_id: int | None = None
            self.observed_order_perm_ids: list[int] = []
            self.blocking_errors: list[str] = []

        def nextValidId(self, orderId: int) -> None:
            del orderId
            self.handshake.set()

        def currentTime(self, time: int) -> None:
            del time
            self.server_time_received = True
            self.server_time_end.set()

        def managedAccounts(self, accountsList: str) -> None:
            self.managed_accounts = tuple(
                account.strip()
                for account in accountsList.split(",")
                if account.strip()
            )
            self.managed_accounts_end.set()

        def position(
            self, account: str, contract: Any, pos: Any, avgCost: float
        ) -> None:
            del avgCost
            self.positions.append(
                _Position(
                    account=account,
                    con_id=int(contract.conId),
                    sec_type=str(contract.secType),
                    quantity=str(pos),
                )
            )

        def positionEnd(self) -> None:
            self.positions_end.set()

        def openOrder(
            self, orderId: int, contract: Any, order: Any, orderState: Any
        ) -> None:
            del orderId, contract, orderState
            perm_id = int(getattr(order, "permId", 0) or 0)
            if perm_id > 0:
                self.observed_order_perm_ids.append(perm_id)

        def openOrderEnd(self) -> None:
            self.open_orders_end.set()

        def contractDetails(self, reqId: int, contractDetails: Any) -> None:
            if reqId == self.contract_request_id:
                self.contract_details.append(contractDetails)

        def contractDetailsEnd(self, reqId: int) -> None:
            if reqId == self.contract_request_id:
                self.contract_details_end.set()

        def tickPrice(
            self, reqId: int, tickType: int, price: float, attrib: Any
        ) -> None:
            del attrib
            if (
                reqId == self.quote_request_id
                and tickType in _PRICE_TICK_TYPES
                and price > 0
            ):
                self.quote_received = True

        def tickSnapshotEnd(self, reqId: int) -> None:
            if reqId == self.quote_request_id:
                self.quote_end.set()

        def marketRule(self, marketRuleId: int, priceIncrements: Any) -> None:
            if marketRuleId == self.requested_market_rule_id and priceIncrements:
                self.market_rule_received = True
                self.market_rule_end.set()

        def configResponseProtoBuf(self, configResponseProto: Any) -> None:
            (
                self.read_only_api,
                self.localhost_only,
            ) = extract_api_safety_settings(configResponseProto)
            self.config_end.set()

        def error(self, reqId: int, *args: Any) -> None:
            error_code, message = _parse_error_arguments(args)
            if error_code in _INFORMATIONAL_ERROR_CODES:
                return
            self.blocking_errors.append(
                f"IBKR error reqId={reqId} code={error_code}: {message}"
            )

        def connectionClosed(self) -> None:
            self.connected = False

    return ProbeApp()


def _request_configuration(app: Any, imports: _IbapiImports) -> None:
    try:
        request = imports.ConfigRequestProto()
        request.reqId = 9100
        app.reqConfigProtoBuf(request)
    except (AttributeError, TypeError) as error:
        app.blocking_errors.append(
            f"installed TWS API cannot request configuration: {error}"
        )
        app.config_end.set()


def _request_quote_and_market_rule(app: Any, deadline: float) -> None:
    if len(app.contract_details) != 1:
        return

    details = app.contract_details[0]
    app.reqMarketDataType(3)
    app.reqMktData(
        app.quote_request_id,
        details.contract,
        "",
        True,
        False,
        [],
    )
    _wait_or_record(
        app.quote_end,
        deadline,
        app,
        "quote snapshot timed out",
    )

    rule_id = _select_market_rule(details)
    if rule_id is None:
        app.blocking_errors.append(
            "contract details did not provide an unambiguous market rule"
        )
        return
    app.requested_market_rule_id = rule_id
    app.reqMarketRule(rule_id)
    _wait_or_record(
        app.market_rule_end,
        deadline,
        app,
        "market-rule request timed out",
    )


def _select_option_position(
    positions: list[_Position], config: ProbeConfig
) -> _Position | None:
    candidates = [
        position
        for position in positions
        if position.account == config.expected_account
        and position.sec_type == "OPT"
        and _is_positive(position.quantity)
        and (config.option_con_id is None or position.con_id == config.option_con_id)
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]


def _select_market_rule(details: Any) -> int | None:
    exchanges = [
        value.strip()
        for value in str(getattr(details, "validExchanges", "")).split(",")
    ]
    rule_ids = [
        value.strip() for value in str(getattr(details, "marketRuleIds", "")).split(",")
    ]
    pairs = [
        (exchange, int(rule_id))
        for exchange, rule_id in zip(exchanges, rule_ids, strict=False)
        if rule_id.isdigit() and int(rule_id) > 0
    ]
    smart_rules = [rule_id for exchange, rule_id in pairs if exchange == "SMART"]
    if len(smart_rules) == 1:
        return smart_rules[0]
    if len(pairs) == 1:
        return pairs[0][1]
    return None


def _observation(app: Any, *, connected: bool) -> ProbeObservation:
    return ProbeObservation(
        connected=connected,
        server_version=app.server_version,
        server_time_received=app.server_time_received,
        read_only_api=app.read_only_api,
        localhost_only=app.localhost_only,
        managed_accounts=tuple(app.managed_accounts),
        positions_complete=app.positions_end.is_set(),
        open_orders_complete=app.open_orders_end.is_set(),
        option_con_id=app.option_con_id,
        contract_details_count=len(app.contract_details),
        quote_received=app.quote_received,
        market_rule_received=app.market_rule_received,
        observed_order_perm_ids=tuple(sorted(set(app.observed_order_perm_ids))),
        blocking_errors=tuple(app.blocking_errors),
    )


def _wait(event: Event, deadline: float) -> bool:
    return event.wait(max(0.0, deadline - monotonic()))


def _wait_or_record(event: Event, deadline: float, app: Any, message: str) -> bool:
    completed = _wait(event, deadline)
    if not completed:
        app.blocking_errors.append(message)
    return completed


def _is_positive(value: str) -> bool:
    try:
        return float(value) > 0
    except ValueError:
        return False


def _first_attribute(value: Any, *names: str) -> Any | None:
    if value is None:
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _optional_bool(value: Any, *names: str) -> bool | None:
    found = _first_attribute(value, *names)
    if found is None:
        return None
    return bool(found)


def _parse_error_arguments(args: tuple[Any, ...]) -> tuple[int, str]:
    if len(args) >= 3 and isinstance(args[1], int):
        return int(args[1]), str(args[2])
    if len(args) >= 2:
        return int(args[0]), str(args[1])
    return -1, "unparseable error callback"
