"""Deterministic, in-process data for exercising the workbench.

It has no TWS or socket dependency.  Its optional execution transport returns
synthetic acknowledgement IDs only, so ``--demo-data`` can never place an
order even when the paper-execution UI is being rehearsed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from ..broker import (
    REQUIRED_COMPLETIONS,
    BrokerCapture,
    CapturedContract,
    CapturedExecution,
    CapturedMarketRule,
    CapturedOrder,
    CapturedPosition,
    CapturedQuote,
    PortfolioRequest,
    SnapshotRequest,
)
from ..broker.execution import PaperSubmission
from ..domain import BrokerSnapshot, PlanResult, PriceBand
from ..execution import (
    ExecutionJournal,
    JournalEntry,
    JournalFill,
    JournalLayer,
    MarketExitCandidate,
    PriceUpdateCandidate,
    classify_journal_layer,
)
from ..snapshot import SnapshotCoordinator, SnapshotResult
from ..trailing import TrailingPlan

DEMO_ACCOUNT = "DU0000000"
DEMO_SCENARIOS = (
    "standard",
    "convert",
    "working-trail",
    "partial-fill",
    "manual-cancel",
    "closed-trail",
    "no-bid",
)
DEMO_TRAILING_CON_ID = 1_004_470_201
_TRAILING_ORDER_ID = 910_201
_TRAILING_PERM_ID = 810_201


@dataclass(frozen=True, slots=True)
class _DemoPosition:
    contract: CapturedContract
    quantity: Decimal
    unit_basis: Decimal
    bid: Decimal | None
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


def seed_demo_journal(path: Path, *, scenario: str = "standard") -> ExecutionJournal:
    """Seed one deterministic example; named scenarios use disposable journals."""
    if scenario not in DEMO_SCENARIOS:
        raise ValueError("unknown demo scenario")
    journal = ExecutionJournal(path)
    if scenario != "standard":
        entry = _scenario_entry(scenario)
        if entry is not None and journal.find(entry.fingerprint) is None:
            journal._write((*journal._entries(), entry))
        if scenario == "closed-trail":
            brackets = _closed_trail_brackets()
            if journal.find(brackets.fingerprint) is None:
                journal._write((*journal._entries(), brackets))
        return journal
    fingerprint = sha256(b"demo-nvda-unverified-bracket-v1").hexdigest()
    if journal.find(fingerprint) is None:
        journal._write(
            (
                *journal._entries(),
                JournalEntry(
                    fingerprint=fingerprint,
                    account=DEMO_ACCOUNT,
                    con_id=1_002_100_161,
                    state="SUBMISSION_UNKNOWN",
                    expected_order_count=2,
                    snapshot_captured_at="0",
                    oca_prefix="demo-nvda-unverified",
                    layers=(
                        JournalLayer(
                            quantity=3,
                            target_price="8.40",
                            stop_price="3.15",
                            tif="GTC",
                            target_percentage="100",
                            stop_percentage="25",
                        ),
                    ),
                ),
            )
        )
    return journal


def _scenario_entry(scenario: str) -> JournalEntry | None:
    if scenario == "convert":
        return JournalEntry(
            fingerprint=sha256(b"demo-trailing-convert-bracket-v1").hexdigest(),
            account=DEMO_ACCOUNT,
            con_id=DEMO_TRAILING_CON_ID,
            state="SUBMITTED",
            expected_order_count=2,
            snapshot_captured_at="0",
            order_ids=(910_101, 910_102),
            perm_ids=(810_101, 810_102),
            oca_prefix="demo-trailing-convert",
            layers=(JournalLayer(2, "12.00", "7.00", "GTC"),),
        )
    if scenario in {"working-trail", "partial-fill", "manual-cancel", "closed-trail"}:
        return JournalEntry(
            fingerprint="trailing-conversion:"
            + sha256(f"demo-{scenario}-v1".encode()).hexdigest(),
            account=DEMO_ACCOUNT,
            con_id=DEMO_TRAILING_CON_ID,
            state="SUBMITTED",
            expected_order_count=1,
            snapshot_captured_at="0",
            order_ids=(_TRAILING_ORDER_ID,),
            perm_ids=(_TRAILING_PERM_ID,),
            trailing_quantity=3,
            trailing_stop="8.80",
            trailing_value="0.25",
            trailing_unit="dollars",
            trailing_limit_offset="0.10",
            trailing_tif="GTC",
            trailing_last_stop="8.80" if scenario == "closed-trail" else "",
            trailing_last_limit="8.70" if scenario == "closed-trail" else "",
        )
    return None


def _closed_trail_brackets() -> JournalEntry:
    """Two completed TSLA brackets precede the three-contract trailing exit."""
    return JournalEntry(
        fingerprint=sha256(b"demo-closed-trail-brackets-v1").hexdigest(),
        account=DEMO_ACCOUNT,
        con_id=DEMO_TRAILING_CON_ID,
        state="RECONCILED",
        expected_order_count=4,
        snapshot_captured_at="0",
        order_ids=(910_301, 910_302, 910_303, 910_304),
        perm_ids=(810_301, 810_302, 810_303, 810_304),
        oca_prefix="demo-closed-trail-brackets",
        layers=(
            JournalLayer(
                1,
                "12.00",
                "7.00",
                "GTC",
                810_301,
                810_302,
                target_percentage="39.53",
                stop_percentage="-18.60",
            ),
            JournalLayer(
                1,
                "11.00",
                "7.50",
                "GTC",
                810_303,
                810_304,
                target_percentage="27.91",
                stop_percentage="-12.79",
            ),
        ),
        fills=(
            JournalFill(
                "DEMO-BRACKET-TARGET",
                810_301,
                "SLD",
                "1",
                "12.00",
                "2026-10-09T14:00:00-04:00",
                "340",
                "USD",
            ),
            JournalFill(
                "DEMO-BRACKET-STOP",
                810_304,
                "SLD",
                "1",
                "7.50",
                "2026-10-09T14:30:00-04:00",
                "-110",
                "USD",
            ),
        ),
    )


class DemoReadOnlyBroker:
    """A fresh, coherent demo capture for every read-only refresh request."""

    def __init__(
        self,
        *,
        clock: Callable[[], Decimal],
        paper_execution_enabled: bool = False,
        scenario: str = "standard",
    ) -> None:
        if scenario not in DEMO_SCENARIOS:
            raise ValueError("unknown demo scenario")
        self._clock = clock
        self._paper_execution_enabled = paper_execution_enabled
        self._scenario = scenario
        self._portfolio_captures = 0
        self._journal: ExecutionJournal | None = None

    def use_journal(self, journal: ExecutionJournal) -> None:
        """Expose acknowledged demo brackets in subsequent simulated reads."""
        self._journal = journal

    def _journal_orders(self, account: str) -> tuple[CapturedOrder, ...]:
        if self._journal is None:
            return ()
        orders: list[CapturedOrder] = []
        for position in _POSITIONS:
            for entry in self._journal.submission_entries(
                account=account, con_id=position.contract.con_id
            ):
                if (
                    entry.state
                    not in {"SUBMITTED", "RECONCILED", "PARTIALLY_RECONCILED"}
                    or len(entry.order_ids) != len(entry.layers) * 2
                    or len(entry.perm_ids) != len(entry.order_ids)
                    or any(value <= 0 for value in (*entry.order_ids, *entry.perm_ids))
                    or len(set(entry.order_ids)) != len(entry.order_ids)
                    or len(set(entry.perm_ids)) != len(entry.perm_ids)
                ):
                    continue
                for index, layer in enumerate(entry.layers):
                    outcome = classify_journal_layer(
                        entry,
                        index,
                        active_perm_ids=frozenset(),
                        observed_perm_ids=frozenset(),
                    )
                    if layer.cancelled or outcome.status.startswith("CLOSED_"):
                        continue
                    group = (
                        f"{entry.oca_prefix or entry.fingerprint[:12]}"
                        f"/tranche-{index + 1}"
                    )
                    for leg, order_type, price in (
                        (0, "LMT", Decimal(layer.target_price)),
                        (1, layer.stop_order_type, Decimal(layer.stop_price)),
                    ):
                        orders.append(
                            CapturedOrder(
                                perm_id=entry.perm_ids[index * 2 + leg],
                                client_id=17,
                                order_id=entry.order_ids[index * 2 + leg],
                                account=account,
                                con_id=entry.con_id,
                                action="SELL",
                                order_type=order_type,
                                remaining=Decimal(layer.quantity),
                                status="Submitted",
                                oca_group=group,
                                parent_id=0,
                                limit_price=(
                                    price
                                    if leg == 0
                                    else Decimal(layer.stop_limit_price)
                                    if layer.stop_order_type == "STP LMT"
                                    else None
                                ),
                                stop_price=price if leg == 1 else None,
                                tif=layer.tif,
                            )
                        )
            for entry in self._journal.trailing_entries(
                account=account, con_id=position.contract.con_id
            ):
                if self._scenario == "manual-cancel" or (
                    self._scenario == "closed-trail" and self._portfolio_captures >= 2
                ):
                    continue
                if (
                    entry.state != "SUBMITTED"
                    or len(entry.order_ids) != 1
                    or len(entry.perm_ids) != 1
                    or not entry.trailing_quantity
                ):
                    continue
                orders.append(
                    CapturedOrder(
                        perm_id=entry.perm_ids[0],
                        client_id=17,
                        order_id=entry.order_ids[0],
                        account=account,
                        con_id=entry.con_id,
                        action="SELL",
                        order_type=(
                            "TRAIL LIMIT" if entry.trailing_limit_offset else "TRAIL"
                        ),
                        remaining=(
                            Decimal("1")
                            if self._scenario == "partial-fill"
                            else Decimal(entry.trailing_quantity)
                        ),
                        status="Submitted",
                        oca_group=None,
                        parent_id=0,
                        stop_price=Decimal(entry.trailing_stop),
                        tif=entry.trailing_tif,
                    )
                )
        return tuple(orders)

    def capture(
        self,
        request: PortfolioRequest | SnapshotRequest,
    ) -> BrokerCapture:
        if isinstance(request, PortfolioRequest):
            self._portfolio_captures += 1
        selected = _selected_position(request)
        now = self._clock()
        account = request.expected_account
        contracts = tuple(position.contract for position in _POSITIONS)
        positions = tuple(
            CapturedPosition(
                account=account,
                contract=position.contract,
                quantity=(
                    Decimal("1")
                    if self._scenario == "partial-fill"
                    and position.contract.con_id == DEMO_TRAILING_CON_ID
                    else position.quantity
                ),
                average_cost=position.unit_basis * position.contract.multiplier,
            )
            for position in _POSITIONS
            if not (
                self._scenario == "closed-trail"
                and self._portfolio_captures >= 2
                and position.contract.con_id == DEMO_TRAILING_CON_ID
            )
        )
        filled = self._scenario == "partial-fill" or (
            self._scenario == "closed-trail" and self._portfolio_captures >= 2
        )
        trailing_executions = (
            (
                CapturedExecution(
                    exec_id=f"DEMO-TRAIL-{self._scenario}",
                    account=account,
                    con_id=DEMO_TRAILING_CON_ID,
                    perm_id=_TRAILING_PERM_ID,
                    side="SLD",
                    quantity=Decimal("2")
                    if self._scenario == "partial-fill"
                    else Decimal("3"),
                    price=Decimal("9.10"),
                    time="2026-10-09T15:30:00-04:00",
                    realized_pnl=Decimal("100")
                    if self._scenario == "partial-fill"
                    else Decimal("150"),
                    currency="USD",
                ),
            )
            if filled
            else ()
        )
        bracket_executions = (
            (
                CapturedExecution(
                    exec_id=exec_id,
                    account=account,
                    con_id=DEMO_TRAILING_CON_ID,
                    perm_id=perm_id,
                    side="SLD",
                    quantity=Decimal("1"),
                    price=Decimal(price),
                    time=time,
                    realized_pnl=Decimal(pnl),
                    currency="USD",
                )
                for exec_id, perm_id, price, time, pnl in (
                    (
                        "DEMO-BRACKET-TARGET",
                        810_301,
                        "12.00",
                        "2026-10-09T14:00:00-04:00",
                        "340",
                    ),
                    (
                        "DEMO-BRACKET-STOP",
                        810_304,
                        "7.50",
                        "2026-10-09T14:30:00-04:00",
                        "-110",
                    ),
                )
            )
            if self._scenario == "closed-trail"
            else ()
        )
        executions = (*bracket_executions, *trailing_executions)
        return BrokerCapture(
            connection_epoch=1,
            connected=True,
            server_version=None,
            server_time=None,
            read_only_api=not self._paper_execution_enabled,
            localhost_only=True,
            managed_accounts=(account,),
            positions=positions,
            orders=(*_orders(account), *self._journal_orders(account)),
            contract_details=contracts,
            quote=CapturedQuote(
                bid=None
                if self._scenario == "no-bid"
                and selected.contract.con_id == DEMO_TRAILING_CON_ID
                else selected.bid,
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
                bands=(PriceBand(Decimal("0"), Decimal("0.01")),),
            ),
            completed=REQUIRED_COMPLETIONS,
            completion_times=tuple(
                (name, now) for name in sorted(REQUIRED_COMPLETIONS)
            ),
            errors=(),
            captured_at=now,
            executions=executions,
            completed_orders_complete=True,
            executions_complete=True,
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

    def capture_closed_history(self, request: SnapshotRequest) -> BrokerCapture | None:
        return self._coordinator.capture_closed_history(request)


class DemoPaperExecutionTransport:
    """Safe local acknowledgement simulator for the paper execution UI."""

    def __init__(self, journal: ExecutionJournal | None = None) -> None:
        self._journal = journal

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
        del host, port, client_id, timeout_seconds
        count = len(plan.pairs) * 2
        prior = (
            tuple(
                entry
                for position in _POSITIONS
                for entry in self._journal.submission_entries(
                    account=snapshot.selected.account,
                    con_id=position.contract.con_id,
                )
            )
            if self._journal is not None
            else ()
        )
        next_order_id = (
            max(
                (value for entry in prior for value in entry.order_ids), default=900_000
            )
            + 1
        )
        next_perm_id = (
            max((value for entry in prior for value in entry.perm_ids), default=800_000)
            + 1
        )
        return PaperSubmission(
            order_ids=tuple(range(next_order_id, next_order_id + count)),
            perm_ids=tuple(range(next_perm_id, next_perm_id + count)),
        )

    def cancel_pair(
        self,
        snapshot: BrokerSnapshot,
        candidate: MarketExitCandidate,
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmission:
        """Acknowledge a simulated cancellation without contacting TWS."""
        del snapshot, host, port, client_id, timeout_seconds
        target = candidate.target_order_id
        stop = candidate.stop_order_id
        return PaperSubmission(order_ids=tuple(sorted((target, stop))), perm_ids=())

    def submit_trailing(
        self,
        snapshot: BrokerSnapshot,
        plan: TrailingPlan,
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmission:
        """Acknowledge one synthetic trailing order without contacting TWS."""
        del plan, host, port, client_id, timeout_seconds
        if self._journal is None:
            raise ValueError("demo journal is unavailable")
        entries = self._journal.submission_entries(
            account=snapshot.selected.account, con_id=snapshot.selected.con_id
        ) + self._journal.trailing_entries(
            account=snapshot.selected.account, con_id=snapshot.selected.con_id
        )
        order_id = (
            max(
                (value for entry in entries for value in entry.order_ids),
                default=900_000,
            )
            + 1
        )
        perm_id = (
            max(
                (value for entry in entries for value in entry.perm_ids),
                default=800_000,
            )
            + 1
        )
        return PaperSubmission((order_id,), (perm_id,))

    def modify_prices(
        self,
        snapshot: BrokerSnapshot,
        candidates: tuple[PriceUpdateCandidate, ...],
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmission:
        """Acknowledge only the exact simulated legs selected for amendment."""
        del host, port, client_id, timeout_seconds
        orders = {order.order_id: order for order in snapshot.working_orders}
        acknowledged: list[tuple[int, int]] = []
        for candidate in candidates:
            for order_id, perm_id, price in (
                (
                    candidate.layer.target_order_id,
                    candidate.layer.target_perm_id,
                    candidate.target_price,
                ),
                (
                    candidate.layer.stop_order_id,
                    candidate.layer.stop_perm_id,
                    candidate.stop_price,
                ),
            ):
                if price is None:
                    continue
                order = orders.get(order_id)
                if (
                    order is None
                    or order.perm_id != perm_id
                    or order.key != snapshot.selected
                ):
                    raise ValueError(
                        "the simulated OCA leg changed before acknowledgement"
                    )
                acknowledged.append((order_id, perm_id))
        return PaperSubmission(
            order_ids=tuple(order_id for order_id, _ in acknowledged),
            perm_ids=tuple(perm_id for _, perm_id in acknowledged),
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
    "DEMO_SCENARIOS",
    "DEMO_TRAILING_CON_ID",
    "DemoPaperExecutionTransport",
    "DemoReadOnlyBroker",
    "DemoSnapshotSource",
    "seed_demo_journal",
]
