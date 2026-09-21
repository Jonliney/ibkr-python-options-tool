"""Paper-only execution controls, kept separate from planning and snapshots."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable

from .domain import BrokerSnapshot, PlanResult, PlanStatus, WorkingOrder


class ExecutionBlocked(RuntimeError):
    """Raised before any broker write when the execution contract is not met."""


class ExecutionOutcomeUnknown(ExecutionBlocked):
    """Raised after a write request when TWS does not provide enough evidence."""


@dataclass(frozen=True, slots=True)
class JournalEntry:
    fingerprint: str
    account: str
    con_id: int
    state: str
    expected_order_count: int = 0
    order_ids: tuple[int, ...] = ()
    perm_ids: tuple[int, ...] = ()


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
            (entry for entry in self._entries() if entry.fingerprint == fingerprint),
            None,
        )

    def owned_perm_ids(self, *, account: str, con_id: int) -> frozenset[int]:
        """Return permanent IDs of broker orders proven to be app-owned."""
        return frozenset(
            perm_id
            for entry in self._entries()
            if entry.account == account
            and entry.con_id == con_id
            and entry.state in {"SUBMITTED", "RECONCILED"}
            for perm_id in entry.perm_ids
            if perm_id > 0
        )

    def begin(self, snapshot: BrokerSnapshot, plan: PlanResult) -> JournalEntry:
        fingerprint = plan.fingerprint
        if plan.status is not PlanStatus.VALID or fingerprint is None:
            raise ExecutionBlocked("only a valid, fingerprinted plan may be sent")
        if self.find(fingerprint) is not None:
            raise ExecutionBlocked(
                "this plan fingerprint is already journaled; no retry is automatic"
            )
        entry = JournalEntry(
            fingerprint=fingerprint,
            account=snapshot.selected.account,
            con_id=snapshot.selected.con_id,
            state="PREPARED",
            expected_order_count=len(plan.pairs) * 2,
        )
        self._write((*self._entries(), entry))
        return entry

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
        )
        self._write((*self._entries(), entry))
        return entry

    def record_submission(
        self, fingerprint: str, *, order_ids: tuple[int, ...], perm_ids: tuple[int, ...]
    ) -> JournalEntry:
        entries = list(self._entries())
        for index, entry in enumerate(entries):
            if entry.fingerprint == fingerprint:
                updated = JournalEntry(
                    fingerprint=entry.fingerprint,
                    account=entry.account,
                    con_id=entry.con_id,
                    state="SUBMISSION_UNKNOWN" if not perm_ids else "SUBMITTED",
                    expected_order_count=entry.expected_order_count,
                    order_ids=order_ids,
                    perm_ids=perm_ids,
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

    def reconcile_snapshot(
        self,
        snapshot: BrokerSnapshot,
    ) -> tuple[JournalEntry, ...]:
        """Adopt complete, app-identifiable OCA pairs observed from TWS.

        This does not turn an incomplete or arbitrary external order into an
        app-owned order.  Every observed order must use the plan fingerprint's
        OCA-group prefix, have a permanent ID, and form a complete SELL LMT /
        SELL STP pair.  Newer entries additionally require the exact planned
        order count.  Older journals have no count and are marked
        ``RECONCILED`` rather than pretending their original acknowledgement
        succeeded.
        """
        entries = list(self._entries())
        reconciled: list[JournalEntry] = []
        for index, entry in enumerate(entries):
            if (
                entry.state not in {"PREPARED", "SUBMISSION_UNKNOWN"}
                or entry.account != snapshot.selected.account
                or entry.con_id != snapshot.selected.con_id
            ):
                continue
            observed = _complete_app_oca_orders(snapshot, entry)
            if not observed or (
                entry.expected_order_count
                and len(observed) != entry.expected_order_count
            ):
                continue
            updated = JournalEntry(
                fingerprint=entry.fingerprint,
                account=entry.account,
                con_id=entry.con_id,
                state="RECONCILED",
                expected_order_count=entry.expected_order_count,
                order_ids=tuple(order.order_id for order in observed),
                perm_ids=tuple(order.perm_id for order in observed),
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
) -> None:
    """Validate the stricter write preconditions before opening a TWS writer."""
    if plan.status is not PlanStatus.VALID or plan.fingerprint is None:
        raise ExecutionBlocked("the refreshed plan is not valid")
    require_paper_management_snapshot(snapshot)
    if any(order.key == snapshot.selected for order in snapshot.working_orders):
        raise ExecutionBlocked("existing related orders block a new paper submission")


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
        require_paper_execution_snapshot(snapshot, plan)
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

    def owned_perm_ids(self, *, account: str, con_id: int) -> frozenset[int]:
        """Return only journal-proven app-owned broker order IDs."""
        return self._journal.owned_perm_ids(account=account, con_id=con_id)

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
