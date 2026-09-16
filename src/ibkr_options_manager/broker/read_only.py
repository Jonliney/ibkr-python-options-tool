from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from ipaddress import ip_address
from typing import Protocol

from ..domain import PriceBand

REQUIRED_COMPLETIONS = frozenset(
    {
        "server_time",
        "managed_accounts",
        "positions",
        "open_orders",
        "configuration",
        "contract_details",
        "quote",
        "market_rule",
    }
)


@dataclass(frozen=True, slots=True)
class SnapshotRequest:
    host: str
    port: int
    client_id: int
    expected_account: str
    option_con_id: int
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        try:
            address = ip_address(self.host)
        except ValueError as error:
            raise ValueError("host must be a literal loopback address") from error
        if not address.is_loopback:
            raise ValueError("host must be a literal loopback address")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.client_id <= 0:
            raise ValueError("client_id must be positive and nonzero")
        if not self.expected_account.strip():
            raise ValueError("expected_account is required")
        if not self.expected_account.strip().upper().startswith("DU"):
            raise ValueError("expected_account must be a paper account ID")
        if self.option_con_id <= 0:
            raise ValueError("option_con_id must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class CapturedContract:
    con_id: int
    sec_type: str
    expiry: str
    strike: Decimal
    right: str
    multiplier: Decimal
    currency: str
    trading_class: str
    exchange: str
    local_symbol: str


@dataclass(frozen=True, slots=True)
class CapturedPosition:
    account: str
    contract: CapturedContract
    quantity: Decimal
    average_cost: Decimal


@dataclass(frozen=True, slots=True)
class CapturedOrder:
    perm_id: int
    client_id: int
    order_id: int
    account: str
    con_id: int
    action: str
    order_type: str
    remaining: Decimal
    status: str
    oca_group: str | None
    parent_id: int


@dataclass(frozen=True, slots=True)
class CapturedQuote:
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    close: Decimal | None
    market_data_type: str
    observed_at: Decimal


@dataclass(frozen=True, slots=True)
class CapturedMarketRule:
    exchange: str
    bands: tuple[PriceBand, ...]


@dataclass(frozen=True, slots=True)
class BrokerCapture:
    connection_epoch: int
    connected: bool
    server_version: int | None
    server_time: int | None
    read_only_api: bool | None
    localhost_only: bool | None
    managed_accounts: tuple[str, ...]
    positions: tuple[CapturedPosition, ...]
    orders: tuple[CapturedOrder, ...]
    contract_details: tuple[CapturedContract, ...]
    quote: CapturedQuote | None
    market_rule: CapturedMarketRule | None
    completed: frozenset[str]
    completion_times: tuple[tuple[str, Decimal], ...]
    errors: tuple[str, ...]
    captured_at: Decimal


class ReadOnlyBroker(Protocol):
    """The transport seam; it deliberately exposes observation only."""

    def capture(self, request: SnapshotRequest) -> BrokerCapture:
        """Return one bounded capture without retaining or mutating orders."""
