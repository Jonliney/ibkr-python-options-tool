"""Deterministic, in-process data for exercising the workbench.

It has no TWS or socket dependency.  Its optional execution transport returns
synthetic acknowledgement IDs only, so ``--demo-data`` can never place an
order even when the paper-execution UI is being rehearsed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from ..broker import (
    REQUIRED_COMPLETIONS,
    BrokerCapture,
    CapturedContract,
    CapturedMarketRule,
    CapturedOrder,
    CapturedPosition,
    CapturedQuote,
    PortfolioRequest,
    SnapshotRequest,
)
from ..broker.execution import PaperSubmission
from ..domain import BrokerSnapshot, PlanResult, PriceBand
from ..snapshot import SnapshotCoordinator, SnapshotResult

DEMO_ACCOUNT = "DU0000000"


@dataclass(frozen=True, slots=True)
class _DemoPosition:
    contract: CapturedContract
    quantity: Decimal
    unit_basis: Decimal
    bid: Decimal
    ask: Decimal


_POSITIONS = (
    _DemoPosition(
        CapturedContract(
            con_id=1_001_500_251,
            sec_type="OPT",
            expiry="20260925",
            strike=Decimal("150"),
            right="C",
            multiplier=Decimal("100"),
            currency="USD",
            trading_class="MSTR",
            exchange="SMART",
            local_symbol="MSTR  260925C00150000",
        ),
        Decimal("10"),
        Decimal("2.74"),
        Decimal("3.12"),
        Decimal("3.18"),
    ),
    _DemoPosition(
        CapturedContract(
            con_id=1_002_100_161,
            sec_type="OPT",
            expiry="20261016",
            strike=Decimal("210"),
            right="C",
            multiplier=Decimal("100"),
            currency="USD",
            trading_class="NVDA",
            exchange="SMART",
            local_symbol="NVDA  261016C00210000",
        ),
        Decimal("7"),
        Decimal("4.20"),
        Decimal("5.76"),
        Decimal("5.85"),
    ),
    _DemoPosition(
        CapturedContract(
            con_id=1_003_625_093,
            sec_type="OPT",
            expiry="20260930",
            strike=Decimal("625"),
            right="P",
            multiplier=Decimal("100"),
            currency="USD",
            trading_class="SPY",
            exchange="SMART",
            local_symbol="SPY  260930P00625000",
        ),
        Decimal("4"),
        Decimal("1.85"),
        Decimal("1.58"),
        Decimal("1.62"),
    ),
    _DemoPosition(
        CapturedContract(
            con_id=1_004_470_201,
            sec_type="OPT",
            expiry="20261120",
            strike=Decimal("470"),
            right="C",
            multiplier=Decimal("100"),
            currency="USD",
            trading_class="TSLA",
            exchange="SMART",
            local_symbol="TSLA  261120C00470000",
        ),
        Decimal("3"),
        Decimal("8.60"),
        Decimal("9.05"),
        Decimal("9.15"),
    ),
)

DEMO_CON_IDS = frozenset(position.contract.con_id for position in _POSITIONS)


class DemoReadOnlyBroker:
    """A fresh, coherent demo capture for every read-only refresh request."""

    def __init__(
        self,
        *,
        clock: Callable[[], Decimal],
        paper_execution_enabled: bool = False,
    ) -> None:
        self._clock = clock
        self._paper_execution_enabled = paper_execution_enabled

    def capture(
        self,
        request: PortfolioRequest | SnapshotRequest,
    ) -> BrokerCapture:
        selected = _selected_position(request)
        now = self._clock()
        account = request.expected_account
        contracts = tuple(position.contract for position in _POSITIONS)
        positions = tuple(
            CapturedPosition(
                account=account,
                contract=position.contract,
                quantity=position.quantity,
                average_cost=position.unit_basis * position.contract.multiplier,
            )
            for position in _POSITIONS
        )
        return BrokerCapture(
            connection_epoch=1,
            connected=True,
            server_version=None,
            server_time=None,
            read_only_api=not self._paper_execution_enabled,
            localhost_only=True,
            managed_accounts=(account,),
            positions=positions,
            orders=_orders(account),
            contract_details=contracts,
            quote=CapturedQuote(
                bid=selected.bid,
                ask=selected.ask,
                last=selected.bid,
                close=selected.unit_basis,
                # FROZEN is an accepted read-only market-data type.  The window
                # independently labels this entire source as simulated data.
                market_data_type="FROZEN",
                observed_at=now,
            ),
            market_rule=CapturedMarketRule(
                exchange="SMART",
                bands=(
                    PriceBand(Decimal("0"), Decimal("0.01")),
                    PriceBand(Decimal("3"), Decimal("0.05")),
                ),
            ),
            completed=REQUIRED_COMPLETIONS,
            completion_times=tuple(
                (name, now) for name in sorted(REQUIRED_COMPLETIONS)
            ),
            errors=(),
            captured_at=now,
        )


class DemoSnapshotSource:
    """Keeps simulated snapshots fresh without weakening live-mode freshness checks."""

    def __init__(
        self,
        broker: DemoReadOnlyBroker,
        *,
        clock: Callable[[], Decimal],
    ) -> None:
        self._coordinator = SnapshotCoordinator(
            broker,
            max_age_seconds=Decimal("1"),
            clock=clock,
        )
        self._request: SnapshotRequest | None = None

    def refresh(self, request: SnapshotRequest) -> SnapshotResult:
        self._request = request
        return self._coordinator.refresh(request)

    def current(self) -> SnapshotResult:
        # Demo data has no live feed to become stale. Re-capture it locally so
        # Preview current draft remains usable for as long as the demo is open.
        if self._request is not None:
            return self._coordinator.refresh(self._request)
        return self._coordinator.current()


class DemoPaperExecutionTransport:
    """Safe local acknowledgement simulator for the paper execution UI."""

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
        del snapshot, host, port, client_id, timeout_seconds
        count = len(plan.pairs) * 2
        return PaperSubmission(
            order_ids=tuple(range(900_001, 900_001 + count)),
            perm_ids=tuple(range(800_001, 800_001 + count)),
        )


def _selected_position(
    request: PortfolioRequest | SnapshotRequest,
) -> _DemoPosition:
    con_id = (
        request.option_con_id
        if isinstance(request, SnapshotRequest)
        else _POSITIONS[0].contract.con_id
    )
    for position in _POSITIONS:
        if position.contract.con_id == con_id:
            return position
    raise ValueError("demo data does not contain the requested option contract")


def _orders(account: str) -> tuple[CapturedOrder, ...]:
    """Include one external order so the reserved-quantity UI can be rehearsed."""
    return (
        CapturedOrder(
            perm_id=496_248_334,
            client_id=0,
            order_id=0,
            account=account,
            con_id=_POSITIONS[0].contract.con_id,
            action="SELL",
            order_type="LMT",
            remaining=Decimal("5"),
            status="Submitted",
            oca_group=None,
            parent_id=0,
        ),
    )


__all__ = [
    "DEMO_ACCOUNT",
    "DEMO_CON_IDS",
    "DemoPaperExecutionTransport",
    "DemoReadOnlyBroker",
    "DemoSnapshotSource",
]
