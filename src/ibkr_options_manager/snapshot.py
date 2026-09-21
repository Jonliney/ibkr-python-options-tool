from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .broker import (
    REQUIRED_COMPLETIONS,
    BrokerCapture,
    ReadOnlyBroker,
    SnapshotRequest,
)
from .domain import (
    BrokerSnapshot,
    ContractKey,
    MarketRule,
    ObservedPosition,
    Quote,
    VerifiedOptionContract,
    WorkingOrder,
)


class SnapshotStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    EMPTY = "EMPTY"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    status: SnapshotStatus
    snapshot: BrokerSnapshot | None
    errors: tuple[str, ...]


class SnapshotCoordinator:
    """Publishes only complete coherent captures from a read-only adapter."""

    def __init__(
        self,
        broker: ReadOnlyBroker,
        *,
        max_age_seconds: Decimal,
        clock: Callable[[], Decimal],
    ) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        self._broker = broker
        self._max_age_seconds = max_age_seconds
        self._clock = clock
        self._current = SnapshotResult(SnapshotStatus.EMPTY, None, ())

    def refresh(self, request: SnapshotRequest) -> SnapshotResult:
        self._current = SnapshotResult(SnapshotStatus.EMPTY, None, ())
        capture = self._broker.capture(request)
        self._current = _publish(capture, request, self._max_age_seconds, self._clock())
        return self._current

    def current(self) -> SnapshotResult:
        snapshot = self._current.snapshot
        if snapshot is None:
            return self._current
        if self._clock() - snapshot.captured_at > self._max_age_seconds:
            self._current = SnapshotResult(
                SnapshotStatus.STALE,
                None,
                ("snapshot exceeded its freshness limit",),
            )
        return self._current


def _publish(
    capture: BrokerCapture,
    request: SnapshotRequest,
    max_age_seconds: Decimal,
    now: Decimal,
) -> SnapshotResult:
    errors: list[str] = []
    missing = sorted(REQUIRED_COMPLETIONS - capture.completed)
    if missing:
        errors.append(f"missing completion barriers: {', '.join(missing)}")
    errors.extend(
        _redact(message, request.expected_account, capture.managed_accounts)
        for message in capture.errors
    )
    if errors:
        return SnapshotResult(SnapshotStatus.BLOCKED, None, tuple(errors))

    positions = [
        position
        for position in capture.positions
        if position.account == request.expected_account
        and position.contract.con_id == request.option_con_id
    ]
    details = [
        contract
        for contract in capture.contract_details
        if contract.con_id == request.option_con_id
    ]
    if len(positions) != 1 or len(details) != 1:
        return SnapshotResult(
            SnapshotStatus.BLOCKED,
            None,
            ("selected option did not resolve to one position and contract",),
        )

    position = positions[0]
    contract = details[0]
    if not _same_required_identity(position.contract, contract):
        return SnapshotResult(
            SnapshotStatus.BLOCKED,
            None,
            ("position contract identity does not match contract details",),
        )
    if capture.quote is None or capture.market_rule is None:
        return SnapshotResult(
            SnapshotStatus.BLOCKED,
            None,
            ("quote or market rule was not captured",),
        )
    if (
        not contract.multiplier.is_finite()
        or contract.multiplier <= 0
        or contract.multiplier != contract.multiplier.to_integral_value()
    ):
        return SnapshotResult(
            SnapshotStatus.BLOCKED,
            None,
            ("contract multiplier is invalid",),
        )

    key = ContractKey(request.expected_account, request.option_con_id)
    verified_contract = VerifiedOptionContract(
        con_id=contract.con_id,
        sec_type=contract.sec_type,
        expiry=contract.expiry,
        strike=contract.strike,
        right=contract.right,
        multiplier=contract.multiplier,
        currency=contract.currency,
        trading_class=contract.trading_class,
        exchange=contract.exchange,
        local_symbol=contract.local_symbol,
    )
    quote = capture.quote
    snapshot = BrokerSnapshot(
        selected=key,
        connected=capture.connected,
        read_only_api=capture.read_only_api is True,
        localhost_only=capture.localhost_only is True,
        paper_account_verified=request.expected_account in capture.managed_accounts,
        complete=True,
        fresh=now - capture.captured_at <= max_age_seconds,
        connection_epoch=capture.connection_epoch,
        errors=capture.errors,
        contract=verified_contract,
        position=ObservedPosition(
            key=key,
            quantity=position.quantity,
            raw_average_cost=position.average_cost,
            unit_basis=position.average_cost / contract.multiplier,
        ),
        working_orders=tuple(
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
                capture.orders,
                key=lambda item: (
                    item.account,
                    item.con_id,
                    item.perm_id,
                    item.client_id,
                    item.order_id,
                ),
            )
        ),
        quote=Quote(
            bid=quote.bid,
            ask=quote.ask,
            last=quote.last,
            close=quote.close,
            market_data_type=quote.market_data_type,
            fresh=now - quote.observed_at <= max_age_seconds,
            observed_at=quote.observed_at,
        ),
        market_rule=MarketRule(
            exchange=capture.market_rule.exchange,
            bands=capture.market_rule.bands,
        ),
        server_version=capture.server_version,
        server_time=capture.server_time,
        captured_at=capture.captured_at,
        completion_times=capture.completion_times,
        api_read_only_observed=capture.read_only_api is not None,
    )
    return SnapshotResult(SnapshotStatus.READY, snapshot, ())


def _redact(
    message: str, expected_account: str, managed_accounts: tuple[str, ...]
) -> str:
    result = message
    for account in {expected_account, *managed_accounts}:
        redacted = "****" if len(account) <= 4 else f"***{account[-4:]}"
        result = result.replace(account, redacted)
    return result


def _same_required_identity(left: object, right: object) -> bool:
    fields = (
        "con_id",
        "sec_type",
        "expiry",
        "strike",
        "right",
        "multiplier",
        "currency",
        "trading_class",
        "local_symbol",
    )
    return all(getattr(left, field) == getattr(right, field) for field in fields)
