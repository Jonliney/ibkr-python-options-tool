"""Bounded TWS reads for the trade-history collector. No order API calls."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event, Thread
from typing import Any

from ..ibkr_probe import (
    _INFORMATIONAL_ERROR_CODES,
    _load_ibapi,
    _parse_error_arguments,
)
from ..order_observation import borrow_client
from ..trade_history import (
    Discovery,
    MinuteBar,
    contract_identity,
    validate_contract,
)
from .ibkr import _capture_contract
from .read_only import (
    CapturedContract,
    CapturedExecution,
    CapturedPosition,
    PortfolioRequest,
)


class IbkrHistorySource:
    def discover(self, request: PortfolioRequest) -> Discovery:
        with _session(request) as app:
            app.reqPositions()
            _wait(app, "positions", request.timeout_seconds)
            from ibapi.execution import ExecutionFilter

            execution_filter = ExecutionFilter()
            execution_filter.acctCode = request.expected_account
            app.reqExecutions(2, execution_filter)
            _wait(app, "executions", request.timeout_seconds)
            candidates = dict(app.positions)
            candidates.update(app.fill_contracts)
            contracts = []
            errors = []
            for con_id, observed in sorted(
                candidates.items(),
                key=lambda item: (item[0] not in app.positions, item[0]),
            ):
                try:
                    resolved = _resolve(app, observed, request.timeout_seconds)
                except (ValueError, RuntimeError) as error:
                    errors.append(f"contract {con_id}: {error}")
                    # Never reuse a response channel after timeout or broker error.
                    if isinstance(error, RuntimeError):
                        break
                else:
                    contracts.append(resolved)
            verified_ids = {c.con_id for c in contracts}
            return Discovery(
                tuple(contracts),
                tuple(f for f in app.fills if f.con_id in verified_ids),
                tuple(errors),
                tuple(
                    p
                    for p in app.position_evidence
                    if p.contract.con_id in verified_ids
                ),
                positions_complete=not errors,
            )

    def history(
        self,
        request: PortfolioRequest,
        contract: CapturedContract,
        start: int,
        end: int,
    ) -> tuple[MinuteBar, ...]:
        if end - start > 86400 or start >= end:
            raise ValueError("history requests must span at most one day")
        with _session(request) as app:
            verified = _resolve(app, contract, request.timeout_seconds)
            query = app.imports.Contract()
            query.conId = verified.con_id
            query.secType = "OPT"
            query.exchange = verified.exchange
            query.currency = verified.currency
            app.reqHistoricalData(
                4,
                query,
                datetime.fromtimestamp(end, UTC).strftime("%Y%m%d %H:%M:%S UTC"),
                f"{end - start} S",
                "1 min",
                "BID",
                0,
                2,
                False,
                [],
            )
            try:
                _wait(app, "history", request.timeout_seconds)
                return tuple(bar for bar in app.bars if start <= bar.timestamp < end)
            finally:
                app.cancelHistoricalData(4)


def _resolve(app: Any, expected: CapturedContract, timeout: float) -> CapturedContract:
    # Position callbacks can omit exchange; SMART resolves the exact conId.
    expected = replace(expected, exchange=expected.exchange or "SMART")
    validate_contract(expected)
    app.details = []
    app.events["details"].clear()
    query = app.imports.Contract()
    query.conId = expected.con_id
    query.secType = "OPT"
    query.exchange = expected.exchange
    query.currency = expected.currency
    app.reqContractDetails(3, query)
    _wait(app, "details", timeout)
    if len(app.details) != 1:
        raise ValueError("option contract resolution was missing or ambiguous")
    result: CapturedContract = app.details[0]
    validate_contract(result)
    if contract_identity(result) != contract_identity(expected):
        raise ValueError("option contract identity mismatch")
    return result


def _wait(app: Any, name: str, timeout: float) -> None:
    if not app.events[name].wait(timeout):
        raise RuntimeError(f"{name} request timed out")
    if app.errors:
        raise RuntimeError("; ".join(app.errors))
    if name != "handshake" and not app.isConnected():
        raise RuntimeError("TWS disconnected during history capture")


@contextmanager
def _session(request: PortfolioRequest) -> Iterator[Any]:
    with borrow_client(request.host, request.port, request.client_id):
        app = _build_app(request.expected_account)
        reader = None
        try:
            app.connect(request.host, request.port, request.client_id)
            reader = Thread(target=app.run, daemon=True, name="ibkr-history-reader")
            reader.start()
            _wait(app, "handshake", request.timeout_seconds)
            app.reqManagedAccts()
            _wait(app, "accounts", request.timeout_seconds)
            if request.expected_account not in app.accounts:
                raise ValueError("collector account not verified by TWS")
            yield app
        finally:
            app.disconnect()
            if reader is not None:
                reader.join(timeout=1)
                if reader.is_alive():
                    raise RuntimeError("history reader did not release its client ID")


def _build_app(account: str) -> Any:
    imports = _load_ibapi()

    class HistoryApp(
        imports.EWrapper,  # type: ignore[misc, name-defined]
        imports.EClient,  # type: ignore[misc, name-defined]
    ):
        def __init__(self) -> None:
            imports.EWrapper.__init__(self)
            imports.EClient.__init__(self, self)
            self.imports = imports
            self.events = {
                name: Event()
                for name in (
                    "handshake",
                    "accounts",
                    "positions",
                    "executions",
                    "details",
                    "history",
                )
            }
            self.errors: list[str] = []
            self.accounts: tuple[str, ...] = ()
            self.positions: dict[int, CapturedContract] = {}
            self.position_evidence: list[CapturedPosition] = []
            self.fill_contracts: dict[int, CapturedContract] = {}
            self.fills: list[CapturedExecution] = []
            self.details: list[CapturedContract] = []
            self.bars: list[MinuteBar] = []

        def nextValidId(self, orderId: int) -> None:
            self.events["handshake"].set()

        def managedAccounts(self, accountsList: str) -> None:
            self.accounts = tuple(a.strip() for a in accountsList.split(","))
            self.events["accounts"].set()

        def position(
            self, acct: str, contract: Any, position: Any, avgCost: float
        ) -> None:
            if (
                acct == account
                and contract.secType == "OPT"
                and Decimal(str(position)) > 0
            ):
                captured = _capture_contract(contract)
                self.positions[captured.con_id] = captured
                self.position_evidence.append(
                    CapturedPosition(
                        acct,
                        captured,
                        Decimal(str(position)),
                        Decimal(str(avgCost)),
                    )
                )

        def positionEnd(self) -> None:
            self.events["positions"].set()

        def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:
            if (
                reqId != 2
                or execution.acctNumber != account
                or contract.secType != "OPT"
            ):
                return
            captured = _capture_contract(contract)
            self.fill_contracts[captured.con_id] = captured
            self.fills.append(
                CapturedExecution(
                    exec_id=execution.execId,
                    account=account,
                    con_id=captured.con_id,
                    perm_id=execution.permId,
                    side=execution.side,
                    quantity=Decimal(str(execution.shares)),
                    price=Decimal(str(execution.price)),
                    time=execution.time,
                    currency=captured.currency,
                )
            )

        def execDetailsEnd(self, reqId: int) -> None:
            if reqId == 2:
                self.events["executions"].set()

        def contractDetails(self, reqId: int, contractDetails: Any) -> None:
            if reqId == 3:
                self.details.append(_capture_contract(contractDetails.contract))

        def contractDetailsEnd(self, reqId: int) -> None:
            if reqId == 3:
                self.events["details"].set()

        def historicalData(self, reqId: int, bar: Any) -> None:
            if reqId != 4:
                return
            try:
                self.bars.append(
                    MinuteBar(
                        int(bar.date),
                        Decimal(str(bar.high)),
                        Decimal(str(bar.low)),
                        Decimal(str(bar.close)),
                    )
                )
            except (ValueError, ArithmeticError) as error:
                self.errors.append(f"invalid historical bar: {error}")
                self.events["history"].set()

        def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
            if reqId == 4:
                self.events["history"].set()

        def error(self, reqId: int, *args: Any) -> None:
            code, message = _parse_error_arguments(args)
            if code not in _INFORMATIONAL_ERROR_CODES:
                self.errors.append(f"TWS {code}: {message}")
                for event in self.events.values():
                    event.set()

        def connectionClosed(self) -> None:
            self.errors.append("TWS history connection closed")
            for event in self.events.values():
                event.set()

    return HistoryApp()
