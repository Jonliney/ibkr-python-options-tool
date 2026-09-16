from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol

from ..broker import SnapshotRequest
from ..domain import (
    BrokerSnapshot,
    PlanRequest,
    PlanStatus,
    RemainderPolicy,
    TriggerMethod,
    build_exit_plan,
)
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
class PlanForm:
    tranche_size: str = "2"
    target_percentages: str = "20, 40, 60, 80, 100"
    stop_loss_percentage: str = "20"
    remainder_policy: RemainderPolicy = RemainderPolicy.NEXT_RUNG
    tif: str = "GTC"
    trigger_method: TriggerMethod = TriggerMethod.DOUBLE_BID_ASK


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
    trigger_method: str
    logical_group: str


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


class SnapshotSource(Protocol):
    def refresh(self, request: SnapshotRequest) -> SnapshotResult: ...

    def current(self) -> SnapshotResult: ...


class PlannerViewModel:
    """Maps broker/domain values to a redacted GUI state; exposes no writes."""

    def __init__(
        self,
        snapshots: SnapshotSource,
        *,
        clock: Callable[[], Decimal],
    ) -> None:
        self._snapshots = snapshots
        self._clock = clock
        self._selection: ConnectionSelection | None = None
        self._account = ""

    def empty(self) -> ViewState:
        return _empty_state()

    def refresh(
        self,
        selection: ConnectionSelection,
        form: PlanForm,
    ) -> ViewState:
        self._selection = selection
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

    def _present(self, result: SnapshotResult, form: PlanForm) -> ViewState:
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
            return _unavailable_state(status, selection, validations)
        return _ready_state(
            result.snapshot,
            selection,
            form,
            now=self._clock(),
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
                "Allocated —",
                "Planned —",
            ),
            pairs=(),
            route_marks=base_marks,
            validations=(
                input_error or ValidationLine("INPUT_INVALID", "Invalid input"),
            ),
            fingerprint=None,
            can_preview=True,
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
            trigger_method=pair.stop.trigger_method.value
            if pair.stop.trigger_method is not None
            else "—",
            logical_group=pair.target.logical_oca_group,
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
                f"{pair.quantity} contract(s) · {pair.trigger_method}",
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
            f"Allocated {result.allocated_quantity}",
            f"Planned {result.planned_quantity}",
        ),
        pairs=pairs,
        route_marks=tuple(route_marks),
        validations=validations,
        fingerprint=result.fingerprint,
        can_preview=True,
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
        allocation=("Position —", "Allocated —", "Planned —"),
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
        allocation=("Position —", "Allocated —", "Planned —"),
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
            trigger_method=form.trigger_method,
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
