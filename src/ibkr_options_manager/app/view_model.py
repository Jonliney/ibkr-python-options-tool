from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol

from ..broker import PortfolioRequest, SnapshotRequest
from ..domain import (
    BrokerSnapshot,
    LayerRequest,
    PlanRequest,
    PlanResult,
    PlanStatus,
    PriceBand,
    RemainderPolicy,
    build_exit_plan,
)
from ..portfolio import PortfolioPosition, PortfolioResult, PortfolioStatus
from ..snapshot import SnapshotResult, SnapshotStatus


class UiStatus(StrEnum):
    EMPTY = "EMPTY"
    READY = "READY"
    BLOCKED = "BLOCKED"
    STALE = "STALE"


class FactState(StrEnum):
    PASS = "PASS"
    INFO = "INFO"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class ConnectionSelection:
    account: str
    con_id: int
    port: int = 7497
    client_id: int = 17
    timeout_seconds: float = 20.0


@dataclass(frozen=True, slots=True)
class ConnectionSettings:
    account: str
    port: int = 7497
    client_id: int = 17
    timeout_seconds: float = 20.0


@dataclass(frozen=True, slots=True)
class PlanForm:
    tranche_size: str = "2"
    target_percentages: str = "20, 40, 60, 80, 100"
    stop_loss_percentage: str = "20"
    remainder_policy: RemainderPolicy = RemainderPolicy.NEXT_RUNG
    tif: str = "GTC"
    layers: tuple[DraftLayerForm, ...] = ()
    paper_execution_mode: bool = False


@dataclass(frozen=True, slots=True)
class DraftLayerForm:
    quantity: str
    target_price: str
    stop_price: str
    target_percentage: str
    tif: str = "GTC"
    runner: bool = False
    stop_percentage: str = ""


@dataclass(frozen=True, slots=True)
class Fact:
    label: str
    value: str
    state: FactState = FactState.INFO


@dataclass(frozen=True, slots=True)
class PlanPairLine:
    index: int
    quantity: int
    target_percentage: Decimal
    target_raw: Decimal
    target_price: Decimal
    stop_raw: Decimal
    stop_price: Decimal
    tif: str
    logical_group: str
    runner: bool = False


@dataclass(frozen=True, slots=True)
class RouteMark:
    label: str
    price: Decimal
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class ValidationLine:
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class PortfolioPositionLine:
    con_id: int
    local_symbol: str
    quantity: str
    unit_basis: str
    working_order_count: int
    eligible: bool
    eligibility: str


@dataclass(frozen=True, slots=True)
class WorkingOrderLine:
    perm_id: int
    action: str
    order_type: str
    remaining: str
    status: str
    order_id: int = 0
    oca_group: str | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: str = ""


@dataclass(frozen=True, slots=True)
class QuoteCalculatorLine:
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    market_data_type: str
    fresh: bool
    bands: tuple[PriceBand, ...]


@dataclass(frozen=True, slots=True)
class PaperExecutionCandidate:
    """A valid plan paired with the snapshot captured immediately before send."""

    snapshot: BrokerSnapshot
    plan: PlanResult
    selection: ConnectionSelection


@dataclass(frozen=True, slots=True)
class PreviewRow:
    values: tuple[str, str, str, str, str, str]


@dataclass(frozen=True, slots=True)
class ViewState:
    status: UiStatus
    status_message: str
    account: str
    connection: tuple[Fact, ...]
    position_title: str
    position: tuple[Fact, ...]
    quote: tuple[Fact, ...]
    market_rule: tuple[str, ...]
    snapshot_age: str
    allocation: tuple[str, str, str]
    pairs: tuple[PlanPairLine, ...]
    route_marks: tuple[RouteMark, ...]
    validations: tuple[ValidationLine, ...]
    fingerprint: str | None
    can_preview: bool
    positions: tuple[PortfolioPositionLine, ...] = ()
    selected_con_id: int | None = None
    working_orders: tuple[WorkingOrderLine, ...] = ()
    preview_headers: tuple[str, str, str, str, str, str] = (
        "Pair",
        "Qty",
        "Target",
        "Stop",
        "TIF",
        "Logical OCA",
    )
    preview_rows: tuple[PreviewRow, ...] = ()
    bracket_form: PlanForm = PlanForm()
    quote_calculator: QuoteCalculatorLine | None = None
    available_quantity: int = 0
    unit_basis: Decimal | None = None
    multiplier: Decimal | None = None


class SnapshotSource(Protocol):
    def refresh(self, request: SnapshotRequest) -> SnapshotResult: ...

    def current(self) -> SnapshotResult: ...


class PortfolioSource(Protocol):
    def refresh(self, request: PortfolioRequest) -> PortfolioResult: ...

    def current(self) -> PortfolioResult: ...


class PlannerViewModel:
    """Maps broker/domain values to a redacted GUI state; exposes no writes."""

    def __init__(
        self,
        snapshots: SnapshotSource,
        *,
        clock: Callable[[], Decimal],
        portfolio: PortfolioSource | None = None,
    ) -> None:
        self._snapshots = snapshots
        self._portfolio = portfolio
        self._clock = clock
        self._selection: ConnectionSelection | None = None
        self._settings: ConnectionSettings | None = None
        self._account = ""
        self._portfolio_positions: tuple[PortfolioPosition, ...] = ()
        self._portfolio_lines: tuple[PortfolioPositionLine, ...] = ()
        self._bracket_forms: dict[int, PlanForm] = {}
        self._latest_snapshot: BrokerSnapshot | None = None

    def empty(self) -> ViewState:
        return _empty_state()

    def latest_snapshot(self) -> BrokerSnapshot | None:
        """Return the most recent coherent selected-position snapshot, if any."""
        return self._latest_snapshot

    def refresh_portfolio(self, settings: ConnectionSettings) -> ViewState:
        self._settings = settings
        self._selection = None
        self._latest_snapshot = None
        self._account = settings.account
        self._portfolio_positions = ()
        self._portfolio_lines = ()
        if self._portfolio is None:
            return _portfolio_unavailable_state(
                settings,
                (ValidationLine("PORTFOLIO_UNAVAILABLE", "Portfolio source missing"),),
            )
        try:
            result = self._portfolio.refresh(
                PortfolioRequest(
                    host="127.0.0.1",
                    port=settings.port,
                    client_id=settings.client_id,
                    expected_account=settings.account,
                    timeout_seconds=settings.timeout_seconds,
                )
            )
        except Exception as error:  # the GUI boundary must fail closed
            return _portfolio_unavailable_state(
                settings,
                (
                    ValidationLine(
                        "PORTFOLIO_REFRESH_FAILED", _redact(str(error), self._account)
                    ),
                ),
            )
        if result.status is not PortfolioStatus.READY or result.snapshot is None:
            validations = tuple(
                ValidationLine("PORTFOLIO_BLOCKED", _redact(message, self._account))
                for message in result.errors
            ) or (
                ValidationLine("PORTFOLIO_BLOCKED", "No coherent portfolio is ready"),
            )
            return _portfolio_unavailable_state(settings, validations)

        self._portfolio_positions = result.snapshot.positions
        self._portfolio_lines = tuple(
            _portfolio_line(position) for position in result.snapshot.positions
        )
        return _portfolio_ready_state(
            result,
            settings,
            self._portfolio_lines,
            now=self._clock(),
        )

    def select_position(
        self,
        con_id: int,
        form: PlanForm | None = None,
    ) -> ViewState:
        settings = self._settings
        if settings is None:
            return _empty_state()
        if con_id not in {
            position.key.con_id for position in self._portfolio_positions
        }:
            return _portfolio_unavailable_state(
                settings,
                (
                    ValidationLine(
                        "POSITION_NOT_IN_PORTFOLIO", "Select a listed option position"
                    ),
                ),
                positions=self._portfolio_lines,
            )
        self._selection = ConnectionSelection(
            account=settings.account,
            con_id=con_id,
            port=settings.port,
            client_id=settings.client_id,
            timeout_seconds=settings.timeout_seconds,
        )
        self._latest_snapshot = None
        chosen = (
            form
            if form is not None
            else self._bracket_forms.get(con_id, PlanForm())
        )
        try:
            result = self._snapshots.refresh(
                SnapshotRequest(
                    host="127.0.0.1",
                    port=settings.port,
                    client_id=settings.client_id,
                    expected_account=settings.account,
                    option_con_id=con_id,
                    timeout_seconds=settings.timeout_seconds,
                )
            )
        except Exception as error:  # the GUI boundary must fail closed
            state = _unavailable_state(
                UiStatus.BLOCKED,
                self._selection,
                (
                    ValidationLine(
                        "POSITION_REFRESH_FAILED", _redact(str(error), self._account)
                    ),
                ),
            )
            return self._decorate(state, selected_con_id=con_id, form=chosen)
        return self._present_selected(result, chosen)

    def preview_action(self, form: PlanForm) -> ViewState:
        if self._selection is None:
            return _empty_state()
        try:
            result = self._snapshots.current()
        except Exception as error:  # the GUI boundary must fail closed
            state = _unavailable_state(
                UiStatus.BLOCKED,
                self._selection,
                (
                    ValidationLine(
                        "SNAPSHOT_FAILED", _redact(str(error), self._account)
                    ),
                ),
            )
            return self._decorate(
                state,
                selected_con_id=self._selection.con_id,
                form=form,
            )
        return self._present_selected(result, form)

    def prepare_paper_execution(
        self,
        form: PlanForm,
    ) -> tuple[ViewState, PaperExecutionCandidate | None]:
        """Refresh once and return the only snapshot/plan pair eligible to send.

        The caller must still require a separate confirmation before forwarding
        this candidate to the execution service.
        """
        selection = self._selection
        if selection is None:
            return _empty_state(), None
        execution_form = replace(form, paper_execution_mode=True)
        try:
            result = self._snapshots.refresh(
                SnapshotRequest(
                    host="127.0.0.1",
                    port=selection.port,
                    client_id=selection.client_id,
                    expected_account=selection.account,
                    option_con_id=selection.con_id,
                    timeout_seconds=selection.timeout_seconds,
                )
            )
        except Exception as error:  # execution must fail closed at the GUI seam
            state = _unavailable_state(
                UiStatus.BLOCKED,
                selection,
                (
                    ValidationLine(
                        "EXECUTION_REFRESH_FAILED", _redact(str(error), self._account)
                    ),
                ),
            )
            return self._decorate(
                state,
                selected_con_id=selection.con_id,
                form=execution_form,
            ), None

        state = self._present_selected(result, execution_form)
        if result.status is not SnapshotStatus.READY or result.snapshot is None:
            self._latest_snapshot = None
            return state, None
        request, input_error = _parse_plan_form(execution_form)
        if input_error is not None or request is None:
            return state, None
        plan = build_exit_plan(result.snapshot, request)
        if plan.status is not PlanStatus.VALID:
            return state, None
        return state, PaperExecutionCandidate(result.snapshot, plan, selection)

    def refresh(
        self,
        selection: ConnectionSelection,
        form: PlanForm,
    ) -> ViewState:
        self._selection = selection
        self._latest_snapshot = None
        self._account = selection.account
        try:
            request = SnapshotRequest(
                host="127.0.0.1",
                port=selection.port,
                client_id=selection.client_id,
                expected_account=selection.account,
                option_con_id=selection.con_id,
                timeout_seconds=selection.timeout_seconds,
            )
            result = self._snapshots.refresh(request)
        except Exception as error:  # the GUI boundary must fail closed
            message = _redact(str(error), selection.account)
            return _unavailable_state(
                UiStatus.BLOCKED,
                selection,
                (ValidationLine("REFRESH_FAILED", message),),
            )
        return self._present(result, form)

    def preview(self, form: PlanForm) -> ViewState:
        if self._selection is None:
            return _empty_state()
        try:
            result = self._snapshots.current()
        except Exception as error:  # the GUI boundary must fail closed
            message = _redact(str(error), self._account)
            return _unavailable_state(
                UiStatus.BLOCKED,
                self._selection,
                (ValidationLine("SNAPSHOT_FAILED", message),),
            )
        return self._present(result, form)

    def _present_selected(
        self,
        result: SnapshotResult,
        form: PlanForm,
    ) -> ViewState:
        selection = self._selection
        if selection is None:
            return _empty_state()
        if result.status is not SnapshotStatus.READY or result.snapshot is None:
            status = (
                UiStatus.STALE
                if result.status is SnapshotStatus.STALE
                else UiStatus.BLOCKED
            )
            validations = tuple(
                ValidationLine("SNAPSHOT_BLOCKED", _redact(message, self._account))
                for message in result.errors
            ) or (ValidationLine("SNAPSHOT_BLOCKED", "No coherent snapshot is ready"),)
            state = _unavailable_state(status, selection, validations)
            return self._decorate(
                state,
                selected_con_id=selection.con_id,
                form=form,
            )

        self._latest_snapshot = result.snapshot
        self._bracket_forms[selection.con_id] = form
        state = _with_preview_rows(
            _ready_state(result.snapshot, selection, form, now=self._clock()),
            form,
        )
        return self._decorate(
            state,
            selected_con_id=selection.con_id,
            form=form,
            snapshot=result.snapshot,
        )

    def _decorate(
        self,
        state: ViewState,
        *,
        selected_con_id: int | None,
        form: PlanForm,
        snapshot: BrokerSnapshot | None = None,
    ) -> ViewState:
        orders = (
            ()
            if snapshot is None
            else _working_order_lines(snapshot)
        )
        return replace(
            state,
            positions=self._portfolio_lines,
            selected_con_id=selected_con_id,
            working_orders=orders,
            bracket_form=state.bracket_form
            if state.bracket_form != PlanForm()
            else form,
            quote_calculator=(
                None
                if snapshot is None
                else _quote_calculator_line(snapshot)
            ),
        )

    def _present(self, result: SnapshotResult, form: PlanForm) -> ViewState:
        selection = self._selection
        if selection is None:
            return _empty_state()
        if result.status is not SnapshotStatus.READY or result.snapshot is None:
            self._latest_snapshot = None
            status = (
                UiStatus.STALE
                if result.status is SnapshotStatus.STALE
                else UiStatus.BLOCKED
            )
            validations = tuple(
                ValidationLine("SNAPSHOT_BLOCKED", _redact(message, self._account))
                for message in result.errors
            ) or (ValidationLine("SNAPSHOT_BLOCKED", "No coherent snapshot is ready"),)
            return _unavailable_state(status, selection, validations)
        self._latest_snapshot = result.snapshot
        return _with_preview_rows(
            _ready_state(result.snapshot, selection, form, now=self._clock()),
            form,
        )


def _ready_state(
    snapshot: BrokerSnapshot,
    selection: ConnectionSelection,
    form: PlanForm,
    *,
    now: Decimal,
) -> ViewState:
    age = max(Decimal("0"), now - snapshot.captured_at)
    account = _redact_account(selection.account)
    connection = (
        Fact("Endpoint", f"127.0.0.1:{selection.port}", FactState.PASS),
        Fact("Client ID", str(selection.client_id), FactState.PASS),
        Fact(
            "Server / API",
            f"{snapshot.server_version or 'unknown'} / {_api_version()}",
        ),
        Fact("Server time", _server_time(snapshot.server_time)),
        Fact(
            "Read-only API",
            _yes_no(snapshot.read_only_api),
            _pass_or_block(snapshot.read_only_api),
        ),
        Fact(
            "Localhost only",
            _yes_no(snapshot.localhost_only),
            _pass_or_block(snapshot.localhost_only),
        ),
        Fact(
            "Paper account",
            account if snapshot.paper_account_verified else "not verified",
            _pass_or_block(snapshot.paper_account_verified),
        ),
        Fact("Connection epoch", str(snapshot.connection_epoch)),
        Fact("Snapshot age", _format_age(age), FactState.PASS),
    )
    contract = snapshot.contract
    position = (
        Fact("Contract ID", str(contract.con_id)),
        Fact("Type", contract.sec_type, FactState.PASS),
        Fact("Expiry", contract.expiry),
        Fact("Strike / right", f"{contract.strike} {contract.right}"),
        Fact("Trading class", contract.trading_class),
        Fact("Multiplier", str(contract.multiplier)),
        Fact("Position", str(snapshot.position.quantity), FactState.PASS),
        Fact("Unit basis", _money(snapshot.position.unit_basis)),
        Fact("Visible orders", str(len(snapshot.working_orders))),
    )
    quote = (
        Fact("Market data", snapshot.quote.market_data_type, FactState.PASS),
        Fact("Bid", _maybe_money(snapshot.quote.bid)),
        Fact("Ask", _maybe_money(snapshot.quote.ask)),
        Fact("Last", _maybe_money(snapshot.quote.last)),
        Fact("Close", _maybe_money(snapshot.quote.close)),
    )
    market_rule = tuple(
        f"{band.low_edge}+  ·  tick {band.increment}"
        for band in snapshot.market_rule.bands
    )
    request, input_error = _parse_plan_form(form)
    base_marks = _observed_marks(snapshot)
    if input_error is not None or request is None:
        return ViewState(
            status=UiStatus.BLOCKED,
            status_message="Plan inputs need attention",
            account=account,
            connection=connection,
            position_title=contract.local_symbol,
            position=position,
            quote=quote,
            market_rule=market_rule,
            snapshot_age=_format_age(age),
            allocation=(
                f"Position {_quantity(snapshot.position.quantity)}",
                "Reserved —",
                "Bracketed —",
            ),
            pairs=(),
            route_marks=base_marks,
            validations=(
                input_error or ValidationLine("INPUT_INVALID", "Invalid input"),
            ),
            fingerprint=None,
            can_preview=True,
            quote_calculator=_quote_calculator_line(snapshot),
        )

    result = build_exit_plan(snapshot, request)
    pairs = tuple(
        PlanPairLine(
            index=pair.index,
            quantity=pair.quantity,
            target_percentage=pair.target_percentage,
            target_raw=pair.target.raw_price,
            target_price=pair.target.rounded_price,
            stop_raw=pair.stop.raw_price,
            stop_price=pair.stop.rounded_price,
            tif=pair.target.tif,
            logical_group=pair.target.logical_oca_group,
            runner=pair.runner,
        )
        for pair in result.pairs
    )
    validations = tuple(
        ValidationLine(
            item.code,
            _redact(item.message, selection.account),
            item.blocking,
        )
        for item in result.validations
    )
    route_marks = (
        *base_marks,
        *(
            RouteMark(
                f"T{pair.index}",
                pair.target_price,
                "TARGET",
                f"+{pair.target_percentage}% · {pair.quantity} contract(s)",
            )
            for pair in pairs
        ),
        *(
            RouteMark(
                f"S{pair.index}",
                pair.stop_price,
                "STOP",
                f"{pair.quantity} contract(s)",
            )
            for pair in pairs
        ),
    )
    ready = result.status is PlanStatus.VALID
    return ViewState(
        status=UiStatus.READY if ready else UiStatus.BLOCKED,
        status_message=(
            "Plan ready for inspection" if ready else "Plan blocked by validation"
        ),
        account=account,
        connection=connection,
        position_title=contract.local_symbol,
        position=position,
        quote=quote,
        market_rule=market_rule,
        snapshot_age=_format_age(age),
        allocation=(
            f"Position {_quantity(snapshot.position.quantity)}",
            f"Reserved {result.allocated_quantity}",
            (
                f"Bracketed {result.planned_quantity} · Open "
                f"{result.available_quantity - result.planned_quantity}"
            ),
        ),
        pairs=pairs,
        route_marks=tuple(route_marks),
        validations=validations,
        fingerprint=result.fingerprint,
        can_preview=True,
        quote_calculator=_quote_calculator_line(snapshot),
        available_quantity=result.available_quantity,
        unit_basis=snapshot.position.unit_basis,
        multiplier=snapshot.contract.multiplier,
    )


def _with_preview_rows(state: ViewState, form: PlanForm) -> ViewState:
    return replace(
        state,
        preview_rows=tuple(
            PreviewRow(
                (
                    f"{pair.index:02d}",
                    str(pair.quantity),
                    f"{pair.target_price}  (+{pair.target_percentage}%)",
                    str(pair.stop_price),
                    pair.tif,
                    pair.logical_group,
                )
            )
            for pair in state.pairs
        ),
        bracket_form=form,
    )


def _quote_calculator_line(snapshot: BrokerSnapshot) -> QuoteCalculatorLine:
    return QuoteCalculatorLine(
        bid=snapshot.quote.bid,
        ask=snapshot.quote.ask,
        last=snapshot.quote.last,
        market_data_type=snapshot.quote.market_data_type,
        fresh=snapshot.quote.fresh,
        bands=snapshot.market_rule.bands,
    )


def _portfolio_line(position: PortfolioPosition) -> PortfolioPositionLine:
    return PortfolioPositionLine(
        con_id=position.key.con_id,
        local_symbol=position.contract.local_symbol or str(position.key.con_id),
        quantity=_quantity(position.quantity),
        unit_basis="—" if position.unit_basis is None else _money(position.unit_basis),
        working_order_count=len(position.working_orders),
        eligible=position.eligible,
        eligibility=position.eligibility,
    )


def _portfolio_ready_state(
    result: PortfolioResult,
    settings: ConnectionSettings,
    positions: tuple[PortfolioPositionLine, ...],
    *,
    now: Decimal,
) -> ViewState:
    snapshot = result.snapshot
    if snapshot is None:
        return _portfolio_unavailable_state(
            settings,
            (ValidationLine("PORTFOLIO_BLOCKED", "No coherent portfolio is ready"),),
        )
    age = max(Decimal("0"), now - snapshot.captured_at)
    count = len(positions)
    return ViewState(
        status=UiStatus.READY,
        status_message=(
            f"{count} open option position{'s' if count != 1 else ''} · select one"
            if count
            else "No open option positions found"
        ),
        account=_redact_account(settings.account),
        connection=(
            Fact("Endpoint", f"127.0.0.1:{settings.port}", FactState.PASS),
            Fact("Client ID", str(settings.client_id), FactState.PASS),
            Fact(
                "Server / API",
                f"{snapshot.server_version or 'unknown'} / {_api_version()}",
            ),
            Fact("Server time", _server_time(snapshot.server_time)),
            Fact("Read-only API", "verified", FactState.PASS),
            Fact("Localhost only", "verified", FactState.PASS),
            Fact("Paper account", _redact_account(settings.account), FactState.PASS),
            Fact("Connection epoch", str(snapshot.connection_epoch)),
            Fact("Snapshot age", _format_age(age), FactState.PASS),
        ),
        position_title="Select an open option position",
        position=(),
        quote=(),
        market_rule=(),
        snapshot_age=_format_age(age),
        allocation=("Position —", "Reserved —", "Bracketed —"),
        pairs=(),
        route_marks=(),
        validations=(),
        fingerprint=None,
        can_preview=False,
        positions=positions,
    )


def _portfolio_unavailable_state(
    settings: ConnectionSettings,
    validations: tuple[ValidationLine, ...],
    *,
    positions: tuple[PortfolioPositionLine, ...] = (),
) -> ViewState:
    return ViewState(
        status=UiStatus.BLOCKED,
        status_message="Portfolio state is not ready",
        account=_redact_account(settings.account),
        connection=(
            Fact("Endpoint", f"127.0.0.1:{settings.port}"),
            Fact("Client ID", str(settings.client_id)),
            Fact("Safety state", "not verified", FactState.BLOCKED),
        ),
        position_title="No verified position",
        position=(),
        quote=(),
        market_rule=(),
        snapshot_age="—",
        allocation=("Position —", "Reserved —", "Bracketed —"),
        pairs=(),
        route_marks=(),
        validations=validations,
        fingerprint=None,
        can_preview=False,
        positions=positions,
    )


def _working_order_lines(snapshot: BrokerSnapshot) -> tuple[WorkingOrderLine, ...]:
    return tuple(
        WorkingOrderLine(
            perm_id=order.perm_id,
            order_id=order.order_id,
            action=order.action,
            order_type=order.order_type,
            remaining=_quantity(order.remaining),
            status=order.status,
            oca_group=order.oca_group,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            tif=order.tif,
        )
        for order in snapshot.working_orders
        if order.key == snapshot.selected
    )


def _unavailable_state(
    status: UiStatus,
    selection: ConnectionSelection,
    validations: tuple[ValidationLine, ...],
) -> ViewState:
    return ViewState(
        status=status,
        status_message=(
            "Snapshot expired — refresh required"
            if status is UiStatus.STALE
            else "Broker state is not ready"
        ),
        account=_redact_account(selection.account),
        connection=(
            Fact("Endpoint", f"127.0.0.1:{selection.port}"),
            Fact("Client ID", str(selection.client_id)),
            Fact("Safety state", "not verified", FactState.BLOCKED),
        ),
        position_title="No verified position",
        position=(),
        quote=(),
        market_rule=(),
        snapshot_age="—",
        allocation=("Position —", "Reserved —", "Bracketed —"),
        pairs=(),
        route_marks=(),
        validations=validations,
        fingerprint=None,
        can_preview=False,
    )


def _empty_state() -> ViewState:
    return ViewState(
        status=UiStatus.EMPTY,
        status_message="Enter the paper account and option contract, then refresh",
        account="—",
        connection=(),
        position_title="No verified position",
        position=(),
        quote=(),
        market_rule=(),
        snapshot_age="—",
        allocation=("Position —", "Reserved —", "Bracketed —"),
        pairs=(),
        route_marks=(),
        validations=(),
        fingerprint=None,
        can_preview=False,
    )


def _parse_plan_form(
    form: PlanForm,
) -> tuple[PlanRequest | None, ValidationLine | None]:
    try:
        layers = tuple(
            LayerRequest(
                quantity=int(layer.quantity.strip()),
                target_price=Decimal(layer.target_price.strip()),
                stop_price=Decimal(layer.stop_price.strip()),
                tif=layer.tif.strip().upper(),
                target_percentage=(
                    None
                    if not layer.target_percentage.strip()
                    else Decimal(layer.target_percentage.strip())
                ),
                runner=layer.runner,
            )
            for layer in form.layers
        )
        tranche_size = int(form.tranche_size.strip())
        targets = tuple(
            Decimal(part.strip())
            for part in form.target_percentages.split(",")
            if part.strip()
        )
        stop = Decimal(form.stop_loss_percentage.strip())
    except (InvalidOperation, ValueError):
        return None, ValidationLine(
            "INPUT_INVALID",
            "Use a whole-number tranche size and comma-separated numeric percentages",
        )
    return (
        PlanRequest(
            tranche_size=tranche_size,
            target_percentages=targets,
            stop_loss_percentage=stop,
            remainder_policy=form.remainder_policy,
            tif=form.tif.strip().upper(),
            layers=layers,
            paper_execution_mode=form.paper_execution_mode,
        ),
        None,
    )


def _observed_marks(snapshot: BrokerSnapshot) -> tuple[RouteMark, ...]:
    marks = [RouteMark("BASIS", snapshot.position.unit_basis, "BASIS", "unit premium")]
    for label, value in (
        ("BID", snapshot.quote.bid),
        ("ASK", snapshot.quote.ask),
        ("LAST", snapshot.quote.last),
    ):
        if value is not None:
            marks.append(
                RouteMark(label, value, "QUOTE", snapshot.quote.market_data_type)
            )
    return tuple(marks)


def _api_version() -> str:
    try:
        return version("ibapi")
    except PackageNotFoundError:
        return "unknown"


def _redact(message: str, account: str) -> str:
    return message.replace(account, _redact_account(account)) if account else message


def _redact_account(account: str) -> str:
    return "****" if len(account) <= 4 else f"***{account[-4:]}"


def _pass_or_block(value: bool) -> FactState:
    return FactState.PASS if value else FactState.BLOCKED


def _yes_no(value: bool) -> str:
    return "verified" if value else "not verified"


def _format_age(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.1'))} s"


def _money(value: Decimal) -> str:
    return f"${value.normalize()}"


def _maybe_money(value: Decimal | None) -> str:
    return "—" if value is None else _money(value)


def _quantity(value: Decimal) -> str:
    return str(int(value)) if value == value.to_integral_value() else str(value)


def _server_time(value: int | None) -> str:
    if value is None:
        return "unknown"
    return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
