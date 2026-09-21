from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .broker import (
    PORTFOLIO_COMPLETIONS,
    BrokerCapture,
    CapturedContract,
    CapturedPosition,
    PortfolioBroker,
    PortfolioRequest,
)
from .domain import ContractKey, WorkingOrder


class PortfolioStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    EMPTY = "EMPTY"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class PortfolioPosition:
    key: ContractKey
    contract: CapturedContract
    quantity: Decimal
    raw_average_cost: Decimal
    unit_basis: Decimal | None
    working_orders: tuple[WorkingOrder, ...]
    eligible: bool
    eligibility: str


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    account: str
    connected: bool
    read_only_api: bool
    localhost_only: bool
    paper_account_verified: bool
    connection_epoch: int
    server_version: int | None
    server_time: int | None
    captured_at: Decimal
    positions: tuple[PortfolioPosition, ...]


@dataclass(frozen=True, slots=True)
class PortfolioResult:
    status: PortfolioStatus
    snapshot: PortfolioSnapshot | None
    errors: tuple[str, ...]


class PortfolioCoordinator:
    """Publishes a coherent inventory using read-only API requests only.

    ``paper_execution_mode`` changes the required TWS API setting, not the
    observation operations this coordinator is allowed to make.
    """

    def __init__(
        self,
        broker: PortfolioBroker,
        *,
        max_age_seconds: Decimal,
        clock: Callable[[], Decimal],
        paper_execution_mode: bool = False,
    ) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        self._broker = broker
        self._max_age_seconds = max_age_seconds
        self._clock = clock
        self._paper_execution_mode = paper_execution_mode
        self._current = PortfolioResult(PortfolioStatus.EMPTY, None, ())

    def refresh(self, request: PortfolioRequest) -> PortfolioResult:
        self._current = PortfolioResult(PortfolioStatus.EMPTY, None, ())
        capture = self._broker.capture(request)
        self._current = _publish_portfolio(
            capture,
            request,
            self._max_age_seconds,
            self._clock(),
            paper_execution_mode=self._paper_execution_mode,
        )
        return self._current

    def current(self) -> PortfolioResult:
        snapshot = self._current.snapshot
        if snapshot is None:
            return self._current
        if self._clock() - snapshot.captured_at > self._max_age_seconds:
            self._current = PortfolioResult(
                PortfolioStatus.STALE,
                None,
                ("portfolio snapshot exceeded its freshness limit",),
            )
        return self._current


def _publish_portfolio(
    capture: BrokerCapture,
    request: PortfolioRequest,
    max_age_seconds: Decimal,
    now: Decimal,
    *,
    paper_execution_mode: bool,
) -> PortfolioResult:
    errors = [
        _redact(message, request.expected_account, capture.managed_accounts)
        for message in capture.errors
    ]
    missing = sorted(PORTFOLIO_COMPLETIONS - capture.completed)
    if missing:
        errors.insert(0, f"missing completion barriers: {', '.join(missing)}")
    if not capture.connected:
        errors.append("TWS is disconnected")
    if paper_execution_mode:
        if capture.read_only_api is not False:
            errors.append("TWS API read-only mode was not explicitly disabled")
    elif capture.read_only_api is not True:
        errors.append("TWS API read-only mode was not verified")
    if capture.localhost_only is not True:
        errors.append("TWS localhost-only mode was not verified")
    if request.expected_account not in capture.managed_accounts:
        errors.append("the configured paper account was not verified")
    if now - capture.captured_at > max_age_seconds:
        errors.append("portfolio snapshot exceeded its freshness limit")
    if errors:
        return PortfolioResult(PortfolioStatus.BLOCKED, None, tuple(errors))

    candidates = [
        position
        for position in capture.positions
        if position.account == request.expected_account
        and position.contract.sec_type == "OPT"
        and position.quantity != 0
    ]
    con_ids = [position.contract.con_id for position in candidates]
    if len(con_ids) != len(set(con_ids)):
        return PortfolioResult(
            PortfolioStatus.BLOCKED,
            None,
            ("portfolio returned duplicate option contract IDs",),
        )

    positions = tuple(_position(position, capture) for position in candidates)
    return PortfolioResult(
        PortfolioStatus.READY,
        PortfolioSnapshot(
            account=request.expected_account,
            connected=True,
            read_only_api=capture.read_only_api is True,
            localhost_only=True,
            paper_account_verified=True,
            connection_epoch=capture.connection_epoch,
            server_version=capture.server_version,
            server_time=capture.server_time,
            captured_at=capture.captured_at,
            positions=positions,
        ),
        (),
    )


def _position(position: CapturedPosition, capture: BrokerCapture) -> PortfolioPosition:
    account = position.account
    contract = position.contract
    quantity = position.quantity
    average_cost = position.average_cost
    multiplier = contract.multiplier
    unit_basis = (
        average_cost / multiplier if multiplier.is_finite() and multiplier > 0 else None
    )
    eligibility = _eligibility(contract, quantity, unit_basis)
    key = ContractKey(account, contract.con_id)
    orders = tuple(
        WorkingOrder(
            perm_id=order.perm_id,
            client_id=order.client_id,
            order_id=order.order_id,
            key=ContractKey(order.account, order.con_id),
            action=order.action,
            order_type=order.order_type,
            remaining=order.remaining,
            status=order.status,
            oca_group=order.oca_group,
            parent_id=order.parent_id,
            observed_at=capture.captured_at,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            tif=order.tif,
        )
        for order in sorted(
            (
                order
                for order in capture.orders
                if order.account == account and order.con_id == contract.con_id
            ),
            key=lambda order: (order.perm_id, order.client_id, order.order_id),
        )
    )
    return PortfolioPosition(
        key=key,
        contract=contract,
        quantity=quantity,
        raw_average_cost=average_cost,
        unit_basis=unit_basis,
        working_orders=orders,
        eligible=eligibility == "Eligible",
        eligibility=eligibility,
    )


def _eligibility(
    contract: CapturedContract,
    quantity: Decimal,
    unit_basis: Decimal | None,
) -> str:
    if contract.con_id <= 0 or not contract.local_symbol.strip():
        return "Incomplete identity"
    if not quantity.is_finite() or quantity != quantity.to_integral_value():
        return "Fractional quantity"
    if quantity < 0:
        return "Short position"
    if quantity == 0:
        return "Closed"
    if unit_basis is None or not unit_basis.is_finite() or unit_basis <= 0:
        return "Invalid basis"
    return "Eligible"


def _redact(
    message: str,
    expected_account: str,
    managed_accounts: tuple[str, ...],
) -> str:
    result = message
    for account in {expected_account, *managed_accounts}:
        redacted = "****" if len(account) <= 4 else f"***{account[-4:]}"
        result = result.replace(account, redacted)
    return result


__all__ = [
    "PortfolioCoordinator",
    "PortfolioPosition",
    "PortfolioResult",
    "PortfolioSnapshot",
    "PortfolioStatus",
]
