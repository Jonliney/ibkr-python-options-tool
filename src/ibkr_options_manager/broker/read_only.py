from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ..connection import validate_paper_connection
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

PORTFOLIO_COMPLETIONS = frozenset(
    {
        "server_time",
        "managed_accounts",
        "positions",
        "open_orders",
        "configuration",
    }
)


@dataclass(frozen=True, slots=True)
class PortfolioRequest:
    host: str
    port: int
    client_id: int
    expected_account: str
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        validate_paper_connection(
            host=self.host,
            port=self.port,
            client_id=self.client_id,
            expected_account=self.expected_account,
            timeout_seconds=self.timeout_seconds,
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
        validate_paper_connection(
            host=self.host,
            port=self.port,
            client_id=self.client_id,
            expected_account=self.expected_account,
            timeout_seconds=self.timeout_seconds,
        )
        if self.option_con_id <= 0:
            raise ValueError("option_con_id must be positive")


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
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: str = ""


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


class PortfolioBroker(Protocol):
    """Read-only transport seam for the account position inventory."""

    def capture(self, request: PortfolioRequest) -> BrokerCapture:
        """Return one bounded portfolio capture without broker mutations."""
