from __future__ import annotations

# ruff: noqa: E501
import json
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from secrets import token_urlsafe
from threading import RLock, Thread
from typing import Any

from starhtml import (
    H1,
    H3,
    Div,
    Form,
    Icon,
    Link,
    P,
    Script,
    Signal,
    Span,
    star_app,
)
from starhtml import (
    Input as HTMLInput,
)
from starhtml.icons import resolver
from starhtml.plugins import position as position_plugin
from starlette.requests import Request
from starlette.responses import JSONResponse

from ...domain import preview_reference_prices, round_up_price
from ...execution import (
    ExecutionBlocked,
    ExecutionOutcomeUnknown,
    JournalEntry,
    LayerOutcome,
    MarketExitCandidate,
    PaperExecutionService,
    PriceUpdateCandidate,
    classify_journal_layer,
)
from ..view_model import (
    ConnectionSettings,
    DraftLayerForm,
    PaperExecutionCandidate,
    PlanForm,
    PlannerViewModel,
    UiStatus,
    ViewState,
)
from .components.ui.alert import Alert, AlertDescription, AlertTitle
from .components.ui.badge import Badge
from .components.ui.button import Button, ButtonVariant
from .components.ui.card import (
    Card,
    CardAction,
    CardContent,
    CardDescription,
    CardHeader,
    CardTitle,
)
from .components.ui.dialog import (
    Dialog,
    DialogClose,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogHeader,
    DialogTitle,
    DialogTrigger,
)
from .components.ui.input import Input
from .components.ui.label import Label
from .components.ui.scroll_area import ScrollArea
from .components.ui.select import (
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
)
from .components.ui.separator import Separator
from .components.ui.toast import Toaster

_STATIC_DIR = Path(__file__).with_name("static")
_ASSETS_DIR = Path(__file__).with_name("assets")


@dataclass(frozen=True, slots=True)
class _ToastNotice:
    title: str
    description: str
    variant: str


class StarUIWorkbench:
    """Server-owned StarUI view over planning and explicitly enabled paper sends."""

    def __init__(
        self,
        view_model: PlannerViewModel,
        *,
        initial_account: str = "",
        initial_con_id: int | None = None,
        demo_mode: bool = False,
        paper_execution: PaperExecutionService | None = None,
    ) -> None:
        _register_bundled_icons()
        self._view_model = view_model
        self._demo_mode = demo_mode
        self._paper_execution = paper_execution
        self._armed_execution: PaperExecutionCandidate | None = None
        self._armed_market_exit: MarketExitCandidate | None = None
        self._armed_market_exits: tuple[MarketExitCandidate, ...] = ()
        self._armed_cancellation: MarketExitCandidate | None = None
        self._armed_price_updates: tuple[PriceUpdateCandidate, ...] = ()
        # TWS can acknowledge a price amendment before its next open-order
        # snapshot reflects it. Retain only that acknowledged presentation
        # value until the broker snapshot catches up; execution still always
        # re-verifies the broker snapshot rather than trusting this display.
        self._pending_active_prices: dict[
            int, tuple[Decimal | None, int, Decimal | None]
        ] = {}
        self._preferred_con_id = initial_con_id
        self._selected_con_id: int | None = None
        self._state = view_model.empty()
        self._drafts: dict[int, tuple[DraftLayerForm, ...]] = {}
        self._settings = ConnectionSettings(account=initial_account)
        self._target_presets = "20, 40, 60, 100"
        self._stop_presets = "25"
        self._last_refreshed_at = "—"
        self._notifications_enabled = False
        self._suppress_toasts = False
        self._toast: _ToastNotice | None = None
        self._status_message = ""
        self._message = "Refresh and select a position to build a draft."
        self._launch_connection = "idle"
        self._launch_refresh_in_progress = False
        self._lock = RLock()
        self.session_token = token_urlsafe(24)
        self.app, route = star_app(
            title=(
                "IBKR Options Manager — Paper OCA manager"
                if paper_execution is not None
                else "IBKR Options Manager — Read-only preview"
            ),
            static_path=str(_STATIC_DIR),
            secret_key=token_urlsafe(32),
            inline_icons=True,
            hdrs=(Link(rel="stylesheet", href="/starui.css"),),
            htmlkw={"lang": "en", "data_theme": "dark"},
            bodykw={"cls": "min-h-screen bg-background text-foreground"},
        )
        self.app.register(position_plugin)
        route(f"/{self.session_token}/")(self._home)
        route(f"/{self.session_token}/connection-status")(self._connection_status)
        route(f"/{self.session_token}/action", methods=["POST"])(self._action)
        self._notifications_enabled = True

    @property
    def _message(self) -> str:
        """Retain a diagnostic status internally while exposing outcomes as toasts."""
        return self._status_message

    @_message.setter
    def _message(self, message: str) -> None:
        self._status_message = message
        if self._notifications_enabled and not self._suppress_toasts:
            self._toast = _toast_notice(message)

    @property
    def path(self) -> str:
        return f"/{self.session_token}/"

    def load_demo_data(self) -> None:
        """Load the existing deterministic demo without starting a browser worker."""
        if not self._demo_mode:
            return
        with self._lock:
            self._refresh_locked()

    def refresh_on_launch(self) -> None:
        """Perform the same read-only refresh as the header control at startup."""
        if self._demo_mode:
            self.load_demo_data()
            return
        with self._lock:
            self._disarm_execution_locked()
            settings = self._settings
            self._launch_connection = "connecting"
            self._suppress_toasts = True
            self._message = "Connecting to TWS…"
        try:
            state = self._view_model.refresh_portfolio(settings)
        except Exception as error:
            with self._lock:
                self._launch_connection = "failed"
                self._message = f"TWS connection failed: {error}"
                self._suppress_toasts = False
            return
        with self._lock:
            # A user cannot alter launch settings until the page is rendered,
            # but still reject a stale completion rather than overwriting a
            # newer state in a future launch-flow change.
            if settings != self._settings:
                self._suppress_toasts = False
                return
            self._apply_refreshed_portfolio_locked(state)
            self._launch_connection = (
                "success" if state.status is UiStatus.READY else "failed"
            )
            self._suppress_toasts = False

    def start_launch_refresh(self) -> None:
        """Run the launch refresh in the background so the first page is immediate."""
        if self._demo_mode:
            self.load_demo_data()
            return
        with self._lock:
            if self._launch_refresh_in_progress:
                return
            self._launch_refresh_in_progress = True
            self._launch_connection = "connecting"
        Thread(
            target=self._finish_launch_refresh,
            name="ibkr-options-launch-refresh",
            daemon=True,
        ).start()

    def _finish_launch_refresh(self) -> None:
        try:
            self.refresh_on_launch()
        finally:
            with self._lock:
                self._launch_refresh_in_progress = False

    def _home(self) -> Any:
        with self._lock:
            return self._page()

    def _connection_status(self) -> JSONResponse:
        """Let the launch dialog wait without repeatedly replacing the page."""
        with self._lock:
            return JSONResponse({"state": self._launch_connection})

    async def _action(self, request: Request) -> Any:
        form = await request.form()
        values = {str(key): str(value) for key, value in form.items()}
        action = values.get("action", "save-draft")

        if action == "launch-refresh":
            # Retry is deliberately a single foreground request.  The retry
            # dialog remains on screen (and its submit button is busy) while
            # TWS is contacted; only its terminal response replaces the
            # page.  Starting another background worker here races the
            # connection-status poll and can repeatedly reopen the dialog.
            self.refresh_on_launch()
            with self._lock:
                return self._page()

        with self._lock:
            if action == "refresh":
                self._target_presets = values.get(
                    "target_presets", self._target_presets
                )
                self._stop_presets = values.get("stop_presets", self._stop_presets)
                self._settings = ConnectionSettings(
                    account=values.get("account", self._settings.account).strip(),
                    port=_positive_int(values.get("port"), self._settings.port),
                    client_id=_positive_int(
                        values.get("client_id"), self._settings.client_id
                    ),
                    timeout_seconds=_positive_float(
                        values.get("timeout"), self._settings.timeout_seconds
                    ),
                )
                self._refresh_locked()
            elif action == "select":
                self._disarm_execution_locked()
                self._save_form_locked(values)
                self._select_locked(_positive_int(values.get("con_id"), 0))
            elif action.startswith("market-exit-arm:"):
                _, _, perm_id = action.partition(":")
                self._arm_market_exit_locked(_positive_int(perm_id, 0))
            elif action.startswith("cancel-pair-arm:"):
                _, _, perm_id = action.partition(":")
                self._arm_cancellation_locked(_positive_int(perm_id, 0))
            elif action == "market-exit-selected":
                self._arm_selected_market_exit_locked(values)
            elif action == "market-exit-confirm":
                self._confirm_market_exit_locked()
            elif action == "cancel-pair-confirm":
                self._confirm_cancellation_locked()
            elif action == "cancel-staged":
                self._disarm_execution_locked()
                self._message = "Staged action cancelled. No orders were sent to TWS."
            elif action == "active-update-arm":
                self._arm_price_updates_locked(values)
            elif action == "price-update-confirm":
                self._confirm_price_updates_locked()
            else:
                if action not in {"execute-arm", "execute-confirm"}:
                    self._disarm_execution_locked()
                self._save_form_locked(values)
                if action == "add-layer":
                    self._add_layer_locked()
                elif action.startswith("remove-layer:"):
                    _, _, layer_index = action.partition(":")
                    self._remove_layer_locked(_positive_int(layer_index, 0))
                elif action in {"equal-split", "equal-split-available"}:
                    self._equal_split_locked(use_available_quantity=True)
                elif action == "equal-split-assigned":
                    self._equal_split_locked(use_available_quantity=False)
                elif action == "execute-arm":
                    self._arm_execution_locked()
                elif action == "execute-confirm":
                    self._confirm_execution_locked()
            return self._page()

    def _refresh_locked(self) -> None:
        self._disarm_execution_locked()
        state = self._view_model.refresh_portfolio(self._settings)
        self._apply_refreshed_portfolio_locked(state)

    def _refresh_after_acknowledged_write_locked(self, acknowledgement: str) -> None:
        """Replace optimistic post-write UI state with a fresh broker snapshot."""
        try:
            self._refresh_locked()
        except Exception as error:  # keep a confirmed write, never hide it
            self._message = (
                f"{acknowledgement} Automatic TWS refresh failed; use Refresh before "
                f"another action. ({error})"
            )
            return
        if self._state.status is UiStatus.READY:
            self._message = f"{acknowledgement} TWS state refreshed."
        else:
            self._message = (
                f"{acknowledgement} TWS refresh could not verify the new state; use "
                "Refresh before another action."
            )

    def _apply_refreshed_portfolio_locked(self, state: ViewState) -> None:
        """Apply an already-read portfolio snapshot while holding the UI lock."""
        previous_con_id = self._selected_con_id
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        target = self._preferred_con_id
        if target is None:
            target = previous_con_id
        available_con_ids = {position.con_id for position in state.positions}
        if target not in available_con_ids:
            target = state.positions[0].con_id if state.positions else None
        self._preferred_con_id = None
        if target is None:
            return
        self._select_locked(target)

    def _select_locked(self, con_id: int) -> None:
        if con_id not in {position.con_id for position in self._state.positions}:
            self._message = "The selected contract is not in the verified portfolio."
            return
        self._selected_con_id = con_id
        state = self._view_model.select_position(
            con_id, self._plan_form(self._drafts.get(con_id, ()))
        )
        if any(
            validation.code == "LAYER_QUANTITY_EXCEEDS_AVAILABLE"
            for validation in state.validations
        ):
            # A fresh broker snapshot has changed the reservable quantity (for
            # example, a bracket was cancelled in TWS). A retained draft is no
            # longer a draft for the available balance, so replace it rather
            # than trapping the position behind its obsolete allocation.
            self._drafts.pop(con_id, None)
            state = self._view_model.select_position(con_id, self._plan_form(()))
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()

    def _plan_form(self, layers: tuple[DraftLayerForm, ...]) -> PlanForm:
        return PlanForm(
            layers=layers,
            paper_execution_mode=self._paper_execution is not None,
        )

    def _disarm_execution_locked(self) -> None:
        self._armed_execution = None
        self._armed_market_exit = None
        self._armed_market_exits = ()
        self._armed_cancellation = None
        self._armed_price_updates = ()

    def _arm_execution_locked(self) -> None:
        if self._paper_execution is None:
            self._message = "Paper transmission is disabled for this launch."
            return
        state, candidate = self._view_model.prepare_paper_execution(
            self._plan_form(self._current_layers())
        )
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()
        if candidate is None:
            self._message = "Execution blocked: refresh and validation did not produce a sendable paper draft."
            return
        self._armed_execution = candidate
        self._message = (
            "Fresh paper snapshot verified. Review the order plan, then confirm."
        )

    def _confirm_execution_locked(self) -> None:
        armed = self._armed_execution
        if (
            self._paper_execution is None
            or armed is None
            or armed.plan.fingerprint is None
        ):
            self._message = "Start execution first; every paper order needs a separate confirmation."
            return
        state, candidate = self._view_model.prepare_paper_execution(
            self._plan_form(self._current_layers())
        )
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()
        if candidate is None:
            self._disarm_execution_locked()
            self._message = (
                "Execution blocked: the fresh snapshot is no longer sendable."
            )
            return
        if candidate.plan.fingerprint != armed.plan.fingerprint:
            self._disarm_execution_locked()
            self._message = "Draft or broker state changed; review the refreshed plan and start execution again."
            return
        try:
            receipt = self._paper_execution.submit(
                candidate.snapshot,
                candidate.plan,
                host="127.0.0.1",
                port=candidate.selection.port,
                client_id=candidate.selection.client_id,
                timeout_seconds=candidate.selection.timeout_seconds,
            )
        except ExecutionOutcomeUnknown as error:
            self._drafts.pop(candidate.selection.con_id, None)
            self._message = (
                f"Submission outcome is unknown: {error}. If TWS shows the complete "
                "bracket, approve it there if required, then Refresh to reconcile it "
                "into Active layers. Do not retry this draft."
            )
        except ExecutionBlocked as error:
            self._message = f"Execution blocked: {error}"
        except Exception as error:  # the isolated writer must never crash the UI
            self._drafts.pop(candidate.selection.con_id, None)
            self._message = f"Submission outcome is unknown: {error}"
        else:
            self._drafts.pop(candidate.selection.con_id, None)
            self._refresh_after_acknowledged_write_locked(
                f"Paper submission acknowledged for {len(receipt.entry.order_ids)} orders."
            )
            self._toast = _ToastNotice(
                title="Orders sent to TWS",
                description=(
                    f"{len(receipt.entry.order_ids)} orders acknowledged. "
                    "Check TWS for Transmit, then Refresh."
                ),
                variant="success",
            )
        finally:
            self._disarm_execution_locked()

    def _arm_market_exit_locked(self, target_perm_id: int) -> None:
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Paper order management is disabled for this launch."
            return
        self._disarm_execution_locked()
        state = self._view_model.select_position(
            self._selected_con_id,
            self._plan_form(self._drafts.get(self._selected_con_id, ())),
        )
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()
        snapshot = self._view_model.latest_snapshot()
        if snapshot is None:
            self._message = (
                "Market exit blocked: a fresh selected-position snapshot is required."
            )
            return
        try:
            candidate = self._paper_execution.prepare_market_exit(
                snapshot,
                target_perm_id=target_perm_id,
                expected_client_id=self._settings.client_id,
            )
        except ExecutionBlocked as error:
            self._message = f"Market exit blocked: {error}"
            return
        self._armed_market_exit = candidate
        self._armed_market_exits = (candidate,)
        self._message = (
            f"Fresh paper snapshot verified. Review the MKT exit for "
            f"{candidate.quantity} contracts, then confirm."
        )

    def _arm_cancellation_locked(self, target_perm_id: int) -> None:
        """Stage deletion of one complete, journal-proven OCA pair only."""
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Paper order management is disabled for this launch."
            return
        self._disarm_execution_locked()
        state = self._view_model.select_position(
            self._selected_con_id,
            self._plan_form(self._drafts.get(self._selected_con_id, ())),
        )
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()
        snapshot = self._view_model.latest_snapshot()
        if snapshot is None:
            self._message = "Bracket cancellation blocked: a fresh selected-position snapshot is required."
            return
        try:
            candidate = self._paper_execution.prepare_market_exit(
                snapshot,
                target_perm_id=target_perm_id,
                expected_client_id=self._settings.client_id,
            )
        except ExecutionBlocked as error:
            self._message = f"Bracket cancellation blocked: {error}"
            return
        self._armed_cancellation = candidate
        self._message = (
            f"Fresh paper snapshot verified. Review cancellation of OCA bracket "
            f"{candidate.oca_group}; no replacement sell order will be sent."
        )

    def _confirm_cancellation_locked(self) -> None:
        """Cancel the staged pair after one more fresh-snapshot equality check."""
        candidate = self._armed_cancellation
        if (
            self._paper_execution is None
            or candidate is None
            or self._selected_con_id is None
        ):
            self._message = "Start bracket cancellation first; every paper change needs a separate confirmation."
            return
        state = self._view_model.select_position(
            self._selected_con_id,
            self._plan_form(self._drafts.get(self._selected_con_id, ())),
        )
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()
        snapshot = self._view_model.latest_snapshot()
        if snapshot is None:
            self._disarm_execution_locked()
            self._message = (
                "Bracket cancellation blocked: the fresh snapshot is unavailable."
            )
            return
        try:
            refreshed = self._paper_execution.prepare_market_exit(
                snapshot,
                target_perm_id=candidate.target_perm_id,
                expected_client_id=self._settings.client_id,
            )
            if refreshed != candidate:
                raise ExecutionBlocked("the OCA bracket changed after review")
            self._paper_execution.cancel_pair(
                snapshot,
                candidate,
                host="127.0.0.1",
                port=self._settings.port,
                client_id=self._settings.client_id,
                timeout_seconds=self._settings.timeout_seconds,
            )
        except ExecutionOutcomeUnknown as error:
            self._message = (
                f"Bracket cancellation outcome is unknown: {error}. Refresh TWS before "
                "taking any further action."
            )
        except ExecutionBlocked as error:
            self._message = f"Bracket cancellation blocked: {error}"
        except Exception as error:
            self._message = f"Bracket cancellation outcome is unknown: {error}"
        else:
            self._refresh_after_acknowledged_write_locked(
                "TWS confirmed both OCA legs were cancelled."
            )
        finally:
            self._disarm_execution_locked()

    def _arm_selected_market_exit_locked(self, values: dict[str, str]) -> None:
        """Arm a verified all-selected-layer paper exit for second confirmation."""
        del values
        selected = self._active_target_perm_ids()
        if not selected:
            self._message = (
                "Select at least one active layer for the staged paper MKT exit."
            )
            return
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Paper order management is disabled for this launch."
            return
        self._disarm_execution_locked()
        state = self._view_model.select_position(
            self._selected_con_id,
            self._plan_form(self._drafts.get(self._selected_con_id, ())),
        )
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()
        snapshot = self._view_model.latest_snapshot()
        if snapshot is None:
            self._message = (
                "Market exit blocked: a fresh selected-position snapshot is required."
            )
            return
        try:
            candidates = self._paper_execution.prepare_market_exits(
                snapshot,
                target_perm_ids=selected,
                expected_client_id=self._settings.client_id,
            )
        except ExecutionBlocked as error:
            self._message = f"Market exit blocked: {error}"
            return
        self._armed_market_exits = candidates
        total = sum((candidate.quantity for candidate in candidates), Decimal("0"))
        self._message = (
            f"Fresh paper snapshot verified. Review cancellation of {len(candidates)} OCA "
            f"layers and one MKT sell for {total} contracts, then confirm."
        )

    def _confirm_market_exit_locked(self) -> None:
        armed = self._armed_market_exits or (
            (self._armed_market_exit,) if self._armed_market_exit is not None else ()
        )
        if self._paper_execution is None or not armed or self._selected_con_id is None:
            self._message = "Start the market exit first; every paper change needs a separate confirmation."
            return
        state = self._view_model.select_position(
            self._selected_con_id,
            self._plan_form(self._drafts.get(self._selected_con_id, ())),
        )
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()
        snapshot = self._view_model.latest_snapshot()
        if snapshot is None:
            self._disarm_execution_locked()
            self._message = "Market exit blocked: the fresh snapshot is unavailable."
            return
        try:
            candidates = self._paper_execution.prepare_market_exits(
                snapshot,
                target_perm_ids=tuple(candidate.target_perm_id for candidate in armed),
                expected_client_id=self._settings.client_id,
            )
            if candidates != armed:
                raise ExecutionBlocked("the OCA layer changed after review")
            if len(candidates) == 1:
                self._paper_execution.cancel_pair_then_submit_market(
                    snapshot,
                    candidates[0],
                    host="127.0.0.1",
                    port=self._settings.port,
                    client_id=self._settings.client_id,
                    timeout_seconds=self._settings.timeout_seconds,
                )
            else:
                self._paper_execution.cancel_pairs_then_submit_market(
                    snapshot,
                    candidates,
                    host="127.0.0.1",
                    port=self._settings.port,
                    client_id=self._settings.client_id,
                    timeout_seconds=self._settings.timeout_seconds,
                )
        except ExecutionOutcomeUnknown as error:
            self._message = (
                f"Market exit outcome is unknown: {error}. Refresh TWS before "
                "taking any further action."
            )
        except ExecutionBlocked as error:
            self._message = f"Market exit blocked: {error}"
        except Exception as error:
            self._message = f"Market exit outcome is unknown: {error}"
        else:
            self._refresh_after_acknowledged_write_locked(
                f"TWS confirmed both selected OCA legs were cancelled and "
                f"acknowledged the standalone MKT sell for "
                f"{sum((candidate.quantity for candidate in candidates), Decimal('0'))} contracts."
            )
        finally:
            self._disarm_execution_locked()

    def _arm_price_updates_locked(
        self,
        values: dict[str, str],
    ) -> None:
        """Prepare price-only app-owned OCA changes for a second confirmation."""
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Paper order management is disabled for this launch."
            return
        selected = self._active_target_perm_ids()
        if not selected:
            self._message = "Select at least one active layer to update."
            return
        self._disarm_execution_locked()
        state = self._view_model.select_position(
            self._selected_con_id,
            self._plan_form(self._drafts.get(self._selected_con_id, ())),
        )
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()
        snapshot = self._view_model.latest_snapshot()
        basis = self._state.unit_basis
        calculator = self._state.quote_calculator
        if snapshot is None or basis is None or calculator is None:
            self._message = "Price update blocked: a fresh priced position is required."
            return
        try:
            layers = self._paper_execution.prepare_market_exits(
                snapshot,
                target_perm_ids=selected,
                expected_client_id=self._settings.client_id,
            )
            orders_by_id = {order.order_id: order for order in snapshot.working_orders}
            updates: list[PriceUpdateCandidate] = []
            for layer in layers:
                target = orders_by_id.get(layer.target_order_id)
                stop = orders_by_id.get(layer.stop_order_id)
                if target is None or stop is None:
                    raise ExecutionBlocked("a selected OCA layer is no longer complete")
                target_percentage = _decimal_value(
                    values.get(f"active_target_{layer.target_perm_id}")
                )
                stop_percentage = _decimal_value(
                    values.get(f"active_stop_{layer.target_perm_id}")
                )
                if (
                    target_percentage is None
                    or target_percentage <= 0
                    or stop_percentage is None
                    or stop_percentage < 0
                    or stop_percentage > 100
                ):
                    raise ExecutionBlocked(
                        "active target must be positive and stop must be 0% to 100%"
                    )
                if stop_percentage == 0:
                    desired_target = round_up_price(
                        basis * (Decimal("1") + target_percentage / Decimal("100")),
                        calculator.bands,
                    )
                    desired_stop = round_up_price(basis, calculator.bands)
                else:
                    prices = preview_reference_prices(
                        basis,
                        target_percentage,
                        stop_percentage,
                        calculator.bands,
                    )
                    desired_target, desired_stop = (
                        prices.target_price,
                        prices.stop_price,
                    )
                updates.append(
                    PriceUpdateCandidate(
                        layer=layer,
                        target_price=(
                            desired_target
                            if desired_target is not None
                            and desired_target != target.limit_price
                            else None
                        ),
                        stop_price=(
                            desired_stop if desired_stop != stop.stop_price else None
                        ),
                        prior_target_price=target.limit_price,
                        prior_stop_price=stop.stop_price,
                    )
                )
            changes = tuple(
                update
                for update in updates
                if update.target_price is not None or update.stop_price is not None
            )
            if not changes:
                raise ExecutionBlocked("none of the selected prices would change")
            self._armed_price_updates = self._paper_execution.prepare_price_updates(
                snapshot,
                updates=changes,
                expected_client_id=self._settings.client_id,
            )
        except (ExecutionBlocked, ValueError) as error:
            self._message = f"Price update blocked: {error}"
            return
        changed_legs = sum(
            int(update.target_price is not None) + int(update.stop_price is not None)
            for update in self._armed_price_updates
        )
        self._message = (
            f"Fresh paper snapshot verified. Review {changed_legs} selected price "
            "amendments, then confirm."
        )

    def _confirm_price_updates_locked(self) -> None:
        updates = self._armed_price_updates
        if (
            self._paper_execution is None
            or not updates
            or self._selected_con_id is None
        ):
            self._message = (
                "Start a price update first; every paper change needs confirmation."
            )
            return
        state = self._view_model.select_position(
            self._selected_con_id,
            self._plan_form(self._drafts.get(self._selected_con_id, ())),
        )
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()
        snapshot = self._view_model.latest_snapshot()
        if snapshot is None:
            self._disarm_execution_locked()
            self._message = "Price update blocked: the fresh snapshot is unavailable."
            return
        try:
            confirmed = self._paper_execution.prepare_price_updates(
                snapshot,
                updates=updates,
                expected_client_id=self._settings.client_id,
            )
            if confirmed != updates:
                raise ExecutionBlocked("the selected OCA layers changed after review")
            receipt = self._paper_execution.modify_prices(
                snapshot,
                updates,
                host="127.0.0.1",
                port=self._settings.port,
                client_id=self._settings.client_id,
                timeout_seconds=self._settings.timeout_seconds,
            )
        except ExecutionOutcomeUnknown as error:
            self._message = f"Price update outcome is unknown: {error}. Refresh TWS before any further action."
        except ExecutionBlocked as error:
            self._message = f"Price update blocked: {error}"
        except Exception as error:
            self._message = f"Price update outcome is unknown: {error}"
        else:
            for update in updates:
                self._remember_pending_active_prices_locked(
                    target_perm_id=update.layer.target_perm_id,
                    stop_perm_id=update.layer.stop_perm_id,
                    target_price=update.target_price,
                    stop_price=update.stop_price,
                )
            self._refresh_after_acknowledged_write_locked(
                f"TWS acknowledged {len(receipt.entry.order_ids)} app-owned OCA "
                "price amendment(s)."
            )
        finally:
            self._disarm_execution_locked()

    def _apply_state_locked(self, state: ViewState) -> None:
        self._state = state
        self._selected_con_id = state.selected_con_id
        self._reconcile_pending_active_prices_locked()
        self._ensure_draft_locked()
        if state.status is UiStatus.READY:
            self._message = "Verified broker state is ready for read-only planning."
        elif state.status is not UiStatus.EMPTY:
            blocking = next(
                (
                    validation.message
                    for validation in state.validations
                    if validation.blocking
                ),
                None,
            )
            self._message = (
                f"{state.status_message}: {blocking}"
                if blocking
                else state.status_message
            )

    def _remember_pending_active_prices_locked(
        self,
        *,
        target_perm_id: int,
        stop_perm_id: int,
        target_price: Decimal | None,
        stop_price: Decimal | None,
    ) -> None:
        """Retain a TWS-acknowledged amendment through one lagging snapshot."""
        if target_price is None and stop_price is None:
            return
        prior = self._pending_active_prices.get(target_perm_id)
        self._pending_active_prices[target_perm_id] = (
            target_price if target_price is not None else (prior[0] if prior else None),
            stop_perm_id,
            stop_price if stop_price is not None else (prior[2] if prior else None),
        )

    def _reconcile_pending_active_prices_locked(self) -> None:
        """Drop presentation overrides as soon as TWS confirms the new prices."""
        if not self._pending_active_prices:
            return
        orders_by_perm = {order.perm_id: order for order in self._state.working_orders}
        pending: dict[int, tuple[Decimal | None, int, Decimal | None]] = {}
        for target_perm_id, (
            target_price,
            stop_perm_id,
            stop_price,
        ) in self._pending_active_prices.items():
            target = orders_by_perm.get(target_perm_id)
            stop = orders_by_perm.get(stop_perm_id)
            if target is None or stop is None:
                continue
            unresolved_target = (
                target_price
                if target_price is not None and target.limit_price != target_price
                else None
            )
            unresolved_stop = (
                stop_price
                if stop_price is not None and stop.stop_price != stop_price
                else None
            )
            if unresolved_target is not None or unresolved_stop is not None:
                pending[target_perm_id] = (
                    unresolved_target,
                    stop_perm_id,
                    unresolved_stop,
                )
        self._pending_active_prices = pending

    def _record_refresh_time_locked(self) -> None:
        """Show the local time of the last completed broker snapshot attempt."""
        self._last_refreshed_at = datetime.now().astimezone().strftime("%H:%M")

    def _announce_reconciliation_locked(self) -> None:
        """Recover app-owned OCA pairs observed after an interrupted send."""
        if self._paper_execution is None:
            return
        snapshot = self._view_model.latest_snapshot()
        if snapshot is None:
            return
        try:
            record_completed_orders = getattr(
                self._paper_execution, "record_completed_orders", None
            )
            if callable(record_completed_orders):
                record_completed_orders(snapshot)
            reconciled = self._paper_execution.reconcile_snapshot(snapshot)
            record_executions = getattr(
                self._paper_execution, "record_executions", None
            )
            if callable(record_executions):
                record_executions(snapshot)
        except ExecutionBlocked as error:
            self._message = f"Journal reconciliation blocked: {error}"
            return
        if reconciled:
            order_count = sum(len(entry.order_ids) for entry in reconciled)
            self._message = (
                f"Recovered {order_count} app-owned orders from TWS. "
                "Their journal status is reconciled."
            )

    def _ensure_draft_locked(self) -> None:
        con_id = self._selected_con_id
        if con_id is None:
            return
        if self._state.bracket_form.layers:
            self._drafts[con_id] = self._state.bracket_form.layers
            return
        if self._drafts.get(con_id):
            return
        basis = self._state.unit_basis
        calculator = self._state.quote_calculator
        presets = self._preset_for_index(0)
        if (
            basis is None
            or calculator is None
            or self._state.available_quantity <= 0
            or presets is None
        ):
            self._drafts[con_id] = ()
            return
        target, stop = presets
        try:
            prices = preview_reference_prices(basis, target, stop, calculator.bands)
        except ValueError:
            self._drafts[con_id] = ()
            return
        self._drafts[con_id] = (
            DraftLayerForm(
                quantity=str(self._state.available_quantity),
                target_price=format(prices.target_price, "f"),
                stop_price=format(prices.stop_price, "f"),
                target_percentage=format(target, "f"),
                stop_percentage=format(stop, "f"),
            ),
        )

    def _current_layers(self) -> tuple[DraftLayerForm, ...]:
        if self._selected_con_id is None:
            return ()
        return self._drafts.get(self._selected_con_id, ())

    def _save_form_locked(self, values: dict[str, str]) -> None:
        self._target_presets = values.get("target_presets", self._target_presets)
        self._stop_presets = values.get("stop_presets", self._stop_presets)
        if self._selected_con_id is None:
            return
        layers: list[DraftLayerForm] = []
        for index, previous in enumerate(self._current_layers(), start=1):
            target = values.get(f"target_{index}", previous.target_percentage)
            stop = values.get(f"stop_{index}", previous.stop_percentage)
            quantity = values.get(f"quantity_{index}", previous.quantity)
            tif = values.get(f"tif_{index}", previous.tif)
            try:
                prices = preview_reference_prices(
                    self._state.unit_basis or Decimal("0"),
                    Decimal(target),
                    Decimal(stop),
                    self._state.quote_calculator.bands
                    if self._state.quote_calculator is not None
                    else (),
                )
            except (InvalidOperation, ValueError):
                self._message = (
                    "Targets must be above 0%; stops must be between 0% and 100%."
                )
                return
            layers.append(
                DraftLayerForm(
                    quantity=quantity,
                    target_price=format(prices.target_price, "f"),
                    stop_price=format(prices.stop_price, "f"),
                    target_percentage=format(Decimal(target), "f"),
                    stop_percentage=format(Decimal(stop), "f"),
                    tif=tif if tif in {"GTC", "DAY"} else previous.tif,
                )
            )
        self._drafts[self._selected_con_id] = tuple(layers)

    def _add_layer_locked(self) -> None:
        layers = list(self._current_layers())
        con_id = self._selected_con_id
        if not layers or len(layers) >= self._state.available_quantity:
            return
        if con_id is None:
            return
        presets = self._preset_for_index(len(layers))
        basis = self._state.unit_basis
        calculator = self._state.quote_calculator
        if presets is None or basis is None or calculator is None:
            self._message = (
                "Enter valid comma-separated LMT and STP preset percentages first."
            )
            return
        target, stop = presets
        try:
            prices = preview_reference_prices(basis, target, stop, calculator.bands)
        except ValueError:
            self._message = (
                "The selected position does not have a usable price increment."
            )
            return
        layers.append(
            DraftLayerForm(
                quantity="0",
                target_price=format(prices.target_price, "f"),
                stop_price=format(prices.stop_price, "f"),
                target_percentage=format(target, "f"),
                stop_percentage=format(stop, "f"),
            )
        )
        self._drafts[con_id] = tuple(
            replace(layer, quantity=str(quantity))
            for layer, quantity in zip(
                layers,
                _split_quantity(self._state.available_quantity, len(layers)),
                strict=True,
            )
        )

    def _remove_layer_locked(self, index: int) -> None:
        layers = list(self._current_layers())
        con_id = self._selected_con_id
        if len(layers) <= 1 or not 1 <= index <= len(layers):
            return
        if con_id is None:
            return
        del layers[index - 1]
        self._drafts[con_id] = tuple(layers)

    def _equal_split_locked(self, *, use_available_quantity: bool) -> None:
        layers = self._current_layers()
        con_id = self._selected_con_id
        if not layers or con_id is None:
            return
        total_quantity = (
            self._state.available_quantity
            if use_available_quantity
            else sum(_int_or_zero(layer.quantity) for layer in layers)
        )
        self._drafts[con_id] = tuple(
            replace(layer, quantity=str(quantity))
            for layer, quantity in zip(
                layers, _split_quantity(total_quantity, len(layers)), strict=True
            )
        )

    def _preset_for_index(self, index: int) -> tuple[Decimal, Decimal] | None:
        targets = _parse_presets(self._target_presets, maximum=Decimal("1000"))
        stops = _parse_presets(self._stop_presets, maximum=Decimal("100"))
        if targets is None or stops is None:
            return None
        return targets[min(index, len(targets) - 1)], stops[min(index, len(stops) - 1)]

    def _page(self) -> Any:
        state = self._state
        ready = state.status is UiStatus.READY
        title = (
            state.position_title
            if self._selected_con_id is not None
            else "Select an option position"
        )
        return Div(
            self._header(ready),
            Div(
                self._inventory(),
                self._workspace(title),
                self._review(),
                cls="grid h-[calc(100vh-3.5rem)] min-h-0 grid-cols-[16rem_minmax(0,1fr)_19rem] overflow-hidden border-t border-border",
            ),
            self._toast_component(),
            self._launch_connection_dialog(),
            Script(_busy_submit_script()),
            cls="h-screen overflow-hidden bg-background text-foreground selection:bg-primary selection:text-primary-foreground",
        )

    def _toast_component(self) -> Any:
        notice = self._toast
        # Keep the official Toaster mounted on every response so the embedded
        # Datastar runtime always has its signal and close button. A normal
        # Toaster uses an ``ifmissing`` signal, which is right for initial
        # hydration but intentionally does not replace a pre-existing signal
        # after an action rerender. The explicit non-ifmissing signal below is
        # the documented server-side update path for each new notice.
        initial_toasts = (
            [
                {
                    "id": 1,
                    "title": notice.title,
                    "description": notice.description,
                    "variant": notice.variant,
                    "timestamp": 1,
                    "order": 0,
                },
                None,
                None,
            ]
            if notice is not None
            else None
        )
        return Div(
            Signal("toasts", initial_toasts, ifmissing=False)
            if initial_toasts is not None
            else None,
            Toaster(position="top-right"),
        )

    def _launch_connection_dialog(self) -> Any:
        """Keep first-run connection feedback in one focused, recoverable surface."""
        state = self._launch_connection
        if state not in {"connecting", "failed"}:
            return None
        connecting = state == "connecting"
        content: list[Any] = [
            DialogHeader(
                DialogTitle("Connecting to TWS" if connecting else "TWS unavailable"),
                DialogDescription(
                    "Reading the paper account and open option positions."
                    if connecting
                    else "Open TWS, enable its API, then retry the connection.",
                ),
            ),
        ]
        if connecting:
            content.append(
                Div(
                    Icon(
                        "lucide:loader-circle",
                        cls="size-4 animate-spin text-muted-foreground",
                    ),
                    Span("Connecting…", cls="text-sm text-muted-foreground"),
                    cls="flex items-center gap-2",
                )
            )
        else:
            content.extend(
                (
                    Alert(
                        Icon("lucide:circle-alert"),
                        AlertTitle("No verified portfolio loaded"),
                        AlertDescription(
                            "The workbench is still available, but order actions remain locked."
                        ),
                        variant="destructive",
                        live=True,
                        cls="border-destructive/70 bg-red-950 text-red-50 [&_p]:text-red-100/90",
                    ),
                    DialogFooter(
                        Form(
                            Button(
                                "Retry connection",
                                type="submit",
                                data_busy_text="Retrying…",
                            ),
                            HTMLInput(
                                type="hidden", name="action", value="launch-refresh"
                            ),
                            action=f"/{self.session_token}/action",
                            method="post",
                        )
                    ),
                )
            )
        return Div(
            Dialog(
                DialogContent(*content, show_close_button=False),
                signal="launch_connection",
                default_open=True,
                size="sm",
            ),
            Script(
                """
                (() => {
                  const dialog = document.getElementById('launch_connection');
                  if (dialog && !dialog.open) dialog.showModal();
                })();
                """
            ),
            Script(
                f"""
                (() => {{
                  const check = async () => {{
                    try {{
                      const response = await fetch('/{self.session_token}/connection-status', {{ cache: 'no-store' }});
                      const status = await response.json();
                      if (status.state !== 'connecting') {{
                        window.location.reload();
                        return;
                      }}
                    }} catch (_) {{
                      // Keep the existing dialog visible; the user can retry.
                    }}
                    window.setTimeout(check, 500);
                  }};
                  window.setTimeout(check, 500);
                }})();
                """
            )
            if connecting
            else None,
            data_launch_connection=state,
        )

    def _header(self, ready: bool) -> Any:
        return Div(
            Div(
                cls="size-2 rounded-full "
                + ("bg-emerald-500" if ready else "bg-amber-400")
            ),
            Span(
                "CONNECTED" if ready else self._state.status,
                cls="text-xs font-semibold tracking-wide",
            ),
            Badge(
                "SIMULATED EXECUTION"
                if self._demo_mode and self._paper_execution is not None
                else "SIMULATED DATA"
                if self._demo_mode
                else "PAPER EXECUTION"
                if self._paper_execution is not None
                else "READ-ONLY",
                variant="outline",
            ),
            Span(
                f"Account {self._state.account or '—'}",
                cls="text-xs text-muted-foreground",
            ),
            Span(cls="flex-1"),
            Span(
                f"Last refreshed {self._last_refreshed_at}",
                cls="text-xs text-muted-foreground",
            ),
            Form(
                Button(
                    "Refresh",
                    variant="outline",
                    size="sm",
                    type="submit",
                    data_busy_text="Refreshing…",
                ),
                HTMLInput(type="hidden", name="action", value="refresh"),
                HTMLInput(type="hidden", name="account", value=self._settings.account),
                HTMLInput(type="hidden", name="port", value=str(self._settings.port)),
                HTMLInput(
                    type="hidden", name="client_id", value=str(self._settings.client_id)
                ),
                HTMLInput(
                    type="hidden",
                    name="timeout",
                    value=str(self._settings.timeout_seconds),
                ),
                action=f"/{self.session_token}/action",
                method="post",
            ),
            self._settings_dialog(),
            cls="flex h-14 items-center gap-3 px-4",
        )

    def _inventory(self) -> Any:
        rows = []
        for position in self._state.positions:
            selected = position.con_id == self._selected_con_id
            symbol, contract_detail = _position_identity(position.local_symbol)
            rows.append(
                Form(
                    Button(
                        Div(
                            Span(symbol, cls="text-sm font-semibold"),
                            Badge(position.quantity, variant="secondary"),
                            cls="flex w-full items-center justify-between",
                        ),
                        P(
                            contract_detail,
                            cls="mt-1.5 w-full text-xs text-muted-foreground",
                        ),
                        variant="ghost",
                        disabled=not position.eligible,
                        type="submit",
                        cls=(
                            "h-auto min-h-20 w-full flex-col items-stretch justify-center gap-0 "
                            "rounded-none border-l-2 px-4 py-4 text-left transition-colors "
                            "hover:bg-accent "
                            + (
                                "border-emerald-400 bg-emerald-500/10 hover:bg-emerald-500/15"
                                if selected
                                else "border-transparent"
                            )
                        ),
                    ),
                    HTMLInput(type="hidden", name="action", value="select"),
                    HTMLInput(type="hidden", name="con_id", value=str(position.con_id)),
                    action=f"/{self.session_token}/action",
                    method="post",
                )
            )
        return Div(
            Div(
                Span(
                    "LONG POSITIONS",
                    cls="text-xs font-semibold tracking-wide text-muted-foreground",
                ),
                Badge(f"{len(self._state.positions)} active", cls="text-[10px]"),
                cls="flex items-center justify-between px-3 py-4",
            ),
            ScrollArea(
                *rows,
                aria_label="Open option positions",
                cls="min-h-0 flex-1",
            ),
            cls="flex min-h-0 flex-col overflow-hidden border-r border-border bg-card/30",
        )

    def _settings_dialog(self) -> Any:
        return Dialog(
            DialogTrigger("Settings", variant="outline", size="sm"),
            DialogContent(
                DialogHeader(
                    DialogTitle("Connection & layer defaults"),
                    DialogDescription(
                        "Changing these values refreshes the verified portfolio and clears the current preview."
                    ),
                ),
                Form(
                    Div(
                        _field(
                            "Account",
                            Input(name="account", value=self._settings.account),
                        ),
                        _field(
                            "Port",
                            Input(
                                name="port",
                                type="number",
                                value=str(self._settings.port),
                            ),
                        ),
                        _field(
                            "Client ID",
                            Input(
                                name="client_id",
                                type="number",
                                value=str(self._settings.client_id),
                            ),
                        ),
                        _field(
                            "Timeout",
                            Input(
                                name="timeout",
                                type="number",
                                value=str(self._settings.timeout_seconds),
                                step="0.5",
                            ),
                        ),
                        cls="grid grid-cols-2 gap-4",
                    ),
                    Separator(cls="my-5"),
                    H3("Layer defaults", cls="text-sm font-semibold"),
                    Div(
                        _field(
                            "LMT targets",
                            Input(name="target_presets", value=self._target_presets),
                        ),
                        _field(
                            "STP losses",
                            Input(name="stop_presets", value=self._stop_presets),
                        ),
                        cls="mt-3 grid grid-cols-2 gap-4",
                    ),
                    P(
                        "Comma-separated percentages. The final value repeats for later layers.",
                        cls="mt-3 text-xs leading-5 text-muted-foreground",
                    ),
                    DialogFooter(
                        DialogClose("Cancel", variant="outline"),
                        Button(
                            "Refresh with settings",
                            type="submit",
                            data_busy_text="Refreshing…",
                        ),
                        cls="mt-6",
                    ),
                    HTMLInput(type="hidden", name="action", value="refresh"),
                    action=f"/{self.session_token}/action",
                    method="post",
                ),
            ),
            signal="connection_settings",
            size="lg",
        )

    def _workspace(self, title: str) -> Any:
        coverage, app_order_count, order_count = self._order_coverage()
        active_pairs = self._active_oca_pairs()
        pending = self._pending_submissions()
        closed = self._closed_submissions()
        return Div(
            self._coverage_alert(coverage, app_order_count, order_count),
            Div(
                Div(
                    H1(title, cls="text-2xl font-semibold tracking-tight"),
                    P(
                        "Build and manage app-owned OCA layers.",
                        cls="mt-2 text-sm text-muted-foreground",
                    ),
                ),
                Div(
                    Span("Cost basis / Ask", cls="text-xs text-muted-foreground"),
                    P(
                        " / ".join(fact.value for fact in self._state.quote[:2]) or "—",
                        cls="mt-1 font-mono text-sm",
                    ),
                    cls="text-right",
                ),
                cls="flex items-start justify-between gap-6",
            ),
            P(
                "TWS orders are pending verification. The broker snapshot may not yet include them."
                if pending
                else f"{self._state.available_quantity} contracts verified available to bracket",
                cls="mt-2 text-sm font-medium "
                + ("text-amber-300" if pending else "text-emerald-400"),
            ),
            Div(
                ScrollArea(
                    self._pending_layers_panel(pending) if pending else None,
                    self._active_layers_panel() if active_pairs else None,
                    self._closed_layers_panel(closed) if closed else None,
                    Div(
                        Separator(cls="flex-1"),
                        Span(
                            "New bracket layers",
                            cls="text-xs font-semibold text-muted-foreground",
                        ),
                        Separator(cls="flex-1"),
                        cls="my-6 flex items-center gap-3",
                    )
                    if active_pairs and not pending
                    else None,
                    self._draft_panel() if not pending else None,
                    aria_label="OCA layers workspace",
                    orientation="vertical",
                    cls="h-full",
                ),
                cls="mt-5 min-h-0 flex-1 overflow-hidden",
            ),
            cls="flex min-w-0 min-h-0 flex-col overflow-hidden px-8 py-6",
        )

    def _order_coverage(self) -> tuple[str, int, int]:
        """Classify displayed order coverage using durable app ownership proof."""
        orders = self._state.working_orders
        if not orders or self._selected_con_id is None:
            return "none", 0, 0
        if self._paper_execution is None:
            return "external", 0, len(orders)
        owned_perm_ids = self._paper_execution.owned_perm_ids(
            account=self._verified_selected_account(),
            con_id=self._selected_con_id,
        )
        app_order_count = sum(order.perm_id in owned_perm_ids for order in orders)
        if app_order_count == len(orders):
            return "app", app_order_count, len(orders)
        if app_order_count:
            return "mixed", app_order_count, len(orders)
        return "external", 0, len(orders)

    def _active_oca_pairs(self) -> tuple[tuple[str, Any, Any], ...]:
        """Return complete LMT/STP pairs that the journal proves are app-owned."""
        if self._paper_execution is None or self._selected_con_id is None:
            return ()
        owned_perm_ids = self._paper_execution.owned_perm_ids(
            account=self._verified_selected_account(),
            con_id=self._selected_con_id,
        )
        groups: dict[str, list[Any]] = {}
        for order in self._state.working_orders:
            if order.perm_id not in owned_perm_ids or not order.oca_group:
                continue
            groups.setdefault(order.oca_group, []).append(order)

        pairs: list[tuple[str, Any, Any]] = []
        for group in sorted(groups):
            orders = groups[group]
            targets = [order for order in orders if order.order_type == "LMT"]
            stops = [order for order in orders if order.order_type == "STP"]
            if (
                len(targets) == 1
                and len(stops) == 1
                and len(orders) == 2
                and all(
                    order.status in {"Submitted", "PreSubmitted"} for order in orders
                )
            ):
                pairs.append((group, targets[0], stops[0]))
        return tuple(pairs)

    def _submission_outcomes(
        self,
    ) -> tuple[tuple[JournalEntry, int, LayerOutcome], ...]:
        if self._paper_execution is None or self._selected_con_id is None:
            return ()
        reader = getattr(self._paper_execution, "submission_entries", None)
        if not callable(reader):
            return ()
        entries = reader(
            account=self._verified_selected_account(),
            con_id=self._selected_con_id,
        )
        active_ids = frozenset(
            {
                order.perm_id
                for _group, target, stop in self._active_oca_pairs()
                for order in (target, stop)
            }
        )
        observed_ids = frozenset(order.perm_id for order in self._state.working_orders)
        return tuple(
            (
                entry,
                index,
                classify_journal_layer(
                    entry,
                    index,
                    active_perm_ids=active_ids,
                    observed_perm_ids=observed_ids,
                ),
            )
            for entry in entries
            for index in range(len(entry.layers))
        )

    def _pending_submissions(
        self,
    ) -> tuple[tuple[JournalEntry, int, LayerOutcome], ...]:
        return tuple(
            item
            for item in self._submission_outcomes()
            if item[2].status
            in {"PENDING", "UNKNOWN", "PARTIAL", "NO_EXECUTION_EVIDENCE"}
        )

    def _closed_submissions(self) -> tuple[tuple[JournalEntry, int, LayerOutcome], ...]:
        return tuple(
            item
            for item in self._submission_outcomes()
            if item[2].status.startswith("CLOSED_")
        )

    def _pending_layers_panel(
        self, items: tuple[tuple[JournalEntry, int, LayerOutcome], ...]
    ) -> Any:
        rows: list[Any] = []
        for entry, index, outcome in items:
            layer = entry.layers[index]
            heading = {
                "PENDING": "Awaiting TWS verification",
                "UNKNOWN": "Outcome not confirmed",
                "PARTIAL": "Partially filled",
                "NO_EXECUTION_EVIDENCE": "No fill evidence",
            }[outcome.status]
            detail = {
                "PENDING": "Check TWS for Transmit or a working order, then Refresh.",
                "UNKNOWN": "Inspect TWS before taking another action. Do not retry this draft.",
                "PARTIAL": (
                    f"{format(outcome.filled_quantity, 'f')} of {layer.quantity} "
                    "contracts filled. Verify the remaining order in TWS."
                ),
                "NO_EXECUTION_EVIDENCE": (
                    "This layer is no longer shown as working, but TWS has not supplied "
                    "a matching execution. Check the TWS trade log."
                ),
            }[outcome.status]
            rows.append(
                Div(
                    Div(
                        Span(
                            f"Layer {index + 1} · {heading}",
                            cls="text-sm font-semibold",
                        ),
                        Badge("VERIFY IN TWS", variant="outline"),
                        cls="flex items-center justify-between gap-3",
                    ),
                    P(detail, cls="mt-2 text-xs leading-5 text-muted-foreground"),
                    P(
                        f"{layer.quantity} contracts · SELL LMT ${layer.target_price} "
                        f"/ SELL STP ${layer.stop_price} · {layer.tif}",
                        cls="mt-2 font-mono text-xs",
                    ),
                    cls="border-b border-border py-4 last:border-b-0",
                )
            )
        title = (
            "Active layers · pending TWS verification"
            if all(outcome.status == "PENDING" for _entry, _index, outcome in items)
            else "Active layers · TWS outcome unknown"
        )
        return Card(
            CardHeader(CardTitle(title)),
            CardContent(*rows),
            cls="border-amber-500/40 bg-amber-500/5",
        )

    def _closed_layers_panel(
        self, items: tuple[tuple[JournalEntry, int, LayerOutcome], ...]
    ) -> Any:
        rows: list[Any] = []
        for entry, index, outcome in items:
            layer = entry.layers[index]
            result = {
                "CLOSED_PROFIT": "Profit",
                "CLOSED_LOSS": "Loss",
                "CLOSED_FLAT": "Flat",
                "CLOSED_PNL_UNKNOWN": "P&L unavailable",
            }[outcome.status]
            pnl_text = (
                f" · {outcome.currency} {outcome.realized_pnl:+,.2f}"
                if outcome.realized_pnl is not None
                else ""
            )
            rows.append(
                Div(
                    Div(
                        Span(
                            f"Layer {index + 1} · {outcome.exit_side} filled",
                            cls="text-sm font-semibold",
                        ),
                        Badge(f"{result}{pnl_text}", variant="outline"),
                        cls="flex items-center justify-between gap-3",
                    ),
                    P(
                        f"{format(outcome.filled_quantity, 'f')} contracts closed · "
                        f"planned LMT ${layer.target_price} / STP ${layer.stop_price}",
                        cls="mt-2 font-mono text-xs text-muted-foreground",
                    ),
                    cls="border-b border-border py-4 last:border-b-0",
                )
            )
        return Card(
            CardHeader(CardTitle("Closed bracket history")),
            CardContent(*rows),
        )

    def _verified_selected_account(self) -> str:
        """Use the unredacted account observed in the selected broker snapshot.

        ``ViewState.account`` is intentionally display-redacted and must never
        be used to identify an execution-journal entry.
        """
        snapshot = self._view_model.latest_snapshot()
        if (
            snapshot is not None
            and self._selected_con_id is not None
            and snapshot.selected.con_id == self._selected_con_id
        ):
            return snapshot.selected.account
        return self._settings.account

    def _active_target_perm_ids(self) -> tuple[int, ...]:
        """Act on every reconciled layer of the currently selected contract."""
        return tuple(
            target.perm_id for _group, target, _stop in self._active_oca_pairs()
        )

    def _active_layers_panel(self) -> Any:
        pairs = self._active_oca_pairs()
        if not pairs:
            return Card(
                CardHeader(
                    CardTitle("No reconciled active layers"),
                    CardDescription(
                        "Refresh TWS to inspect OCA pairs created by this application."
                    ),
                ),
            )
        return Form(
            Card(
                CardHeader(
                    Div(CardTitle("Active OCA layers")),
                    CardAction(
                        Div(
                            Button(
                                "Move stop to B/E",
                                variant="outline",
                                size="sm",
                                type="button",
                                data_move_stops_to_be=True,
                                disabled=self._paper_execution is None,
                            ),
                            Button(
                                "Close all",
                                variant="destructive",
                                size="sm",
                                type="submit",
                                name="action",
                                value="market-exit-selected",
                                disabled=self._paper_execution is None,
                            ),
                            cls="flex flex-wrap items-center justify-end gap-2",
                        ),
                    ),
                ),
                CardContent(
                    Div(
                        ScrollArea(
                            Div(
                                *[
                                    self._active_layer_row(index, group, target, stop)
                                    for index, (group, target, stop) in enumerate(
                                        pairs, start=1
                                    )
                                ],
                                cls="w-full min-w-[41rem]",
                            ),
                            aria_label="Active OCA layer rows",
                            orientation="horizontal",
                            cls="w-full",
                        ),
                        cls="min-w-0",
                    ),
                ),
            ),
            Script(_live_active_script(self._live_active_configuration())),
            id="active-form",
            action=f"/{self.session_token}/action",
            method="post",
        )

    def _active_layer_row(self, index: int, _group: str, target: Any, stop: Any) -> Any:
        calculator = self._state.quote_calculator
        bands = calculator.bands if calculator is not None else ()
        pending = self._pending_active_prices.get(target.perm_id)
        display_target_price = (
            pending[0] if pending and pending[0] is not None else target.limit_price
        )
        display_stop_price = (
            pending[2] if pending and pending[2] is not None else stop.stop_price
        )
        target_percentage = _active_percentage_for_price(
            display_target_price,
            self._state.unit_basis,
            target=True,
            bands=bands,
            presets=_parse_presets(self._target_presets, maximum=Decimal("1000")) or (),
        )
        stop_percentage = _active_percentage_for_price(
            display_stop_price,
            self._state.unit_basis,
            target=False,
            bands=bands,
            presets=_parse_presets(self._stop_presets, maximum=Decimal("100")) or (),
        )
        gain, loss = self._active_layer_projection(
            target,
            stop,
            target_price=display_target_price,
            stop_price=display_stop_price,
        )
        return _layer_row_layout(
            index=index,
            target_field=_percentage_price_field(
                "LMT target",
                Input(
                    name=f"active_target_{target.perm_id}",
                    id=f"active-target-{index}",
                    type="number",
                    value=target_percentage,
                    min="0.1",
                    step="0.1",
                    data_active_input="target",
                    data_active_perm_id=target.perm_id,
                    data_active_original=display_target_price,
                    data_active_initial=target_percentage,
                    data_live_layer=index,
                    cls="pr-8",
                ),
                input_id=f"active-target-{index}",
                price=_price_text(display_target_price),
                outcome=gain,
                outcome_label="gain",
                tone="text-emerald-400",
                layer_index=index,
                kind="active-target",
            ),
            stop_field=_percentage_price_field(
                "STP loss",
                Input(
                    name=f"active_stop_{target.perm_id}",
                    id=f"active-stop-{index}",
                    type="number",
                    value=stop_percentage,
                    min="0",
                    max="100",
                    step="0.1",
                    data_active_input="stop",
                    data_active_perm_id=target.perm_id,
                    data_active_original=display_stop_price,
                    data_active_initial=stop_percentage,
                    data_live_layer=index,
                    cls="pr-8",
                ),
                input_id=f"active-stop-{index}",
                price=_price_text(display_stop_price),
                outcome=loss,
                outcome_label="max loss",
                tone="text-rose-400",
                layer_index=index,
                kind="active-stop",
            ),
            quantity_field=_field(
                "Quantity",
                Input(
                    id=f"active-quantity-{index}",
                    type="number",
                    value=str(target.remaining),
                    readonly=True,
                    data_active_quantity=target.perm_id,
                ),
                input_id=f"active-quantity-{index}",
            ),
            tif_field=_field(
                "TIF",
                Input(
                    id=f"active-tif-{index}",
                    value=target.tif or "—",
                    readonly=True,
                ),
                input_id=f"active-tif-{index}",
            ),
            action_field=Button(
                Icon("lucide:trash-2"),
                variant="outline",
                size="icon",
                type="submit",
                name="action",
                value=f"cancel-pair-arm:{target.perm_id}",
                disabled=self._paper_execution is None,
                aria_label=f"Delete OCA layer {index}",
                title="Delete OCA bracket",
                cls="mt-5",
            ),
        )

    def _active_layer_projection(
        self,
        target: Any,
        stop: Any,
        *,
        target_price: Decimal | None,
        stop_price: Decimal | None,
    ) -> tuple[str, str]:
        basis = self._state.unit_basis
        multiplier = self._state.multiplier
        if basis is None or multiplier is None:
            return "—", "—"
        if target_price is None or stop_price is None:
            return "—", "—"
        try:
            quantity = Decimal(str(target.remaining))
        except InvalidOperation:
            return "—", "—"
        if quantity <= 0:
            return "—", "—"
        gain = (target_price - basis) * multiplier * quantity
        loss = (stop_price - basis) * multiplier * quantity
        return _money(gain), _money(loss)

    def _coverage_alert(
        self,
        coverage: str,
        app_order_count: int,
        order_count: int,
    ) -> Any:
        if coverage == "app":
            available = self._state.available_quantity
            coverage_message = (
                f"{app_order_count} app-created orders were reconciled with TWS. "
                "This position is fully covered; a new bracket will not be created."
                if available == 0
                else (
                    f"{app_order_count} app-created orders were reconciled with TWS. "
                    f"{available} contracts remain available for a new bracket."
                )
            )
            return Alert(
                AlertTitle("App-managed OCA coverage active"),
                AlertDescription(coverage_message),
                cls="mb-5 border-emerald-500/40 bg-emerald-500/10 text-emerald-100",
            )
        if coverage == "mixed":
            return Alert(
                AlertTitle("Mixed order coverage detected"),
                AlertDescription(
                    f"{app_order_count} of {order_count} related orders are app-managed. "
                    "External orders remain inspect-only and prevent a new bracket."
                ),
                cls="mb-5 border-amber-500/40 bg-amber-500/10 text-amber-100",
            )
        if coverage == "external":
            return Alert(
                AlertTitle("Existing order coverage detected"),
                AlertDescription(
                    "Associated external orders remain inspect-only. "
                    "Only verified available contracts can be bracketed."
                ),
                cls="mb-5 border-amber-500/40 bg-amber-500/10 text-amber-100",
            )
        return Div(cls="hidden")

    def _draft_panel(self) -> Any:
        layers = self._current_layers()
        return Form(
            Card(
                CardHeader(
                    Div(
                        CardTitle("Layered OCA draft"),
                    ),
                    CardAction(
                        Div(
                            Button(
                                "Split all available",
                                variant="outline",
                                size="sm",
                                type="submit",
                                name="action",
                                value="equal-split-available",
                            ),
                            Button(
                                "Split assigned",
                                variant="outline",
                                size="sm",
                                type="submit",
                                name="action",
                                value="equal-split-assigned",
                            ),
                            Button(
                                "Add layer",
                                variant="secondary",
                                size="sm",
                                type="submit",
                                name="action",
                                value="add-layer",
                                disabled=len(layers) >= self._state.available_quantity,
                            ),
                            cls="flex flex-wrap items-center justify-end gap-2",
                        ),
                    ),
                ),
                CardContent(
                    Div(
                        ScrollArea(
                            Div(
                                *[
                                    self._draft_layer_row(index, layer, len(layers))
                                    for index, layer in enumerate(layers, start=1)
                                ],
                                Div(
                                    Span(
                                        f"{sum(_int_or_zero(layer.quantity) for layer in layers)} "
                                        f"of {self._state.available_quantity} contracts allocated",
                                        data_live_allocation=True,
                                        cls="text-xs text-muted-foreground",
                                    ),
                                    cls="mt-5 flex justify-end",
                                ),
                                cls="w-full min-w-[41rem]",
                            ),
                            aria_label="Draft layer rows",
                            orientation="horizontal",
                            cls="w-full",
                        ),
                        cls="min-w-0",
                    ),
                ),
            ),
            HTMLInput(type="hidden", name="target_presets", value=self._target_presets),
            HTMLInput(type="hidden", name="stop_presets", value=self._stop_presets),
            Script(_live_draft_script(self._live_draft_configuration())),
            id="draft-form",
            action=f"/{self.session_token}/action",
            method="post",
        )

    def _live_draft_configuration(self) -> dict[str, Any] | None:
        """Expose verified draft-calculation inputs to the local WebView only."""
        basis = self._state.unit_basis
        multiplier = self._state.multiplier
        calculator = self._state.quote_calculator
        if basis is None or multiplier is None or calculator is None:
            return None
        return {
            "basis": format(basis, "f"),
            "multiplier": format(multiplier, "f"),
            "available": self._state.available_quantity,
            "bands": [
                {
                    "low": format(band.low_edge, "f"),
                    "increment": format(band.increment, "f"),
                }
                for band in calculator.bands
            ],
        }

    def _live_active_configuration(self) -> dict[str, Any] | None:
        """Expose verified price arithmetic for client-side active-change review."""
        basis = self._state.unit_basis
        calculator = self._state.quote_calculator
        if basis is None or calculator is None:
            return None
        return {
            "basis": format(basis, "f"),
            "multiplier": format(self._state.multiplier or Decimal("0"), "f"),
            "bands": [
                {
                    "low": format(band.low_edge, "f"),
                    "increment": format(band.increment, "f"),
                }
                for band in calculator.bands
            ],
        }

    def _draft_layer_row(self, index: int, layer: DraftLayerForm, count: int) -> Any:
        tif_signal = Signal(f"tif_{index}_value", _ref_only=True)
        gain, loss = self._layer_projection(layer)
        return _layer_row_layout(
            index=index,
            target_field=_percentage_price_field(
                "LMT target",
                Input(
                    name=f"target_{index}",
                    id=f"target_{index}",
                    type="number",
                    value=layer.target_percentage,
                    min="0.1",
                    step="0.1",
                    data_live_input="target",
                    data_live_layer=index,
                    cls="pr-8",
                ),
                input_id=f"target_{index}",
                price=layer.target_price,
                outcome=gain,
                outcome_label="gain",
                tone="text-emerald-400",
                layer_index=index,
                kind="target",
            ),
            stop_field=_percentage_price_field(
                "STP loss",
                Input(
                    name=f"stop_{index}",
                    id=f"stop_{index}",
                    type="number",
                    value=layer.stop_percentage,
                    min="0.1",
                    max="100",
                    step="0.1",
                    data_live_input="stop",
                    data_live_layer=index,
                    cls="pr-8",
                ),
                input_id=f"stop_{index}",
                price=layer.stop_price,
                outcome=loss,
                outcome_label="max loss",
                tone="text-rose-400",
                layer_index=index,
                kind="stop",
            ),
            quantity_field=_field(
                "Quantity",
                Input(
                    name=f"quantity_{index}",
                    id=f"quantity_{index}",
                    type="number",
                    value=layer.quantity,
                    min="1",
                    step="1",
                    data_live_input="quantity",
                    data_live_layer=index,
                ),
                input_id=f"quantity_{index}",
            ),
            tif_field=_field(
                "TIF",
                Div(
                    Select(
                        SelectTrigger(SelectValue(), id=f"tif_{index}"),
                        SelectContent(
                            SelectItem("GTC", value="GTC"),
                            SelectItem("DAY", value="DAY"),
                        ),
                        value=layer.tif,
                        label=layer.tif,
                        signal=f"tif_{index}",
                    ),
                    HTMLInput(
                        type="hidden",
                        name=f"tif_{index}",
                        data_bind=tif_signal,
                    ),
                ),
                input_id=f"tif_{index}",
            ),
            action_field=Button(
                Icon("lucide:trash-2"),
                variant="outline",
                size="icon",
                type="submit",
                name="action",
                value=f"remove-layer:{index}",
                disabled=count <= 1,
                aria_label=f"Remove layer {index}",
                cls="mt-5",
            ),
        )

    def _layer_projection(self, layer: DraftLayerForm) -> tuple[str, str]:
        basis = self._state.unit_basis
        multiplier = self._state.multiplier
        quantity = _int_or_zero(layer.quantity)
        if basis is None or multiplier is None or quantity <= 0:
            return "—", "—"
        try:
            gain = (Decimal(layer.target_price) - basis) * multiplier * quantity
            loss = (Decimal(layer.stop_price) - basis) * multiplier * quantity
        except InvalidOperation:
            return "—", "—"
        return _money(gain), _money(loss)

    def _outcome_projection(self, layers: tuple[DraftLayerForm, ...]) -> Any:
        basis = self._state.unit_basis
        multiplier = self._state.multiplier
        outcomes = []
        if basis is not None and multiplier is not None:
            for layer in layers:
                quantity = _int_or_zero(layer.quantity)
                try:
                    target = (
                        (Decimal(layer.target_price) - basis) * multiplier * quantity
                    )
                    stop = (Decimal(layer.stop_price) - basis) * multiplier * quantity
                except InvalidOperation:
                    continue
                outcomes.append((target, stop))
        gain = sum((target for target, _ in outcomes), Decimal("0"))
        loss = sum((stop for _, stop in outcomes), Decimal("0"))
        return Card(
            CardHeader(
                CardTitle("Outcome projection", cls="text-sm"),
                cls="px-4",
            ),
            CardContent(
                Div(
                    *_metric(
                        "Expected gain",
                        _money(gain),
                        "text-emerald-400",
                        live_key="gain",
                    ),
                    *_metric(
                        "Max loss", _money(loss), "text-rose-400", live_key="loss"
                    ),
                    *_metric(
                        "Breakeven after",
                        _breakeven(outcomes),
                        "text-amber-300",
                        live_key="breakeven",
                    ),
                    cls="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] items-baseline gap-x-3 gap-y-3",
                ),
                cls="px-4",
            ),
            data_draft_outcome=True,
            cls="gap-4 rounded-2xl py-4 shadow-none",
        )

    def _review(self) -> Any:
        pending = self._pending_submissions()
        market_exits = self._armed_market_exits or (
            (self._armed_market_exit,) if self._armed_market_exit is not None else ()
        )
        cancellation = self._armed_cancellation
        price_updates = self._armed_price_updates
        armed_execution = self._armed_execution
        action_rows: list[Any] = []
        if market_exits:
            action_rows = self._review_market_exit_plan(market_exits)
        elif cancellation is not None:
            action_rows = [self._review_cancellation_plan(cancellation)]
        elif price_updates:
            action_rows = [
                self._review_price_update(index, update)
                for index, update in enumerate(price_updates, start=1)
            ]
        elif armed_execution is not None:
            action_rows = [
                self._review_pair(index, layer)
                for index, layer in enumerate(self._current_layers(), start=1)
            ]
        draft_rows = (
            [
                self._review_pair(index, layer)
                for index, layer in enumerate(self._current_layers(), start=1)
            ]
            if not pending
            else []
        )
        has_active_layers = bool(self._active_oca_pairs())
        has_staged_action = bool(action_rows)
        review_badge = (
            Badge("MKT EXIT", variant="outline", cls="text-[10px]")
            if market_exits
            else Badge("CANCEL", variant="outline", cls="text-[10px]")
            if cancellation is not None
            else Badge("PRICE UPDATE", variant="outline", cls="text-[10px]")
            if price_updates
            else Badge("DRAFT", variant="outline", cls="text-[10px]")
            if has_staged_action
            else Div(
                Badge(
                    "DRAFT",
                    variant="outline",
                    data_draft_review_badge=True,
                    cls="text-[10px]" if draft_rows else "hidden text-[10px]",
                ),
                Badge(
                    "PRICE UPDATE",
                    variant="outline",
                    data_active_review_badge=True,
                    cls="hidden text-[10px]",
                )
                if has_active_layers
                else None,
                cls="flex items-center",
            )
        )
        return Div(
            Div(
                Span(
                    "ACTION REVIEW",
                    cls="text-xs font-semibold tracking-wide text-muted-foreground",
                ),
                review_badge,
                cls="flex items-center justify-between px-4 py-4",
            ),
            ScrollArea(
                *action_rows,
                aria_label="Planned order actions",
                cls="min-h-0 flex-1 px-4",
            )
            if has_staged_action
            else ScrollArea(
                Div(
                    *draft_rows,
                    data_draft_review=True,
                    cls="min-h-0" if draft_rows else "hidden min-h-0",
                ),
                self._live_active_review(hidden=bool(draft_rows))
                if has_active_layers
                else None,
                aria_label="Planned order actions",
                cls="min-h-0 flex-1 px-4",
            )
            if draft_rows or has_active_layers
            else Div(
                P(
                    "Pending TWS orders are shown in Active layers. Refresh after reviewing them in TWS."
                    if pending
                    else "Add a layer or modify an existing one to continue.",
                    cls="text-center text-sm leading-6 text-muted-foreground",
                ),
                cls="flex min-h-0 flex-1 items-center justify-center px-6",
            ),
            self._execution_control(),
            cls="flex min-h-0 flex-col overflow-hidden border-l border-border bg-card/30",
        )

    def _execution_control(self) -> Any:
        if self._pending_submissions():
            return Div(
                P("Waiting for TWS", cls="text-sm font-semibold"),
                P(
                    "Review pending orders in TWS and use Refresh to verify their state. "
                    "Do not resubmit this draft.",
                    cls="mt-2 text-xs leading-5 text-muted-foreground",
                ),
                cls="mx-4 mb-4 rounded-lg border border-amber-500/40 bg-amber-500/5 p-4",
            )
        market_exits = self._armed_market_exits or (
            (self._armed_market_exit,) if self._armed_market_exit is not None else ()
        )
        if self._armed_cancellation is not None:
            return self._staged_action_controls(
                confirm_action="cancel-pair-confirm",
                confirm_variant="destructive",
                busy_text="Cancelling…",
            )
        if market_exits:
            return self._staged_action_controls(
                confirm_action="market-exit-confirm",
                confirm_variant="destructive",
            )
        if self._armed_price_updates:
            return self._staged_action_controls(
                confirm_action="price-update-confirm",
            )
        if self._armed_execution is not None:
            return self._staged_action_controls(
                confirm_action="execute-confirm",
            )
        can_execute_draft = (
            self._paper_execution is not None
            and self._state.available_quantity > 0
            and bool(self._current_layers())
        )
        return Div(
            Div(
                Button(
                    "Execute paper order",
                    variant="default",
                    type="submit",
                    name="action",
                    value="execute-arm",
                    form="draft-form",
                    data_busy_text="Checking…",
                    disabled=not can_execute_draft,
                    cls="w-full",
                ),
                data_draft_execute=True,
                cls="w-full",
            ),
            Div(
                Button(
                    "Execute paper order",
                    variant="default",
                    type="submit",
                    name="action",
                    value="active-update-arm",
                    form="active-form",
                    data_active_execute=True,
                    disabled=True,
                    data_busy_text="Checking…",
                    cls="w-full",
                ),
                data_active_execute_control=True,
                cls="hidden w-full",
            )
            if self._active_oca_pairs()
            else None,
            self._outcome_projection(self._current_layers()),
            cls="mx-4 mb-4 flex w-[calc(100%-2rem)] flex-col gap-3",
        )

    def _staged_action_controls(
        self,
        *,
        confirm_action: str,
        confirm_variant: ButtonVariant = "default",
        busy_text: str = "Submitting…",
    ) -> Any:
        """One deliberate Cancel / Confirm bar for every staged order change."""
        return Form(
            Div(
                Button(
                    "Cancel",
                    variant="outline",
                    type="submit",
                    name="action",
                    value="cancel-staged",
                    cls="flex-1",
                ),
                Button(
                    "Confirm",
                    variant=confirm_variant,
                    type="submit",
                    name="action",
                    value=confirm_action,
                    data_busy_text=busy_text,
                    cls="flex-[2]",
                ),
                cls="flex gap-2",
            ),
            action=f"/{self.session_token}/action",
            method="post",
            cls="mx-4 mb-4 w-[calc(100%-2rem)]",
        )

    def _live_active_review(self, *, hidden: bool) -> Any:
        """Client-side review rows sharing the draft order-review component."""
        rows: list[Any] = []
        for index, (_group, target, stop) in enumerate(
            self._active_oca_pairs(), start=1
        ):
            pending = self._pending_active_prices.get(target.perm_id)
            target_price = (
                pending[0]
                if pending is not None and pending[0] is not None
                else target.limit_price
            )
            stop_price = (
                pending[2]
                if pending is not None and pending[2] is not None
                else stop.stop_price
            )
            rows.append(
                self._review_oca_pair(
                    index=index,
                    quantity=str(target.remaining),
                    tif=target.tif,
                    lines=(
                        self._review_order_line(
                            "UPDATE SELL LMT",
                            _price_transition(
                                target_price,
                                None,
                                tone="text-emerald-400",
                                current_attributes={
                                    "data_active_review_target": target.perm_id
                                },
                            ),
                            "text-emerald-400",
                            row_attributes={
                                "data_active_review_target_row": target.perm_id
                            },
                            hidden=True,
                        ),
                        self._review_order_line(
                            "UPDATE SELL STP",
                            _price_transition(
                                stop_price,
                                None,
                                tone="text-rose-400",
                                current_attributes={
                                    "data_active_review_stop": target.perm_id
                                },
                            ),
                            "text-rose-400",
                            row_attributes={
                                "data_active_review_stop_row": target.perm_id
                            },
                            hidden=True,
                        ),
                    ),
                    row_attributes={"data_active_review_row": target.perm_id},
                    hidden=True,
                )
            )
        return Div(
            P(
                "Modify an active LMT or STP price to continue.",
                data_active_review_empty=True,
                cls="text-center text-sm leading-6 text-muted-foreground",
            ),
            *rows,
            data_active_review=True,
            cls="hidden min-h-0" if hidden else "min-h-0",
        )

    def _review_pair(self, index: int, layer: DraftLayerForm) -> Any:
        return self._review_oca_pair(
            index=index,
            quantity=layer.quantity,
            tif=layer.tif,
            quantity_attributes={"data_live_review_quantity": index},
            lines=(
                self._review_order_line(
                    "SELL LMT",
                    Span(
                        f"${layer.target_price}",
                        data_live_review_price=f"target-{index}",
                        aria_live="polite",
                        cls="text-sm font-semibold text-emerald-400",
                    ),
                    "text-emerald-400",
                ),
                self._review_order_line(
                    "SELL STP",
                    Span(
                        f"${layer.stop_price}",
                        data_live_review_price=f"stop-{index}",
                        aria_live="polite",
                        cls="text-sm font-semibold text-rose-400",
                    ),
                    "text-rose-400",
                ),
            ),
        )

    def _review_price_update(self, index: int, update: PriceUpdateCandidate) -> Any:
        """Display only the selected legs whose price will actually change."""
        rows: list[Any] = []
        if update.target_price is not None:
            rows.append(
                self._review_order_line(
                    "UPDATE SELL LMT",
                    _price_transition(
                        update.prior_target_price,
                        update.target_price,
                        tone="text-emerald-400",
                    ),
                    "text-emerald-400",
                )
            )
        if update.stop_price is not None:
            rows.append(
                self._review_order_line(
                    "UPDATE SELL STP",
                    _price_transition(
                        update.prior_stop_price,
                        update.stop_price,
                        tone="text-rose-400",
                    ),
                    "text-rose-400",
                )
            )
        return self._review_oca_pair(
            index=index,
            quantity=str(update.layer.quantity),
            tif=update.layer.tif,
            lines=tuple(rows),
        )

    def _review_oca_pair(
        self,
        *,
        index: int,
        quantity: str,
        tif: str,
        lines: tuple[Any, ...],
        quantity_attributes: dict[str, Any] | None = None,
        row_attributes: dict[str, Any] | None = None,
        hidden: bool = False,
    ) -> Any:
        """Shared order-review structure for new drafts and active amendments."""
        row_cls = "border-b border-border py-4"
        if hidden:
            row_cls = f"hidden {row_cls}"
        return Div(
            Div(
                P(f"OCA-{index}", cls="text-xs font-semibold"),
                P(
                    f"{quantity} contracts · {tif}",
                    cls="mt-0.5 text-xs text-muted-foreground",
                    **(quantity_attributes or {}),
                ),
            ),
            Div(*lines, cls="mt-3 space-y-3 border-l-2 border-border pl-3"),
            cls=row_cls,
            **(row_attributes or {}),
        )

    def _review_order_line(
        self,
        label: str,
        value: Any,
        tone: str,
        *,
        row_attributes: dict[str, Any] | None = None,
        hidden: bool = False,
    ) -> Any:
        """One linked LMT or STP line in the common OCA action-review component."""
        row_cls = f"flex items-center justify-between gap-3 {tone}"
        if hidden:
            row_cls = f"hidden {row_cls}"
        return Div(
            Span(label, cls=f"text-xs font-semibold {tone}"),
            value,
            cls=row_cls,
            **(row_attributes or {}),
        )

    def _review_market_exit_plan(
        self, candidates: tuple[MarketExitCandidate, ...]
    ) -> list[Any]:
        """Show the actual bulk-exit sequence once, without duplicating its MKT leg."""
        rows: list[Any] = []
        for index, candidate in enumerate(candidates, start=1):
            rows.append(
                self._review_oca_pair(
                    index=index,
                    quantity=format(candidate.quantity, "f"),
                    tif=candidate.tif,
                    lines=(
                        Div(
                            Span(
                                "CANCEL BRACKET",
                                cls="block text-xs font-semibold text-amber-300",
                            ),
                            Span(
                                candidate.oca_group,
                                cls="mt-1 block break-all text-xs font-semibold text-amber-300",
                            ),
                        ),
                    ),
                )
            )
        total = sum((candidate.quantity for candidate in candidates), Decimal("0"))
        rows.append(
            Div(
                P(
                    "Create MKT sell order",
                    cls="text-xs font-semibold",
                ),
                P(
                    f"{format(total, 'f')} contracts",
                    cls="mt-0.5 text-xs text-muted-foreground",
                ),
                Div(
                    Div(
                        Span(
                            "SELL MKT",
                            cls="block text-xs font-semibold text-emerald-400",
                        ),
                        Span(
                            f"{format(total, 'f')} contracts",
                            cls="mt-1 block text-xs font-semibold text-emerald-400",
                        ),
                    ),
                    cls="mt-3 border-l-2 border-border pl-3",
                ),
                cls="border-b border-border py-4",
            )
        )
        return rows

    def _review_cancellation_plan(self, candidate: MarketExitCandidate) -> Any:
        """Show the complete pair that will be removed, with no replacement leg."""
        return self._review_oca_pair(
            index=1,
            quantity=format(candidate.quantity, "f"),
            tif=candidate.tif,
            lines=(
                Div(
                    Span(
                        "CANCEL BRACKET",
                        cls="block text-xs font-semibold text-amber-300",
                    ),
                    Span(
                        candidate.oca_group,
                        cls="mt-1 block break-all text-xs font-semibold text-amber-300",
                    ),
                ),
            ),
        )


def _live_draft_script(configuration: dict[str, Any] | None) -> str:
    """Calculate a local, illustrative draft without weakening server validation."""
    if configuration is None:
        return ""
    payload = json.dumps(configuration, separators=(",", ":"))
    return f"""
(() => {{
  const config = {payload};
  const start = () => {{
    const form = document.getElementById('draft-form');
    if (!form) return;
    const basis = Number(config.basis), multiplier = Number(config.multiplier);
    const bands = config.bands.map((band) => ({{ low: Number(band.low), increment: Number(band.increment) }}));
    const value = (name, index) => Number(form.elements[`${{name}}_${{index}}`]?.value);
    const assigned = (selector, text) => document.querySelectorAll(selector).forEach((node) => {{ node.textContent = text; }});
    const priceText = (number) => `$${{Number(number.toFixed(6)).toString()}}`;
    const money = (number) => `${{number >= 0 ? '+' : '-'}}$${{Math.abs(number).toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})}}`;
    const roundUp = (number) => {{
      let candidate = number;
      for (let attempt = 0; attempt <= bands.length; attempt += 1) {{
        const candidates = bands.filter((item) => item.low <= candidate + 1e-9);
        const band = candidates[candidates.length - 1];
        if (!band) return NaN;
        const rounded = Math.ceil(number / band.increment - 1e-9) * band.increment;
        const roundedCandidates = bands.filter((item) => item.low <= rounded + 1e-9);
        const roundedBand = roundedCandidates[roundedCandidates.length - 1];
        if (roundedBand === band) return rounded;
        candidate = rounded;
      }}
      return NaN;
    }};
    const update = () => {{
      const outcomes = [];
      let allocated = 0;
      form.querySelectorAll('[data-live-input="target"]').forEach((input) => {{
        const index = input.dataset.liveLayer;
        const target = value('target', index), stop = value('stop', index);
        const quantity = Math.trunc(value('quantity', index));
        allocated += Number.isInteger(quantity) && quantity > 0 ? quantity : 0;
        const valid = Number.isFinite(target) && target > 0 && Number.isFinite(stop) && stop > 0 && stop <= 100 && Number.isInteger(quantity) && quantity > 0;
        const targetPrice = valid ? roundUp(basis * (1 + target / 100)) : NaN;
        const stopPrice = valid ? roundUp(basis * (1 - stop / 100)) : NaN;
        const gain = valid && Number.isFinite(targetPrice) ? (targetPrice - basis) * multiplier * quantity : NaN;
        const loss = valid && Number.isFinite(stopPrice) ? (stopPrice - basis) * multiplier * quantity : NaN;
        assigned(`[data-live-price="target-${{index}}"], [data-live-review-price="target-${{index}}"]`, Number.isFinite(targetPrice) ? priceText(targetPrice) : '—');
        assigned(`[data-live-price="stop-${{index}}"], [data-live-review-price="stop-${{index}}"]`, Number.isFinite(stopPrice) ? priceText(stopPrice) : '—');
        assigned(`[data-live-outcome="target-${{index}}"]`, Number.isFinite(gain) ? `${{money(gain)}} gain` : '— gain');
        assigned(`[data-live-outcome="stop-${{index}}"]`, Number.isFinite(loss) ? `${{money(loss)}} max loss` : '— max loss');
        assigned(`[data-live-review-quantity="${{index}}"]`, `${{Number.isInteger(quantity) && quantity > 0 ? quantity : '—'}} contracts · ${{form.elements[`tif_${{index}}`]?.value || 'GTC'}}`);
        if (Number.isFinite(gain) && Number.isFinite(loss)) outcomes.push({{ gain, loss }});
      }});
      const gain = outcomes.reduce((total, outcome) => total + outcome.gain, 0);
      const loss = outcomes.reduce((total, outcome) => total + outcome.loss, 0);
      assigned('[data-live-metric="gain"]', money(gain));
      assigned('[data-live-metric="loss"]', money(loss));
      let remainingStops = outcomes.reduce((total, outcome) => total + outcome.loss, 0), secured = 0, breakeven = '—';
      outcomes.slice(0, -1).some((outcome, index) => {{ secured += outcome.gain; remainingStops -= outcome.loss; const floor = secured + remainingStops; if (floor >= 0) {{ breakeven = `Layer ${{index + 1}} (${{money(floor)}})`; return true; }} return false; }});
      assigned('[data-live-metric="breakeven"]', breakeven);
      assigned('[data-live-allocation]', `${{allocated}} of ${{config.available}} contracts allocated`);
    }};
    form.querySelectorAll('[data-live-input]').forEach((input) => input.addEventListener('input', update));
    form.querySelectorAll('[data-live-input]').forEach((input) => input.addEventListener('change', update));
    update();
  }};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {{ once: true }});
  else start();
}})();
"""


def _live_active_script(configuration: dict[str, Any] | None) -> str:
    """Use the same local pricing display as draft layers; the server remains authoritative."""
    if configuration is None:
        return ""
    payload = json.dumps(configuration, separators=(",", ":"))
    return f"""
(() => {{
  const config = {payload};
  const start = () => {{
    const form = document.getElementById('active-form');
    if (!form) return;
    const basis = Number(config.basis), multiplier = Number(config.multiplier);
    const bands = config.bands.map((band) => ({{ low: Number(band.low), increment: Number(band.increment) }}));
    const assigned = (selector, text) => document.querySelectorAll(selector).forEach((node) => {{ node.textContent = text; }});
    const priceText = (number) => `$${{Number(number.toFixed(6)).toString()}}`;
    const money = (number) => `${{number >= 0 ? '+' : '-'}}$${{Math.abs(number).toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})}}`;
    const roundUp = (number) => {{
      let candidate = number;
      for (let attempt = 0; attempt <= bands.length; attempt += 1) {{
        const candidates = bands.filter((item) => item.low <= candidate + 1e-9);
        const band = candidates[candidates.length - 1];
        if (!band) return NaN;
        const rounded = Math.ceil(number / band.increment - 1e-9) * band.increment;
        const roundedCandidates = bands.filter((item) => item.low <= rounded + 1e-9);
        if (roundedCandidates[roundedCandidates.length - 1] === band) return rounded;
        candidate = rounded;
      }}
      return NaN;
    }};
    const setHidden = (node, hidden, display = 'flex') => {{
      if (!node) return;
      node.classList.toggle('hidden', hidden);
      node.classList.toggle(display, !hidden);
    }};
    const setReviewMode = (active) => {{
      document.querySelectorAll('[data-draft-review], [data-draft-review-badge], [data-draft-execute], [data-draft-outcome]').forEach((node) => {{
        node.classList.toggle('hidden', active);
      }});
      document.querySelectorAll('[data-active-review], [data-active-review-badge], [data-active-execute-control]').forEach((node) => {{
        node.classList.toggle('hidden', !active);
      }});
    }};
    const update = () => {{
      let changed = false;
      form.querySelectorAll('[data-active-input="target"]').forEach((targetInput) => {{
        const permId = targetInput.dataset.activePermId;
        const stopInput = form.querySelector(`[data-active-input="stop"][data-active-perm-id="${{permId}}"]`);
        const index = targetInput.dataset.liveLayer;
        const target = Number(targetInput.value), stop = Number(stopInput?.value);
        const quantity = Number(form.querySelector(`[data-active-quantity="${{permId}}"]`)?.value);
        const targetPrice = Number.isFinite(target) && target > 0 ? roundUp(basis * (1 + target / 100)) : NaN;
        const stopPrice = Number.isFinite(stop) && stop >= 0 && stop <= 100 ? roundUp(basis * (1 - stop / 100)) : NaN;
        const gain = Number.isFinite(targetPrice) && Number.isFinite(quantity) ? (targetPrice - basis) * multiplier * quantity : NaN;
        const loss = Number.isFinite(stopPrice) && Number.isFinite(quantity) ? (stopPrice - basis) * multiplier * quantity : NaN;
        assigned(`[data-live-price="active-target-${{index}}"]`, Number.isFinite(targetPrice) ? priceText(targetPrice) : '—');
        assigned(`[data-live-price="active-stop-${{index}}"]`, Number.isFinite(stopPrice) ? priceText(stopPrice) : '—');
        assigned(`[data-live-outcome="active-target-${{index}}"]`, Number.isFinite(gain) ? `${{money(gain)}} gain` : '— gain');
        assigned(`[data-live-outcome="active-stop-${{index}}"]`, Number.isFinite(loss) ? `${{money(loss)}} max loss` : '— max loss');
        const originalTarget = Number(targetInput.dataset.activeOriginal);
        const originalStop = Number(stopInput?.dataset.activeOriginal);
        const targetEdited = targetInput.value.trim() !== (targetInput.dataset.activeInitial || '').trim();
        const stopEdited = stopInput?.value.trim() !== (stopInput?.dataset.activeInitial || '').trim();
        const targetChanged = targetEdited && Number.isFinite(targetPrice) && Math.abs(targetPrice - originalTarget) > 1e-8;
        const stopChanged = stopEdited && Number.isFinite(stopPrice) && Math.abs(stopPrice - originalStop) > 1e-8;
        const row = document.querySelector(`[data-active-review-row="${{permId}}"]`);
        const targetRow = document.querySelector(`[data-active-review-target-row="${{permId}}"]`);
        const stopRow = document.querySelector(`[data-active-review-stop-row="${{permId}}"]`);
        const targetText = document.querySelector(`[data-active-review-target="${{permId}}"]`);
        const stopText = document.querySelector(`[data-active-review-stop="${{permId}}"]`);
        if (targetText) targetText.textContent = targetChanged ? priceText(targetPrice) : '';
        if (stopText) stopText.textContent = stopChanged ? priceText(stopPrice) : '';
        setHidden(targetRow, !targetChanged);
        setHidden(stopRow, !stopChanged);
        setHidden(row, !(targetChanged || stopChanged), 'block');
        changed ||= targetChanged || stopChanged;
      }});
      const empty = document.querySelector('[data-active-review-empty]');
      if (empty) empty.classList.toggle('hidden', changed);
      document.querySelectorAll('[data-active-execute]').forEach((button) => {{ button.disabled = !changed; }});
      setReviewMode(changed);
    }};
    form.querySelectorAll('[data-active-input]').forEach((input) => input.addEventListener('input', update));
    form.querySelectorAll('[data-active-input]').forEach((input) => input.addEventListener('change', update));
    form.querySelectorAll('[data-move-stops-to-be]').forEach((button) => button.addEventListener('click', () => {{
      form.querySelectorAll('[data-active-input="stop"]').forEach((input) => {{ input.value = '0'; }});
      update();
    }}));
    update();
  }};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {{ once: true }});
  else start();
}})();
"""


def _field(
    label: str,
    control: Any,
    *,
    input_id: str | None = None,
    suffix: str | None = None,
) -> Any:
    return Div(
        Label(label, fr=input_id, cls="text-xs font-medium text-muted-foreground"),
        Div(
            control,
            Span(suffix, cls="shrink-0 font-mono text-xs text-muted-foreground")
            if suffix
            else None,
            cls="mt-1 flex items-center gap-2",
        ),
        cls="min-w-0 space-y-0.5",
    )


def _layer_row_layout(
    *,
    index: int,
    target_field: Any,
    stop_field: Any,
    quantity_field: Any,
    tif_field: Any,
    action_field: Any,
) -> Any:
    """Keep draft and active OCA rows structurally identical."""
    return Div(
        Div(
            Span(f"LAYER {index}", cls="text-xs font-semibold"),
            P(f"OCA-{index}", cls="mt-2 text-xs text-muted-foreground"),
            cls="min-w-20",
        ),
        target_field,
        stop_field,
        quantity_field,
        tif_field,
        action_field,
        # This is deliberately a shared, bundled grid utility: arbitrary
        # Tailwind values are not present in StarUI's precompiled stylesheet.
        cls="grid grid-cols-[5rem_minmax(10rem,1fr)_minmax(10rem,1fr)_minmax(5rem,0.6fr)_5rem_2.25rem] items-start gap-3 border-t border-border py-4 first:border-t-0",
    )


def _percentage_price_field(
    label: str,
    control: Any,
    *,
    input_id: str,
    price: str,
    outcome: str,
    outcome_label: str,
    tone: str,
    layer_index: int,
    kind: str,
) -> Any:
    """Render a percentage input with its calculated price and layer outcome."""
    return Div(
        Div(
            Label(label, fr=input_id, cls="text-xs font-medium text-muted-foreground"),
            Span(
                f"${price}",
                data_live_price=f"{kind}-{layer_index}",
                aria_live="polite",
                cls="text-xs font-semibold text-foreground",
            ),
            cls="flex items-center justify-between gap-2",
        ),
        Div(
            control,
            Span(
                "%",
                cls="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-sm text-muted-foreground",
            ),
            cls="relative mt-1",
        ),
        Div(
            Span(
                f"{outcome} {outcome_label}",
                data_live_outcome=f"{kind}-{layer_index}",
                aria_live="polite",
                cls=f"text-xs {tone}",
            ),
            cls="mt-1 flex justify-end",
        ),
        cls="min-w-0 space-y-0.5",
    )


def _active_order_value(label: str, price: Decimal | None, tone: str) -> Any:
    return Div(
        Span(label, cls="text-xs text-muted-foreground"),
        Span(
            "—" if price is None else f"${format(price, 'f')}",
            cls=f"mt-1 text-sm font-semibold {tone}",
        ),
        cls="min-w-28",
    )


def _price_transition(
    previous: Decimal | None,
    current: Decimal | None,
    *,
    tone: str,
    current_attributes: dict[str, Any] | None = None,
) -> Any:
    """Render an inspectable old-to-new price transition with a Lucide arrow."""
    return Span(
        Span(
            "—" if previous is None else f"${format(previous, 'f')}",
            cls="text-muted-foreground",
        ),
        Span("to", cls="sr-only"),
        Icon(
            "lucide:arrow-right",
            aria_hidden="true",
            cls="size-3.5 shrink-0 text-muted-foreground",
        ),
        Span(
            "" if current is None else f"${format(current, 'f')}",
            cls=tone,
            **(current_attributes or {}),
        ),
        aria_live="polite",
        cls="inline-flex items-center gap-1.5 font-mono text-sm font-semibold tabular-nums",
    )


def _price_percentage(
    price: Decimal | None,
    basis: Decimal | None,
    *,
    target: bool,
) -> str:
    if price is None or basis is None or basis <= 0:
        return "—"
    percentage = ((price / basis) - 1) * Decimal("100")
    if not target:
        percentage = -percentage
    return format(percentage.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), "f")


def _active_percentage_for_price(
    price: Decimal | None,
    basis: Decimal | None,
    *,
    target: bool,
    bands: tuple[Any, ...],
    presets: tuple[Decimal, ...],
) -> str:
    """Recover a configured percentage when a broker price was tick-rounded.

    The live order contains the rounded price only. Prefer a configured preset
    that produces that exact price, so an untouched 20% plan remains 20% rather
    than being displayed as its rounded-price inverse (for example, 20.3%).
    A non-preset/manual order falls back to the observable derived percentage.
    """
    if price is None or basis is None or basis <= 0:
        return "—"
    for percentage in presets:
        factor = Decimal("1") + percentage / Decimal("100")
        if not target:
            factor = Decimal("1") - percentage / Decimal("100")
        try:
            candidate = round_up_price(basis * factor, bands)
        except ValueError:
            break
        if candidate == price:
            return format(percentage, "f")
    return _price_percentage(price, basis, target=target)


def _price_text(price: Decimal | None) -> str:
    return "—" if price is None else format(price, "f")


def _position_identity(local_symbol: str) -> tuple[str, str]:
    """Render IBKR's OCC-style local symbol as a compact inventory label."""
    parts = local_symbol.split(maxsplit=1)
    symbol = parts[0] if parts else local_symbol
    contract = parts[1] if len(parts) > 1 else ""
    if (
        len(contract) != 15
        or not contract[:6].isdigit()
        or contract[6] not in {"C", "P"}
        or not contract[7:].isdigit()
    ):
        return symbol, contract or "Option contract"
    try:
        expiry = datetime.strptime(contract[:6], "%y%m%d")
    except ValueError:
        return symbol, contract
    strike = Decimal(contract[7:]) / Decimal("1000")
    right = "CALL" if contract[6] == "C" else "PUT"
    return (
        symbol,
        f"{format(strike, 'f')} {right} · {expiry.strftime('%b').upper()} "
        f"{expiry.day} '{expiry.strftime('%y')}",
    )


def _register_bundled_icons() -> None:
    """Keep StarUI icons inside the application instead of loading a CDN."""
    try:
        raw = json.loads((_ASSETS_DIR / "lucide.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Bundled StarUI icon assets are unavailable.") from exc
    resolver.preload("lucide", raw=raw)


def _parse_presets(raw: str, *, maximum: Decimal) -> tuple[Decimal, ...] | None:
    values: list[Decimal] = []
    for value in raw.split(","):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
        if not number.is_finite() or not Decimal("0") < number <= maximum:
            return None
        values.append(number)
    return tuple(values) if values else None


def _split_quantity(total: int, count: int) -> tuple[int, ...]:
    if total <= 0 or count <= 0:
        return ()
    each, remainder = divmod(total, count)
    return tuple(each + int(index < remainder) for index in range(count))


def _positive_int(value: str | None, default: int) -> int:
    try:
        number = int(value or "")
    except ValueError:
        return default
    return number if number > 0 else default


def _positive_float(value: str | None, default: float) -> float:
    try:
        number = float(value or "")
    except ValueError:
        return default
    return number if number > 0 else default


def _decimal_value(value: str | None) -> Decimal | None:
    try:
        number = Decimal(value or "")
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _int_or_zero(value: str) -> int:
    try:
        return max(0, int(value))
    except ValueError:
        return 0


def _money(value: Decimal) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _toast_notice(message: str) -> _ToastNotice:
    """Turn internal status detail into a short, actionable user notification."""
    normalized = " ".join(message.split())
    lowered = normalized.lower()
    if "portfolio state is not ready" in lowered or "tws connection failed" in lowered:
        return _ToastNotice(
            title="Could not connect to TWS",
            description="Make sure TWS is open and try again.",
            variant="error",
        )
    if any(
        term in lowered
        for term in (
            "blocked",
            "failed",
            "unavailable",
            "unknown",
            "disabled",
            "invalid",
            "not in the verified",
        )
    ):
        variant = "error"
    elif any(
        term in lowered
        for term in (
            "acknowledged",
            "confirmed",
            "reconciled",
            "recovered",
            "cancelled",
            "verified",
        )
    ):
        variant = "success"
    else:
        variant = "info"
    title, separator, detail = normalized.partition(":")
    if not separator:
        title, detail = normalized, ""
    if len(title) > 52:
        title, detail = title[:49].rstrip() + "…", ""
    if len(detail) > 150:
        detail = detail[:147].rstrip() + "…"
    return _ToastNotice(title=title, description=detail.strip(), variant=variant)


def _busy_submit_script() -> str:
    """Mark explicitly asynchronous server actions busy during navigation."""
    return """
    (() => {
      if (window.__ibkrBusySubmitInstalled) return;
      window.__ibkrBusySubmitInstalled = true;
      document.addEventListener('submit', (event) => {
        const button = event.submitter;
        if (!(button instanceof HTMLButtonElement) || button.disabled) return;
        // Local form actions (add/remove/split a layer) are immediate state
        // edits.  Only controls that opt in with data-busy-text should change
        // appearance or become disabled while a TWS/network request runs.
        const text = button.dataset.busyText;
      if (!text) return;
        const form = event.target;
        if (form instanceof HTMLFormElement && form.dataset.ibkrSubmitting === 'true') {
          event.preventDefault();
          return;
        }
        if (form instanceof HTMLFormElement) form.dataset.ibkrSubmitting = 'true';
        // Do not set `disabled` during the submit event.  Qt WebEngine can
        // then abandon the form's default navigation, leaving a spinner on a
        // page that never receives the server response.  `aria-disabled` and
        // pointer-events preserve the visible lock without changing submitter
        // semantics.
        button.setAttribute('aria-disabled', 'true');
        button.setAttribute('aria-busy', 'true');
        button.style.pointerEvents = 'none';
        button.replaceChildren(
          Object.assign(document.createElement('span'), {
            className: 'size-3.5 animate-spin rounded-full border-2 border-current border-r-transparent',
            'aria-hidden': 'true',
          }),
          document.createTextNode(text),
        );
      }, true);
    })();
    """


def _metric(label: str, value: str, tone: str, *, live_key: str | None = None) -> Any:
    return (
        P(label, cls="min-w-0 text-xs font-medium text-muted-foreground"),
        P(
            value,
            data_live_metric=live_key,
            aria_live="polite" if live_key else None,
            cls=f"min-w-0 break-words text-right font-mono text-xs font-semibold tabular-nums {tone}",
        ),
    )


def _breakeven(outcomes: list[tuple[Decimal, Decimal]]) -> str:
    remaining_stops = sum((stop for _, stop in outcomes), Decimal("0"))
    secured = Decimal("0")
    for index, (target, stop) in enumerate(outcomes[:-1], start=1):
        secured += target
        remaining_stops -= stop
        floor = secured + remaining_stops
        if floor >= 0:
            return f"Layer {index} ({_money(floor)})"
    return "—"
