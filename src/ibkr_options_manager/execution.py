"""Paper-only execution controls, kept separate from planning and snapshots."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable

from .domain import (
    BrokerSnapshot,
    ObservedExecution,
    PlanResult,
    PlanStatus,
    WorkingOrder,
)


class ExecutionBlocked(RuntimeError):
    """Raised before any broker write when the execution contract is not met."""


class ExecutionOutcomeUnknown(ExecutionBlocked):
    """Raised after a write request when TWS does not provide enough evidence."""


@dataclass(frozen=True, slots=True)
class JournalLayer:
    quantity: int
    target_price: str
    stop_price: str
    tif: str
    target_perm_id: int = 0
    stop_perm_id: int = 0


@dataclass(frozen=True, slots=True)
class JournalFill:
    exec_id: str
    perm_id: int
    side: str
    quantity: str
    price: str
    time: str
    realized_pnl: str | None = None
    currency: str = ""


@dataclass(frozen=True, slots=True)
class LayerOutcome:
    status: str
    filled_quantity: Decimal = Decimal("0")
    realized_pnl: Decimal | None = None
    currency: str = ""
    exit_side: str = ""


@dataclass(frozen=True, slots=True)
class JournalEntry:
    fingerprint: str
    account: str
    con_id: int
    state: str
    expected_order_count: int = 0
    order_ids: tuple[int, ...] = ()
    perm_ids: tuple[int, ...] = ()
    snapshot_captured_at: str = ""
    layers: tuple[JournalLayer, ...] = ()
    fills: tuple[JournalFill, ...] = ()


def classify_journal_layer(
    entry: JournalEntry,
    index: int,
    *,
    active_perm_ids: frozenset[int],
    observed_perm_ids: frozenset[int],
) -> LayerOutcome:
    """Classify one planned layer only from exact journal and broker evidence."""
    layer = entry.layers[index]
    target_id, stop_id = layer.target_perm_id, layer.stop_perm_id
    if (not target_id or not stop_id) and len(entry.perm_ids) == len(entry.layers) * 2:
        target_id, stop_id = entry.perm_ids[index * 2 : index * 2 + 2]
    ids = {target_id, stop_id} - {0}
    related = tuple(fill for fill in entry.fills if fill.perm_id in ids)
    try:
        filled = sum((Decimal(fill.quantity) for fill in related), Decimal("0"))
    except InvalidOperation:
        return LayerOutcome("UNKNOWN")
    if filled > 0:
        exit_side = (
            "Target and stop"
            if {fill.perm_id for fill in related} == ids
            else "Target"
            if any(fill.perm_id == target_id for fill in related)
            else "Stop"
        )
        if filled < layer.quantity:
            return LayerOutcome("PARTIAL", filled_quantity=filled, exit_side=exit_side)
        if filled > layer.quantity:
            return LayerOutcome("UNKNOWN", filled_quantity=filled)
        currencies = {fill.currency for fill in related if fill.currency}
        if (
            any(fill.realized_pnl is None or not fill.currency for fill in related)
            or len(currencies) != 1
        ):
            return LayerOutcome(
                "CLOSED_PNL_UNKNOWN", filled_quantity=filled, exit_side=exit_side
            )
        try:
            pnl = sum(
                (
                    Decimal(fill.realized_pnl)
                    for fill in related
                    if fill.realized_pnl is not None
                ),
                Decimal("0"),
            )
        except InvalidOperation:
            return LayerOutcome(
                "CLOSED_PNL_UNKNOWN", filled_quantity=filled, exit_side=exit_side
            )
        return LayerOutcome(
            "CLOSED_PROFIT" if pnl > 0 else "CLOSED_LOSS" if pnl < 0 else "CLOSED_FLAT",
            filled_quantity=filled,
            realized_pnl=pnl,
            currency=next(iter(currencies)),
            exit_side=exit_side,
        )
    if ids and ids.issubset(active_perm_ids):
        return LayerOutcome("ACTIVE")
    if entry.state in {"PREPARED", "SUBMISSION_UNKNOWN"}:
        return LayerOutcome("UNKNOWN")
    if not ids & observed_perm_ids and (
        entry.state in {"RECONCILED", "SUPERSEDED"}
        or bool(set(entry.perm_ids) & observed_perm_ids)
    ):
        return LayerOutcome("NO_EXECUTION_EVIDENCE")
    return LayerOutcome("PENDING")


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    """The durable record returned after one deliberately non-retryable send."""

    entry: JournalEntry


class PaperOrderTransport(Protocol):
    """Narrow writer seam so the domain and GUI never import ibapi directly."""

    def submit(
        self,
        snapshot: BrokerSnapshot,
        plan: PlanResult,
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmissionResult: ...


class PaperSubmissionResult(Protocol):
    @property
    def order_ids(self) -> tuple[int, ...]: ...

    @property
    def perm_ids(self) -> tuple[int, ...]: ...


@dataclass(frozen=True, slots=True)
class MarketExitCandidate:
    """One app-owned OCA target that may be changed to a paper MKT exit."""

    account: str
    con_id: int
    target_order_id: int
    target_perm_id: int
    client_id: int
    quantity: Decimal
    tif: str
    oca_group: str
    stop_order_id: int
    stop_perm_id: int


@dataclass(frozen=True, slots=True)
class PriceUpdateCandidate:
    """A price-only amendment to one proven app-owned OCA pair."""

    layer: MarketExitCandidate
    target_price: Decimal | None = None
    stop_price: Decimal | None = None
    prior_target_price: Decimal | None = None
    prior_stop_price: Decimal | None = None


@runtime_checkable
class PaperMarketExitTransport(Protocol):
    """The narrow transport seam for a staged paper market exit."""

    def cancel_pair_then_submit_market(
        self,
        snapshot: BrokerSnapshot,
        candidate: MarketExitCandidate,
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmissionResult: ...


@runtime_checkable
class PaperBulkMarketExitTransport(Protocol):
    """Transport seam for a verified multi-layer market exit."""

    def cancel_pairs_then_submit_market(
        self,
        snapshot: BrokerSnapshot,
        candidates: tuple[MarketExitCandidate, ...],
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmissionResult: ...


@runtime_checkable
class PaperOcaCancellationTransport(Protocol):
    """Narrow transport seam for cancelling one app-owned OCA pair only."""

    def cancel_pair(
        self,
        snapshot: BrokerSnapshot,
        candidate: MarketExitCandidate,
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmissionResult: ...


@runtime_checkable
class PaperPriceUpdateTransport(Protocol):
    """Narrow transport seam for price-only amendments to app-owned pairs."""

    def modify_prices(
        self,
        snapshot: BrokerSnapshot,
        candidates: tuple[PriceUpdateCandidate, ...],
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> PaperSubmissionResult: ...


class ExecutionJournal:
    """Durable duplicate suppression for app-owned paper submissions."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def find(self, fingerprint: str) -> JournalEntry | None:
        return next(
            (
                entry
                for entry in reversed(self._entries())
                if entry.fingerprint == fingerprint
            ),
            None,
        )

    def owned_perm_ids(self, *, account: str, con_id: int) -> frozenset[int]:
        """Return permanent IDs of broker orders proven to be app-owned."""
        return frozenset(
            perm_id
            for entry in self._entries()
            if entry.account == account
            and entry.con_id == con_id
            and entry.state in {"SUBMITTED", "RECONCILED", "PARTIALLY_RECONCILED"}
            for perm_id in entry.perm_ids
            if perm_id > 0
        )

    def submission_entries(
        self, *, account: str, con_id: int
    ) -> tuple[JournalEntry, ...]:
        """Read paper order attempts without granting any management authority."""
        return tuple(
            entry
            for entry in self._entries()
            if entry.account == account
            and entry.con_id == con_id
            and len(entry.fingerprint) == 64
            and entry.state
            in {
                "PREPARED",
                "SUBMITTED",
                "SUBMISSION_UNKNOWN",
                "PARTIALLY_RECONCILED",
                "RECONCILED",
                "SUPERSEDED",
            }
        )

    def begin(self, snapshot: BrokerSnapshot, plan: PlanResult) -> JournalEntry:
        fingerprint = plan.fingerprint
        if plan.status is not PlanStatus.VALID or fingerprint is None:
            raise ExecutionBlocked("only a valid, fingerprinted plan may be sent")
        entries = list(self._entries())
        prior_index = next(
            (
                index
                for index in range(len(entries) - 1, -1, -1)
                if entries[index].fingerprint == fingerprint
            ),
            None,
        )
        if prior_index is not None and not self._may_replace_absent_attempt(
            entries[prior_index], snapshot, entries
        ):
            raise ExecutionBlocked(
                "this plan fingerprint is already journaled; no retry is automatic"
            )
        if prior_index is not None:
            # A known, app-owned attempt can be retired only after the exact
            # fresh snapshot proves every acknowledged broker order is gone.
            # PREPARED and acknowledgement-unknown attempts intentionally
            # remain non-retryable: a timeout must never create duplicates.
            prior = entries[prior_index]
            entries[prior_index] = JournalEntry(
                fingerprint=prior.fingerprint,
                account=prior.account,
                con_id=prior.con_id,
                state="SUPERSEDED",
                expected_order_count=prior.expected_order_count,
                order_ids=prior.order_ids,
                perm_ids=prior.perm_ids,
                snapshot_captured_at=prior.snapshot_captured_at,
                layers=prior.layers,
                fills=prior.fills,
            )
        entry = JournalEntry(
            fingerprint=fingerprint,
            account=snapshot.selected.account,
            con_id=snapshot.selected.con_id,
            state="PREPARED",
            expected_order_count=len(plan.pairs) * 2,
            snapshot_captured_at=str(snapshot.captured_at),
            layers=tuple(
                JournalLayer(
                    quantity=pair.quantity,
                    target_price=format(pair.target.rounded_price, "f"),
                    stop_price=format(pair.stop.rounded_price, "f"),
                    tif=pair.target.tif,
                )
                for pair in plan.pairs
            ),
        )
        self._write(tuple((*entries, entry)))
        return entry

    @staticmethod
    def _may_replace_absent_attempt(
        entry: JournalEntry,
        snapshot: BrokerSnapshot,
        entries: list[JournalEntry],
    ) -> bool:
        """Allow an explicit recreation only after known prior orders disappear.

        This is deliberately narrower than an ordinary retry.  We need known
        permanent IDs, a broker-confirmed submission/reconciliation state, and
        a later fresh snapshot for the same account and contract with none of
        those orders still working. Older journal entries without a capture
        time need a completed app cancellation for the same broker orders.
        An unknown outcome has no such proof and stays blocked forever pending
        manual investigation.
        """
        if (
            entry.state not in {"SUBMITTED", "RECONCILED", "PARTIALLY_RECONCILED"}
            or not entry.perm_ids
            or entry.account != snapshot.selected.account
            or entry.con_id != snapshot.selected.con_id
        ):
            return False
        prior_perm_ids = frozenset(entry.perm_ids)
        if any(
            order.key == snapshot.selected and order.perm_id in prior_perm_ids
            for order in snapshot.working_orders
        ):
            return False
        if entry.snapshot_captured_at:
            try:
                return snapshot.captured_at > Decimal(entry.snapshot_captured_at)
            except ArithmeticError:
                return False
        return any(
            completed.state == "COMPLETED"
            and set(completed.order_ids) & set(entry.order_ids)
            for completed in entries
        )

    def begin_market_exit(
        self, snapshot: BrokerSnapshot, candidate: MarketExitCandidate
    ) -> JournalEntry:
        """Durably reserve one exact app-owned pair before cancelling it.

        A cancellation can succeed just before a client disconnects, so this
        operation must be non-retryable in the same way as new OCA submission.
        """
        return self.begin_management(
            snapshot,
            operation="market-exit",
            material=(
                candidate.target_order_id,
                candidate.target_perm_id,
                candidate.stop_order_id,
                candidate.stop_perm_id,
                candidate.quantity,
            ),
            expected_order_count=1,
        )

    def begin_management(
        self,
        snapshot: BrokerSnapshot,
        *,
        operation: str,
        material: tuple[object, ...],
        expected_order_count: int,
    ) -> JournalEntry:
        """Durably reserve one non-retryable app-owned management attempt."""
        if not operation or expected_order_count <= 0:
            raise ExecutionBlocked("management journal entry is incomplete")
        encoded = ":".join(
            str(value)
            for value in (
                snapshot.selected.account,
                snapshot.selected.con_id,
                *material,
            )
        )
        fingerprint = f"{operation}:{sha256(encoded.encode()).hexdigest()}"
        if self.find(fingerprint) is not None:
            raise ExecutionBlocked(
                "this management attempt is already journaled; no retry is automatic"
            )
        entry = JournalEntry(
            fingerprint=fingerprint,
            account=snapshot.selected.account,
            con_id=snapshot.selected.con_id,
            state="PREPARED",
            expected_order_count=expected_order_count,
            snapshot_captured_at=str(snapshot.captured_at),
        )
        self._write((*self._entries(), entry))
        return entry

    def record_submission(
        self, fingerprint: str, *, order_ids: tuple[int, ...], perm_ids: tuple[int, ...]
    ) -> JournalEntry:
        entries = list(self._entries())
        for index in range(len(entries) - 1, -1, -1):
            entry = entries[index]
            if entry.fingerprint == fingerprint:
                layers = entry.layers
                if len(perm_ids) == len(layers) * 2 and all(perm_ids):
                    layers = tuple(
                        replace(
                            layer,
                            target_perm_id=perm_ids[layer_index * 2],
                            stop_perm_id=perm_ids[layer_index * 2 + 1],
                        )
                        for layer_index, layer in enumerate(layers)
                    )
                updated = JournalEntry(
                    fingerprint=entry.fingerprint,
                    account=entry.account,
                    con_id=entry.con_id,
                    state="SUBMISSION_UNKNOWN" if not perm_ids else "SUBMITTED",
                    expected_order_count=entry.expected_order_count,
                    order_ids=order_ids,
                    perm_ids=perm_ids,
                    snapshot_captured_at=entry.snapshot_captured_at,
                    layers=layers,
                    fills=entry.fills,
                )
                entries[index] = updated
                self._write(tuple(entries))
                return updated
        raise ExecutionBlocked(
            "execution journal entry disappeared before acknowledgement"
        )

    def mark_unknown(self, fingerprint: str) -> JournalEntry:
        """Record an indeterminate transport outcome and permanently block a retry."""
        return self.record_submission(fingerprint, order_ids=(), perm_ids=())

    def record_management_completion(
        self, fingerprint: str, *, order_ids: tuple[int, ...]
    ) -> JournalEntry:
        """Record a broker-confirmed management operation with no new orders."""
        entries = list(self._entries())
        for index in range(len(entries) - 1, -1, -1):
            entry = entries[index]
            if entry.fingerprint == fingerprint:
                updated = JournalEntry(
                    fingerprint=entry.fingerprint,
                    account=entry.account,
                    con_id=entry.con_id,
                    state="COMPLETED",
                    expected_order_count=entry.expected_order_count,
                    order_ids=order_ids,
                    perm_ids=(),
                    snapshot_captured_at=entry.snapshot_captured_at,
                    layers=entry.layers,
                    fills=entry.fills,
                )
                entries[index] = updated
                self._write(tuple(entries))
                return updated
        raise ExecutionBlocked(
            "management journal entry disappeared before acknowledgement"
        )

    def record_completed_orders(self, snapshot: BrokerSnapshot) -> None:
        """Recover planned layer IDs from exact app OCA groups for display only."""
        entries = list(self._entries())
        changed = False
        for index, entry in enumerate(entries):
            if (
                entry.account != snapshot.selected.account
                or entry.con_id != snapshot.selected.con_id
                or len(entry.fingerprint) != 64
                or not entry.layers
            ):
                continue
            layers = list(entry.layers)
            for layer_index, layer in enumerate(layers):
                group = f"{entry.fingerprint[:12]}/tranche-{layer_index + 1}"
                candidates = [
                    (order.oca_group, order.action, order.order_type, order.perm_id)
                    for order in snapshot.working_orders
                    if order.key == snapshot.selected
                ]
                if snapshot.completed_orders_complete:
                    candidates.extend(
                        (order.oca_group, order.action, order.order_type, order.perm_id)
                        for order in snapshot.completed_orders
                        if order.account == entry.account
                        and order.con_id == entry.con_id
                    )
                for order_type, field in (
                    ("LMT", "target_perm_id"),
                    ("STP", "stop_perm_id"),
                ):
                    matching_ids = {
                        perm_id
                        for oca_group, action, kind, perm_id in candidates
                        if oca_group == group
                        and action == "SELL"
                        and kind == order_type
                        and perm_id > 0
                    }
                    if len(matching_ids) != 1:
                        continue
                    perm_id = next(iter(matching_ids))
                    if field == "target_perm_id":
                        if layer.target_perm_id in {0, perm_id}:
                            layer = replace(layer, target_perm_id=perm_id)
                    elif layer.stop_perm_id in {0, perm_id}:
                        layer = replace(layer, stop_perm_id=perm_id)
                layers[layer_index] = layer
            if tuple(layers) != entry.layers:
                entries[index] = replace(entry, layers=tuple(layers))
                changed = True
        if changed:
            self._write(tuple(entries))

    def record_executions(self, snapshot: BrokerSnapshot) -> None:
        """Cache complete, exact-contract TWS fill evidence by permanent order ID."""
        if not snapshot.executions_complete:
            return
        entries = list(self._entries())
        changed = False
        for index, entry in enumerate(entries):
            if (
                entry.account != snapshot.selected.account
                or entry.con_id != snapshot.selected.con_id
                or len(entry.fingerprint) != 64
            ):
                continue
            known_ids = set(entry.perm_ids) | {
                perm_id
                for layer in entry.layers
                for perm_id in (layer.target_perm_id, layer.stop_perm_id)
                if perm_id > 0
            }
            if not known_ids:
                continue
            fills = {_execution_identity(fill.exec_id): fill for fill in entry.fills}
            for observed in snapshot.executions:
                if not _usable_execution(observed, snapshot, known_ids):
                    continue
                identity = _execution_identity(observed.exec_id)
                prior = fills.get(identity)
                if prior is not None and _execution_revision(
                    prior.exec_id
                ) > _execution_revision(observed.exec_id):
                    continue
                replacement = JournalFill(
                    exec_id=observed.exec_id,
                    perm_id=observed.perm_id,
                    side=observed.side,
                    quantity=format(observed.quantity, "f"),
                    price=format(observed.price, "f"),
                    time=observed.time,
                    realized_pnl=(
                        format(observed.realized_pnl, "f")
                        if observed.realized_pnl is not None
                        else prior.realized_pnl
                        if prior is not None and prior.exec_id == observed.exec_id
                        else None
                    ),
                    currency=observed.currency or (prior.currency if prior else ""),
                )
                fills[identity] = replacement
            updated_fills = tuple(sorted(fills.values(), key=lambda fill: fill.exec_id))
            if updated_fills != entry.fills:
                entries[index] = replace(entry, fills=updated_fills)
                changed = True
        if changed:
            self._write(tuple(entries))

    def reconcile_snapshot(
        self,
        snapshot: BrokerSnapshot,
    ) -> tuple[JournalEntry, ...]:
        """Adopt complete, app-identifiable OCA pairs observed from TWS.

        This does not turn an incomplete or arbitrary external order into an
        app-owned order.  Every observed order must use the plan fingerprint's
        OCA-group prefix, have a permanent ID, and form a complete SELL LMT /
        SELL STP pair. If an unknown submission has since lost a sibling pair
        in TWS, its surviving complete pairs can be recovered safely as
        ``PARTIALLY_RECONCILED``. The original expected count is retained so a
        later snapshot can promote the entry once every planned order is
        observed. Older journals have no count and are marked ``RECONCILED``.
        """
        entries = list(self._entries())
        reconciled: list[JournalEntry] = []
        for index, entry in enumerate(entries):
            if (
                entry.state
                not in {
                    "PREPARED",
                    "SUBMITTED",
                    "SUBMISSION_UNKNOWN",
                    "PARTIALLY_RECONCILED",
                }
                or entry.account != snapshot.selected.account
                or entry.con_id != snapshot.selected.con_id
            ):
                continue
            observed = _complete_app_oca_orders(snapshot, entry)
            if not observed:
                continue
            state = (
                "RECONCILED"
                if not entry.expected_order_count
                or len(observed) == entry.expected_order_count
                else "PARTIALLY_RECONCILED"
            )
            updated = JournalEntry(
                fingerprint=entry.fingerprint,
                account=entry.account,
                con_id=entry.con_id,
                state=state,
                expected_order_count=entry.expected_order_count,
                order_ids=tuple(order.order_id for order in observed),
                perm_ids=tuple(order.perm_id for order in observed),
                snapshot_captured_at=entry.snapshot_captured_at,
                layers=entry.layers,
                fills=entry.fills,
            )
            entries[index] = updated
            reconciled.append(updated)
        if reconciled:
            self._write(tuple(entries))
        return tuple(reconciled)

    def _entries(self) -> tuple[JournalEntry, ...]:
        try:
            payload = json.loads(self._path.read_text())
        except FileNotFoundError:
            return ()
        except (OSError, json.JSONDecodeError) as error:
            raise ExecutionBlocked(
                f"execution journal is unreadable: {error}"
            ) from error
        if not isinstance(payload, list):
            raise ExecutionBlocked("execution journal has an invalid format")
        try:
            return tuple(
                JournalEntry(
                    fingerprint=str(item["fingerprint"]),
                    account=str(item["account"]),
                    con_id=int(item["con_id"]),
                    state=str(item["state"]),
                    expected_order_count=int(item.get("expected_order_count", 0)),
                    order_ids=tuple(int(value) for value in item.get("order_ids", ())),
                    perm_ids=tuple(int(value) for value in item.get("perm_ids", ())),
                    snapshot_captured_at=str(item.get("snapshot_captured_at", "")),
                    layers=tuple(
                        JournalLayer(
                            quantity=int(layer["quantity"]),
                            target_price=str(layer["target_price"]),
                            stop_price=str(layer["stop_price"]),
                            tif=str(layer["tif"]),
                            target_perm_id=int(layer.get("target_perm_id", 0)),
                            stop_perm_id=int(layer.get("stop_perm_id", 0)),
                        )
                        for layer in item.get("layers", ())
                    ),
                    fills=tuple(
                        JournalFill(
                            exec_id=str(fill["exec_id"]),
                            perm_id=int(fill["perm_id"]),
                            side=str(fill["side"]),
                            quantity=str(fill["quantity"]),
                            price=str(fill["price"]),
                            time=str(fill["time"]),
                            realized_pnl=(
                                None
                                if fill.get("realized_pnl") is None
                                else str(fill["realized_pnl"])
                            ),
                            currency=str(fill.get("currency", "")),
                        )
                        for fill in item.get("fills", ())
                    ),
                )
                for item in payload
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ExecutionBlocked(
                "execution journal contains an invalid entry"
            ) from error

    def _write(self, entries: tuple[JournalEntry, ...]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps([asdict(entry) for entry in entries], indent=2))
        os.replace(temporary, self._path)


def require_paper_management_snapshot(snapshot: BrokerSnapshot) -> None:
    """Validate TWS/account safety before an app-owned order modification."""
    if (
        not snapshot.paper_account_verified
        or not snapshot.selected.account.upper().startswith("DU")
    ):
        raise ExecutionBlocked("paper account verification is required")
    if not snapshot.connected or not snapshot.complete or not snapshot.fresh:
        raise ExecutionBlocked("a complete, fresh connection snapshot is required")
    if not snapshot.localhost_only:
        raise ExecutionBlocked("localhost-only TWS access is required")
    if not snapshot.api_read_only_observed or snapshot.read_only_api:
        raise ExecutionBlocked("TWS must explicitly report API read-only mode disabled")


def require_paper_execution_snapshot(
    snapshot: BrokerSnapshot,
    plan: PlanResult,
    *,
    owned_perm_ids: frozenset[int] = frozenset(),
) -> None:
    """Validate the stricter write preconditions before opening a TWS writer."""
    if plan.status is not PlanStatus.VALID or plan.fingerprint is None:
        raise ExecutionBlocked("the refreshed plan is not valid")
    require_paper_management_snapshot(snapshot)
    external = [
        order
        for order in snapshot.working_orders
        if order.key == snapshot.selected and order.perm_id not in owned_perm_ids
    ]
    if external:
        raise ExecutionBlocked("external related orders block a new paper submission")


class PaperExecutionService:
    """The one application service permitted to call the isolated paper writer.

    A fresh plan must already have been built from the immediate pre-send
    snapshot.  The journal is written before opening TWS so a timeout or a
    process crash can never lead to an automatic duplicate submission.
    """

    def __init__(
        self,
        transport: PaperOrderTransport,
        journal: ExecutionJournal,
    ) -> None:
        self._transport = transport
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
    ) -> SubmissionReceipt:
        require_paper_execution_snapshot(
            snapshot,
            plan,
            owned_perm_ids=self.owned_perm_ids(
                account=snapshot.selected.account,
                con_id=snapshot.selected.con_id,
            ),
        )
        entry = self._journal.begin(snapshot, plan)
        try:
            result = self._transport.submit(
                snapshot,
                plan,
                host=host,
                port=port,
                client_id=client_id,
                timeout_seconds=timeout_seconds,
            )
            order_ids = tuple(int(value) for value in result.order_ids)
            perm_ids = tuple(int(value) for value in result.perm_ids)
            if not order_ids or len(order_ids) != len(perm_ids) or not all(perm_ids):
                raise ExecutionBlocked("TWS acknowledgement was incomplete")
        except Exception:
            self._journal.mark_unknown(entry.fingerprint)
            raise
        return SubmissionReceipt(
            self._journal.record_submission(
                entry.fingerprint,
                order_ids=order_ids,
                perm_ids=perm_ids,
            )
        )

    def reconcile_snapshot(self, snapshot: BrokerSnapshot) -> tuple[JournalEntry, ...]:
        """Record broker-observed app OCA pairs after an interrupted send."""
        return self._journal.reconcile_snapshot(snapshot)

    def record_completed_orders(self, snapshot: BrokerSnapshot) -> None:
        """Persist exact layer identifiers found in completed TWS orders."""
        self._journal.record_completed_orders(snapshot)

    def record_executions(self, snapshot: BrokerSnapshot) -> None:
        """Persist read-only fill and realized P&L observations."""
        self._journal.record_executions(snapshot)

    def owned_perm_ids(self, *, account: str, con_id: int) -> frozenset[int]:
        """Return only journal-proven app-owned broker order IDs."""
        return self._journal.owned_perm_ids(account=account, con_id=con_id)

    def submission_entries(
        self, *, account: str, con_id: int
    ) -> tuple[JournalEntry, ...]:
        """Expose durable submission attempts to the read-only UI."""
        return self._journal.submission_entries(account=account, con_id=con_id)

    def prepare_market_exit(
        self,
        snapshot: BrokerSnapshot,
        *,
        target_perm_id: int,
        expected_client_id: int,
    ) -> MarketExitCandidate:
        """Return one exact app-owned OCA target eligible for an MKT change."""
        require_paper_management_snapshot(snapshot)
        owned = self.owned_perm_ids(
            account=snapshot.selected.account,
            con_id=snapshot.selected.con_id,
        )
        targets = [
            order
            for order in snapshot.working_orders
            if order.perm_id == target_perm_id
        ]
        if len(targets) != 1:
            raise ExecutionBlocked("the selected target is missing or ambiguous")
        target = targets[0]
        if target.perm_id not in owned:
            raise ExecutionBlocked(
                "the selected target was not created by this application"
            )
        if (
            target.key != snapshot.selected
            or target.action != "SELL"
            or target.order_type != "LMT"
            or target.order_id <= 0
            or target.client_id != expected_client_id
            or target.remaining <= 0
            or not target.oca_group
            or not target.tif
            or target.status not in {"Submitted", "PreSubmitted"}
        ):
            raise ExecutionBlocked(
                "the selected LMT target is no longer safely modifiable"
            )
        peers = [
            order
            for order in snapshot.working_orders
            if order.oca_group == target.oca_group
        ]
        stops = [
            order
            for order in peers
            if order.action == "SELL"
            and order.order_type == "STP"
            and order.perm_id in owned
            and order.order_id > 0
            and order.client_id == expected_client_id
            and order.remaining == target.remaining
            and order.status in {"Submitted", "PreSubmitted"}
        ]
        if len(peers) != 2 or len(stops) != 1:
            raise ExecutionBlocked(
                "the app-owned OCA pair is incomplete or has changed"
            )
        if snapshot.position.quantity < target.remaining:
            raise ExecutionBlocked("the position quantity no longer covers this layer")
        stop = stops[0]
        return MarketExitCandidate(
            account=snapshot.selected.account,
            con_id=snapshot.selected.con_id,
            target_order_id=target.order_id,
            target_perm_id=target.perm_id,
            client_id=target.client_id,
            quantity=target.remaining,
            tif=target.tif,
            oca_group=target.oca_group,
            stop_order_id=stop.order_id,
            stop_perm_id=stop.perm_id,
        )

    def prepare_market_exits(
        self,
        snapshot: BrokerSnapshot,
        *,
        target_perm_ids: tuple[int, ...],
        expected_client_id: int,
    ) -> tuple[MarketExitCandidate, ...]:
        """Prepare every selected pair before any cancellation can occur."""
        selected = tuple(sorted(set(target_perm_ids)))
        if not selected:
            raise ExecutionBlocked("select at least one active layer")
        candidates = tuple(
            self.prepare_market_exit(
                snapshot,
                target_perm_id=perm_id,
                expected_client_id=expected_client_id,
            )
            for perm_id in selected
        )
        if len({candidate.oca_group for candidate in candidates}) != len(candidates):
            raise ExecutionBlocked(
                "selected layers do not resolve to distinct OCA pairs"
            )
        total = sum((candidate.quantity for candidate in candidates), Decimal("0"))
        if snapshot.position.quantity < total:
            raise ExecutionBlocked(
                "the position quantity no longer covers selected layers"
            )
        return candidates

    def prepare_price_updates(
        self,
        snapshot: BrokerSnapshot,
        *,
        updates: tuple[PriceUpdateCandidate, ...],
        expected_client_id: int,
    ) -> tuple[PriceUpdateCandidate, ...]:
        """Re-verify selected app pairs and allow only finite positive prices."""
        if not updates:
            raise ExecutionBlocked("select at least one active layer")
        validated: list[PriceUpdateCandidate] = []
        seen_targets: set[int] = set()
        for update in updates:
            if update.layer.target_perm_id in seen_targets:
                raise ExecutionBlocked("selected layers must be distinct")
            seen_targets.add(update.layer.target_perm_id)
            fresh = self.prepare_market_exit(
                snapshot,
                target_perm_id=update.layer.target_perm_id,
                expected_client_id=expected_client_id,
            )
            if fresh != update.layer:
                raise ExecutionBlocked("the selected OCA layer changed since review")
            orders_by_id = {order.order_id: order for order in snapshot.working_orders}
            target = orders_by_id.get(update.layer.target_order_id)
            stop = orders_by_id.get(update.layer.stop_order_id)
            if target is None or stop is None:
                raise ExecutionBlocked("the selected OCA layer is no longer complete")
            if (
                update.prior_target_price is not None
                and target.limit_price != update.prior_target_price
            ) or (
                update.prior_stop_price is not None
                and stop.stop_price != update.prior_stop_price
            ):
                raise ExecutionBlocked("the selected OCA prices changed since review")
            for price in (update.target_price, update.stop_price):
                if price is not None and (not price.is_finite() or price <= 0):
                    raise ExecutionBlocked("updated prices must be positive and finite")
            if update.target_price is None and update.stop_price is None:
                raise ExecutionBlocked("each selected layer needs a price change")
            validated.append(update)
        return tuple(validated)

    def modify_prices(
        self,
        snapshot: BrokerSnapshot,
        updates: tuple[PriceUpdateCandidate, ...],
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> SubmissionReceipt:
        """Perform a revalidated, price-only amendment of selected OCA pairs."""
        refreshed = self.prepare_price_updates(
            snapshot,
            updates=updates,
            expected_client_id=client_id,
        )
        if refreshed != updates:
            raise ExecutionBlocked("the selected OCA layers changed since confirmation")
        transport = self._transport
        if not isinstance(transport, PaperPriceUpdateTransport):
            raise ExecutionBlocked(
                "the configured paper transport cannot modify app-owned OCA prices"
            )
        expected_order_count = sum(
            int(update.target_price is not None) + int(update.stop_price is not None)
            for update in updates
        )
        entry = self._journal.begin_management(
            snapshot,
            operation="price-update",
            material=tuple(
                value
                for update in updates
                for value in (
                    update.layer.target_order_id,
                    update.layer.target_perm_id,
                    update.prior_target_price,
                    update.target_price,
                    update.layer.stop_order_id,
                    update.layer.stop_perm_id,
                    update.prior_stop_price,
                    update.stop_price,
                )
            ),
            expected_order_count=expected_order_count,
        )
        try:
            result = transport.modify_prices(
                snapshot,
                updates,
                host=host,
                port=port,
                client_id=client_id,
                timeout_seconds=timeout_seconds,
            )
            order_ids = tuple(int(value) for value in result.order_ids)
            perm_ids = tuple(int(value) for value in result.perm_ids)
            if (
                len(order_ids) != expected_order_count
                or len(perm_ids) != expected_order_count
                or not all(perm_ids)
            ):
                raise ExecutionOutcomeUnknown(
                    "TWS did not acknowledge every selected price amendment"
                )
        except Exception:
            self._journal.mark_unknown(entry.fingerprint)
            raise
        return SubmissionReceipt(
            self._journal.record_submission(
                entry.fingerprint,
                order_ids=order_ids,
                perm_ids=perm_ids,
            )
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
    ) -> SubmissionReceipt:
        """Cancel one re-verified app-owned bracket without replacing it.

        The cancellation is deliberate and non-retryable.  Its successful
        receipt records the two broker order IDs that TWS confirmed cancelled;
        it creates no replacement order and grants no ownership to anything
        that was not already journal-proven.
        """
        refreshed = self.prepare_market_exit(
            snapshot,
            target_perm_id=candidate.target_perm_id,
            expected_client_id=client_id,
        )
        if refreshed != candidate:
            raise ExecutionBlocked("the selected OCA layer changed since confirmation")
        transport = self._transport
        if not isinstance(transport, PaperOcaCancellationTransport):
            raise ExecutionBlocked(
                "the configured paper transport cannot cancel an app-owned OCA bracket"
            )
        entry = self._journal.begin_management(
            snapshot,
            operation="cancel-bracket",
            material=(
                candidate.target_order_id,
                candidate.target_perm_id,
                candidate.stop_order_id,
                candidate.stop_perm_id,
            ),
            expected_order_count=2,
        )
        expected_order_ids = tuple(
            sorted((candidate.target_order_id, candidate.stop_order_id))
        )
        try:
            result = transport.cancel_pair(
                snapshot,
                candidate,
                host=host,
                port=port,
                client_id=client_id,
                timeout_seconds=timeout_seconds,
            )
            order_ids = tuple(sorted(int(value) for value in result.order_ids))
            if order_ids != expected_order_ids:
                raise ExecutionOutcomeUnknown(
                    "TWS did not acknowledge cancellation of both selected OCA legs"
                )
        except Exception:
            self._journal.mark_unknown(entry.fingerprint)
            raise
        return SubmissionReceipt(
            self._journal.record_management_completion(
                entry.fingerprint,
                order_ids=order_ids,
            )
        )

    def cancel_pair_then_submit_market(
        self,
        snapshot: BrokerSnapshot,
        candidate: MarketExitCandidate,
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> SubmissionReceipt:
        """Cancel one exact owned pair, then submit one standalone paper MKT."""
        refreshed = self.prepare_market_exit(
            snapshot,
            target_perm_id=candidate.target_perm_id,
            expected_client_id=client_id,
        )
        if refreshed != candidate:
            raise ExecutionBlocked("the selected OCA layer changed since confirmation")
        transport = self._transport
        if not isinstance(transport, PaperMarketExitTransport):
            raise ExecutionBlocked(
                "the configured paper transport cannot execute a staged market exit"
            )
        entry = self._journal.begin_market_exit(snapshot, candidate)
        try:
            result = transport.cancel_pair_then_submit_market(
                snapshot,
                candidate,
                host=host,
                port=port,
                client_id=client_id,
                timeout_seconds=timeout_seconds,
            )
            order_ids = tuple(int(value) for value in result.order_ids)
            perm_ids = tuple(int(value) for value in result.perm_ids)
            if len(order_ids) != 1 or len(perm_ids) != 1 or not perm_ids[0]:
                raise ExecutionOutcomeUnknown(
                    "TWS did not acknowledge the standalone market order"
                )
        except Exception:
            self._journal.mark_unknown(entry.fingerprint)
            raise
        return SubmissionReceipt(
            self._journal.record_submission(
                entry.fingerprint,
                order_ids=order_ids,
                perm_ids=perm_ids,
            )
        )

    def cancel_pairs_then_submit_market(
        self,
        snapshot: BrokerSnapshot,
        candidates: tuple[MarketExitCandidate, ...],
        *,
        host: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
    ) -> SubmissionReceipt:
        """Cancel all selected pairs, recheck once, then send one total MKT."""
        refreshed = self.prepare_market_exits(
            snapshot,
            target_perm_ids=tuple(candidate.target_perm_id for candidate in candidates),
            expected_client_id=client_id,
        )
        if refreshed != candidates:
            raise ExecutionBlocked("the selected OCA layers changed since confirmation")
        transport = self._transport
        if not isinstance(transport, PaperBulkMarketExitTransport):
            raise ExecutionBlocked(
                "the configured paper transport cannot execute a staged market exit"
            )
        entry = self._journal.begin_management(
            snapshot,
            operation="market-exit-many",
            material=tuple(
                value
                for candidate in candidates
                for value in (
                    candidate.target_order_id,
                    candidate.target_perm_id,
                    candidate.stop_order_id,
                    candidate.stop_perm_id,
                    candidate.quantity,
                )
            ),
            expected_order_count=1,
        )
        try:
            result = transport.cancel_pairs_then_submit_market(
                snapshot,
                candidates,
                host=host,
                port=port,
                client_id=client_id,
                timeout_seconds=timeout_seconds,
            )
            order_ids = tuple(int(value) for value in result.order_ids)
            perm_ids = tuple(int(value) for value in result.perm_ids)
            if len(order_ids) != 1 or len(perm_ids) != 1 or not perm_ids[0]:
                raise ExecutionOutcomeUnknown(
                    "TWS did not acknowledge the standalone market order"
                )
        except Exception:
            self._journal.mark_unknown(entry.fingerprint)
            raise
        return SubmissionReceipt(
            self._journal.record_submission(
                entry.fingerprint,
                order_ids=order_ids,
                perm_ids=perm_ids,
            )
        )


def default_paper_journal_path() -> Path:
    """Return the user-local, durable paper submission journal location."""
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return root / "IBKR Options Manager" / "paper-execution-journal.json"


def _execution_identity(exec_id: str) -> str:
    prefix, separator, revision = exec_id.rpartition(".")
    return prefix if separator and revision.isdigit() else exec_id


def _execution_revision(exec_id: str) -> int:
    _prefix, separator, revision = exec_id.rpartition(".")
    return int(revision) if separator and revision.isdigit() else 0


def _usable_execution(
    execution: ObservedExecution,
    snapshot: BrokerSnapshot,
    known_ids: set[int],
) -> bool:
    return (
        bool(execution.exec_id)
        and execution.account == snapshot.selected.account
        and execution.con_id == snapshot.selected.con_id
        and execution.perm_id in known_ids
        and execution.side.upper() in {"SLD", "SELL"}
        and execution.quantity.is_finite()
        and execution.quantity > 0
        and execution.price.is_finite()
        and execution.price > 0
        and (execution.realized_pnl is None or execution.realized_pnl.is_finite())
    )


def _complete_app_oca_orders(
    snapshot: BrokerSnapshot,
    entry: JournalEntry,
) -> tuple[WorkingOrder, ...]:
    prefix = f"{entry.fingerprint[:12]}/tranche-"
    groups: dict[str, list[WorkingOrder]] = {}
    for order in snapshot.working_orders:
        group = order.oca_group
        if (
            order.key != snapshot.selected
            or not group
            or not group.startswith(prefix)
            or order.action != "SELL"
            or order.order_type not in {"LMT", "STP"}
            or order.perm_id <= 0
            or order.order_id <= 0
        ):
            continue
        groups.setdefault(group, []).append(order)

    complete: list[WorkingOrder] = []
    for group in sorted(groups):
        pair = groups[group]
        if len(pair) != 2 or {order.order_type for order in pair} != {"LMT", "STP"}:
            return ()
        complete.extend(sorted(pair, key=lambda order: order.order_type))
    return tuple(complete)
