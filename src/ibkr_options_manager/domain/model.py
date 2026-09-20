from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class PlanStatus(StrEnum):
    VALID = "VALID"
    BLOCKED = "BLOCKED"


class RemainderPolicy(StrEnum):
    NEXT_RUNG = "NEXT_RUNG"
    ADD_TO_LAST = "ADD_TO_LAST"


@dataclass(frozen=True, slots=True)
class ContractKey:
    account: str
    con_id: int


@dataclass(frozen=True, slots=True)
class VerifiedOptionContract:
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
class ObservedPosition:
    key: ContractKey
    quantity: Decimal
    raw_average_cost: Decimal
    unit_basis: Decimal


@dataclass(frozen=True, slots=True)
class WorkingOrder:
    perm_id: int
    client_id: int
    order_id: int
    key: ContractKey
    action: str
    order_type: str
    remaining: Decimal
    status: str
    oca_group: str | None = None
    parent_id: int = 0
    observed_at: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class Quote:
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    close: Decimal | None
    market_data_type: str
    fresh: bool
    observed_at: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class PriceBand:
    low_edge: Decimal
    increment: Decimal


@dataclass(frozen=True, slots=True)
class MarketRule:
    exchange: str
    bands: tuple[PriceBand, ...]


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    selected: ContractKey
    connected: bool
    read_only_api: bool
    localhost_only: bool
    paper_account_verified: bool
    complete: bool
    fresh: bool
    connection_epoch: int
    errors: tuple[str, ...]
    contract: VerifiedOptionContract
    position: ObservedPosition
    working_orders: tuple[WorkingOrder, ...]
    quote: Quote
    market_rule: MarketRule
    server_version: int | None = None
    server_time: int | None = None
    captured_at: Decimal = Decimal("0")
    completion_times: tuple[tuple[str, Decimal], ...] = ()


@dataclass(frozen=True, slots=True)
class PlanRequest:
    tranche_size: int
    target_percentages: tuple[Decimal, ...]
    stop_loss_percentage: Decimal
    remainder_policy: RemainderPolicy
    tif: str
    layers: tuple[LayerRequest, ...] = ()


@dataclass(frozen=True, slots=True)
class LayerRequest:
    """One explicitly priced, read-only OCA-layer draft."""

    quantity: int
    target_price: Decimal
    stop_price: Decimal
    tif: str
    target_percentage: Decimal | None = None
    runner: bool = False


@dataclass(frozen=True, slots=True)
class Validation:
    code: str
    message: str
    blocking: bool


@dataclass(frozen=True, slots=True)
class OrderIntent:
    account: str
    con_id: int
    action: str
    order_type: str
    quantity: int
    raw_price: Decimal
    rounded_price: Decimal
    tif: str
    logical_oca_group: str
    oca_type: int


@dataclass(frozen=True, slots=True)
class ExitPair:
    index: int
    target_percentage: Decimal
    quantity: int
    target: OrderIntent
    stop: OrderIntent
    runner: bool = False


@dataclass(frozen=True, slots=True)
class PlanResult:
    status: PlanStatus
    fingerprint: str | None
    allocated_quantity: int
    available_quantity: int
    planned_quantity: int
    pairs: tuple[ExitPair, ...]
    validations: tuple[Validation, ...]


@dataclass(frozen=True, slots=True)
class ReferencePricePreview:
    reference_price: Decimal
    target_percentage: Decimal
    target_price: Decimal
    stop_loss_percentage: Decimal
    stop_price: Decimal
