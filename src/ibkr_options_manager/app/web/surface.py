from __future__ import annotations

# ruff: noqa: E501
import asyncio
import json
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from secrets import token_urlsafe
from threading import Event, RLock, Thread
from time import monotonic
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
    to_xml,
)
from starhtml import (
    Input as HTMLInput,
)
from starhtml.datastar import evt
from starhtml.icons import resolver
from starhtml.plugins import Plugin
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse

from ...domain import (
    BrokerSnapshot,
    VerifiedOptionContract,
    preview_reference_prices,
    round_up_price,
)
from ...domain.outcome import ExitScenario, PositionOutcome, project_position_outcome
from ...execution import (
    ExecutionBlocked,
    ExecutionOutcomeUnknown,
    JournalEntry,
    LayerOutcome,
    MarketExitCandidate,
    PaperExecutionService,
    PriceUpdateCandidate,
    ambiguous_oca_prefixes,
    classify_journal_layer,
)
from ...observation import ObservationSettings, PositionObserver
from ...price_update_trace import record_price_update_event
from ..view_model import (
    ConnectionSettings,
    DraftLayerForm,
    FactState,
    PaperExecutionCandidate,
    PlanForm,
    PlannerViewModel,
    PortfolioPositionLine,
    UiStatus,
    ViewState,
)
from .components.ui.alert import Alert, AlertDescription, AlertTitle
from .components.ui.badge import Badge
from .components.ui.button import Button
from .components.ui.card import (
    Card,
    CardContent,
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
from .components.ui.dropdown_menu import (
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuItem,
    DropdownMenuTrigger,
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
from .components.ui.tooltip import Tooltip, TooltipContent, TooltipTrigger

_STATIC_DIR = Path(__file__).with_name("static")
_ASSETS_DIR = Path(__file__).with_name("assets")
# StarHTML 0.7.0's position.js imports Floating UI from a CDN. Keep the same
# plugin and API, but serve its dependency locally inside the loopback webview.
_LOCAL_POSITION_PLUGIN = Plugin(  # type: ignore[no-untyped-call]
    "position",
    signals=("x", "y", "placement", "visible", "is_positioning"),
    critical_css=(
        "[data-positioning=true]:not([popover]){visibility:hidden!important;opacity:0!important}"
        "[data-positioning=false]:not([popover]){visibility:visible!important;opacity:1!important;transition:opacity 150ms ease-out}"
    ),
    static_path=_STATIC_DIR,
    package_name="ibkr_options_manager",
)


@dataclass(frozen=True, slots=True)
class _ToastNotice:
    title: str
    description: str
    variant: str


@dataclass(frozen=True, slots=True)
class _PriceUpdateImpact:
    title: str
    details: tuple[str, ...]
    concerns: frozenset[tuple[int, str]]


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
        observe_positions: bool = False,
        observer_client_id: int = 18,
    ) -> None:
        _register_bundled_icons()
        self._view_model = view_model
        self._demo_mode = demo_mode
        self._paper_execution = paper_execution
        self._observe_positions = observe_positions and not demo_mode
        self._observer_client_id = observer_client_id
        self._observer: PositionObserver | None = None
        self._observer_generation = 0
        self._observer_signal = Event()
        self._observer_health = "idle"
        self._queued_observer_health: str | None = None
        self._pending_observation = False
        self._observation_thread: Thread | None = None
        self._closed = False
        self._inventory_revision = 0
        self._new_position_ids: set[int] = set()
        self._observation_requires_reload = False
        self._selected_quantity_change: tuple[int, int, int] | None = None
        self._verified_position_ids: set[int] = set()
        self._verified_position_quantities: dict[int, int] = {}
        self._verified_position_account: str | None = None
        self._observer_retry_at = 0.0
        self._armed_execution: PaperExecutionCandidate | None = None
        self._armed_execution_deadline: float | None = None
        self._recovery_requested_fingerprint: str | None = None
        self._armed_market_exit: MarketExitCandidate | None = None
        self._armed_market_exits: tuple[MarketExitCandidate, ...] = ()
        self._armed_cancellation: MarketExitCandidate | None = None
        self._armed_cancellations: tuple[MarketExitCandidate, ...] = ()
        self._active_action_verified = False
        self._review_all_active_exits = False
        self._armed_price_updates: tuple[PriceUpdateCandidate, ...] = ()
        self._warned_price_update_concerns: frozenset[tuple[int, str]] = frozenset()
        self._price_update_retry_required = False
        self._armed_active_percentages: dict[int, tuple[str, str]] = {}
        # TWS can acknowledge a price amendment before its next open-order
        # snapshot reflects it. Retain only that acknowledged presentation
        # value until the broker snapshot catches up; execution still always
        # re-verifies the broker snapshot rather than trusting this display.
        self._pending_active_prices: dict[
            int, tuple[Decimal | None, int, Decimal | None]
        ] = {}
        self._preferred_con_id = initial_con_id
        self._selected_con_id: int | None = None
        self._selected_closed_con_id: int | None = None
        self._session_position_account = initial_account
        self._session_seen_positions: dict[int, PortfolioPositionLine] = {}
        self._session_closed_positions: dict[int, PortfolioPositionLine] = {}
        self._state = view_model.empty()
        self._drafts: dict[int, tuple[DraftLayerForm, ...]] = {}
        self._projection_comparison: PositionOutcome | None = None
        self._settings = ConnectionSettings(account=initial_account)
        self._target_presets = "20, 40, 60, 100"
        self._stop_presets = "25"
        self._last_refreshed_at = "—"
        self._notifications_enabled = False
        self._suppress_toasts = False
        self._toast: _ToastNotice | None = None
        self._toast_revision = 0
        self._toast_rendered_revision = 0
        self._status_message = ""
        self._message = "Refresh and select a position to build a draft."
        self._launch_connection = "idle"
        self._launch_refresh_in_progress = False
        self._submission_review_required = False
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
            hdrs=(
                Link(rel="stylesheet", href="/starui.css"),
                Link(rel="stylesheet", href="/layers.css"),
            ),
            htmlkw={"lang": "en", "data_theme": "dark"},
            bodykw={"cls": "min-h-screen bg-background text-foreground"},
        )
        self.app.register(_LOCAL_POSITION_PLUGIN)
        route(f"/{self.session_token}/")(self._home)
        route(f"/{self.session_token}/connection-status")(self._connection_status)
        route(f"/{self.session_token}/inventory-events")(self._inventory_events)
        route(f"/{self.session_token}/inventory-fragment")(self._inventory_fragment)
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
            if self._toast is not None:
                self._toast_revision += 1

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
            if state.status is UiStatus.READY:
                self._start_observer_locked()

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

    def close(self) -> None:
        with self._lock:
            self._closed = True
            observer = self._observer
            self._observer = None
            self._observer_signal.set()
        if observer is not None:
            observer.stop()
        if self._observation_thread is not None:
            self._observation_thread.join(timeout=1)

    async def _inventory_events(self, request: Request) -> StreamingResponse:
        async def stream() -> Any:
            while not self._closed and not await request.is_disconnected():
                with self._lock:
                    payload = json.dumps(
                        {
                            "revision": self._inventory_revision,
                            "observer": self._observer_health,
                        }
                    )
                # A quiet position subscription still needs a visible liveness
                # signal. The browser treats missed heartbeats as a lost stream.
                yield f"data: {payload}\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    def _inventory_fragment(self) -> HTMLResponse:
        with self._lock:
            change = self._selected_quantity_change
            return HTMLResponse(
                to_xml(self._inventory()),
                headers={
                    "Cache-Control": "no-store",
                    "X-Inventory-Revision": str(self._inventory_revision),
                    "X-Selected-Changed": "1" if self._observation_requires_reload else "0",
                    "X-Selected-Quantity-Change": str(change[2] - change[1]) if change else "0",
                },
            )

    def _start_observer_locked(self) -> None:
        if not self._observe_positions or self._closed:
            return
        try:
            settings = ObservationSettings(
                account=self._settings.account,
                port=self._settings.port,
                client_id=self._observer_client_id,
                capture_client_id=self._settings.client_id,
                timeout_seconds=self._settings.timeout_seconds,
            )
        except ValueError:
            self._observer_health = "error"
            self._disarm_execution_locked()
            self._state = replace(
                self._state,
                status=UiStatus.STALE,
                can_preview=False,
                fingerprint=None,
            )
            self._inventory_revision += 1
            return
        if self._observer is None:
            self._observer = PositionObserver(self._position_hint, self._position_health)
        self._observer_generation = -1
        self._observer.start(
            settings,
            on_generation=lambda generation: setattr(
                self, "_observer_generation", generation
            ),
        )
        self._observer_health = "connecting"
        self._observer_retry_at = monotonic() + 5
        if self._observation_thread is None:
            self._observation_thread = Thread(target=self._observation_loop, name="ibkr-observation-reconcile", daemon=True)
            self._observation_thread.start()

    def _position_hint(self, generation: int) -> None:
        if generation != self._observer_generation or self._closed:
            return
        self._pending_observation = True
        self._observer_signal.set()

    def _position_health(self, generation: int, health: str) -> None:
        if generation != self._observer_generation or self._closed:
            return
        self._queued_observer_health = health
        self._observer_signal.set()

    def _observation_loop(self) -> None:
        while not self._closed:
            self._observer_signal.wait(1)
            self._observer_signal.clear()
            with self._lock:
                if self._closed:
                    return
                if self._queued_observer_health is not None:
                    self._observer_health = self._queued_observer_health
                    self._queued_observer_health = None
                    if self._observer_health != "connected":
                        self._disarm_execution_locked()
                        self._state = replace(
                            self._state,
                            status=UiStatus.STALE,
                            can_preview=False,
                            fingerprint=None,
                        )
                        self._observation_requires_reload = True
                        self._observer_retry_at = monotonic() + 5
                    self._inventory_revision += 1
                if (
                    self._observer_health in {"error", "disconnected"}
                    and monotonic() >= self._observer_retry_at
                ):
                    self._start_observer_locked()
                if not self._pending_observation:
                    continue
                self._pending_observation = False
                previous_selected = self._selected_con_id
                self._suppress_toasts = True
                try:
                    self._refresh_locked(auto_select=False, preserve_invalid_drafts=True)
                    if self._state.status in {UiStatus.READY, UiStatus.BLOCKED}:
                        self._observation_requires_reload = previous_selected != self._selected_con_id
                    else:
                        self._observation_requires_reload = True
                except Exception:
                    self._disarm_execution_locked()
                    self._state = replace(
                        self._state,
                        status=UiStatus.STALE,
                        can_preview=False,
                        fingerprint=None,
                    )
                    self._observation_requires_reload = True
                finally:
                    self._suppress_toasts = False
                    self._inventory_revision += 1

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
            # Each response carries only feedback produced by this action.
            self._toast = None
            trading_actions = {
                "market-exit-selected",
                "cancel-all-active",
                "cancel-all-confirm",
                "active-action-execute",
                "market-exit-confirm",
                "cancel-pair-confirm",
                "active-update-arm",
                "price-update-confirm",
                "execute-arm",
                "execute-confirm",
            }
            if (
                self._observe_positions
                and self._observer_health != "connected"
                and (
                    action in trading_actions
                    or action.startswith("market-exit-arm:")
                    or action.startswith("cancel-pair-arm:")
                )
            ):
                self._disarm_execution_locked()
                self._message = "TWS observation is unavailable. Refresh and wait for reconnection before reviewing an order."
                return self._page()
            draft_change = action in {
                "add-layer",
                "build-draft",
                "equal-split",
                "equal-split-available",
                "equal-split-assigned",
            } or action.startswith("remove-layer:")
            if draft_change:
                self._projection_comparison = self._projection_state()[1]
            else:
                self._projection_comparison = None
            if action == "refresh":
                self._selected_quantity_change = None
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
                self._start_observer_locked()
                self._submission_review_required = False
            elif action == "select":
                self._disarm_execution_locked()
                self._save_form_locked(values)
                self._selected_quantity_change = None
                self._select_locked(_positive_int(values.get("con_id"), 0))
            elif action == "select-session-closed":
                self._disarm_execution_locked()
                con_id = _positive_int(values.get("con_id"), 0)
                if con_id in self._session_closed_positions:
                    self._selected_closed_con_id = con_id
                    self._selected_con_id = None
                else:
                    self._message = "This contract is no longer in session history."
            elif action == "acknowledge-position-change":
                self._disarm_execution_locked()
                self._selected_quantity_change = None
            elif action.startswith("market-exit-arm:"):
                _, _, perm_id = action.partition(":")
                self._arm_market_exit_locked(_positive_int(perm_id, 0))
            elif action.startswith("cancel-pair-arm:"):
                _, _, perm_id = action.partition(":")
                self._arm_cancellation_locked(_positive_int(perm_id, 0))
            elif action == "market-exit-selected":
                self._arm_selected_market_exit_locked(values)
            elif action == "cancel-all-active":
                self._arm_all_cancellations_locked()
            elif action == "active-action-execute":
                self._execute_active_action_locked()
            elif action == "market-exit-confirm":
                self._confirm_market_exit_locked()
            elif action == "cancel-pair-confirm":
                self._confirm_cancellation_locked()
            elif action == "cancel-all-confirm":
                self._confirm_all_cancellations_locked()
            elif action == "resolve-cancelled-bracket":
                self._resolve_cancelled_bracket_locked(values)
            elif action.startswith("verify-cancelled-bracket:"):
                self._recovery_requested_fingerprint = action.partition(":")[2]
                if self._cancelled_bracket_recovery(self._submission_outcomes()) is None:
                    self._recovery_requested_fingerprint = None
                    self._message = (
                        "Cancellation verification blocked: refresh TWS and check "
                        "that both bracket legs are gone."
                    )
            elif action.startswith("dismiss-cancelled:"):
                self._dismiss_cancelled_layer_locked(action)
            elif action == "cancel-staged":
                self._disarm_execution_locked()
                self._set_review_status_locked(
                    "Staged action cancelled. No orders were sent to TWS."
                )
            elif action == "active-update-arm":
                self._arm_price_updates_locked(values)
            elif action == "price-update-confirm":
                self._confirm_price_updates_locked(values)
            else:
                if action not in {"execute-arm", "execute-confirm"}:
                    self._disarm_execution_locked()
                removing_index = (
                    _positive_int(action.partition(":")[2], 0)
                    if action.startswith("remove-layer:")
                    else None
                )
                if action == "build-draft" and self._current_layers():
                    self._message = "A draft already exists. Edit its layers or remove them first."
                    return self._page()
                if not self._save_form_locked(values, removing_index=removing_index):
                    return self._page()
                if action == "add-layer":
                    self._add_layer_locked()
                elif action == "build-draft":
                    self._build_draft_locked()
                elif action in {"equal-split", "equal-split-available"}:
                    self._equal_split_locked(use_available_quantity=True)
                elif action == "equal-split-assigned":
                    self._equal_split_locked(use_available_quantity=False)
                elif action == "execute-arm":
                    self._arm_execution_locked()
                elif action == "execute-confirm":
                    self._confirm_execution_locked()
            return self._page()

    def _refresh_locked(
        self, *, auto_select: bool = True, preserve_invalid_drafts: bool = False
    ) -> None:
        if self._session_position_account != self._settings.account:
            self._session_seen_positions.clear()
            self._session_closed_positions.clear()
            self._selected_closed_con_id = None
            self._session_position_account = self._settings.account
        self._projection_comparison = None
        self._recovery_requested_fingerprint = None
        self._disarm_execution_locked()
        state = self._view_model.refresh_portfolio(self._settings)
        self._apply_refreshed_portfolio_locked(
            state, auto_select=auto_select, preserve_invalid_drafts=preserve_invalid_drafts
        )

    def _refresh_after_acknowledged_write_locked(self, acknowledgement: str) -> bool:
        """Replace optimistic post-write UI state with a fresh broker snapshot."""
        try:
            self._refresh_locked()
        except Exception as error:  # keep a confirmed write, never hide it
            self._message = (
                f"{acknowledgement} Automatic TWS refresh failed; use Refresh before "
                f"another action. ({error})"
            )
            return False
        if self._state.status is UiStatus.READY:
            self._message = f"{acknowledgement} TWS state refreshed."
            return True
        else:
            self._message = (
                f"{acknowledgement} TWS refresh could not verify the new state; use "
                "Refresh before another action."
            )
            return False

    def _apply_refreshed_portfolio_locked(
        self,
        state: ViewState,
        *,
        auto_select: bool = True,
        preserve_invalid_drafts: bool = False,
    ) -> None:
        """Apply an already-read portfolio snapshot while holding the UI lock."""
        if preserve_invalid_drafts and state.status is UiStatus.READY:
            self._record_selected_quantity_change_locked(state)
        self._record_verified_positions_locked(state)
        previous_con_id = self._selected_con_id
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        if self._selected_closed_con_id in self._session_closed_positions:
            return
        self._selected_closed_con_id = None
        if previous_con_id in self._session_closed_positions:
            self._selected_closed_con_id = previous_con_id
            return
        target = self._preferred_con_id
        if target is None:
            target = previous_con_id
        available_con_ids = {position.con_id for position in state.positions}
        if target not in available_con_ids:
            target = state.positions[0].con_id if auto_select and state.positions else None
        self._preferred_con_id = None
        if target is None:
            self._selected_quantity_change = None
            return
        self._select_locked(target, preserve_invalid_draft=preserve_invalid_drafts)
        if self._selected_con_id != previous_con_id:
            self._selected_quantity_change = None

    def _record_selected_quantity_change_locked(self, current_state: ViewState) -> None:
        con_id = self._selected_con_id
        if (
            con_id is None
            or self._verified_position_account != current_state.account
            or con_id not in self._verified_position_quantities
        ):
            return
        after = next(
            (position for position in current_state.positions if position.con_id == con_id and position.eligible),
            None,
        )
        if after is None:
            return
        try:
            new_quantity = Decimal(after.quantity)
        except InvalidOperation:
            return
        if (
            not new_quantity.is_finite()
            or new_quantity <= 0
            or new_quantity != new_quantity.to_integral_value()
        ):
            return
        baseline = (
            self._selected_quantity_change[1]
            if self._selected_quantity_change is not None
            and self._selected_quantity_change[0] == con_id
            else self._verified_position_quantities[con_id]
        )
        self._selected_quantity_change = (
            (con_id, baseline, int(new_quantity)) if int(new_quantity) != baseline else None
        )

    def _record_verified_positions_locked(self, state: ViewState) -> None:
        if state.status is not UiStatus.READY:
            return
        if self._session_position_account != self._settings.account:
            self._session_seen_positions.clear()
            self._session_closed_positions.clear()
            self._selected_closed_con_id = None
            self._session_position_account = self._settings.account
        current = {position.con_id: position for position in state.positions}
        for con_id, position in self._session_seen_positions.items():
            if con_id not in current:
                self._session_closed_positions[con_id] = position
        for con_id, position in current.items():
            self._session_seen_positions[con_id] = position
            self._session_closed_positions.pop(con_id, None)
        quantities: dict[int, int] = {}
        for position in state.positions:
            if not position.eligible:
                continue
            try:
                quantity = Decimal(position.quantity)
            except InvalidOperation:
                continue
            if (
                quantity.is_finite()
                and quantity > 0
                and quantity == quantity.to_integral_value()
            ):
                quantities[position.con_id] = int(quantity)
        verified_ids = {
            position.con_id for position in state.positions if position.eligible
        }
        if self._verified_position_account == state.account:
            self._new_position_ids.update(
                verified_ids - self._verified_position_ids
            )
        else:
            self._new_position_ids.clear()
        self._new_position_ids.intersection_update(verified_ids)
        self._verified_position_ids = verified_ids
        self._verified_position_quantities = quantities
        self._verified_position_account = state.account

    def _select_locked(self, con_id: int, *, preserve_invalid_draft: bool = False) -> None:
        if con_id not in {position.con_id for position in self._state.positions}:
            self._message = "The selected contract is not in the verified portfolio."
            return
        self._selected_con_id = con_id
        self._selected_closed_con_id = None
        self._new_position_ids.discard(con_id)
        state = self._view_model.select_position(
            con_id, self._plan_form(self._drafts.get(con_id, ()))
        )
        if not preserve_invalid_draft and any(
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
        self._armed_execution_deadline = None
        self._armed_market_exit = None
        self._armed_market_exits = ()
        self._armed_cancellation = None
        self._armed_cancellations = ()
        self._active_action_verified = False
        self._review_all_active_exits = False
        self._armed_price_updates = ()
        self._warned_price_update_concerns = frozenset()
        self._price_update_retry_required = False
        self._armed_active_percentages = {}

    def _set_review_status_locked(self, message: str) -> None:
        """Keep routine review transitions out of the notification queue."""
        self._status_message = message
        self._toast = None

    def _show_success_toast_locked(self, title: str, description: str) -> None:
        self._toast_revision += 1
        self._toast = _ToastNotice(title, description, "success")

    def _arm_execution_locked(self) -> None:
        drafted = sum(_int_or_zero(layer.quantity) for layer in self._current_layers())
        if not 0 < drafted <= self._planning_available_quantity():
            self._message = "Draft quantity must be positive and within the verified available contracts."
            return
        if self._pending_submissions():
            self._message = "Review pending brackets in TWS and Refresh before submitting another draft."
            return
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
        self._armed_execution_deadline = monotonic() + 10
        self._set_review_status_locked(
            "Fresh paper snapshot verified. Review the order plan and confirm within 10 seconds."
        )

    def _confirm_execution_locked(self) -> None:
        if self._armed_execution is not None and (
            self._armed_execution_deadline is None
            or monotonic() >= self._armed_execution_deadline
        ):
            self._disarm_execution_locked()
            self._message = "Paper bracket confirmation expired. Press Execute again to review a fresh plan."
            return
        drafted = sum(_int_or_zero(layer.quantity) for layer in self._current_layers())
        if not 0 < drafted <= self._planning_available_quantity():
            self._disarm_execution_locked()
            self._message = "Draft quantity must be positive and within the verified available contracts."
            return
        if self._pending_submissions():
            self._disarm_execution_locked()
            self._message = "Review pending brackets in TWS and Refresh before submitting another draft."
            return
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
            self._toast = None
            self._submission_review_required = True
        except ExecutionBlocked as error:
            self._message = f"Execution blocked: {error}"
        except Exception as error:  # the isolated writer must never crash the UI
            self._drafts.pop(candidate.selection.con_id, None)
            self._message = f"Submission outcome is unknown: {error}"
        else:
            self._drafts.pop(candidate.selection.con_id, None)
            refreshed = self._refresh_after_acknowledged_write_locked(
                f"Paper submission acknowledged for {len(receipt.entry.order_ids)} orders."
            )
            self._submission_review_required = True
            if refreshed:
                self._toast = None
        finally:
            self._disarm_execution_locked()

    def _arm_market_exit_locked(self, target_perm_id: int) -> None:
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Paper order management is disabled for this launch."
            return
        self._disarm_execution_locked()
        snapshot = self._view_model.latest_snapshot()
        if snapshot is None:
            self._message = (
                "Market exit blocked: select a position before reviewing its orders."
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
        self._set_review_status_locked(
            f"Review the MKT exit for {candidate.quantity} contracts, then press "
            "Execute paper order to verify a fresh snapshot."
        )

    def _arm_cancellation_locked(self, target_perm_id: int) -> None:
        """Stage deletion of one complete, journal-proven OCA pair only."""
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Paper order management is disabled for this launch."
            return
        self._disarm_execution_locked()
        snapshot = self._view_model.latest_snapshot()
        if snapshot is None:
            self._message = "Bracket cancellation blocked: select a position before reviewing its orders."
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
        self._set_review_status_locked(
            f"Review cancellation of OCA bracket {candidate.oca_group}; no replacement "
            "sell order will be sent. Press Execute paper order to verify a fresh snapshot."
        )

    def _arm_all_cancellations_locked(self) -> None:
        """Stage every active, journal-proven OCA pair on this position."""
        self._disarm_execution_locked()
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Paper order management is disabled for this launch."
            return
        target_ids = self._active_target_perm_ids()
        snapshot = self._view_model.latest_snapshot()
        if not target_ids or snapshot is None:
            self._message = "Bracket cancellation blocked: no active layers are available."
            return
        try:
            candidates = self._paper_execution.prepare_market_exits(
                snapshot,
                target_perm_ids=target_ids,
                expected_client_id=self._settings.client_id,
            )
        except ExecutionBlocked as error:
            self._message = f"Bracket cancellation blocked: {error}"
            return
        self._armed_cancellations = candidates
        self._set_review_status_locked(
            f"Review cancellation of {len(candidates)} active OCA brackets. "
            "The position will remain open. Press Execute paper order to verify a fresh snapshot."
        )

    def _execute_active_action_locked(self) -> None:
        """Verify a reviewed cancellation or market exit before showing Confirm."""
        market_exits = self._armed_market_exits or (
            (self._armed_market_exit,) if self._armed_market_exit is not None else ()
        )
        cancellation = self._armed_cancellation
        cancellations = self._armed_cancellations
        if (
            self._paper_execution is None
            or self._selected_con_id is None
            or (not market_exits and cancellation is None and not cancellations)
        ):
            self._message = "Review an active-layer action before executing it."
            return
        self._active_action_verified = False
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
            self._message = "Execution blocked: the fresh snapshot is unavailable."
            return
        try:
            if cancellations:
                if set(self._active_target_perm_ids()) != {
                    candidate.target_perm_id for candidate in cancellations
                }:
                    raise ExecutionBlocked("the set of active OCA layers changed after review")
                refreshed = self._paper_execution.prepare_market_exits(
                    snapshot,
                    target_perm_ids=tuple(candidate.target_perm_id for candidate in cancellations),
                    expected_client_id=self._settings.client_id,
                )
                if refreshed != cancellations:
                    raise ExecutionBlocked("the OCA layers changed after review")
            elif cancellation is not None:
                refreshed = self._paper_execution.prepare_market_exit(
                    snapshot,
                    target_perm_id=cancellation.target_perm_id,
                    expected_client_id=self._settings.client_id,
                )
                if refreshed != cancellation:
                    raise ExecutionBlocked("the OCA bracket changed after review")
            else:
                if self._review_all_active_exits and set(
                    self._active_target_perm_ids()
                ) != {candidate.target_perm_id for candidate in market_exits}:
                    raise ExecutionBlocked("the set of active OCA layers changed after review")
                refreshed_exits = self._paper_execution.prepare_market_exits(
                    snapshot,
                    target_perm_ids=tuple(
                        candidate.target_perm_id for candidate in market_exits
                    ),
                    expected_client_id=self._settings.client_id,
                )
                if refreshed_exits != market_exits:
                    raise ExecutionBlocked("the OCA layers changed after review")
        except ExecutionBlocked as error:
            self._disarm_execution_locked()
            self._message = f"Execution blocked: {error}. Review the latest state again."
            return
        self._active_action_verified = True
        self._set_review_status_locked(
            "Fresh paper snapshot verified. Review the action, then confirm."
        )

    def _confirm_cancellation_locked(self) -> None:
        """Cancel the staged pair after one more fresh-snapshot equality check."""
        candidate = self._armed_cancellation
        if (
            self._paper_execution is None
            or candidate is None
            or self._selected_con_id is None
            or not self._active_action_verified
        ):
            self._message = "Press Execute paper order before confirming bracket cancellation."
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
            refreshed = self._refresh_after_acknowledged_write_locked(
                "TWS confirmed both OCA legs were cancelled."
            )
            observed = self._view_model.latest_snapshot()
            cancelled_ids = {candidate.target_perm_id, candidate.stop_perm_id}
            if (
                observed is not None
                and observed.selected.account == candidate.account
                and observed.selected.con_id == candidate.con_id
                and observed.complete
                and observed.fresh
                and not any(
                    order.perm_id in cancelled_ids
                    for order in observed.working_orders
                )
            ):
                self._show_success_toast_locked(
                    "Selected bracket orders cancelled",
                    "The position remains open.",
                )
            elif refreshed:
                self._message = (
                    "Bracket cancellation needs verification: the refreshed TWS "
                    "snapshot still shows a selected order. Check TWS and refresh."
                )
        finally:
            self._disarm_execution_locked()

    def _confirm_all_cancellations_locked(self) -> None:
        """Cancel reviewed pairs one at a time, verifying TWS between writes."""
        candidates = self._armed_cancellations
        if (
            self._paper_execution is None
            or self._selected_con_id is None
            or not candidates
            or not self._active_action_verified
        ):
            self._message = "Press Execute paper order before confirming bracket cancellation."
            return
        cancelled = 0
        try:
            for index, candidate in enumerate(candidates):
                state = self._view_model.select_position(
                    self._selected_con_id,
                    self._plan_form(self._drafts.get(self._selected_con_id, ())),
                )
                self._apply_state_locked(state)
                self._record_refresh_time_locked()
                self._announce_reconciliation_locked()
                snapshot = self._view_model.latest_snapshot()
                if snapshot is None:
                    raise ExecutionBlocked("the fresh snapshot is unavailable")
                remaining = candidates[index:]
                if set(self._active_target_perm_ids()) != {
                    item.target_perm_id for item in remaining
                }:
                    raise ExecutionBlocked("the set of active OCA layers changed")
                refreshed = self._paper_execution.prepare_market_exits(
                    snapshot,
                    target_perm_ids=tuple(item.target_perm_id for item in remaining),
                    expected_client_id=self._settings.client_id,
                )
                if refreshed != remaining:
                    raise ExecutionBlocked("an OCA layer changed after review")
                self._paper_execution.cancel_pair(
                    snapshot,
                    candidate,
                    host="127.0.0.1",
                    port=self._settings.port,
                    client_id=self._settings.client_id,
                    timeout_seconds=self._settings.timeout_seconds,
                )
                cancelled += 1
        except (ExecutionBlocked, ExecutionOutcomeUnknown) as error:
            self._message = (
                f"Cancelled {cancelled} of {len(candidates)} brackets. Stopped: {error}. "
                "Refresh TWS before another action."
            )
        except Exception as error:
            self._message = (
                f"Cancelled {cancelled} of {len(candidates)} brackets. Outcome is unknown: "
                f"{error}. Refresh TWS before another action."
            )
        else:
            if self._refresh_after_acknowledged_write_locked(
                f"TWS confirmed cancellation of {cancelled} active OCA brackets."
            ):
                observed = self._view_model.latest_snapshot()
                cancelled_ids = {
                    perm_id
                    for candidate in candidates
                    for perm_id in (candidate.target_perm_id, candidate.stop_perm_id)
                }
                if (
                    observed is not None
                    and observed.selected.account == candidates[0].account
                    and observed.selected.con_id == candidates[0].con_id
                    and observed.complete
                    and observed.fresh
                    and not any(
                        order.perm_id in cancelled_ids
                        for order in observed.working_orders
                    )
                ):
                    self._show_success_toast_locked(
                        "Active brackets cancelled", "The position remains open."
                    )
                else:
                    self._message = (
                        "Bracket cancellation needs verification: refreshed TWS still "
                        "shows a selected order. Check TWS and refresh."
                    )
        finally:
            self._disarm_execution_locked()

    def _resolve_cancelled_bracket_locked(self, values: dict[str, str]) -> None:
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Cancellation verification requires a selected position."
            return
        if values.get("confirmed") != "yes":
            self._message = "Confirm both bracket legs are cancelled in TWS first."
            return
        con_id = self._selected_con_id
        state = self._view_model.select_position(con_id, self._plan_form(()))
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()
        snapshot = self._view_model.latest_snapshot()
        if snapshot is None:
            self._message = "Cancellation verification needs a fresh TWS snapshot."
            return
        try:
            self._paper_execution.confirm_cancelled_unknown(
                snapshot,
                values.get("fingerprint", ""),
                confirmed_in_tws=True,
            )
        except ExecutionBlocked as error:
            self._message = f"Cancellation verification blocked: {error}"
            return
        self._drafts[con_id] = ()
        self._recovery_requested_fingerprint = None
        self._show_success_toast_locked(
            "Cancelled bracket cleared",
            "You can add a new layer for the verified available quantity.",
        )

    def _dismiss_cancelled_layer_locked(self, action: str) -> None:
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Select a position before removing a cancelled row."
            return
        _, _, identity = action.partition(":")
        parts = identity.rsplit(":", 2)
        if len(parts) != 3:
            self._message = "Cancelled row identity is missing."
            return
        fingerprint, attempt_captured_at, index_text = parts
        snapshot = self._view_model.latest_snapshot()
        if snapshot is None or snapshot.selected.con_id != self._selected_con_id:
            self._message = "Refresh the selected position before removing this row."
            return
        try:
            self._paper_execution.dismiss_cancelled_layer(
                snapshot, fingerprint, attempt_captured_at, int(index_text)
            )
        except (ExecutionBlocked, ValueError) as error:
            self._message = f"Cancelled row could not be removed: {error}"
            return
        self._show_success_toast_locked(
            "Cancelled row removed",
            "Its order history remains saved for duplicate protection.",
        )

    def _arm_selected_market_exit_locked(self, values: dict[str, str]) -> None:
        """Review an all-active-layer exit from the displayed snapshot."""
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
        self._review_all_active_exits = True
        total = sum((candidate.quantity for candidate in candidates), Decimal("0"))
        self._set_review_status_locked(
            f"Review cancellation of {len(candidates)} OCA layers and one MKT sell "
            f"for {total} contracts, then press Execute paper order."
        )

    def _confirm_market_exit_locked(self) -> None:
        armed = self._armed_market_exits or (
            (self._armed_market_exit,) if self._armed_market_exit is not None else ()
        )
        if (
            self._paper_execution is None
            or not armed
            or self._selected_con_id is None
            or not self._active_action_verified
        ):
            self._message = "Press Execute paper order before confirming the market exit."
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
            if self._review_all_active_exits and set(
                self._active_target_perm_ids()
            ) != {candidate.target_perm_id for candidate in armed}:
                raise ExecutionBlocked("the set of active OCA layers changed after review")
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
            refreshed = self._refresh_after_acknowledged_write_locked(
                f"TWS confirmed both selected OCA legs were cancelled and "
                f"acknowledged the standalone MKT sell for "
                f"{sum((candidate.quantity for candidate in candidates), Decimal('0'))} contracts."
            )
            if refreshed:
                self._show_success_toast_locked(
                    "Market sell sent to TWS",
                    "Selected bracket orders were cancelled. Check TWS for the fill.",
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
            edited_percentages: dict[int, tuple[str, str]] = {}
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
                    or stop_percentage <= -100
                ):
                    raise ExecutionBlocked(
                        "active target must be positive and stop return must be above -100%"
                    )
                edited_percentages[layer.target_perm_id] = (
                    format(target_percentage, "f"),
                    format(stop_percentage, "f"),
                )
                desired_target = round_up_price(
                    basis * (Decimal("1") + target_percentage / Decimal("100")),
                    calculator.bands,
                )
                desired_stop = round_up_price(
                    basis * (Decimal("1") + stop_percentage / Decimal("100")),
                    calculator.bands,
                )
                shown_target = _decimal_value(_active_percentage_for_price(
                    target.limit_price, basis, target=True,
                    bands=calculator.bands,
                    presets=_parse_presets(self._target_presets, maximum=Decimal("1000")) or (),
                ))
                shown_stop = _decimal_value(_active_percentage_for_price(
                    stop.stop_price, basis, target=True,
                    bands=calculator.bands,
                    presets=_parse_presets(self._stop_presets, maximum=Decimal("100")) or (),
                ))
                updates.append(
                    PriceUpdateCandidate(
                        layer=layer,
                        target_price=(
                            _edited_active_price(
                                target.limit_price, desired_target,
                                target_percentage, shown_target,
                            )
                        ),
                        stop_price=(
                            _edited_active_price(
                                stop.stop_price, desired_stop,
                                stop_percentage, shown_stop,
                            )
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
            prior_state = self._paper_execution.price_update_attempt_state(
                snapshot, self._armed_price_updates
            )
            if prior_state is not None and prior_state != "SUBMISSION_UNKNOWN":
                raise ExecutionBlocked(
                    "this exact amendment has already been sent or reserved; refresh TWS"
                )
            self._price_update_retry_required = prior_state == "SUBMISSION_UNKNOWN"
            self._armed_active_percentages = edited_percentages
            self._warned_price_update_concerns = _price_update_impact(
                snapshot, self._armed_price_updates
            ).concerns
        except (ExecutionBlocked, ValueError) as error:
            self._disarm_execution_locked()
            self._message = f"Price update blocked: {error}"
            return
        changed_legs = sum(
            int(update.target_price is not None) + int(update.stop_price is not None)
            for update in self._armed_price_updates
        )
        if self._price_update_retry_required:
            self._message = (
                "An earlier price amendment has an unknown outcome. Inspect the order "
                "in TWS for a pending change, then confirm the check below before retrying."
            )
        elif not self._warned_price_update_concerns:
            self._confirm_price_updates_locked({})
        else:
            self._set_review_status_locked(
                f"Fresh paper snapshot verified. Review {changed_legs} selected price "
                "amendments, then confirm."
            )

    def _confirm_price_updates_locked(self, values: dict[str, str]) -> None:
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
        retry_acknowledged = values.get("ack_unknown_price_update") == "on"
        if self._price_update_retry_required and not retry_acknowledged:
            self._message = (
                "Price update blocked: check TWS and acknowledge that the old working "
                "price remains and no amendment is waiting for Transmit."
            )
            record_price_update_event(
                "ui_result",
                outcome="blocked",
                reason="unknown amendment acknowledgement missing",
            )
            return
        record_price_update_event(
            "ui_confirm_requested",
            con_id=self._selected_con_id,
            client_id=self._settings.client_id,
            retry_acknowledged=retry_acknowledged,
            requested=[
                {
                    "target_order_id": update.layer.target_order_id,
                    "target_price": str(update.target_price)
                    if update.target_price is not None
                    else None,
                    "stop_order_id": update.layer.stop_order_id,
                    "stop_price": str(update.stop_price)
                    if update.stop_price is not None
                    else None,
                }
                for update in updates
            ],
        )
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
            record_price_update_event(
                "ui_result", outcome="blocked", reason="fresh snapshot unavailable"
            )
            return
        latest_concerns = _price_update_impact(snapshot, updates).concerns
        if latest_concerns - self._warned_price_update_concerns:
            self._warned_price_update_concerns = latest_concerns
            self._message = (
                "The fresh quote changes the immediate-sell warning. Review it and "
                "confirm again; no price amendment was sent."
            )
            record_price_update_event(
                "ui_result",
                outcome="blocked",
                reason="new immediate-sell concern after refresh",
            )
            return
        prior_execution_ids = {fill.exec_id for fill in snapshot.executions}
        edited_percentages = dict(self._armed_active_percentages)
        submitted = False
        try:
            confirmed = self._paper_execution.prepare_price_updates(
                snapshot,
                updates=updates,
                expected_client_id=self._settings.client_id,
            )
            if confirmed != updates:
                raise ExecutionBlocked("the selected OCA layers changed after review")
            submitted = True
            receipt = self._paper_execution.modify_prices(
                snapshot,
                updates,
                host="127.0.0.1",
                port=self._settings.port,
                client_id=self._settings.client_id,
                timeout_seconds=self._settings.timeout_seconds,
                allow_unknown_retry=self._price_update_retry_required,
            )
        except Exception as error:
            if not submitted:
                self._message = f"Price update blocked: {error}"
                record_price_update_event("ui_result", outcome="blocked", reason=str(error))
                return
            # A marketable amended limit can fill while TWS cancels its OCA
            # sibling. A 202 callback or a missing open order cannot establish
            # whether the amendment failed; use fresh executions instead.
            try:
                self._refresh_locked()
                refreshed = self._view_model.latest_snapshot()
            except Exception:
                refreshed = None
            if snapshot.executions_complete and _price_update_fills_verified(
                refreshed, updates, prior_execution_ids
            ):
                try:
                    self._paper_execution.record_verified_price_updates(
                        snapshot, updates, edited_percentages
                    )
                except ExecutionBlocked as journal_error:
                    self._message = f"Price update outcome is unknown: {journal_error}. Check TWS and refresh."
                    return
                self._message = "Price update filled: the selected exit sold in TWS."
                self._show_success_toast_locked(
                    "Exit filled in TWS", "The selected exit sold. Position and layers refreshed."
                )
                record_price_update_event("ui_result", outcome="filled", reason=str(error))
            else:
                self._message = (
                    f"Price update outcome is unknown: {error}. Check TWS and refresh "
                    "before another action."
                )
                record_price_update_event("ui_result", outcome="unknown", reason=str(error))
        else:
            try:
                self._paper_execution.record_verified_price_updates(
                    snapshot, updates, edited_percentages
                )
            except ExecutionBlocked as journal_error:
                self._message = (
                    f"Price update acknowledged, but layer history could not be updated: "
                    f"{journal_error}. Refresh and check TWS."
                )
                return
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
            record_price_update_event(
                "ui_result",
                outcome="acknowledged",
                refreshed_message=self._status_message,
                displayed_orders=[
                    {
                        "perm_id": order.perm_id,
                        "order_id": order.order_id,
                        "limit_price": str(order.limit_price)
                        if order.limit_price is not None
                        else None,
                        "stop_price": str(order.stop_price)
                        if order.stop_price is not None
                        else None,
                        "status": order.status,
                    }
                    for order in self._state.working_orders
                    if order.order_id in receipt.entry.order_ids
                ],
            )
            if self._status_message.endswith("TWS state refreshed."):
                self._show_success_toast_locked(
                    "Price update sent to TWS",
                    "Check TWS for any required Transmit.",
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
            message = (
                f"{state.status_message}: {blocking}"
                if blocking
                else state.status_message
            )
            blocking_codes = {
                validation.code
                for validation in state.validations
                if validation.blocking
            }
            if blocking_codes == {"POSITION_FULLY_ALLOCATED"}:
                # This is an expected inventory state, not a failed position
                # selection. Keep the planner blocked without an error toast.
                self._status_message = message
                self._toast = None
            else:
                self._message = message

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
        self._last_refreshed_at = datetime.now().astimezone().strftime("%H:%M:%S")

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
        # An empty saved draft is an intentional reset, not a request for a
        # replacement default layer on the next selection or refresh.
        if con_id in self._drafts:
            return
        if any(order.oca_group for order in self._state.working_orders):
            # Existing TWS OCA exposure is shown first. A new draft is an
            # explicit choice through Add layer, even when quantity remains.
            self._drafts[con_id] = ()
            return
        # A position starts without a draft. Creating one requires Add Layer
        # or Build Draft, so the empty state is visible on first selection.
        self._drafts[con_id] = ()

    def _current_layers(self) -> tuple[DraftLayerForm, ...]:
        if self._selected_con_id is None:
            return ()
        return self._drafts.get(self._selected_con_id, ())

    def _save_form_locked(
        self, values: dict[str, str], *, removing_index: int | None = None
    ) -> bool:
        self._target_presets = values.get("target_presets", self._target_presets)
        self._stop_presets = values.get("stop_presets", self._stop_presets)
        if self._selected_con_id is None:
            return True
        available = self._planning_available_quantity()
        quantities = [
            values.get(f"quantity_{index}", previous.quantity)
            for index, previous in enumerate(self._current_layers(), start=1)
            if index != removing_index
        ]
        try:
            parsed = [Decimal(quantity) for quantity in quantities]
        except InvalidOperation:
            parsed = []
        if (
            len(parsed) != len(quantities)
            or any(
                not quantity.is_finite()
                or quantity != quantity.to_integral_value()
                or not 1 <= quantity <= available
                for quantity in parsed
            )
            or sum(parsed) > available
        ):
            self._message = f"Draft quantities must be whole contracts from 1 to {available}, with no more than {available} assigned in total."
            return False
        layers: list[DraftLayerForm] = []
        for index, previous in enumerate(self._current_layers(), start=1):
            if index == removing_index:
                continue
            target = values.get(f"target_{index}", previous.target_percentage)
            stop = values.get(f"stop_{index}", previous.stop_percentage)
            quantity = values.get(f"quantity_{index}", previous.quantity)
            tif = values.get(f"tif_{index}", previous.tif)
            try:
                target_value, stop_value = Decimal(target), Decimal(stop)
                prices = preview_reference_prices(
                    self._state.unit_basis or Decimal("0"),
                    target_value,
                    stop_value,
                    self._state.quote_calculator.bands
                    if self._state.quote_calculator is not None
                    else (),
                )
            except (InvalidOperation, ValueError):
                self._message = (
                    "Targets must be above 0%; stops must be between 0% and 100%."
                )
                return False
            layers.append(
                DraftLayerForm(
                    quantity=quantity,
                    target_price=(previous.target_price if target_value == Decimal(previous.target_percentage) else format(prices.target_price, "f")),
                    stop_price=(previous.stop_price if stop_value == Decimal(previous.stop_percentage) else format(prices.stop_price, "f")),
                    target_percentage=format(target_value, "f"),
                    stop_percentage=format(stop_value, "f"),
                    tif=tif if tif in {"GTC", "DAY"} else previous.tif,
                )
            )
        self._drafts[self._selected_con_id] = tuple(layers)
        return True

    def _add_layer_locked(self) -> None:
        layers = list(self._current_layers())
        con_id = self._selected_con_id
        if len(layers) >= self._planning_available_quantity():
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
        pending = self._pending_submissions()
        if pending:
            if any(outcome.status != "PENDING" for _entry, _index, outcome in pending):
                self._message = (
                    "Add Layer blocked: review unresolved brackets in TWS "
                    "before adding a draft."
                )
                return
            try:
                previous_targets = tuple(
                    Decimal(entry.layers[index].target_price)
                    for entry, index, _outcome in pending
                ) + tuple(Decimal(layer.target_price) for layer in layers)
            except InvalidOperation:
                self._message = "A pending target price is invalid; review TWS and Refresh."
                return
            if any(not price.is_finite() or price <= 0 for price in previous_targets):
                self._message = "A pending target price is invalid; review TWS and Refresh."
                return
            target_presets = _parse_presets(
                self._target_presets, maximum=Decimal("1000")
            ) or ()
            higher_target = _next_target_preset_above(
                previous_targets,
                basis=basis,
                bands=calculator.bands,
                presets=target_presets,
            )
            target = higher_target if higher_target is not None else target_presets[-1]
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
                _split_quantity(self._planning_available_quantity(), len(layers)),
                strict=True,
            )
        )

    def _build_draft_locked(self) -> None:
        """Create a full draft from LMT defaults without changing existing rows."""
        con_id = self._selected_con_id
        available = self._planning_available_quantity()
        if con_id is None or self._state.status is not UiStatus.READY or available <= 0:
            self._message = "Refresh a position with available contracts before building a draft."
            return
        if self._current_layers():
            self._message = "A draft already exists. Edit its layers or remove them first."
            return
        if any(
            outcome.status != "PENDING"
            for _entry, _index, outcome in self._pending_submissions()
        ):
            self._message = (
                "Build Draft blocked: review unresolved brackets in TWS "
                "before building a draft."
            )
            return
        targets = _parse_presets(self._target_presets, maximum=Decimal("1000"))
        stops = _parse_presets(self._stop_presets, maximum=Decimal("100"))
        basis = self._state.unit_basis
        calculator = self._state.quote_calculator
        if targets is None or stops is None or basis is None or calculator is None:
            self._message = "Enter valid LMT and STP defaults before building a draft."
            return
        count = min(available, len(targets))
        quantities = _split_quantity(available, count)
        layers: list[DraftLayerForm] = []
        try:
            for index, (target, quantity) in enumerate(
                zip(targets[:count], quantities, strict=True)
            ):
                stop = stops[min(index, len(stops) - 1)]
                prices = preview_reference_prices(basis, target, stop, calculator.bands)
                layers.append(
                    DraftLayerForm(
                        quantity=str(quantity),
                        target_price=format(prices.target_price, "f"),
                        stop_price=format(prices.stop_price, "f"),
                        target_percentage=format(target, "f"),
                        stop_percentage=format(stop, "f"),
                    )
                )
        except ValueError:
            self._message = "The selected position does not have a usable price increment."
            return
        self._drafts[con_id] = tuple(layers)

    def _remove_layer_locked(self, index: int) -> None:
        layers = list(self._current_layers())
        con_id = self._selected_con_id
        if not 1 <= index <= len(layers):
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
            self._planning_available_quantity()
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
        projection = self._projection_state()
        recovery_dialog = (
            None
            if self._submission_review_required
            else self._cancelled_bracket_recovery(self._submission_outcomes())
        )
        if self._selected_closed_con_id in self._session_closed_positions:
            closed_workspace, closed_review = self._closed_session_workspace(
                self._selected_closed_con_id
            )
            content = Div(
                Div(self._inventory(), id="position-inventory"),
                closed_workspace,
                closed_review,
                cls="grid h-[calc(100vh-3.5rem)] min-h-0 grid-cols-[16rem_minmax(0,1fr)_19rem] overflow-hidden border-t border-border",
            )
        elif state.status is UiStatus.READY and not state.positions and not self._session_closed_positions:
            content = self._empty_positions()
        else:
            snapshot = self._view_model.latest_snapshot()
            title = (
                _contract_display_name(snapshot.contract)
                if snapshot is not None
                and snapshot.selected.con_id == self._selected_con_id
                else (
                    state.position_title
                    if self._selected_con_id is not None
                    else "Select an option position"
                )
            )
            content = Div(
                Div(self._inventory(), id="position-inventory"),
                self._workspace(title),
                self._review(projection),
                cls="grid h-[calc(100vh-3.5rem)] min-h-0 grid-cols-[16rem_minmax(0,1fr)_19rem] overflow-hidden border-t border-border",
            )
        return Div(
            Script(_projection_script(projection[2])),
            Script(
                f"""(() => {{
                  const restoreKey = 'position-draft-restore:{self.session_token}:{self._selected_con_id}';
                  const restoreDraft = () => {{
                    try {{
                      const stored = sessionStorage.getItem(restoreKey);
                      if (stored) {{
                        const fields = JSON.parse(stored);
                        const form = document.getElementById('draft-form');
                        if (form) {{
                          sessionStorage.removeItem(restoreKey);
                          const restored = [];
                          Object.entries(fields).forEach(([name, value]) => {{
                            const input = form.elements.namedItem(name);
                            if (input && 'value' in input) {{
                              input.value = value;
                              restored.push(input);
                            }}
                          }});
                          restored.forEach(input => input.dispatchEvent(
                            new Event('input', {{bubbles: true}})
                          ));
                        }}
                      }}
                    }} catch (_) {{ /* Browser storage may be unavailable. */ }}
                  }};
                  if (document.readyState === 'loading') {{
                    document.addEventListener('DOMContentLoaded', restoreDraft, {{once: true}});
                  }} else restoreDraft();
                  const saveDraft = () => {{
                    const form = document.getElementById('draft-form');
                    if (!form) return;
                    const fields = {{}};
                    form.querySelectorAll('input[name], select[name]').forEach(input => {{
                      fields[input.name] = input.value;
                    }});
                    try {{ sessionStorage.setItem(restoreKey, JSON.stringify(fields)); }} catch (_) {{}}
                  }};
                  document.addEventListener('click', event => {{
                    if (event.target.closest('#position-change-update')) saveDraft();
                  }});
                  let seen = '{self._inventory_revision}';
                  const setEventStatus = (state, label) => {{
                    const status = document.getElementById('tws-updates-status');
                    const statusText = document.getElementById('tws-updates-label');
                    if (!status || !statusText) return;
                    status.dataset.connectionState = state;
                    status.dataset.headerStatus = label;
                    statusText.textContent = label;
                  }};
                  let stream;
                  let lastHeartbeat = 0;
                  let fetching = false;
                  const connectStream = () => {{
                    lastHeartbeat = Date.now();
                    stream = new EventSource('/{self.session_token}/inventory-events');
                    stream.onerror = () => setEventStatus('warning', 'TWS updates reconnecting');
                    stream.onmessage = async (event) => {{
                    let update;
                    try {{ update = JSON.parse(event.data); }} catch (_) {{
                      setEventStatus('warning', 'TWS updates unavailable');
                      return;
                    }}
                    lastHeartbeat = Date.now();
                    setEventStatus(
                      update.observer === 'connected' ? 'ready' :
                        update.observer === 'connecting' ? 'starting' : 'warning',
                      update.observer === 'connected' ? 'TWS connected' :
                        update.observer === 'connecting' ? 'Connecting to TWS' :
                        update.observer === 'client-id-in-use' ? 'TWS observer ID in use' :
                        'TWS updates unavailable'
                    );
                    const revision = String(update.revision);
                    if (revision === seen || fetching) return;
                    fetching = true;
                    try {{
                      const response = await fetch('/{self.session_token}/inventory-fragment', {{cache:'no-store'}});
                      if (!response.ok) return;
                      if (response.headers.get('X-Inventory-Revision') !== revision) return;
                      seen = revision;
                      if (response.headers.get('X-Selected-Changed') === '1') {{
                        saveDraft();
                        window.location.reload(); return;
                      }}
                      const slot = document.getElementById('position-inventory');
                      if (!slot) {{ saveDraft(); window.location.reload(); return; }}
                      slot.innerHTML = await response.text();
                      const change = Number(response.headers.get('X-Selected-Quantity-Change') || '0');
                      const notice = document.getElementById('selected-quantity-notice');
                      const message = document.getElementById('selected-quantity-message');
                      if (notice && message) {{
                        notice.hidden = change === 0;
                        const count = Math.abs(change);
                        message.textContent = change > 0
                          ? `${{count}} new ${{count === 1 ? 'contract was' : 'contracts were'}} added to this position in TWS. Update this view to plan brackets for the latest quantity.`
                          : `${{count}} ${{count === 1 ? 'contract was' : 'contracts were'}} removed from this position in TWS. Update this view to review the remaining protection and draft quantities.`;
                      }}
                    }} catch (_) {{ /* The next heartbeat retries this revision. */ }}
                    finally {{ fetching = false; }}
                    }};
                  }};
                  connectStream();
                  window.setInterval(() => {{
                    if (Date.now() - lastHeartbeat <= 7000) return;
                    setEventStatus('warning', 'TWS updates delayed');
                    stream.close();
                    connectStream();
                  }}, 2000);
                }})();"""
            ) if self._observe_positions else None,
            self._header(),
            content,
            self._toast_component(),
            self._launch_connection_dialog(),
            self._submission_review_dialog(),
            recovery_dialog,
            Script(_busy_submit_script()),
            cls="h-screen overflow-hidden bg-background text-foreground selection:bg-primary selection:text-primary-foreground",
        )

    def _submission_review_dialog(self) -> Any:
        if not self._submission_review_required:
            return None
        return Div(
            Dialog(
                DialogContent(
                    DialogHeader(
                        DialogTitle("Orders sent to TWS"),
                        DialogDescription(
                            "The orders were sent to TWS. Confirm or transmit them "
                            "there if prompted, then refresh their status here."
                        ),
                    ),
                    DialogFooter(
                        Form(
                            Button(
                                "Refresh order status",
                                type="submit",
                                data_busy_text="Checking…",
                            ),
                            HTMLInput(type="hidden", name="action", value="refresh"),
                            action=f"/{self.session_token}/action",
                            method="post",
                        )
                    ),
                    show_close_button=False,
                ),
                signal="submission_review",
                default_open=True,
                dismissible=False,
                size="sm",
            ),
            data_submission_review=True,
        )

    def _empty_positions(self) -> Any:
        return Div(
            Div(
                Icon("lucide:link-2", cls="size-7 text-primary", aria_hidden="true"),
                H1("No option positions detected", cls="mt-6 text-2xl font-semibold tracking-tight"),
                P(
                    "Buy a long option contract in TWS, then refresh to load it here.",
                    cls="mt-3 max-w-md text-sm leading-6 text-muted-foreground",
                ),
                Form(
                    Button(
                        "Refresh positions",
                        type="submit",
                        data_busy_text="Refreshing…",
                    ),
                    HTMLInput(type="hidden", name="action", value="refresh"),
                    action=f"/{self.session_token}/action",
                    method="post",
                    cls="mt-9",
                ),
                cls="max-w-lg",
            ),
            data_empty_positions=True,
            cls="flex h-[calc(100vh-3.5rem)] items-center justify-center border-t border-border px-6",
        )

    def _toast_component(self) -> Any:
        notice = self._toast if self._toast_revision > self._toast_rendered_revision else None
        if notice is not None:
            self._toast_rendered_revision = self._toast_revision
        # Keep the official Toaster mounted on every response so the embedded
        # Datastar runtime always has its signal and close button. A normal
        # Toaster uses an ``ifmissing`` signal, which is right for initial
        # hydration but intentionally does not replace a pre-existing signal
        # after an action rerender. The explicit non-ifmissing signal below is
        # the documented server-side update path for each new notice.
        initial_toasts = (
            [
                {
                    "id": self._toast_revision,
                    "title": notice.title,
                    "description": notice.description,
                    "variant": notice.variant,
                    "timestamp": self._toast_revision,
                    "order": 0,
                },
                None,
                None,
            ]
            if notice is not None
            else [None, None, None]
        )
        return Div(
            Signal("toasts", initial_toasts, ifmissing=False),
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
            # show() has no native ::backdrop; this visual layer leaves Settings clickable.
            Div(
                cls="connection-failure-backdrop",
                data_launch_backdrop=True,
                aria_hidden="true",
            )
            if not connecting
            else None,
            Dialog(
                DialogContent(*content, show_close_button=False),
                signal="launch_connection",
                default_open=True,
                # On failure, keep Settings reachable so the user can correct
                # connection details. Order actions still require verified state.
                modal=connecting,
                size="sm",
            ),
            Script(
                f"""
                (() => {{
                  const dialog = document.getElementById('launch_connection');
                  if (dialog && !dialog.open) dialog.{"showModal" if connecting else "show"}();
                }})();
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

    def _header(self) -> Any:
        state = self._state
        snapshot = self._view_model.latest_snapshot()
        selected_snapshot = (
            snapshot
            if snapshot is not None
            and snapshot.selected.con_id == self._selected_con_id
            else None
        )
        verified_data = selected_snapshot is not None or any(
            fact.label == "Paper account" and fact.state is FactState.PASS
            for fact in state.connection
        )
        if self._demo_mode:
            connection = _header_status("TWS not connected", "link-2", "muted")
        elif self._launch_connection == "connecting":
            connection = _header_status("Connecting to TWS", "link-2", "muted")
        elif self._observe_positions and self._observer_health in {"error", "disconnected", "client-id-in-use"}:
            label = (
                "Observer client ID in use"
                if self._observer_health == "client-id-in-use"
                else "TWS observation unavailable"
            )
            connection = _header_status(label, "link-2", "warning")
        elif verified_data:
            connection = _header_status("TWS connected", "link-2", "ready")
        elif self._launch_connection == "failed":
            connection = _header_status("TWS unavailable", "link-2", "warning")
        elif state.status is UiStatus.EMPTY:
            connection = _header_status("Not connected", "link-2", "muted")
        else:
            connection = _header_status("Connection unverified", "link-2", "muted")
        plan_status = None
        if self._selected_con_id is not None:
            if state.status is UiStatus.STALE:
                plan_status = _header_status("Refresh required", "x", "warning")
            elif state.status is UiStatus.READY or any(
                validation.code == "POSITION_FULLY_ALLOCATED"
                for validation in state.validations
            ):
                plan_status = None
            else:
                reason = next(
                    (
                        validation.message
                        for validation in state.validations
                        if validation.blocking
                    ),
                    None,
                )
                plan_status = _header_status(
                    "New layer unavailable", "x", "warning", title=reason
                )
        if self._demo_mode:
            account_mode = _header_status("Test data", "shield-off", "muted")
        elif self._settings.account.strip().upper().startswith("DU"):
            account_mode = _header_status("Paper TWS account", "shield", "paper")
        elif self._settings.account.strip().upper().startswith("U"):
            account_mode = _header_status("Live TWS account", "shield-alert", "live")
        else:
            account_mode = _header_status("Account unverified", "shield-off", "muted")
        if self._observe_positions:
            if self._observer_health in {"error", "disconnected", "client-id-in-use"} or self._launch_connection == "failed":
                initial_label = "TWS updates unavailable"
                initial_state = "warning"
            elif self._launch_connection == "connecting" or self._observer_health == "connecting":
                initial_label = "Connecting to TWS"
                initial_state = "starting"
            else:
                initial_label = "Checking TWS updates"
                initial_state = "starting"
            connection = Div(
                Icon("lucide:radio", cls="size-4 shrink-0 tws-updates-icon", aria_hidden="true"),
                Span(initial_label, id="tws-updates-label", cls="text-xs font-medium whitespace-nowrap", aria_live="polite"),
                id="tws-updates-status",
                cls="flex shrink-0 items-center gap-1.5",
                data_connection_state=initial_state,
                data_header_status=initial_label,
                title="TWS position observer and window event stream; a fresh broker snapshot is still required for order review",
            )
        return Div(
            connection,
            plan_status,
            account_mode,
            Span(
                f"Account {state.account or '—'}",
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

    def _closed_session_workspace(self, con_id: int) -> Any:
        position = self._session_closed_positions[con_id]
        reader = getattr(self._paper_execution, "submission_entries", None)
        entries = reader(account=self._settings.account, con_id=con_id) if callable(reader) else ()
        rows = []
        realized = Decimal("0")
        pnl_verified = True
        for entry in entries:
            for index in range(len(entry.layers)):
                outcome = classify_journal_layer(
                    entry, index, active_perm_ids=frozenset(),
                    observed_perm_ids=frozenset(),
                )
                if outcome.status.startswith("CLOSED_"):
                    rows.append(self._closed_layer_row(
                        len(rows) + 1, entry, index, outcome,
                        recover_legacy=False,
                    ))
                    if outcome.realized_pnl is not None and outcome.currency == "USD":
                        realized += outcome.realized_pnl
                    else:
                        pnl_verified = False
                else:
                    pnl_verified = False
                    rows.append(self._pending_layer_row(
                        len(rows) + 1, entry, index, outcome, read_only=True,
                    ))
        symbol, contract_detail = _position_identity(position.local_symbol)
        title = f"{symbol} {contract_detail}".strip()
        result = _header_pnl(realized, "USD") if pnl_verified and rows else "—"
        center = Div(
            Div(
                H1(title, cls="min-w-0 text-2xl font-semibold tracking-tight"),
                Badge("Closed this session", variant="secondary"),
                cls="flex flex-wrap items-center justify-between gap-3",
            ),
            Div(
                _contract_header_metric("Held / total", "0 / 0"),
                _contract_header_metric("Available", "0"),
                _contract_header_metric("Average price", "—"),
                _contract_header_metric("Last bid", "—"),
                _contract_header_metric("Last ask", "—"),
                _contract_header_metric("Realised P&L", result),
                cls="contract-header-facts",
            ),
            ScrollArea(
                Div(*rows, cls="mt-2") if rows else P(
                    "No app-owned layer history is available for this contract.",
                    cls="pt-8 text-sm text-muted-foreground",
                ),
                cls="min-h-0 flex-1",
            ),
            data_closed_session=True,
            cls="flex min-h-0 min-w-0 flex-col gap-5 overflow-hidden px-8 py-8",
        )
        review = Div(
            Div(
                Span("ACTION REVIEW", cls="text-xs font-semibold tracking-wide text-muted-foreground"),
                cls="flex items-center justify-between px-4 py-4",
            ),
            Div(
                P("Closed position", cls="mt-4 text-base font-semibold"),
                P(
                    "This session view is read-only. Check TWS for any layer that still needs verification.",
                    cls="mt-2 text-center text-sm leading-6 text-muted-foreground",
                ),
                cls="flex min-h-0 flex-1 flex-col items-center justify-center px-6",
            ),
            Div(Button("Execute paper order", disabled=True, cls="w-full"), cls="mx-4 mb-4"),
            cls="flex min-h-0 flex-col overflow-hidden border-l border-border bg-card/30",
        )
        return center, review

    def _inventory(self) -> Any:
        rows = []
        for position in self._state.positions:
            selected = position.con_id == self._selected_con_id
            symbol, contract_detail = _position_identity(position.local_symbol)
            rows.append(
                Form(
                    Button(
                        Div(
                            Div(
                                Span(symbol, cls="text-sm font-semibold"),
                                Badge(
                                    "NEW",
                                    variant="outline",
                                    cls="new-position-badge",
                                    data_new_position=True,
                                )
                                if position.con_id in self._new_position_ids
                                else None,
                                cls="flex min-w-0 items-center gap-1.5",
                                data_position_name=True,
                            ),
                            Badge(
                                Span(position.quantity, data_position_count=True),
                                Span(
                                    Icon(
                                        "lucide:loader-circle",
                                        cls="size-3 animate-spin",
                                    ),
                                    data_position_spinner=True,
                                    aria_hidden="true",
                                ),
                                Span(
                                    "Loading position",
                                    cls="sr-only hidden",
                                    data_position_loading=True,
                                ),
                                variant="secondary",
                                data_position_quantity=True,
                                data_loading="false",
                                aria_live="polite",
                            ),
                            cls="flex w-full items-center justify-between",
                        ),
                        P(
                            contract_detail,
                            cls="mt-1.5 w-full text-xs text-muted-foreground",
                        ),
                        variant="ghost",
                        disabled=not position.eligible,
                        type="submit",
                        data_busy_text="Loading position…",
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
        closed_rows = []
        for position in self._session_closed_positions.values():
            symbol, contract_detail = _position_identity(position.local_symbol)
            closed_rows.append(
                Form(
                    Button(
                        Div(
                            Span(symbol, cls="text-sm font-semibold"),
                            Badge("0", variant="secondary"),
                            cls="flex w-full items-center justify-between",
                        ),
                        P(contract_detail, cls="mt-1.5 w-full text-xs text-muted-foreground"),
                        variant="ghost",
                        type="submit",
                        cls=(
                            "h-auto min-h-20 w-full flex-col items-stretch justify-center gap-0 "
                            "rounded-none border-l-2 px-4 py-4 text-left hover:bg-accent "
                            + ("border-emerald-400 bg-emerald-500/10" if position.con_id == self._selected_closed_con_id else "border-transparent")
                        ),
                    ),
                    HTMLInput(type="hidden", name="action", value="select-session-closed"),
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
                Div(
                    Span("CLOSED THIS SESSION", cls="text-xs font-semibold tracking-wide text-muted-foreground"),
                    cls="border-t border-border px-3 py-4",
                ) if closed_rows else None,
                *closed_rows,
                aria_label="Open option positions",
                cls="min-h-0 flex-1",
            ),
            cls="flex h-full min-h-0 flex-col overflow-hidden border-r border-border bg-card/30",
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

    def _selected_quantity_notice(self) -> Any:
        change = self._selected_quantity_change
        delta = change[2] - change[1] if change else 0
        count = abs(delta)
        if delta > 0:
            message = (
                f"{count} new {'contract was' if count == 1 else 'contracts were'} "
                "added to this position in TWS. Update this view to plan brackets "
                "for the latest quantity."
            )
        else:
            message = (
                f"{count} {'contract was' if count == 1 else 'contracts were'} "
                "removed from this position in TWS. Update this view to review "
                "the remaining protection and draft quantities."
            )
        return Div(
            Span(message, id="selected-quantity-message"),
            Form(
                Button(
                    "Update view",
                    type="submit",
                    id="position-change-update",
                    variant="outline",
                ),
                HTMLInput(
                    type="hidden", name="action", value="acknowledge-position-change"
                ),
                action=f"/{self.session_token}/action",
                method="post",
                cls="shrink-0",
            ),
            id="selected-quantity-notice",
            role="status",
            hidden=delta == 0,
            cls="selected-quantity-notice",
        )

    def _workspace(self, title: str) -> Any:
        coverage, _, _ = self._order_coverage()
        active_pairs = self._active_oca_pairs()
        outcomes = self._submission_outcomes()
        active_target_ids = {target.perm_id for _group, target, _stop in active_pairs}
        pending = tuple(
            item
            for item in outcomes
            if item[2].status
            in {"PENDING", "UNKNOWN", "PARTIAL", "NO_EXECUTION_EVIDENCE", "GROUP_COLLISION"}
            or (
                item[2].status.startswith("CLOSED_")
                and _journal_target_perm_id(item[0], item[1]) in active_target_ids
            )
        )
        draft_allowed = not pending or all(
            outcome.status == "PENDING" for _entry, _index, outcome in pending
        )
        planning_available = self._planning_available_quantity()
        snapshot = self._view_model.latest_snapshot()
        selected_snapshot = (
            snapshot
            if snapshot is not None
            and snapshot.selected.con_id == self._selected_con_id
            else None
        )
        quote = selected_snapshot.quote if selected_snapshot is not None else None
        basis = (
            selected_snapshot.position.unit_basis
            if selected_snapshot is not None
            else None
        )
        currency = selected_snapshot.contract.currency if selected_snapshot else "USD"
        realized = (
            Decimal("0")
            if callable(getattr(self._paper_execution, "submission_entries", None))
            else None
        )
        for _entry, _index, outcome in outcomes:
            if outcome.status in {"PARTIAL", "UNKNOWN", "NO_EXECUTION_EVIDENCE", "GROUP_COLLISION"}:
                realized = None
                break
            if not outcome.status.startswith("CLOSED_"):
                continue
            if outcome.realized_pnl is None or outcome.currency != currency:
                realized = None
                break
            if realized is not None:
                realized += outcome.realized_pnl
        return Div(
            self._selected_quantity_notice(),
            Div(
                H1(title, cls="min-w-0 text-2xl font-semibold tracking-tight"),
                Div(
                    Div(
                        self._set_stops_dialog(active_pairs, basis, quote, selected_snapshot.fresh if selected_snapshot is not None else False)
                        if basis is not None
                        else None,
                        Tooltip(
                            TooltipTrigger(
                                Button(
                                    Icon(
                                        "lucide:equal", cls="size-4", aria_hidden="true"
                                    ),
                                    variant="outline",
                                    size="icon",
                                    data_move_stops_to_be=True,
                                    aria_label="Move all active stops to B/E",
                                    disabled=self._paper_execution is None
                                    or bool(self._armed_price_updates),
                                ),
                                delay_duration=250,
                            ),
                            TooltipContent("Move all active stops to B/E"),
                        ),
                        Tooltip(
                            TooltipTrigger(
                                Button(
                                    Icon("lucide:trash-2", cls="size-4", aria_hidden="true"),
                                    variant="outline",
                                    size="icon",
                                    type="submit",
                                    form="active-form",
                                    name="action",
                                    value="cancel-all-active",
                                    aria_label="Delete all active layers",
                                    disabled=self._paper_execution is None
                                    or bool(self._armed_price_updates),
                                ),
                                delay_duration=250,
                            ),
                            TooltipContent("Delete all active layers"),
                        ),
                        Tooltip(
                            TooltipTrigger(
                                Button(
                                    Icon("lucide:log-out", cls="size-4", aria_hidden="true"),
                                    variant="outline",
                                    size="icon",
                                    type="submit",
                                    form="active-form",
                                    name="action",
                                    value="market-exit-selected",
                                    aria_label="Sell all active layers",
                                    disabled=self._paper_execution is None
                                    or bool(self._armed_price_updates),
                                ),
                                delay_duration=250,
                            ),
                            TooltipContent("Sell all active layers"),
                        ),
                        cls="flex items-center gap-2",
                    )
                    if active_pairs
                    else None,
                    Separator(orientation="vertical", cls="h-5 self-center")
                    if active_pairs and draft_allowed
                    else None,
                    Div(
                        Tooltip(
                            TooltipTrigger(
                                DropdownMenu(
                                    DropdownMenuTrigger(
                                        Icon(
                                            "lucide:split",
                                            cls="size-4",
                                            aria_hidden="true",
                                        ),
                                        size="icon",
                                        aria_label="Split draft layer quantities",
                                        disabled=not draft_allowed
                                        or planning_available <= 0
                                        or len(self._current_layers()) < 2,
                                    ),
                                    DropdownMenuContent(
                                        DropdownMenuItem(
                                            "Split all available",
                                            data_on_click="document.getElementById('split-all-submit').click()",
                                        ),
                                        DropdownMenuItem(
                                            "Split assigned",
                                            data_on_click="document.getElementById('split-assigned-submit').click()",
                                        ),
                                        align="end",
                                    ),
                                ),
                                delay_duration=250,
                            ),
                            TooltipContent("Split draft layer quantities"),
                        ),
                        Tooltip(
                            TooltipTrigger(
                                Button(
                                    Icon(
                                        "lucide:plus", cls="size-4", aria_hidden="true"
                                    ),
                                    "Add Layer",
                                    variant="default",
                                    size="default",
                                    type="submit",
                                    form="draft-form",
                                    name="action",
                                    value="add-layer",
                                    aria_label="Create new OCA bracket",
                                    disabled=not draft_allowed
                                    or planning_available <= 0
                                    or len(self._current_layers())
                                    >= planning_available,
                                ),
                                delay_duration=250,
                            ),
                            TooltipContent("Create new OCA bracket"),
                        ),
                        cls="flex items-center gap-2",
                    ),
                    cls="ml-auto flex flex-wrap items-center justify-end gap-2",
                ),
                cls="flex flex-wrap items-center gap-4",
            ),
            Div(
                _contract_header_metric(
                    "Held / total",
                    self._header_quantity(outcomes, selected_snapshot),
                ),
                _contract_header_metric(
                    "Available", f"{planning_available:g}" if draft_allowed else "—"
                ),
                _contract_header_metric(
                    "Average price", _header_price(basis, currency)
                ),
                _contract_header_metric(
                    "Last bid", _header_price(quote.bid if quote else None, currency)
                ),
                _contract_header_metric(
                    "Last ask", _header_price(quote.ask if quote else None, currency)
                ),
                _contract_header_metric(
                    "Realised P&L", _header_pnl(realized, currency)
                ),
                cls="contract-header-facts",
            )
            if selected_snapshot is not None
            else None,
            self._coverage_alert(
                coverage,
                selected_snapshot,
                uncertain_app_orders=any(
                    outcome.status in {"PENDING", "UNKNOWN", "GROUP_COLLISION"}
                    for _entry, _index, outcome in outcomes
                ),
            ),
            self._submission_attention(outcomes),
            Div(
                ScrollArea(
                    self._existing_layers_panel(active_pairs, outcomes)
                    if active_pairs or outcomes
                    else None,
                    Div(
                        self._draft_panel(show_empty_state=not (active_pairs or outcomes)),
                        cls=(
                            "mt-2 border-t border-border pt-2"
                            if (active_pairs or outcomes) and self._current_layers()
                            else "hidden"
                            if active_pairs or outcomes
                            else "h-full"
                            if not self._current_layers()
                            else ""
                        ),
                    )
                    if draft_allowed and planning_available > 0
                    else None,
                    aria_label="OCA layers workspace",
                    orientation="vertical",
                    cls="h-full",
                ),
                cls="mt-5 min-h-0 flex-1 overflow-hidden",
            ),
            cls="workspace-content flex min-w-0 min-h-0 flex-col overflow-hidden px-8 py-6",
        )

    def _cancelled_bracket_recovery(
        self, outcomes: tuple[tuple[JournalEntry, int, LayerOutcome], ...]
    ) -> Any:
        if not callable(getattr(self._paper_execution, "confirm_cancelled_unknown", None)):
            return None
        snapshot = self._view_model.latest_snapshot()
        if (
            snapshot is None
            or not snapshot.complete
            or not snapshot.fresh
            or snapshot.selected.account != self._verified_selected_account()
            or snapshot.selected.con_id != self._selected_con_id
        ):
            return None
        working = snapshot.working_orders if snapshot is not None else ()

        def has_working_leg(entry: JournalEntry) -> bool:
            groups = {
                _journal_oca_group(entry, index)
                for index in range(len(entry.layers))
            }
            return any(
                order.oca_group in groups
                or order.order_id in entry.order_ids
                or (order.perm_id > 0 and order.perm_id in entry.perm_ids)
                for order in working
            )

        def observed_later(entry: JournalEntry) -> bool:
            try:
                return snapshot.captured_at > Decimal(entry.snapshot_captured_at)
            except (InvalidOperation, ValueError):
                return False

        unresolved = {
            entry.fingerprint: entry
            for entry, _index, outcome in outcomes
            if entry.state in {
                "SUBMISSION_UNKNOWN", "PARTIALLY_RECONCILED", "SUBMITTED", "RECONCILED"
            }
            and outcome.status in {"UNKNOWN", "NO_EXECUTION_EVIDENCE", "GROUP_COLLISION"}
            and not has_working_leg(entry)
            and observed_later(entry)
        }
        if not unresolved:
            self._recovery_requested_fingerprint = None
            return None
        requested = self._recovery_requested_fingerprint
        collision_fingerprints = {
            entry.fingerprint
            for entry, _index, outcome in outcomes
            if outcome.status == "GROUP_COLLISION"
        }
        other_brackets_working = any(
            order.key == snapshot.selected and order.oca_group
            for order in working
        ) if snapshot is not None else False
        if requested in unresolved:
            fingerprint = requested
        else:
            automatic = {
                fingerprint: entry
                for fingerprint, entry in unresolved.items()
                if entry.state in {"SUBMISSION_UNKNOWN", "PARTIALLY_RECONCILED"}
                and fingerprint not in collision_fingerprints
            }
            if (
                not automatic
                or other_brackets_working
                or (self._toast is not None and self._toast.variant == "error")
            ):
                return None
            fingerprint = next(iter(automatic))
        entry = unresolved[fingerprint]
        return Div(
            Dialog(
                DialogContent(
                    DialogHeader(
                        DialogTitle("Confirm bracket status in TWS"),
                        DialogDescription(
                            "Find the following tranche IDs in TWS and check that "
                            "neither listed order remains."
                        ),
                    ),
                    Div(
                        *(
                            Div(
                                P("Tranche ID · OCA group", cls="text-xs text-muted-foreground"),
                                P(
                                    _journal_oca_group(entry, index),
                                    cls="mt-1 break-all text-sm",
                                ),
                                Div(
                                    Span("LMT target", cls="text-muted-foreground"),
                                    Span(
                                        f"{layer.target_price} · {layer.quantity} contracts",
                                    ),
                                    cls="mt-3 flex justify-between gap-3 text-sm",
                                ),
                                Div(
                                    Span("STP loss", cls="text-muted-foreground"),
                                    Span(
                                        f"{layer.stop_price} · {layer.quantity} contracts",
                                    ),
                                    cls="mt-2 flex justify-between gap-3 text-sm",
                                ),
                                cls="rounded-md border border-border p-4",
                            )
                            for index, layer in enumerate(entry.layers)
                        ),
                        cls="grid max-h-[40vh] gap-3 overflow-y-auto",
                    ),
                    P(
                        self._message,
                        role="alert",
                        cls="text-sm text-destructive",
                    )
                    if self._message.startswith("Cancellation verification blocked:")
                    else None,
                    Form(
                        Label(
                            HTMLInput(
                                type="checkbox",
                                name="confirmed",
                                value="yes",
                                required=True,
                            ),
                            "I confirmed the listed LMT and STP orders are gone in TWS",
                            cls="flex items-center gap-2 text-sm",
                        ),
                        DialogFooter(
                            Button(
                                "Verify cancellation",
                                type="submit",
                                data_busy_text="Verifying…",
                            ),
                            cls="mt-6",
                        ),
                        HTMLInput(
                            type="hidden", name="action", value="resolve-cancelled-bracket"
                        ),
                        HTMLInput(
                            type="hidden", name="fingerprint", value=fingerprint
                        ),
                        action=f"/{self.session_token}/action",
                        method="post",
                        data_cancelled_bracket_recovery=True,
                    ),
                    show_close_button=False,
                ),
                signal="cancelled_bracket_recovery",
                default_open=True,
                dismissible=False,
                size="md",
            ),
            data_cancelled_bracket_recovery_dialog=True,
        )

    def _header_quantity(
        self,
        outcomes: tuple[tuple[JournalEntry, int, LayerOutcome], ...],
        snapshot: BrokerSnapshot | None,
    ) -> str:
        if snapshot is None:
            return "—"
        held = snapshot.position.quantity
        sold = Decimal("0")
        for _entry, _index, outcome in outcomes:
            if outcome.status in {"PARTIAL", "UNKNOWN", "NO_EXECUTION_EVIDENCE", "GROUP_COLLISION"}:
                return f"{held:g} / —"
            if outcome.status.startswith("CLOSED_"):
                sold += outcome.filled_quantity
        return f"{held:g} / {held + sold:g}"

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

    def _set_stops_dialog(
        self, pairs: tuple[tuple[str, Any, Any], ...], basis: Decimal,
        quote: Any, snapshot_fresh: bool,
    ) -> Any:
        current_prices = {stop.stop_price for _group, _target, stop in pairs}
        initial_price = next(iter(current_prices)) if len(current_prices) == 1 else None
        initial_return = (
            _price_percentage(initial_price, basis, target=True)
            if initial_price is not None else ""
        )
        live_ask = (
            quote.ask
            if snapshot_fresh
            and quote is not None
            and quote.fresh
            and quote.market_data_type == "LIVE"
            and quote.ask is not None
            and quote.ask.is_finite()
            and quote.ask > 0
            else None
        )
        ask_text = (
            f"${live_ask:,.{max(2, -live_ask.normalize().as_tuple().exponent)}f}"
            if live_ask is not None else "Unavailable"
        )
        return Tooltip(
            TooltipTrigger(
                Dialog(
                    DialogTrigger(
                        Icon("lucide:arrow-up", cls="size-4", aria_hidden="true"),
                        variant="outline", size="icon", aria_label="Set all active stops",
                        disabled=self._paper_execution is None or bool(self._armed_price_updates),
                    ),
                    DialogContent(
                        DialogHeader(
                            DialogTitle("Set all active stops"),
                            DialogDescription(
                                "Enter a stop price or return percentage to be applied to all active layers."
                            ),
                        ),
                        Div(
                            Div(
                                Label("Stop price", fr="all-stop-value", data_stop_input_label=True, cls="text-xs font-medium text-muted-foreground"),
                                Span(f"{initial_return}% from entry" if initial_return else "—", data_stop_dialog_inverse=True, aria_live="polite", cls="text-xs font-semibold text-foreground"),
                                cls="flex items-center justify-between gap-2",
                            ),
                            Div(
                                Button("%", type="button", variant="outline", size="icon", data_stop_mode="return", aria_label="Enter return percentage from entry", aria_pressed="false", cls="stop-mode-button"),
                                Button("$", type="button", variant="outline", size="icon", data_stop_mode="price", aria_label="Enter stop price in dollars", aria_pressed="true", cls="stop-mode-button"),
                                Input(
                                    id="all-stop-value", type="number", min="0", step="any",
                                    value=_price_text(initial_price) if initial_price is not None else "",
                                    data_stop_dialog_value=True, cls="min-w-0 flex-1",
                                ),
                                cls="mt-1 flex items-center gap-2",
                            ),
                            cls="space-y-0.5",
                        ),
                        Div(
                            *(Button(f"{pct:+d}%" if pct > 0 else f"{pct}%", type="button", variant="outline", data_stop_preset=str(pct))
                              for pct in (20, 0, -20, -25, -35)),
                            cls="flex flex-wrap gap-2",
                        ),
                        Div(
                            Div(Span("Active layers", cls="text-xs text-muted-foreground"), Span(str(len(pairs)), cls="text-sm font-semibold"), cls="flex items-center justify-between gap-4"),
                            Div(Span("Entry cost", cls="text-xs text-muted-foreground"), Span(f"${basis.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}", cls="text-sm font-semibold"), cls="flex items-center justify-between gap-4"),
                            Div(Span("Latest ask at refresh" if live_ask is not None else "Latest ask", cls="text-xs text-muted-foreground"), Span(ask_text, cls="text-sm font-semibold"), cls="flex items-center justify-between gap-4"),
                            Div(Span("Stop price", cls="text-xs text-muted-foreground"), Span("—", data_stop_dialog_summary=True, aria_live="polite", cls="text-right text-sm font-semibold"), cls="flex items-center justify-between gap-4"),
                            cls="space-y-2 rounded-md border border-border bg-muted/20 px-4 py-3",
                        ),
                        DialogFooter(
                            DialogClose("Cancel", variant="outline"),
                            Button("Apply to active layers", type="button", data_apply_all_stops=True),
                            cls="mt-4",
                        ),
                        data_stop_dialog=True,
                        data_stop_basis=format(basis, "f"),
                    ),
                    data_on_focusin=evt.stopPropagation(),
                    data_on_focusout=evt.stopPropagation(),
                ),
                delay_duration=250,
            ),
            TooltipContent("Set all active stops"),
        )

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
        ambiguous = ambiguous_oca_prefixes(entries)
        active_ids = frozenset(
            {
                order.perm_id
                for _group, target, stop in self._active_oca_pairs()
                for order in (target, stop)
            }
        )
        observed_ids = frozenset(order.perm_id for order in self._state.working_orders)
        snapshot = self._view_model.latest_snapshot()
        verified = (
            snapshot is not None
            and snapshot.complete
            and snapshot.fresh
            and snapshot.selected.con_id == self._selected_con_id
            and snapshot.selected.account == self._verified_selected_account()
        )

        def outcome_for(entry: JournalEntry, index: int) -> LayerOutcome:
            if (
                entry.state == "SUBMISSION_UNKNOWN"
                and (
                    entry.account,
                    entry.con_id,
                    entry.oca_prefix or entry.fingerprint[:12],
                ) in ambiguous
            ):
                return LayerOutcome("GROUP_COLLISION")
            outcome = classify_journal_layer(
                entry,
                index,
                active_perm_ids=active_ids,
                observed_perm_ids=observed_ids,
            )
            if outcome.status == "CANCELLED" and not verified:
                return LayerOutcome("UNKNOWN")
            return outcome

        return tuple(
            (
                entry,
                index,
                outcome_for(entry, index),
            )
            for entry in entries
            for index in range(len(entry.layers))
            if not (
                entry.layers[index].hidden_from_workspace
                and classify_journal_layer(
                    entry,
                    index,
                    active_perm_ids=active_ids,
                    observed_perm_ids=observed_ids,
                ).status == "CANCELLED"
            )
        )

    def _pending_submissions(
        self,
    ) -> tuple[tuple[JournalEntry, int, LayerOutcome], ...]:
        return tuple(
            item
            for item in self._submission_outcomes()
            if item[2].status
            in {"PENDING", "UNKNOWN", "PARTIAL", "NO_EXECUTION_EVIDENCE", "GROUP_COLLISION"}
        )

    def _planning_available_quantity(self) -> int:
        """Reserve journal-backed exits absent from the broker's order snapshot."""
        observed_ids = {order.perm_id for order in self._state.working_orders}
        unobserved = 0
        for entry, index, outcome in self._pending_submissions():
            layer = entry.layers[index]
            ids = {layer.target_perm_id, layer.stop_perm_id} - {0}
            if ids & observed_ids:
                # The broker's available quantity already reserves this leg.
                continue
            remaining = layer.quantity - outcome.filled_quantity
            if remaining != remaining.to_integral_value():
                return 0
            if remaining > 0:
                unobserved += int(remaining)
        return max(0, self._state.available_quantity - unobserved)

    def _pending_layer_row(
        self, number: int, entry: JournalEntry, index: int, outcome: LayerOutcome,
        *, read_only: bool = False,
    ) -> Any:
        layer = entry.layers[index]
        heading = {
            "PENDING": "Awaiting TWS verification",
            "UNKNOWN": "Awaiting TWS review",
            "GROUP_COLLISION": "Conflicting order group",
            "PARTIAL": "Partially filled",
            "NO_EXECUTION_EVIDENCE": "No fill evidence",
            "CONFLICT": "Fill and working order conflict",
            "CANCELLED": "Bracket cancelled",
        }[outcome.status]
        detail = {
            "PENDING": "Check TWS for Transmit or a working order, then Refresh.",
            "UNKNOWN": "Review both orders in TWS. Transmit there if held and correct, then Refresh.",
            "GROUP_COLLISION": "An older bracket reused this OCA group. Do not transmit; resolve the conflicting orders in TWS first.",
            "PARTIAL": (
                f"{format(outcome.filled_quantity, 'f')} of {layer.quantity} "
                "contracts filled. Verify the remaining order in TWS."
            ),
            "NO_EXECUTION_EVIDENCE": (
                "This layer is no longer shown as working, but TWS has not supplied "
                "a matching execution. Check the TWS trade log."
            ),
            "CONFLICT": (
                "A fill is recorded, but TWS still shows the pair as working. "
                "Inspect TWS before taking another action."
            ),
            "CANCELLED": "Both OCA legs were confirmed cancelled; the position remains open.",
        }[outcome.status]
        return Div(
            Div(
                _oca_layer_label(number, _journal_oca_group(entry, index)),
                cls="min-w-20",
            ),
            _sold_percentage_price_field(
                "LMT target",
                value=layer.target_percentage,
                price=layer.target_price,
                input_id=f"verify-target-{number}",
            ),
            _sold_percentage_price_field(
                "STP loss",
                value=layer.stop_percentage,
                price=layer.stop_price,
                input_id=f"verify-stop-{number}",
            ),
            _field(
                "Quantity",
                Input(
                    id=f"verify-quantity-{number}",
                    value=str(layer.quantity),
                    disabled=True,
                ),
                input_id=f"verify-quantity-{number}",
            ),
            _field(
                "TIF",
                Input(id=f"verify-tif-{number}", value=layer.tif, disabled=True),
                input_id=f"verify-tif-{number}",
            ),
            Button(
                Icon("lucide:trash-2", cls="size-4", aria_hidden="true"),
                variant="outline",
                size="icon",
                type="submit",
                name="action",
                value=(
                    f"dismiss-cancelled:{entry.fingerprint}:"
                    f"{entry.snapshot_captured_at}:{index}"
                ),
                aria_label=f"Remove cancelled layer {number} from view",
                title="Remove cancelled row from view",
                cls="relative z-[3] mt-5",
            )
            if outcome.status == "CANCELLED" and not read_only
            else Button(
                Icon("lucide:badge-check", cls="size-4", aria_hidden="true"),
                variant="outline",
                size="icon",
                type="submit",
                name="action",
                value=f"verify-cancelled-bracket:{entry.fingerprint}",
                aria_label=f"Verify cancellation of layer {number} in TWS",
                title="Verify that this bracket was cancelled in TWS",
                cls="relative z-[3] mt-5",
            )
            if outcome.status in {"NO_EXECUTION_EVIDENCE", "GROUP_COLLISION"} and not read_only
            else Div(cls="min-w-0"),
            Div(
                Span(
                    "CANCELLED" if outcome.status == "CANCELLED" else "RESOLVE IN TWS" if outcome.status == "GROUP_COLLISION" else "VERIFY IN TWS",
                    cls="sold-layer-status",
                ),
                Span(heading, cls="sold-layer-result"),
                cls="sold-layer-badge",
                title=detail,
            ),
            data_layer_state="cancelled" if outcome.status == "CANCELLED" else "verify",
            data_result_tone="cancelled" if outcome.status == "CANCELLED" else "verify",
            cls="sold-layer-row grid grid-cols-[5rem_minmax(10rem,1fr)_minmax(10rem,1fr)_minmax(5rem,0.6fr)_5rem_2.25rem] items-start gap-3 border-t border-border py-4",
        )

    def _closed_layer_row(
        self, number: int, entry: JournalEntry, index: int, outcome: LayerOutcome,
        *, recover_legacy: bool = True,
    ) -> Any:
        layer = entry.layers[index]
        recovered = None
        calculator = self._state.quote_calculator
        if recover_legacy and (
            not layer.target_percentage or not layer.stop_percentage
        ) and calculator is not None:
            recovered = _recover_legacy_layer_percentages(
                target_price=layer.target_price,
                stop_price=layer.stop_price,
                bands=calculator.bands,
                target_presets=_parse_presets(
                    self._target_presets, maximum=Decimal("1000")
                )
                or (),
                stop_presets=_parse_presets(self._stop_presets, maximum=Decimal("100"))
                or (),
            )
        target_percentage = layer.target_percentage or (
            recovered[0] if recovered else ""
        )
        stop_percentage = layer.stop_percentage or (recovered[1] if recovered else "")
        result = "P&L unavailable"
        if outcome.realized_pnl is not None:
            result = (
                f"{_money(outcome.realized_pnl)} USD"
                if outcome.currency == "USD"
                else f"{outcome.currency} {outcome.realized_pnl:+,.2f}"
            )
        result_tone = {
            "CLOSED_PROFIT": "profit",
            "CLOSED_LOSS": "loss",
            "CLOSED_FLAT": "flat",
            "CLOSED_PNL_UNKNOWN": "unknown",
        }[outcome.status]
        return Div(
            Div(
                _oca_layer_label(number, _journal_oca_group(entry, index)),
                P("Closed", cls="mt-2 text-xs text-muted-foreground"),
                cls="min-w-20",
            ),
            _sold_percentage_price_field(
                "LMT target",
                value=target_percentage,
                price=layer.target_price,
                input_id=f"sold-target-{number}",
                inferred=not layer.target_percentage and bool(recovered),
            ),
            _sold_percentage_price_field(
                "STP loss",
                value=stop_percentage,
                price=layer.stop_price,
                input_id=f"sold-stop-{number}",
                inferred=not layer.stop_percentage and bool(recovered),
            ),
            _field(
                "Quantity",
                Input(
                    id=f"sold-quantity-{number}",
                    value=format(outcome.filled_quantity, "f"),
                    disabled=True,
                ),
                input_id=f"sold-quantity-{number}",
            ),
            _field(
                "TIF",
                Input(id=f"sold-tif-{number}", value=layer.tif, disabled=True),
                input_id=f"sold-tif-{number}",
            ),
            Div(cls="min-w-0"),
            Div(
                Span("SOLD", cls="sold-layer-status"),
                Span(result, cls="sold-layer-result"),
                cls="sold-layer-badge",
            ),
            data_layer_state="sold",
            data_result_tone=result_tone,
            cls="sold-layer-row grid grid-cols-[5rem_minmax(10rem,1fr)_minmax(10rem,1fr)_minmax(5rem,0.6fr)_5rem_2.25rem] items-start gap-3 border-t border-border py-4",
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

    def _existing_layers_panel(
        self,
        pairs: tuple[tuple[str, Any, Any], ...],
        outcomes: tuple[tuple[JournalEntry, int, LayerOutcome], ...],
    ) -> Any:
        """Keep journal layers in submission order as their broker state changes."""
        pairs_by_target = {
            target.perm_id: (group, target, stop) for group, target, stop in pairs
        }
        used_target_ids: set[int] = set()
        rows: list[Any] = []
        working_count = 0
        for entry, index, outcome in outcomes:
            target_id = _journal_target_perm_id(entry, index)
            pair = pairs_by_target.get(target_id)
            number = len(rows) + 1
            if outcome.status == "ACTIVE" and pair is not None:
                rows.append(self._active_layer_row(number, *pair))
                used_target_ids.add(target_id)
                working_count += 1
            elif outcome.status.startswith("CLOSED_"):
                if pair is not None:
                    rows.append(
                        self._pending_layer_row(
                            number, entry, index, LayerOutcome("CONFLICT")
                        )
                    )
                    used_target_ids.add(target_id)
                else:
                    rows.append(self._closed_layer_row(number, entry, index, outcome))
            elif outcome.status in {
                "PENDING",
                "UNKNOWN",
                "PARTIAL",
                "NO_EXECUTION_EVIDENCE",
                "GROUP_COLLISION",
                "CANCELLED",
            }:
                rows.append(self._pending_layer_row(number, entry, index, outcome))
                if pair is not None:
                    used_target_ids.add(target_id)
        for pair in pairs:
            if pair[1].perm_id not in used_target_ids:
                rows.append(self._active_layer_row(len(rows) + 1, *pair))
                working_count += 1

        return Form(
            ScrollArea(
                Div(*rows, cls="oca-layer-list w-full min-w-[41rem]"),
                aria_label="Existing OCA layer rows",
                orientation="horizontal",
                cls="w-full",
            ),
            Script(_live_active_script(self._live_active_configuration()))
            if working_count
            else None,
            id="active-form",
            action=f"/{self.session_token}/action",
            method="post",
        )

    def _active_layer_row(self, index: int, group: str, target: Any, stop: Any) -> Any:
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
            target=True,
            bands=bands,
            presets=_parse_presets(self._stop_presets, maximum=Decimal("100")) or (),
        )
        staged_percentages = self._armed_active_percentages.get(target.perm_id)
        if staged_percentages is not None:
            target_percentage, stop_percentage = staged_percentages
        gain, loss = self._active_layer_projection(
            target,
            stop,
            target_price=display_target_price,
            stop_price=display_stop_price,
        )
        return _layer_row_layout(
            index=index,
            state="working",
            oca_group=group,
            target_field=_percentage_price_field(
                "LMT target",
                Input(
                    name=f"active_target_{target.perm_id}",
                    id=f"active-target-{index}",
                    type="number",
                    value=target_percentage,
                    min="0.1",
                    step="0.1",
                    disabled=bool(self._armed_price_updates),
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
                "STP return from entry",
                Input(
                    name=f"active_stop_{target.perm_id}",
                    id=f"active-stop-{index}",
                    type="number",
                    value=stop_percentage,
                    min="-99.9",
                    step="0.1",
                    disabled=bool(self._armed_price_updates),
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
                outcome_label="at stop",
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
        snapshot: BrokerSnapshot | None,
        *,
        uncertain_app_orders: bool = False,
    ) -> Any:
        held = snapshot.position.quantity if snapshot is not None else None
        available = self._state.available_quantity
        reserved = held - available if held is not None else None
        message = (
            f"{reserved:g} {'contract already has' if reserved == 1 else 'contracts already have'} exit orders in TWS. "
            f"{available:g} {'remains' if available == 1 else 'remain'} available for new brackets. "
            if reserved is not None and reserved >= 0
            else f"{available:g} {'contract remains' if available == 1 else 'contracts remain'} available for new brackets. "
        )
        if coverage in {"mixed", "external"}:
            return Alert(
                AlertTitle("Existing TWS exit orders"),
                AlertDescription(
                    message
                    + (
                        "An app submission is still unverified. Inspect TWS before "
                        "changing these orders."
                        if uncertain_app_orders
                        else "Orders placed outside this app are view-only here."
                    )
                ),
                cls="mt-5 border-amber-500/40 bg-amber-500/10 text-amber-100",
            )
        return Div(cls="hidden")

    def _submission_attention(
        self, outcomes: tuple[tuple[JournalEntry, int, LayerOutcome], ...]
    ) -> Any:
        statuses = {outcome.status for _entry, _index, outcome in outcomes}
        if "GROUP_COLLISION" in statuses:
            return Alert(
                AlertTitle("Conflicting bracket orders in TWS"),
                AlertDescription(
                    "An earlier and a newer bracket reused the same OCA group. "
                    "Do not transmit the pending orders. Inspect and resolve the "
                    "old and new orders in TWS, then Refresh here. Once every "
                    "order in the conflicting groups is gone, use Verify to clear "
                    "the uncertain submission."
                ),
                cls="mt-5 border-destructive/50 bg-destructive/10 text-foreground",
            )
        if statuses & {"PENDING", "UNKNOWN"}:
            return Alert(
                AlertTitle("Orders need a decision in TWS"),
                AlertDescription(
                    "Check each LMT and STP pair in TWS. If the orders are held "
                    "for Transmit and the prices and quantities are correct, "
                    "transmit them there, then Refresh here. If the orders are "
                    "gone, confirm that in TWS before using Verify. Do not "
                    "submit the draft again while its outcome is uncertain."
                ),
                cls="mt-5 border-amber-500/40 bg-amber-500/10 text-amber-100",
            )
        return None

    def _draft_panel(self, *, show_empty_state: bool = True) -> Any:
        layers = self._current_layers()
        return Form(
            Div(
                H3("Build your exit draft", cls="text-lg font-semibold"),
                P(
                    "Use your LMT targets to split the available contracts into layers, "
                    "then adjust prices and quantities before reviewing any order.",
                    cls="mt-2 max-w-md text-sm leading-6 text-muted-foreground",
                ),
                Div(
                    Button(
                        "Build Draft",
                        type="submit",
                        name="action",
                        value="build-draft",
                        cls="draft-build-button",
                    ),
                    Button(
                        Icon("lucide:plus", cls="size-4", aria_hidden="true"),
                        "Add Layer",
                        variant="outline",
                        type="submit",
                        name="action",
                        value="add-layer",
                    ),
                    cls="mt-5 flex flex-wrap items-center justify-center gap-3",
                ),
                data_draft_empty_state=True,
                cls="draft-empty-state",
            )
            if not layers and show_empty_state
            else Div(cls="hidden")
            if not layers
            else ScrollArea(
                Div(
                    *[
                        self._draft_layer_row(index, layer)
                        for index, layer in enumerate(layers, start=1)
                    ],
                    cls="oca-layer-list w-full min-w-[41rem]",
                ),
                aria_label="Draft layer rows",
                orientation="horizontal",
                cls="w-full",
            ),
            Button(
                type="submit",
                name="action",
                value="equal-split-available",
                id="split-all-submit",
                cls="hidden",
                tabindex="-1",
                aria_hidden="true",
            ),
            Button(
                type="submit",
                name="action",
                value="equal-split-assigned",
                id="split-assigned-submit",
                cls="hidden",
                tabindex="-1",
                aria_hidden="true",
            ),
            HTMLInput(type="hidden", name="target_presets", value=self._target_presets),
            HTMLInput(type="hidden", name="stop_presets", value=self._stop_presets),
            Script(_live_draft_script(self._live_draft_configuration())),
            id="draft-form",
            action=f"/{self.session_token}/action",
            method="post",
            cls="h-full" if not layers and show_empty_state else "",
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
            "available": self._planning_available_quantity(),
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

    def _draft_layer_row(self, index: int, layer: DraftLayerForm) -> Any:
        tif_signal = Signal(f"tif_{index}_value", _ref_only=True)
        gain, loss = self._layer_projection(layer)
        return _layer_row_layout(
            index=index,
            state="draft",
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
                    data_live_initial=layer.target_percentage,
                    data_live_original=layer.target_price,
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
                    data_live_initial=layer.stop_percentage,
                    data_live_original=layer.stop_price,
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
                Div(
                    Input(
                        name=f"quantity_{index}",
                        id=f"quantity_{index}",
                        type="number",
                        value=layer.quantity,
                        min="1",
                        max=str(self._planning_available_quantity()),
                        step="1",
                        data_live_input="quantity",
                        data_live_layer=index,
                        aria_describedby=f"quantity_share_{index}",
                        cls="pr-12",
                    ),
                    Span(
                        cls="draft-quantity-ring",
                        data_quantity_ring=index,
                        style=f"--quantity-share: {min(100, 100 * _int_or_zero(layer.quantity) / max(1, self._planning_available_quantity())):.4f}%",
                        aria_hidden="true",
                    ),
                    Span(
                        f"{layer.quantity} of {self._planning_available_quantity()} available contracts",
                        id=f"quantity_share_{index}",
                        cls="sr-only",
                        data_quantity_share=index,
                    ),
                    cls="draft-quantity-control",
                ),
                input_id=f"quantity_{index}",
            ),
            tif_field=_field(
                "TIF",
                Div(
                    Select(
                        SelectTrigger(SelectValue(), id=f"tif_{index}_trigger"),
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
                input_id=f"tif_{index}_trigger",
            ),
            action_field=Button(
                Icon("lucide:trash-2"),
                variant="outline",
                size="icon",
                type="submit",
                name="action",
                value=f"remove-layer:{index}",
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

    def _projection_state(
        self,
    ) -> tuple[PositionOutcome, PositionOutcome, dict[str, Any]]:
        """Compare observed exits with the proposed whole-position exit plan."""
        basis, multiplier = self._state.unit_basis, self._state.multiplier
        position = next(
            (
                item
                for item in self._state.positions
                if item.con_id == self._selected_con_id
            ),
            None,
        )
        try:
            held = Decimal(position.quantity) if position is not None else Decimal("0")
        except InvalidOperation:
            held = Decimal("-1")
        outcomes = self._submission_outcomes()
        snapshot = self._view_model.latest_snapshot()
        currency = snapshot.contract.currency if snapshot is not None else None
        realized = Decimal("0")
        pending_quantity = Decimal("0")
        observed: list[ExitScenario] = []
        proposed: list[ExitScenario] = []
        pending_config: list[dict[str, Any]] = []
        blocking_codes = {
            validation.code
            for validation in self._state.validations
            if validation.blocking
        }
        projection_status_usable = self._state.status is UiStatus.READY or (
            self._state.status is UiStatus.BLOCKED
            and blocking_codes == {"POSITION_FULLY_ALLOCATED"}
        )
        unresolved = (
            not projection_status_usable
            or position is None
            or snapshot is None
            or not snapshot.connected
            or not snapshot.complete
            or not snapshot.fresh
            or not snapshot.paper_account_verified
            or snapshot.connection_epoch <= 0
            or bool(snapshot.errors)
            or snapshot.selected.con_id != self._selected_con_id
            or snapshot.position.quantity != held
        )
        for _entry, _index, outcome in outcomes:
            if outcome.status.startswith("CLOSED_"):
                if outcome.realized_pnl is None or outcome.currency != currency:
                    unresolved = True
                else:
                    realized += outcome.realized_pnl
            elif outcome.status == "PENDING":
                layer = _entry.layers[_index]
                quantity = Decimal(layer.quantity)
                pending_quantity += quantity
                if basis is None or multiplier is None or quantity <= 0:
                    unresolved = True
                    continue
                try:
                    target_price = Decimal(layer.target_price)
                    stop_price = Decimal(layer.stop_price)
                except InvalidOperation:
                    unresolved = True
                    continue
                if (
                    not target_price.is_finite()
                    or not stop_price.is_finite()
                    or target_price <= 0
                    or stop_price <= 0
                ):
                    unresolved = True
                    continue
                scenario = ExitScenario(
                    quantity,
                    (target_price - basis) * multiplier * quantity,
                    (stop_price - basis) * multiplier * quantity,
                )
                observed.append(scenario)
                proposed.append(scenario)
                pending_config.append(
                    {
                        "quantity": format(quantity, "f"),
                        "gain": format(scenario.target_pnl, "f"),
                        "loss": format(scenario.stop_pnl, "f"),
                    }
                )
            elif outcome.status != "ACTIVE":
                unresolved = True
        active_config: list[dict[str, Any]] = []
        removed_ids = {
            candidate.target_perm_id for candidate in self._armed_market_exits
        }
        if self._armed_market_exit is not None:
            removed_ids.add(self._armed_market_exit.target_perm_id)
        if self._armed_cancellation is not None:
            removed_ids.add(self._armed_cancellation.target_perm_id)
        removed_ids.update(
            candidate.target_perm_id for candidate in self._armed_cancellations
        )
        updates = {
            update.layer.target_perm_id: update for update in self._armed_price_updates
        }
        for _group, target, stop in self._active_oca_pairs():
            try:
                quantity = Decimal(target.remaining)
                stop_quantity = Decimal(stop.remaining)
            except InvalidOperation:
                unresolved = True
                continue
            if quantity <= 0 or quantity != stop_quantity:
                unresolved = True
                continue
            if (
                basis is None
                or multiplier is None
                or target.limit_price is None
                or stop.stop_price is None
            ):
                unresolved = True
                continue
            current = ExitScenario(
                quantity,
                (target.limit_price - basis) * multiplier * quantity,
                (stop.stop_price - basis) * multiplier * quantity,
            )
            observed.append(current)
            if target.perm_id in removed_ids:
                continue
            pending = self._pending_active_prices.get(target.perm_id)
            update = updates.get(target.perm_id)
            target_price = (
                update.target_price
                if update is not None and update.target_price is not None
                else pending[0]
                if pending is not None and pending[0] is not None
                else target.limit_price
            )
            stop_price = (
                update.stop_price
                if update is not None and update.stop_price is not None
                else pending[2]
                if pending is not None and pending[2] is not None
                else stop.stop_price
            )
            changed = ExitScenario(
                quantity,
                (target_price - basis) * multiplier * quantity,
                (stop_price - basis) * multiplier * quantity,
            )
            proposed.append(changed)
            active_config.append(
                {
                    "id": target.perm_id,
                    "quantity": format(quantity, "f"),
                    "gain": format(changed.target_pnl, "f"),
                    "loss": format(changed.stop_pnl, "f"),
                }
            )
        draft_config: list[dict[str, Any]] = []
        for layer in self._current_layers():
            try:
                quantity = Decimal(layer.quantity)
                target_price = Decimal(layer.target_price)
                stop_price = Decimal(layer.stop_price)
            except InvalidOperation:
                unresolved = True
                continue
            if quantity <= 0 or basis is None or multiplier is None:
                unresolved = True
                continue
            item = ExitScenario(
                quantity,
                (target_price - basis) * multiplier * quantity,
                (stop_price - basis) * multiplier * quantity,
            )
            proposed.append(item)
            observed.append(item)
            draft_config.append(
                {
                    "quantity": format(quantity, "f"),
                    "gain": format(item.target_pnl, "f"),
                    "loss": format(item.stop_pnl, "f"),
                }
            )
        if basis is None or multiplier is None or self._pending_active_prices:
            unresolved = True
        if sum((item.quantity for item in proposed), Decimal("0")) > held:
            unresolved = True
        baseline = project_position_outcome(
            held_quantity=held,
            realized_pnl=realized,
            exits=tuple(observed),
            unresolved=unresolved,
        )
        market_exit = bool(self._armed_market_exits or self._armed_market_exit)
        proposed_outcome = project_position_outcome(
            held_quantity=held,
            realized_pnl=realized,
            exits=tuple(proposed),
            unresolved=unresolved or market_exit,
        )
        baseline = self._projection_comparison or baseline
        if market_exit:
            # A staged market sell has no fill price. Keep the last verified
            # layer projection visible for review; execution never uses it.
            proposed_outcome = baseline
        return (
            baseline,
            proposed_outcome,
            {
                "held": format(held, "f"),
                "realized": format(realized, "f"),
                "unresolved": unresolved,
                "pendingQuantity": format(pending_quantity, "f"),
                "marketExit": market_exit,
                "removed": list(removed_ids),
                "staged": bool(removed_ids or updates or self._armed_execution),
                "active": active_config,
                "pending": pending_config,
                "draft": draft_config,
                "baselineGain": format(baseline.expected_gain, "f")
                if baseline.expected_gain is not None
                else None,
                "baselineLoss": format(baseline.max_loss, "f")
                if baseline.max_loss is not None
                else None,
                "baselineCoveredGain": format(baseline.covered_gain, "f"),
                "baselineCoveredLoss": format(baseline.covered_loss, "f"),
                "baselineCoveredQuantity": format(baseline.covered_quantity, "f"),
            },
        )

    def _outcome_projection(
        self, projection: tuple[PositionOutcome, PositionOutcome, dict[str, Any]]
    ) -> Any:
        baseline, outcome, config = projection
        partial = (
            not config["unresolved"]
            and not config["marketExit"]
            and 0 < outcome.covered_quantity < outcome.held_quantity
        )
        estimate = (
            config["unresolved"]
            and not config["marketExit"]
            and outcome.covered_quantity > 0
            and outcome.covered_quantity <= outcome.held_quantity
        )
        gain_value = outcome.covered_gain if partial or estimate else outcome.expected_gain
        loss_value = outcome.covered_loss if partial or estimate else outcome.max_loss
        baseline_gain = baseline.covered_gain if partial else baseline.expected_gain
        baseline_loss = baseline.covered_loss if partial else baseline.max_loss
        if estimate:
            baseline_gain = None
            baseline_loss = None
        if partial and baseline.covered_quantity != outcome.covered_quantity:
            baseline_gain = None
            baseline_loss = None
        gain_delta = (
            gain_value - baseline_gain
            if gain_value is not None and baseline_gain is not None
            else None
        )
        loss_delta = (
            loss_value - baseline_loss
            if loss_value is not None and baseline_loss is not None
            else None
        )
        status = _projection_status(
            outcome, config["unresolved"], config["marketExit"], estimate,
        )
        return Card(
            CardHeader(
                CardTitle("Outcome projection", cls="text-sm"),
                cls="px-4",
            ),
            CardContent(
                Div(
                    *_metric(
                        "Estimated gain" if estimate else "Expected gain",
                        _projection_gain_value(gain_value, gain_delta),
                        "text-emerald-400",
                        live_key="gain",
                        help_text=(
                            "Realised P&L plus projected target results from shown "
                            "layers. Pending bracket prices assume TWS accepts "
                            "the submitted exits; other contracts are excluded."
                            if Decimal(config["pendingQuantity"]) else
                            "Realised P&L plus projected gains from the shown layers. "
                            "Excludes contracts without a verified target and stop."
                            if partial else
                            "Realised P&L plus projected gains from the current layer plan."
                        ),
                    ),
                    *_metric(
                        "Estimated stop result" if estimate else "Max loss",
                        _projection_loss_value(loss_value, loss_delta),
                        "text-rose-400",
                        live_key="loss",
                        help_text=(
                            "Projected stop results from shown layers; excludes "
                            "realised P&L. Pending stops may not be working in TWS."
                            if Decimal(config["pendingQuantity"]) else
                            "Projected result at the shown layer stops; excludes "
                            "realised P&L and contracts without a verified target and stop."
                            if partial else
                            "Projected losses at the current layer stops; excludes realised P&L."
                        ),
                    ),
                    cls="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] items-baseline gap-x-3 gap-y-3",
                ),
                P(
                    status,
                    data_projection_status=True,
                    cls="mt-3 text-xs leading-5 text-muted-foreground"
                    + (" hidden" if not status else ""),
                ),
                cls="px-4",
            ),
            data_outcome_projection=True,
            cls="gap-4 rounded-2xl py-4 shadow-none",
        )

    def _review(
        self, projection: tuple[PositionOutcome, PositionOutcome, dict[str, Any]]
    ) -> Any:
        pending = self._pending_submissions()
        market_exits = self._armed_market_exits or (
            (self._armed_market_exit,) if self._armed_market_exit is not None else ()
        )
        cancellation = self._armed_cancellation
        cancellations = self._armed_cancellations
        price_updates = self._armed_price_updates
        armed_execution = self._armed_execution
        action_rows: list[Any] = []
        if market_exits:
            action_rows = self._review_market_exit_plan(market_exits)
        elif cancellation is not None:
            action_rows = [self._review_cancellation_plan(cancellation)]
        elif cancellations:
            action_rows = [
                self._review_cancellation_plan(candidate, index=index)
                for index, candidate in enumerate(cancellations, start=1)
            ]
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
        draft_allowed = not pending or all(
            outcome.status == "PENDING" for _entry, _index, outcome in pending
        )
        draft_rows = (
            [
                self._review_pair(index, layer)
                for index, layer in enumerate(self._current_layers(), start=1)
            ]
            if draft_allowed
            else []
        )
        has_active_layers = bool(self._active_oca_pairs())
        has_staged_action = bool(action_rows)
        review_badge = (
            Badge("MKT EXIT", variant="outline", cls="text-[10px]")
            if market_exits
            else Badge("CANCEL", variant="outline", cls="text-[10px]")
            if cancellation is not None or cancellations
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
                    "NEXT STEP",
                    variant="outline",
                    data_active_review_badge=True,
                    cls="text-[10px]" if not draft_rows else "hidden text-[10px]",
                ),
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
                    Div(
                        *draft_rows,
                        data_draft_review=True,
                        data_has_draft_rows="true" if draft_rows else "false",
                        cls="min-h-0" if draft_rows else "hidden min-h-0",
                    ),
                    self._live_active_review(hidden=bool(draft_rows))
                    if has_active_layers
                    else None,
                    cls="flex min-h-full flex-col",
                ),
                aria_label="Planned order actions",
                cls="min-h-0 flex-1 px-4",
            )
            if draft_rows or has_active_layers
            else self._new_layer_review_empty(overlay=False)
            if draft_allowed and self._planning_available_quantity() > 0
            else Div(
                P(
                    "Add a layer or modify an existing one to continue.",
                    cls="text-center text-sm leading-6 text-muted-foreground",
                ),
                cls="flex min-h-0 flex-1 items-center justify-center px-6",
            ),
            Div(self._draft_quantity_alert(), cls="mx-4 mb-3 min-w-0")
            if draft_rows and not has_staged_action
            else None,
            Div(self._outcome_projection(projection), cls="mx-4 mb-3"),
            self._execution_control(),
            cls="flex min-h-0 flex-col overflow-hidden border-l border-border bg-card/30",
        )

    def _draft_quantity_alert(self) -> Any:
        available = self._planning_available_quantity()
        drafted = sum(_int_or_zero(layer.quantity) for layer in self._current_layers())
        return Alert(
            Icon("lucide:circle-alert"),
            AlertTitle("Draft exceeds available contracts"),
            AlertDescription(
                f"{drafted} contracts drafted; {available} available. "
                "Reduce a layer's quantity.",
                data_draft_quantity_message=True,
            ),
            variant="destructive",
            data_draft_quantity_alert=True,
            live=True,
            cls=(
                "min-w-0 break-words border-destructive/70 "
                "bg-red-950 text-red-50 [&_p]:text-red-100/90"
                + ("" if drafted > available else " hidden")
            ),
        )

    def _execution_control(self) -> Any:
        if self._pending_submissions():
            return Div(
                Button(
                    "Execute paper order",
                    variant="default",
                    type="button",
                    disabled=True,
                    cls="w-full",
                ),
                cls="mx-4 mb-4 w-[calc(100%-2rem)]",
            )
        market_exits = self._armed_market_exits or (
            (self._armed_market_exit,) if self._armed_market_exit is not None else ()
        )
        if (self._armed_cancellation is not None or self._armed_cancellations or market_exits) and not self._active_action_verified:
            return self._reviewed_active_action_controls()
        if self._armed_cancellations:
            return self._staged_action_controls(
                confirm_action="cancel-all-confirm",
                busy_text="Cancelling…",
                impact=(
                    "All active brackets will close",
                    (
                        "Confirm requests cancellation of every reviewed active OCA bracket. "
                        "The position remains open without those brackets' protection.",
                    ),
                ),
            )
        if self._armed_cancellation is not None:
            return self._staged_action_controls(
                confirm_action="cancel-pair-confirm",
                busy_text="Cancelling…",
                impact=(
                    "Bracket will close",
                    (
                        "Confirm requests cancellation of both working orders in this "
                        "OCA bracket. If both cancel, the position stays open without "
                        "this bracket's protection.",
                    ),
                ),
            )
        if market_exits:
            return self._staged_action_controls(
                confirm_action="market-exit-confirm",
                impact=(
                    "Sell and close bracket",
                    (
                        "Confirm requests cancellation of the selected OCA bracket. "
                        "Only after both legs are confirmed cancelled does the app "
                        "submit a SELL MKT for the remaining contracts. The fill "
                        "price is not guaranteed.",
                    ),
                ),
            )
        if self._armed_price_updates:
            impact = _price_update_impact(
                self._view_model.latest_snapshot(), self._armed_price_updates
            )
            return self._staged_action_controls(
                confirm_action="price-update-confirm",
                retry_acknowledgement=self._price_update_retry_required,
                impact=(impact.title, impact.details),
            )
        if self._armed_execution is not None:
            return self._staged_action_controls(
                confirm_action="execute-confirm",
            )
        has_active_layers = bool(self._active_oca_pairs())
        has_draft_layers = bool(self._current_layers())
        available = self._planning_available_quantity()
        drafted = sum(_int_or_zero(layer.quantity) for layer in self._current_layers())
        can_execute_draft = (
            self._paper_execution is not None
            and available > 0
            and has_draft_layers
        )
        return Div(
            self._cancel_changes_control(staged=False)
            if has_active_layers
            else None,
            Div(
                Button(
                    "Execute paper order",
                    variant="default",
                    type="submit",
                    name="action",
                    value="execute-arm",
                    form="draft-form",
                    data_busy_text="Checking…",
                    data_execute_enabled=str(can_execute_draft).lower(),
                    disabled=not can_execute_draft or not 0 < drafted <= available,
                    cls="w-full",
                ),
                data_draft_execute=True,
                cls=(
                    "hidden w-full"
                    if has_active_layers and not has_draft_layers
                    else "w-full"
                ),
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
                cls="hidden w-full" if has_draft_layers else "w-full",
            )
            if has_active_layers
            else None,
            cls="mx-4 mb-4 flex w-[calc(100%-2rem)] flex-col",
        )

    def _cancel_changes_control(self, *, staged: bool) -> Any:
        """Share the cancel slot across local edits and staged paper actions."""
        button_options = (
            {"name": "action", "value": "cancel-staged"}
            if staged
            else {"data_reset_active_prices": True, "disabled": True}
        )
        slot_options = (
            {"data_staged_cancel": True, "data_reset_visible": "true"}
            if staged
            else {
                "data_price_edit_reset": True,
                "data_reset_visible": "false",
                "aria_hidden": "true",
            }
        )
        return Div(
            Button(
                "Cancel changes",
                variant="outline",
                size="sm",
                type="submit" if staged else "button",
                cls="w-full",
                **button_options,
            ),
            cls="price-edit-reset w-full",
            **slot_options,
        )

    def _reviewed_active_action_controls(self) -> Any:
        """Keep review separate from the fresh check and final confirmation."""
        return Form(
            self._cancel_changes_control(staged=True),
            Div(
                Button(
                    "Execute paper order",
                    variant="default",
                    type="submit",
                    name="action",
                    value="active-action-execute",
                    data_busy_text="Checking…",
                    cls="w-full",
                ),
                cls="w-full",
            ),
            action=f"/{self.session_token}/action",
            method="post",
            cls="mx-4 mb-4 flex w-[calc(100%-2rem)] flex-col",
        )

    def _staged_action_controls(
        self,
        *,
        confirm_action: str,
        busy_text: str = "Submitting…",
        retry_acknowledgement: bool = False,
        impact: tuple[str, tuple[str, ...]] | None = None,
    ) -> Any:
        """One deliberate Cancel / Confirm bar for every staged order change."""
        return Form(
            Alert(
                AlertTitle(impact[0]),
                AlertDescription(
                    *(P(detail) for detail in impact[1]),
                    cls="space-y-1",
                ),
                cls="mb-3 border-amber-500/40 bg-amber-500/10 text-amber-100",
            )
            if impact
            else None,
            Label(
                HTMLInput(
                    type="checkbox",
                    name="ack_unknown_price_update",
                    value="on",
                    cls="mr-2 align-middle",
                ),
                "I checked TWS: the order still shows the old price and no amendment is waiting for Transmit.",
                cls="mb-3 block text-xs leading-5 text-amber-300",
            )
            if retry_acknowledgement
            else None,
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
                    variant="destructive",
                    type="submit",
                    name="action",
                    value=confirm_action,
                    data_busy_text=busy_text,
                    data_paper_confirm=True,
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
            self._new_layer_review_empty()
            if self._planning_available_quantity() > 0
            else Div(
                Div(
                    Icon("lucide:equal", cls="size-7", aria_hidden="true"),
                    cls="mb-5 flex size-14 items-center justify-center rounded-2xl border border-border bg-muted/40 text-foreground",
                ),
                H3(
                    "Ready to adjust a price?",
                    cls="text-base font-semibold tracking-tight text-foreground",
                ),
                P(
                    "Change a target or stop in an active layer to preview the update here.",
                    cls="mt-2 max-w-64 text-center text-sm leading-6 text-muted-foreground",
                ),
                Button(
                    "Edit active prices",
                    Icon("lucide:arrow-right", cls="size-4", aria_hidden="true"),
                    variant="outline",
                    type="button",
                    data_edit_active_prices=True,
                    cls="mt-5 gap-2",
                ),
                data_active_review_empty=True,
                cls="absolute inset-0 flex flex-col items-center justify-center px-4 py-8 text-center",
            ),
            *rows,
            data_active_review=True,
            cls="hidden min-h-full flex-1 flex-col" if hidden else "flex min-h-full flex-1 flex-col",
        )

    def _new_layer_review_empty(self, *, overlay: bool = True) -> Any:
        available = self._planning_available_quantity()
        return Div(
            Div(
                Icon("lucide:plus", cls="size-7", aria_hidden="true"),
                cls="mb-5 flex size-14 items-center justify-center rounded-2xl border border-border bg-muted/40 text-foreground",
            ),
            H3(
                "Contracts still need protection",
                cls="text-base font-semibold tracking-tight text-foreground",
            ),
            P(
                f"{available} {'contract is' if available == 1 else 'contracts are'} available for a new exit layer.",
                cls="mt-2 max-w-64 text-center text-sm leading-6 text-muted-foreground",
            ),
            Button(
                "Add a layer",
                Icon("lucide:arrow-right", cls="size-4", aria_hidden="true"),
                variant="outline",
                type="submit",
                form="draft-form",
                name="action",
                value="add-layer",
                cls="mt-5 gap-2",
            ),
            data_active_review_empty=True,
            cls=(
                "absolute inset-0 flex flex-col items-center justify-center px-4 py-8 text-center"
                if overlay
                else "flex min-h-0 flex-1 flex-col items-center justify-center px-4 py-8 text-center"
            ),
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
                        _sell_price_with_return(
                            layer.target_price, self._state.unit_basis
                        ),
                        data_live_review_price=f"target-{index}",
                        aria_live="polite",
                        cls="text-sm font-semibold text-emerald-400",
                    ),
                    "text-emerald-400",
                ),
                self._review_order_line(
                    "SELL STP",
                    Span(
                        _sell_price_with_return(
                            layer.stop_price, self._state.unit_basis
                        ),
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

    def _review_cancellation_plan(
        self, candidate: MarketExitCandidate, *, index: int = 1
    ) -> Any:
        """Show the complete pair that will be removed, with no replacement leg."""
        return self._review_oca_pair(
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
    const sellPriceText = (number, display = priceText(number)) => `${{display}} (${{((number / basis - 1) * 100) >= 0 ? '+' : ''}}${{((number / basis - 1) * 100).toFixed(1)}}%)`;
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
      let invalid = false;
      let assignedQuantity = 0;
      let quantitiesValid = true;
      form.querySelectorAll('[data-live-input="target"]').forEach((input) => {{
        const index = input.dataset.liveLayer;
        const stopInput = form.elements[`stop_${{index}}`];
        const target = value('target', index), stop = value('stop', index);
        const quantity = value('quantity', index);
        const quantityValid = Number.isInteger(quantity) && quantity >= 1 && quantity <= config.available;
        const ring = form.querySelector(`[data-quantity-ring="${{index}}"]`);
        const share = form.querySelector(`[data-quantity-share="${{index}}"]`);
        if (ring) ring.style.setProperty('--quantity-share', `${{quantityValid ? Math.min(100, quantity / config.available * 100) : 0}}%`);
        if (share) share.textContent = quantityValid ? `${{quantity}} of ${{config.available}} available contracts (${{Math.round(quantity / config.available * 100)}}%)` : `Enter 1 to ${{config.available}} available contracts`;
        if (Number.isInteger(quantity) && quantity > 0) assignedQuantity += quantity;
        if (!quantityValid) quantitiesValid = false;
        const valid = Number.isFinite(target) && target > 0 && Number.isFinite(stop) && stop > 0 && stop <= 100 && quantityValid;
        const targetPrice = valid ? (target === Number(input.dataset.liveInitial) ? Number(input.dataset.liveOriginal) : roundUp(basis * (1 + target / 100))) : NaN;
        const stopPrice = valid ? (stop === Number(stopInput?.dataset.liveInitial) ? Number(stopInput?.dataset.liveOriginal) : roundUp(basis * (1 - stop / 100))) : NaN;
        const gain = valid && Number.isFinite(targetPrice) ? (targetPrice - basis) * multiplier * quantity : NaN;
        const loss = valid && Number.isFinite(stopPrice) ? (stopPrice - basis) * multiplier * quantity : NaN;
        assigned(`[data-live-price="target-${{index}}"]`, Number.isFinite(targetPrice) ? (target === Number(input.dataset.liveInitial) ? `$${{input.dataset.liveOriginal}}` : priceText(targetPrice)) : '—');
        assigned(`[data-live-price="stop-${{index}}"]`, Number.isFinite(stopPrice) ? (stop === Number(stopInput?.dataset.liveInitial) ? `$${{stopInput.dataset.liveOriginal}}` : priceText(stopPrice)) : '—');
        assigned(`[data-live-review-price="target-${{index}}"]`, Number.isFinite(targetPrice) ? sellPriceText(targetPrice, target === Number(input.dataset.liveInitial) ? `$${{input.dataset.liveOriginal}}` : priceText(targetPrice)) : '—');
        assigned(`[data-live-review-price="stop-${{index}}"]`, Number.isFinite(stopPrice) ? sellPriceText(stopPrice, stop === Number(stopInput?.dataset.liveInitial) ? `$${{stopInput.dataset.liveOriginal}}` : priceText(stopPrice)) : '—');
        assigned(`[data-live-outcome="target-${{index}}"]`, Number.isFinite(gain) ? `${{money(gain)}} gain` : '— gain');
        assigned(`[data-live-outcome="stop-${{index}}"]`, Number.isFinite(loss) ? `${{money(loss)}} max loss` : '— max loss');
        assigned(`[data-live-review-quantity="${{index}}"]`, `${{quantityValid ? quantity : '—'}} contracts · ${{form.elements[`tif_${{index}}`]?.value || 'GTC'}}`);
        if (Number.isFinite(gain) && Number.isFinite(loss)) outcomes.push({{ quantity, gain, loss }});
        else invalid = true;
      }});
      const over = assignedQuantity > config.available;
      const quantityAlert = document.querySelector('[data-draft-quantity-alert]');
      if (quantityAlert) {{
        quantityAlert.classList.toggle('hidden', !over);
        if (over) quantityAlert.querySelector('[data-draft-quantity-message]').textContent = `${{assignedQuantity}} contracts drafted; ${{config.available}} available. Reduce a layer's quantity.`;
      }}
      const executeButton = document.querySelector('[data-draft-execute] [data-execute-enabled]');
      if (executeButton) executeButton.disabled = executeButton.dataset.executeEnabled !== 'true' || assignedQuantity <= 0 || over || !quantitiesValid;
      invalid = invalid || !quantitiesValid || over;
      window.ibkrProjection?.updateDraft(outcomes, invalid);
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
      const hasDraftRows = document.querySelector('[data-draft-review]')?.dataset.hasDraftRows === 'true';
      const showActive = active || !hasDraftRows;
      document.querySelectorAll('[data-draft-review], [data-draft-review-badge], [data-draft-execute]').forEach((node) => {{
        node.classList.toggle('hidden', showActive);
      }});
      document.querySelectorAll('[data-active-review], [data-active-review-badge], [data-active-execute-control]').forEach((node) => {{
        node.classList.toggle('hidden', !showActive);
      }});
      document.querySelectorAll('[data-active-review-badge]').forEach((badge) => {{
        badge.textContent = active ? 'PRICE UPDATE' : 'NEXT STEP';
      }});
      document.querySelector('[data-active-review]')?.classList.toggle('flex', showActive);
    }};
    const update = () => {{
      let changed = false, edited = false;
      const outcomes = [];
      let invalid = false;
      form.querySelectorAll('[data-active-input="target"]').forEach((targetInput) => {{
        const permId = targetInput.dataset.activePermId;
        const stopInput = form.querySelector(`[data-active-input="stop"][data-active-perm-id="${{permId}}"]`);
        const index = targetInput.dataset.liveLayer;
        const target = Number(targetInput.value), stop = Number(stopInput?.value);
        const quantity = Number(form.querySelector(`[data-active-quantity="${{permId}}"]`)?.value);
        const targetPrice = Number.isFinite(target) && target > 0 ? (target === Number(targetInput.dataset.activeInitial) ? Number(targetInput.dataset.activeOriginal) : roundUp(basis * (1 + target / 100))) : NaN;
        const stopPrice = Number.isFinite(stop) && stop > -100 ? (stop === Number(stopInput?.dataset.activeInitial) ? Number(stopInput?.dataset.activeOriginal) : roundUp(basis * (1 + stop / 100))) : NaN;
        const gain = Number.isFinite(targetPrice) && Number.isFinite(quantity) ? (targetPrice - basis) * multiplier * quantity : NaN;
        const loss = Number.isFinite(stopPrice) && Number.isFinite(quantity) ? (stopPrice - basis) * multiplier * quantity : NaN;
        if (Number.isFinite(gain) && Number.isFinite(loss) && quantity > 0) outcomes.push({{ id: Number(permId), quantity, gain, loss }});
        else invalid = true;
        assigned(`[data-live-price="active-target-${{index}}"]`, Number.isFinite(targetPrice) ? (target === Number(targetInput.dataset.activeInitial) ? `$${{targetInput.dataset.activeOriginal}}` : priceText(targetPrice)) : '—');
        assigned(`[data-live-price="active-stop-${{index}}"]`, Number.isFinite(stopPrice) ? (stop === Number(stopInput?.dataset.activeInitial) ? `$${{stopInput.dataset.activeOriginal}}` : priceText(stopPrice)) : '—');
        assigned(`[data-live-outcome="active-target-${{index}}"]`, Number.isFinite(gain) ? `${{money(gain)}} gain` : '— gain');
        assigned(`[data-live-outcome="active-stop-${{index}}"]`, Number.isFinite(loss) ? `${{money(loss)}} at stop` : '— at stop');
        const originalTarget = Number(targetInput.dataset.activeOriginal);
        const originalStop = Number(stopInput?.dataset.activeOriginal);
        const targetEdited = targetInput.value.trim() !== (targetInput.dataset.activeInitial || '').trim();
        const stopEdited = stopInput?.value.trim() !== (stopInput?.dataset.activeInitial || '').trim();
        edited ||= targetEdited || stopEdited;
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
      const resetVisible = edited && !form.querySelector('[data-active-input]:disabled');
      document.querySelectorAll('[data-reset-active-prices]').forEach((button) => {{
        button.disabled = !resetVisible;
      }});
      document.querySelectorAll('[data-price-edit-reset]').forEach((slot) => {{
        slot.dataset.resetVisible = String(resetVisible);
        slot.setAttribute('aria-hidden', String(!resetVisible));
      }});
      setReviewMode(changed);
      window.ibkrProjection?.updateActive(outcomes, invalid);
    }};
    form.querySelectorAll('[data-active-input]').forEach((input) => input.addEventListener('input', update));
    document.querySelectorAll('[data-edit-active-prices]').forEach((button) => button.addEventListener('click', () => {{
      const firstPrice = form.querySelector('[data-active-input]:not(:disabled)');
      if (!firstPrice) return;
      firstPrice.scrollIntoView({{block: 'center', behavior: 'smooth'}});
      firstPrice.focus({{preventScroll: true}});
    }}));
    form.querySelectorAll('[data-active-input]').forEach((input) => input.addEventListener('change', update));
    document.querySelectorAll('[data-move-stops-to-be]').forEach((button) => button.addEventListener('click', () => {{
      form.querySelectorAll('[data-active-input="stop"]').forEach((input) => {{ input.value = '0'; }});
      update();
    }}));
    const stopDialog = document.querySelector('[data-stop-dialog]');
    if (stopDialog) {{
      const valueInput = stopDialog.querySelector('[data-stop-dialog-value]');
      const inputLabel = stopDialog.querySelector('[data-stop-input-label]');
      const inverse = stopDialog.querySelector('[data-stop-dialog-inverse]');
      const summary = stopDialog.querySelector('[data-stop-dialog-summary]');
      const apply = stopDialog.querySelector('[data-apply-all-stops]');
      const dialogPriceText = (number) => `$${{number.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 6}})}}`;
      let mode = 'price';
      const showMode = () => {{
        inputLabel.textContent = mode === 'price' ? 'Stop price' : 'Return from entry';
        valueInput.min = mode === 'price' ? '0' : '-99.999999';
        stopDialog.querySelectorAll('[data-stop-mode]').forEach((button) => {{
          button.setAttribute('aria-pressed', String(button.dataset.stopMode === mode));
        }});
      }};
      const previewStop = () => {{
        const raw = valueInput.value;
        const number = Number(raw);
        const proposed = mode === 'price' ? number : basis * (1 + number / 100);
        const rounded = raw.trim() && Number.isFinite(proposed) && proposed > 0 ? roundUp(proposed) : NaN;
        const rate = Number.isFinite(rounded) ? (rounded / basis - 1) * 100 : NaN;
        inverse.textContent = Number.isFinite(rounded)
          ? (mode === 'price'
            ? `${{rate >= 0 ? '+' : ''}}${{Number(rate.toFixed(2))}}% from entry`
            : dialogPriceText(rounded))
          : '—';
        summary.textContent = Number.isFinite(rounded)
          ? `${{dialogPriceText(rounded)}} (${{rate >= 0 ? '+' : ''}}${{Number(rate.toFixed(2))}}%)`
          : '—';
        apply.disabled = !Number.isFinite(rounded);
        return {{ rounded, rate }};
      }};
      valueInput.addEventListener('input', previewStop);
      valueInput.addEventListener('change', () => {{
        const value = previewStop();
        if (mode === 'price' && Number.isFinite(value.rounded)) {{
          valueInput.value = String(Number(value.rounded.toFixed(6)));
          previewStop();
        }}
      }});
      stopDialog.querySelectorAll('[data-stop-mode]').forEach((button) => button.addEventListener('click', () => {{
        const value = previewStop();
        if (Number.isFinite(value.rounded)) {{
          valueInput.value = button.dataset.stopMode === 'price'
            ? String(Number(value.rounded.toFixed(6)))
            : String(Number(value.rate.toFixed(4)));
        }}
        mode = button.dataset.stopMode;
        showMode();
        previewStop();
        valueInput.focus();
      }}));
      stopDialog.querySelectorAll('[data-stop-preset]').forEach((button) => button.addEventListener('click', () => {{
        mode = 'return';
        valueInput.value = button.dataset.stopPreset;
        showMode();
        previewStop();
      }}));
      apply.addEventListener('click', () => {{
        const value = previewStop();
        if (!Number.isFinite(value.rounded)) return;
        const rate = (value.rounded / basis - 1) * 100;
        form.querySelectorAll('[data-active-input="stop"]').forEach((input) => {{
          input.value = String(Math.floor(rate * 1e8) / 1e8);
        }});
        update();
        stopDialog.closest('dialog')?.close();
      }});
      showMode();
      previewStop();
    }}
    document.querySelectorAll('[data-reset-active-prices]').forEach((button) => button.addEventListener('click', () => {{
      form.querySelectorAll('[data-active-input]').forEach((input) => {{
        input.value = input.dataset.activeInitial || '';
      }});
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


def _journal_target_perm_id(entry: JournalEntry, index: int) -> int:
    layer = entry.layers[index]
    if layer.target_perm_id:
        return layer.target_perm_id
    if len(entry.perm_ids) == len(entry.layers) * 2:
        return entry.perm_ids[index * 2]
    return 0


def _journal_oca_group(entry: JournalEntry, index: int) -> str:
    """Use the persisted OCA name, including a distinct repeat-attempt suffix."""
    return f"{entry.oca_prefix or entry.fingerprint[:12]}/tranche-{index + 1}"


def _oca_layer_label(index: int, group: str) -> Any:
    return Tooltip(
        TooltipTrigger(
            Button(
                f"LAYER {index}",
                variant="ghost",
                size="sm",
                type="button",
                aria_label=f"Layer {index}, OCA group {group}",
                cls="oca-layer-trigger",
            ),
            delay_duration=250,
        ),
        TooltipContent(group, side="right", cls="font-mono"),
        signal=f"oca_layer_{index}",
    )


def _layer_row_layout(
    *,
    index: int,
    state: str,
    target_field: Any,
    stop_field: Any,
    quantity_field: Any,
    tif_field: Any,
    action_field: Any,
    oca_group: str | None = None,
) -> Any:
    """Keep draft and active OCA rows structurally identical."""
    return Div(
        Div(
            _oca_layer_label(index, oca_group)
            if oca_group
            else Span(f"DRAFT {index}", cls="text-xs font-semibold"),
            P("Active", cls="mt-2 text-xs text-muted-foreground")
            if state == "working"
            else None,
            cls="min-w-20",
        ),
        target_field,
        stop_field,
        quantity_field,
        tif_field,
        action_field,
        data_layer_state=state,
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


def _sold_percentage_price_field(
    label: str, *, value: str, price: str, input_id: str, inferred: bool = False
) -> Any:
    """Retain the active field geometry without inventing old percentages."""
    return Div(
        Div(
            Label(label, fr=input_id, cls="text-xs font-medium text-muted-foreground"),
            Span(f"${price}", cls="text-xs font-semibold text-foreground"),
            cls="flex items-center justify-between gap-2",
        ),
        Div(
            Input(
                id=input_id,
                value=f"≈{value}" if inferred else value or "—",
                disabled=True,
                cls="pr-8",
                aria_label=(
                    f"{label} approximately {value} percent, inferred from the "
                    "recorded prices and configured presets"
                )
                if inferred
                else None,
            ),
            Span(
                "%",
                cls="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-sm text-muted-foreground",
            ),
            cls="relative mt-1",
        ),
        Div(cls="sold-field-spacer"),
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


def _recover_legacy_layer_percentages(
    *,
    target_price: str,
    stop_price: str,
    bands: tuple[Any, ...],
    target_presets: tuple[Decimal, ...],
    stop_presets: tuple[Decimal, ...],
) -> tuple[str, str] | None:
    """Recover old journal percentages only when both prices identify one pair."""
    if not bands:
        return None
    try:
        target = Decimal(target_price)
        stop = Decimal(stop_price)
        if not target.is_finite() or not stop.is_finite() or target <= 0 or stop <= 0:
            return None
        target_tick = max(
            (band for band in bands if band.low_edge <= target),
            key=lambda band: band.low_edge,
        ).increment
        stop_tick = max(
            (band for band in bands if band.low_edge <= stop),
            key=lambda band: band.low_edge,
        ).increment
        matches: list[tuple[Decimal, Decimal]] = []
        for target_pct in target_presets:
            target_factor = Decimal("1") + target_pct / Decimal("100")
            if target_factor <= 0:
                continue
            target_low = (target - target_tick) / target_factor
            target_high = target / target_factor
            for stop_pct in stop_presets:
                stop_factor = Decimal("1") - stop_pct / Decimal("100")
                if stop_factor <= 0:
                    continue
                stop_low = (stop - stop_tick) / stop_factor
                stop_high = stop / stop_factor
                lower = max(target_low, stop_low)
                upper = min(target_high, stop_high)
                if lower >= upper:
                    continue
                basis = (lower + upper) / Decimal("2")
                if (
                    round_up_price(basis * target_factor, bands) == target
                    and round_up_price(basis * stop_factor, bands) == stop
                ):
                    matches.append((target_pct, stop_pct))
    except (InvalidOperation, ValueError):
        return None
    if len(matches) != 1:
        return None
    return format(matches[0][0], "f"), format(matches[0][1], "f")


def _edited_active_price(
    working: Decimal | None,
    calculated: Decimal | None,
    entered_percentage: Decimal,
    shown_percentage: Decimal | None,
) -> Decimal | None:
    """Keep an untouched broker price even if inverse rounding differs a tick."""
    if entered_percentage == shown_percentage or calculated == working:
        return None
    return calculated


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


def _sell_price_with_return(raw_price: str, basis: Decimal | None) -> str:
    try:
        price = Decimal(raw_price)
    except InvalidOperation:
        return "—"
    if not price.is_finite() or price <= 0:
        return "—"
    amount = f"${format(price, 'f')}"
    if basis is None or basis <= 0:
        return amount
    change = (price / basis - 1) * Decimal("100")
    return f"{amount} ({change:+.1f}%)"


def _price_update_fills_verified(
    snapshot: BrokerSnapshot | None,
    updates: tuple[PriceUpdateCandidate, ...],
    prior_execution_ids: set[str],
) -> bool:
    """Confirm every amended layer exited via a new, exact broker execution."""
    if (
        snapshot is None or not snapshot.connected or not snapshot.complete
        or not snapshot.fresh or not snapshot.executions_complete
    ):
        return False
    for update in updates:
        fills = (
            fill for fill in snapshot.executions
            if fill.exec_id not in prior_execution_ids
            and fill.account == update.layer.account
            and fill.con_id == update.layer.con_id
            and fill.side.upper() in {"SLD", "SELL"}
            and fill.perm_id in {
                perm_id for perm_id, price in (
                    (update.layer.target_perm_id, update.target_price),
                    (update.layer.stop_perm_id, update.stop_price),
                ) if price is not None
            }
        )
        if sum((fill.quantity for fill in fills), Decimal("0")) != update.layer.quantity:
            return False
    return bool(updates)


def _price_update_impact(
    snapshot: BrokerSnapshot | None,
    updates: tuple[PriceUpdateCandidate, ...],
) -> _PriceUpdateImpact:
    """Describe quote proximity without treating one quote as a fill guarantee."""
    quote = snapshot.quote if snapshot is not None else None
    reliable = bool(
        snapshot is not None
        and snapshot.fresh
        and quote is not None
        and quote.fresh
        and quote.market_data_type == "LIVE"
    )
    ask = quote.ask if quote is not None else None
    bid = quote.bid if quote is not None else None
    ask = ask if ask is not None and ask.is_finite() and ask > 0 else None
    bid = bid if bid is not None and bid.is_finite() and bid > 0 else None
    concerns: set[tuple[int, str]] = set()
    details: list[str] = []
    crosses_quote = False
    for index, update in enumerate(updates, start=1):
        perm_id = update.layer.target_perm_id
        if update.stop_price is not None:
            if ask is not None and update.stop_price >= ask:
                concerns.add((perm_id, "stop-crosses-ask"))
                crosses_quote = True
                details.append(
                    f"Layer {index}: SELL STP ${_price_text(update.stop_price)} is at or above "
                    f"the {'current' if reliable else 'latest snapshot'} ask ${_price_text(ask)}."
                )
            elif ask is None or not reliable:
                concerns.add((perm_id, "stop-quote-unknown"))
        if update.target_price is not None:
            if bid is not None and update.target_price <= bid:
                concerns.add((perm_id, "limit-crosses-bid"))
                crosses_quote = True
                details.append(
                    f"Layer {index}: SELL LMT ${_price_text(update.target_price)} is at or below "
                    f"the {'current' if reliable else 'latest snapshot'} bid ${_price_text(bid)}."
                )
            elif bid is None or not reliable:
                concerns.add((perm_id, "limit-quote-unknown"))
    if crosses_quote:
        title = "Possible immediate sell"
        details.append(
            "Confirming may cause these sell orders to execute soon and close "
            "their OCA brackets. A stop does not guarantee its fill price."
        )
    elif concerns:
        title = "Immediate sell risk cannot be assessed"
        details.append(
            "A current live bid or ask is unavailable for every modified sell leg. "
            "Check TWS before confirming; a changed order may execute soon."
        )
    else:
        title = "No immediate sell indicated by quote"
        details.append(
            "The modified sell prices do not cross the latest live bid or ask. "
            "Market prices can change before TWS acknowledges the amendment."
        )
    if quote is not None and not reliable:
        details.append(
            f"Quote status: {quote.market_data_type.lower().replace('_', ' ')}; "
            "the displayed prices may not reflect the current market."
        )
    return _PriceUpdateImpact(title, tuple(details), frozenset(concerns))


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


def _contract_display_name(contract: VerifiedOptionContract) -> str:
    """Use verified contract fields for the selected position heading."""
    try:
        expiry = datetime.strptime(contract.expiry, "%Y%m%d")
    except (ValueError, TypeError):
        return contract.local_symbol
    right = {"C": "Call", "P": "Put"}.get(contract.right)
    if right is None:
        return contract.local_symbol
    strike = format(contract.strike.normalize(), "f")
    return (
        f"{contract.trading_class} {expiry.strftime('%b')}{expiry.day}'"
        f"{expiry.strftime('%y')} {strike} {right}"
    )


def _contract_header_metric(label: str, value: str) -> Any:
    return Div(
        Span(label, cls="block text-xs text-muted-foreground"),
        Span(value, cls="mt-1 block text-sm font-medium tabular-nums"),
        cls="min-w-0",
    )


def _header_status(
    label: str, icon: str, tone: str, *, title: str | None = None
) -> Any:
    icon_color = {
        "ready": "text-emerald-400",
        "warning": "text-amber-300",
        "muted": "text-muted-foreground",
        "paper": "text-cyan-400",
        "live": "text-red-400",
    }[tone]
    return Div(
        Icon(f"lucide:{icon}", cls=f"size-4 shrink-0 {icon_color}", aria_hidden="true"),
        Span(label, cls="text-xs font-medium whitespace-nowrap"),
        cls="flex shrink-0 items-center gap-1.5",
        title=title,
        data_header_status=label,
    )


def _header_price(price: Decimal | None, currency: str) -> str:
    if price is None or not price.is_finite():
        return "—"
    amount = f"{price:,.2f}"
    return f"${amount}" if currency == "USD" else f"{currency} {amount}"


def _header_pnl(value: Decimal | None, currency: str) -> str:
    if value is None or not value.is_finite():
        return "—"
    return _money(value) if currency == "USD" else f"{currency} {value:+,.2f}"


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


def _next_target_preset_above(
    prices: tuple[Decimal, ...],
    *,
    basis: Decimal,
    bands: tuple[Any, ...],
    presets: tuple[Decimal, ...],
) -> Decimal | None:
    """Choose the closest configured target above existing planned LMT prices."""
    if not prices or basis <= 0:
        return None
    highest = max(prices)
    candidates: list[tuple[Decimal, Decimal]] = []
    for percentage in presets:
        try:
            rounded = round_up_price(
                basis * (Decimal("1") + percentage / Decimal("100")), bands
            )
        except ValueError:
            return None
        if rounded > highest:
            candidates.append((rounded, percentage))
    return min(candidates)[1] if candidates else None


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


def _projection_gain_value(value: Decimal | None, delta: Decimal | None) -> Any:
    return _projection_change_value(value, delta, metric="gain")


def _projection_loss_value(value: Decimal | None, delta: Decimal | None) -> Any:
    return _projection_change_value(value, delta, metric="loss")


def _projection_change_value(
    value: Decimal | None, delta: Decimal | None, *, metric: str
) -> Any:
    changed = value is not None and delta is not None and bool(delta)
    up = changed and (delta > 0 if metric == "gain" else delta < 0)
    amount = f"${abs(delta):,.2f}" if changed else "—"
    baseline = value - delta if changed else None
    if metric == "gain":
        direction = "Expected gain increased" if up else "Expected gain decreased"
    elif changed and (value < 0 or baseline < 0):
        direction = "More loss" if up else "Less loss"
    else:
        direction = "Lower stop outcome" if up else "Higher stop outcome"
    comparison_label = (
        f"{direction} by {amount}"
        if changed
        else "No change from loaded plan"
        if value is not None and delta is not None
        else "Comparison unavailable"
    )
    return Span(
        Span(
            _money(value) if value is not None else "— Incomplete",
            cls="whitespace-nowrap",
            **{f"data_{metric}_value": True},
        ),
        Span(
            "(",
            Span(
                Span(
                    Icon("lucide:arrow-up", cls="size-3", aria_hidden="true"),
                    cls="" if up else "hidden",
                    **{f"data_{metric}_arrow": "up"},
                ),
                Span(
                    Icon("lucide:arrow-down", cls="size-3", aria_hidden="true"),
                    cls="hidden" if up or not changed else "",
                    **{f"data_{metric}_arrow": "down"},
                ),
                Span(amount, **{f"data_{metric}_amount": True}),
                cls="inline-flex items-center gap-0.5",
            ),
            ")",
            aria_label=comparison_label,
            cls="text-muted-foreground text-[11px] font-normal leading-4 whitespace-nowrap",
            **{f"data_{metric}_change": True},
        ),
        cls="inline-flex flex-col items-end",
    )


def _projection_status(
    outcome: PositionOutcome,
    unresolved: bool,
    market_exit: bool,
    estimate: bool = False,
) -> str:
    if market_exit:
        return ""
    if unresolved:
        if outcome.covered_quantity > outcome.held_quantity:
            return "Proposed exits exceed the held quantity."
        if estimate:
            return "Estimate from shown layers. Refresh TWS before reviewing an order."
        return "Refresh TWS to calculate an outcome."
    if outcome.covered_quantity > outcome.held_quantity:
        return "Proposed exits exceed the held quantity."
    return ""


def _projection_script(configuration: dict[str, Any]) -> str:
    """Fast local preview; server-rendered Decimal projection is authoritative."""
    payload = json.dumps(configuration, separators=(",", ":"))
    return f"""
(() => {{
  const config = {payload};
  const start = () => {{
    const active = new Map(config.active.map((item) => [item.id, item]));
    let draft = config.draft, invalidDraft = false, invalidActive = false;
    const money = (number) => `${{number >= 0 ? '+' : '-'}}$${{Math.abs(number).toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}}`;
    const updateMetric = (node, value, baseline, kind) => {{
      if (!node) return;
      const select = (part) => node.querySelector('[data-' + kind + '-' + part + ']');
      const change = select('change');
      select('value').textContent = value === null ? '— Incomplete' : money(value);
      const changed = value !== null && baseline !== null && Math.abs(value - baseline) > 0.005;
      if (!changed) {{
        select('amount').textContent = '—';
        node.querySelector('[data-' + kind + '-arrow="up"]').classList.add('hidden');
        node.querySelector('[data-' + kind + '-arrow="down"]').classList.add('hidden');
        change.setAttribute('aria-label', value !== null && baseline !== null ? 'No change from loaded plan' : 'Comparison unavailable');
        return;
      }}
      const up = kind === 'gain' ? value > baseline : value < baseline;
      const amount = `$${{Math.abs(value - baseline).toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}}`;
      select('amount').textContent = amount;
      node.querySelector('[data-' + kind + '-arrow="up"]').classList.toggle('hidden', !up);
      node.querySelector('[data-' + kind + '-arrow="down"]').classList.toggle('hidden', up);
      const direction = kind === 'gain'
        ? (up ? 'Expected gain increased' : 'Expected gain decreased')
        : (value < 0 || baseline < 0)
          ? (up ? 'More loss' : 'Less loss')
          : (up ? 'Lower stop outcome' : 'Higher stop outcome');
      change.setAttribute('aria-label', `${{direction}} by ${{amount}}`);
    }};
    const render = () => {{
      const exits = [...active.values()].filter((item) => !config.removed.includes(item.id)).concat(config.pending, draft);
      const covered = exits.reduce((total, item) => total + Number(item.quantity), 0);
      const gain = Number(config.realized) + exits.reduce((total, item) => total + Number(item.gain), 0);
      const loss = exits.reduce((total, item) => total + Number(item.loss), 0);
      const overallocated = covered > Number(config.held) + 1e-8;
      const complete = config.marketExit || (!config.unresolved && !invalidDraft && !invalidActive && !overallocated && Number.isFinite(covered) && Math.abs(covered - Number(config.held)) < 1e-8);
      const partial = !config.marketExit && !config.unresolved && !invalidDraft && !invalidActive && !overallocated && covered > 0 && covered < Number(config.held);
      const estimate = !config.marketExit && config.unresolved && !invalidDraft && !invalidActive && !overallocated && covered > 0 && covered <= Number(config.held);
      const projected = complete || partial || estimate;
      const gainNode = document.querySelector('[data-live-metric="gain"]');
      const lossNode = document.querySelector('[data-live-metric="loss"]');
      const comparablePartial = partial && Math.abs(covered - Number(config.baselineCoveredQuantity)) < 1e-8;
      const gainBaseline = estimate ? null : partial ? (comparablePartial ? Number(config.baselineCoveredGain) : null) : config.baselineGain === null ? null : Number(config.baselineGain);
      const lossBaseline = estimate ? null : partial ? (comparablePartial ? Number(config.baselineCoveredLoss) : null) : config.baselineLoss === null ? null : Number(config.baselineLoss);
      updateMetric(gainNode, projected ? (config.marketExit ? (config.baselineGain === null ? null : Number(config.baselineGain)) : gain) : null, gainBaseline, 'gain');
      updateMetric(lossNode, projected ? (config.marketExit ? (config.baselineLoss === null ? null : Number(config.baselineLoss)) : loss) : null, lossBaseline, 'loss');
      const status = document.querySelector('[data-projection-status]');
      if (status) {{
        status.textContent = config.marketExit ? '' : invalidDraft || invalidActive ? 'Complete valid prices and quantities for every edited layer.' : overallocated ? 'Proposed exits exceed the held quantity.' : estimate ? 'Estimate from shown layers. Refresh TWS before reviewing an order.' : config.unresolved ? 'Refresh TWS to calculate an outcome.' : '';
        status.classList.toggle('hidden', !status.textContent);
      }}
    }};
    window.ibkrProjection = {{
      updateDraft(items, invalid) {{
        if (config.staged) return;
        draft = items; invalidDraft = invalid; render();
      }},
      updateActive(items, invalid) {{
        if (config.staged) return;
        items.forEach((item) => active.set(item.id, item));
        invalidActive = invalid;
        render();
      }},
    }};
    render();
  }};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {{once: true}});
  else start();
}})();
"""


def _toast_notice(message: str) -> _ToastNotice | None:
    """Notify only when a problem needs attention; successes are explicit."""
    normalized = " ".join(message.split())
    lowered = normalized.lower()
    if "paper bracket confirmation expired" in lowered:
        return _ToastNotice(
            "Review expired",
            "Press Execute again to review current prices and quantities.",
            "warning",
        )
    if "portfolio state is not ready" in lowered or "tws connection failed" in lowered:
        return _ToastNotice(
            title="Couldn't connect to TWS",
            description="Check that TWS is open, then try again.",
            variant="error",
        )
    if not any(
        term in lowered
        for term in (
            "blocked", "failed", "unavailable", "unknown", "disabled",
            "invalid", "not in the verified", "could not", "needs attention",
            "refresh required", "must be", "enter valid", "does not have a usable",
            "select at least", "press execute", "start execution first",
            "start a price update first", "state changed", "quote changes",
            "earlier price amendment", "targets must",
        )
    ):
        return None
    if "automatic tws refresh failed" in lowered or "tws refresh could not" in lowered:
        return _ToastNotice(
            "Action acknowledged by TWS",
            "Refresh to verify the latest orders and position before another change.",
            "warning",
        )
    if "outcome is unknown" in lowered:
        description = (
            "Check TWS and refresh. Do not resend this draft."
            if lowered.startswith("submission outcome is unknown")
            else "Check TWS and refresh. Do not retry until the outcome is clear."
        )
        return _ToastNotice("Order status is uncertain", description, "error")
    if "journal reconciliation blocked" in lowered:
        return _ToastNotice(
            "Couldn't reconcile orders",
            "Refresh and check the app's orders in TWS.",
            "error",
        )
    if "targets must" in lowered:
        return _ToastNotice(
            "Fix the layer prices",
            "Target must be above 0%; stop must be between 0% and 100%.",
            "error",
        )
    if "quote changes" in lowered:
        return _ToastNotice(
            "Check the changed quote",
            "The price warning changed. Review it before confirming again.",
            "error",
        )
    if "earlier price amendment" in lowered:
        return _ToastNotice(
            "Check the earlier price change",
            "Inspect the order in TWS before retrying.",
            "error",
        )
    prefix, separator, detail = normalized.partition(":")
    titles = {
        "Execution blocked": "Couldn't send bracket orders",
        "Market exit blocked": "Couldn't send the market sell",
        "Bracket cancellation blocked": "Couldn't cancel bracket orders",
        "Price update blocked": "Couldn't change prices",
        "Plan blocked by validation": "Plan needs attention",
    }
    title = titles.get(prefix, "Couldn't complete this action")
    body = detail.strip() if separator else normalized
    if len(body) > 160:
        body = body[:157].rstrip() + "…"
    return _ToastNotice(title, body, "error")


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
        const quantity = button.querySelector('[data-position-quantity]');
        if (quantity) {
          quantity.dataset.loading = 'true';
          quantity.querySelector('[data-position-loading]')?.classList.remove('hidden');
          return;
        }
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


def _metric(
    label: str,
    value: str,
    tone: str,
    *,
    live_key: str | None = None,
    help_text: str | None = None,
) -> Any:
    label_node = (
        Div(
            Span(label),
            Tooltip(
                TooltipTrigger(
                    Button(
                        Icon("lucide:info", cls="size-3"),
                        variant="ghost",
                        size="icon",
                        type="button",
                        aria_label=f"How {label.lower()} is calculated",
                        cls="size-5 shrink-0 text-muted-foreground",
                    ),
                    delay_duration=250,
                ),
                TooltipContent(help_text, side="left", cls="max-w-56 leading-5"),
                signal=f"projection_{live_key}",
            ),
            cls="flex min-w-0 items-center gap-1 text-xs font-medium text-muted-foreground",
        )
        if help_text
        else P(label, cls="min-w-0 text-xs font-medium text-muted-foreground")
    )
    return (
        label_node,
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
