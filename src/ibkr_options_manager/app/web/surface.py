from __future__ import annotations

# ruff: noqa: E501
import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from secrets import token_urlsafe
from threading import Event, RLock, Thread
from time import monotonic
from typing import Any, cast
from uuid import uuid4

from starhtml import (
    H1,
    H3,
    Circle,
    Div,
    Fieldset,
    Form,
    Icon,
    Link,
    P,
    Polygon,
    Script,
    Signal,
    Span,
    Svg,
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

from ...cancellation_trace import (
    current_cancellation_context,
    current_snapshot_context,
    record_cancellation_event,
)
from ...domain import (
    BrokerSnapshot,
    VerifiedOptionContract,
    preview_reference_prices,
    round_down_price,
    round_up_price,
    stop_limit_price,
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
from ...trailing import TrailingPlan, TrailingRequest
from ..position_observation import VerifiedPositionChanges
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
from .components.ui.checkbox import Checkbox
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
from .components.ui.separator import Separator
from .components.ui.toast import Toaster
from .components.ui.toggle_group import ToggleGroup, ToggleGroupItem
from .components.ui.tooltip import Tooltip, TooltipContent, TooltipTrigger

_STATIC_DIR = Path(__file__).with_name("static")
_ASSETS_DIR = Path(__file__).with_name("assets")
_LAYER_TARGET_LABEL = "LMT"
_LAYER_STOP_LABEL = "STP"
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


@dataclass(frozen=True, slots=True)
class _TrailingFillSummary:
    quantity: Decimal
    average_price: Decimal | None
    realized_pnl: Decimal | None


class StarUIWorkbench:
    """Server-owned StarUI view over planning and explicitly enabled paper sends."""

    def __init__(
        self,
        view_model: PlannerViewModel,
        *,
        initial_account: str = "",
        initial_con_id: int | None = None,
        demo_mode: bool = False,
        demo_scenario: str = "standard",
        paper_execution: PaperExecutionService | None = None,
        observe_positions: bool = False,
        observer_client_id: int = 18,
        save_account: Callable[[str], None] | None = None,
    ) -> None:
        _register_bundled_icons()
        self._view_model = view_model
        self._demo_mode = demo_mode
        self._demo_scenario = demo_scenario
        self._demo_closed_con_id = (
            initial_con_id if demo_scenario == "closed-trail" else None
        )
        self._paper_execution = paper_execution
        self._observe_positions = observe_positions and not demo_mode
        self._observer_client_id = observer_client_id
        self._observer: PositionObserver | None = None
        self._observer_settings: ObservationSettings | None = None
        self._observer_generation = 0
        self._observer_signal = Event()
        self._observer_health = "idle"
        self._queued_observer_health: str | None = None
        self._pending_observation = False
        self._observation_thread: Thread | None = None
        self._closed = False
        self._inventory_revision = 0
        self._position_changes = VerifiedPositionChanges()
        self._observation_requires_reload = False
        self._observer_retry_at = 0.0
        self._armed_execution: PaperExecutionCandidate | None = None
        self._armed_execution_deadline: float | None = None
        self._recovery_requested_fingerprint: str | None = None
        self._recovery_requested_layer_index: int | None = None
        self._armed_market_exit: MarketExitCandidate | None = None
        self._armed_market_exits: tuple[MarketExitCandidate, ...] = ()
        self._armed_cancellation: MarketExitCandidate | None = None
        self._armed_cancellations: tuple[MarketExitCandidate, ...] = ()
        self._armed_trailing: TrailingPlan | None = None
        self._bulk_cancel_trace_id: str | None = None
        self._bulk_cancel_started_at: float | None = None
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
            int, tuple[Decimal | None, int, Decimal | None, Decimal | None]
        ] = {}
        self._preferred_con_id = initial_con_id
        self._selected_con_id: int | None = None
        self._selected_closed_con_id: int | None = None
        self._session_position_account = initial_account
        self._session_seen_positions: dict[int, PortfolioPositionLine] = {}
        self._session_closed_positions: dict[int, PortfolioPositionLine] = {}
        self._session_contract_snapshots: dict[int, BrokerSnapshot] = {}
        self._state = view_model.empty()
        self._drafts: dict[int, tuple[DraftLayerForm, ...]] = {}
        self._default_stop_type = "STP"
        self._default_stop_limit_offset = "5"
        self._default_stop_limit_unit = "percent"
        self._position_stop_config: dict[int, tuple[str, str, str]] = {}
        self._projection_comparison: PositionOutcome | None = None
        self._settings = ConnectionSettings(account=initial_account)
        self._save_account = save_account
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
        self._launch_account_error = ""
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
            if self._demo_scenario == "closed-trail":
                self._refresh_locked()
                if self._demo_closed_con_id in self._session_closed_positions:
                    self._selected_closed_con_id = self._demo_closed_con_id

    def refresh_on_launch(self) -> None:
        """Perform the same read-only refresh as the header control at startup."""
        if self._demo_mode:
            self.load_demo_data()
            return
        with self._lock:
            self._disarm_execution_locked()
            settings = self._settings
            if not settings.account.strip().upper().startswith("DU"):
                self._launch_connection = "failed"
                self._launch_account_error = (
                    "Enter your paper account ID (starts with DU)."
                )
                return
            self._launch_connection = "connecting"
            self._launch_account_error = ""
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

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    def _inventory_fragment(self) -> HTMLResponse:
        with self._lock:
            change = self._position_changes.selected_change
            return HTMLResponse(
                to_xml(self._inventory()),
                headers={
                    "Cache-Control": "no-store",
                    "X-Inventory-Revision": str(self._inventory_revision),
                    "X-Selected-Changed": "1"
                    if self._observation_requires_reload
                    else "0",
                    "X-Selected-Quantity-Change": str(change[2] - change[1])
                    if change
                    else "0",
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
        if (
            self._observer is not None
            and settings == self._observer_settings
            and self._observer_health in {"connecting", "connected"}
        ):
            return
        if self._observer is None:
            self._observer = PositionObserver(
                self._position_hint, self._position_health
            )
        self._observer_settings = settings
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
            self._observation_thread = Thread(
                target=self._observation_loop,
                name="ibkr-observation-reconcile",
                daemon=True,
            )
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
                    self._refresh_locked(
                        auto_select=False, preserve_invalid_drafts=True
                    )
                    if self._state.status in {UiStatus.READY, UiStatus.BLOCKED}:
                        self._observation_requires_reload = (
                            previous_selected != self._selected_con_id
                        )
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
            with self._lock:
                if self._launch_connection != "failed":
                    return self._page()
            account = values.get("account", "").strip().upper()
            if not account.upper().startswith("DU"):
                with self._lock:
                    self._launch_connection = "failed"
                    self._launch_account_error = (
                        "Enter your paper account ID (starts with DU)."
                    )
                    return self._page()
            with self._lock:
                if account != self._settings.account:
                    self._disarm_execution_locked()
                    self._state = self._view_model.empty()
                    self._selected_con_id = None
                    self._selected_closed_con_id = None
                    self._drafts.clear()
                    self._position_stop_config.clear()
                    self._session_seen_positions.clear()
                    self._session_closed_positions.clear()
                    self._session_contract_snapshots.clear()
                    self._position_changes.clear_selected_change()
                    self._session_position_account = account
                self._settings = replace(self._settings, account=account)
                self._launch_account_error = ""
            if self._save_account is not None:
                try:
                    self._save_account(account)
                except (OSError, ValueError):
                    with self._lock:
                        self._launch_connection = "failed"
                        self._launch_account_error = "Could not save the account ID. Check your local settings and retry."
                        return self._page()
            self.refresh_on_launch()
            with self._lock:
                return self._page()

        with self._lock:
            # Each response carries only feedback produced by this action.
            self._toast = None
            contract_locked = bool(self._unresolved_management_entries())
            if contract_locked and action not in {
                "refresh",
                "select",
                "select-session-closed",
                "verify-management",
            }:
                self._disarm_execution_locked()
                self._message = (
                    "This contract is locked while an order outcome is uncertain. "
                    "Check TWS, then verify its state before making changes."
                )
                return self._page()
            trading_actions = {
                "market-exit-selected",
                "cancel-all-active",
                "cancel-all-confirm",
                "trailing-convert-arm",
                "trailing-convert-confirm",
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
                stop_type = values.get("global_stop_type", self._default_stop_type)
                offset = values.get(
                    "global_stop_limit_offset", self._default_stop_limit_offset
                )
                unit = values.get(
                    "global_stop_limit_unit", self._default_stop_limit_unit
                )
                if stop_type not in {"STP", "STP LMT"} or not _valid_stop_limit_offset(
                    offset, unit
                ):
                    self._message = "Choose STP or STP LMT and enter an offset above zero (and below 100% for percentages)."
                    return self._page()
                self._default_stop_type = stop_type
                self._default_stop_limit_offset = offset
                self._default_stop_limit_unit = unit
                # Position choices belong to existing drafts. An empty draft
                # must inherit the new session default for its next layer.
                self._position_stop_config = {
                    con_id: choice
                    for con_id, choice in self._position_stop_config.items()
                    if self._drafts.get(con_id)
                }
                self._position_changes.clear_selected_change()
                self._target_presets = values.get(
                    "target_presets", self._target_presets
                )
                self._stop_presets = values.get("stop_presets", self._stop_presets)
                new_settings = ConnectionSettings(
                    account=values.get("account", self._settings.account)
                    .strip()
                    .upper(),
                    port=_positive_int(values.get("port"), self._settings.port),
                    client_id=_positive_int(
                        values.get("client_id"), self._settings.client_id
                    ),
                    timeout_seconds=_positive_float(
                        values.get("timeout"), self._settings.timeout_seconds
                    ),
                )
                if (
                    self._save_account is not None
                    and new_settings.account.upper().startswith("DU")
                ):
                    try:
                        self._save_account(new_settings.account)
                    except (OSError, ValueError):
                        self._message = "Could not save the account ID. Check your local settings and retry."
                        return self._page()
                self._settings = new_settings
                self._refresh_locked()
                self._start_observer_locked()
                self._submission_review_required = False
            elif action == "select":
                self._disarm_execution_locked()
                if not contract_locked:
                    self._save_form_locked(values)
                self._position_changes.clear_selected_change()
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
                self._position_changes.clear_selected_change()
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
            elif action == "trailing-convert-arm":
                self._arm_trailing_conversion_locked(values)
            elif action == "active-action-execute":
                self._execute_active_action_locked()
            elif action == "market-exit-confirm":
                self._confirm_market_exit_locked()
            elif action == "cancel-pair-confirm":
                self._confirm_cancellation_locked()
            elif action == "cancel-all-confirm":
                self._confirm_all_cancellations_locked()
            elif action == "trailing-convert-confirm":
                self._confirm_trailing_conversion_locked()
            elif action == "resolve-cancelled-bracket":
                self._resolve_cancelled_bracket_locked(values)
            elif action.startswith("verify-cancelled-bracket:"):
                identity = action.partition(":")[2]
                fingerprint, _, index_text = identity.partition(":")
                self._recovery_requested_fingerprint = fingerprint
                self._recovery_requested_layer_index = (
                    int(index_text) if index_text.isdecimal() else None
                )
                if (
                    self._cancelled_bracket_recovery(self._submission_outcomes())
                    is None
                ):
                    self._recovery_requested_fingerprint = None
                    self._recovery_requested_layer_index = None
                    self._message = (
                        "Bracket verification is unavailable; refresh the selected "
                        "position and try again."
                    )
            elif action == "verify-bracket-exists":
                self._verify_bracket_exists_locked(values)
            elif action == "verify-management":
                self._verify_management_locked(values)
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
                    self._message = (
                        "A draft already exists. Edit its layers or remove them first."
                    )
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
        self,
        *,
        auto_select: bool = True,
        preserve_invalid_drafts: bool = False,
        include_quote: bool = True,
    ) -> None:
        if self._session_position_account != self._settings.account:
            self._position_stop_config.clear()
            self._session_seen_positions.clear()
            self._session_closed_positions.clear()
            self._session_contract_snapshots.clear()
            self._selected_closed_con_id = None
            self._session_position_account = self._settings.account
        self._projection_comparison = None
        self._recovery_requested_fingerprint = None
        self._recovery_requested_layer_index = None
        self._disarm_execution_locked()
        state = self._view_model.refresh_portfolio(self._settings)
        self._apply_refreshed_portfolio_locked(
            state,
            auto_select=auto_select,
            preserve_invalid_drafts=preserve_invalid_drafts,
            include_quote=include_quote,
        )
        self._refresh_closed_history_locked()

    def _refresh_closed_history_locked(self) -> None:
        if self._paper_execution is None or self._state.status is not UiStatus.READY:
            return
        for con_id in self._session_closed_positions:
            baseline = self._session_contract_snapshots.get(con_id)
            if baseline is None:
                continue
            try:
                snapshot = self._view_model.refresh_closed_history(
                    self._settings, baseline
                )
                if snapshot is None:
                    continue
                self._paper_execution.record_completed_orders(snapshot)
                self._paper_execution.record_executions(snapshot)
            except ExecutionBlocked as error:
                self._message = f"Closed history reconciliation blocked: {error}"
            except Exception:
                self._message = "Closed history refresh unavailable. Check TWS connection and Refresh."

    def _refresh_after_acknowledged_write_locked(
        self, acknowledgement: str, *, include_quote: bool = True
    ) -> bool:
        """Replace optimistic post-write UI state with a fresh broker snapshot."""
        try:
            self._refresh_locked(include_quote=include_quote)
        except Exception as error:  # keep a confirmed write, never hide it
            self._message = (
                f"{acknowledgement} Automatic TWS refresh failed; use Refresh before "
                f"another action. ({error})"
            )
            return False
        snapshot = self._view_model.latest_snapshot()
        selected_snapshot_ready = (
            snapshot is not None
            and snapshot.complete
            and snapshot.fresh
            and snapshot.selected.account == self._settings.account
            and snapshot.selected.con_id == self._selected_con_id
        )
        if self._state.status is UiStatus.READY or selected_snapshot_ready:
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
        include_quote: bool = True,
    ) -> None:
        """Apply an already-read portfolio snapshot while holding the UI lock."""
        self._record_verified_positions_locked(
            state, track_selected_quantity=preserve_invalid_drafts
        )
        previous_con_id = self._selected_con_id
        select_first_arrival = (
            not auto_select
            and previous_con_id is None
            and not self._state.positions
            and self._selected_closed_con_id is None
            and self._preferred_con_id is None
            and state.status is UiStatus.READY
            and len(state.positions) == 1
            and state.positions[0].eligible
        )
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
            target = (
                state.positions[0].con_id
                if (auto_select or select_first_arrival) and state.positions
                else None
            )
        self._preferred_con_id = None
        if target is None:
            self._position_changes.clear_selected_change()
            return
        self._select_locked(
            target,
            preserve_invalid_draft=preserve_invalid_drafts,
            include_quote=include_quote,
        )
        if self._selected_con_id != previous_con_id:
            self._position_changes.clear_selected_change()

    def _record_verified_positions_locked(
        self, state: ViewState, *, track_selected_quantity: bool = False
    ) -> None:
        if state.status is not UiStatus.READY:
            return
        if self._session_position_account != self._settings.account:
            self._session_seen_positions.clear()
            self._session_closed_positions.clear()
            self._session_contract_snapshots.clear()
            self._selected_closed_con_id = None
            self._session_position_account = self._settings.account
        current = {position.con_id: position for position in state.positions}
        for con_id, position in self._session_seen_positions.items():
            if con_id not in current:
                self._session_closed_positions[con_id] = position
        for con_id, position in current.items():
            self._session_seen_positions[con_id] = position
            self._session_closed_positions.pop(con_id, None)
        self._position_changes.observe(
            state.account,
            state.positions,
            selected_con_id=self._selected_con_id,
            track_selected_quantity=track_selected_quantity,
        )

    def _select_locked(
        self,
        con_id: int,
        *,
        preserve_invalid_draft: bool = False,
        include_quote: bool = True,
    ) -> None:
        if con_id not in {position.con_id for position in self._state.positions}:
            self._message = "The selected contract is not in the verified portfolio."
            return
        self._selected_con_id = con_id
        self._selected_closed_con_id = None
        self._position_changes.select(con_id)
        state = self._view_model.select_position(
            con_id,
            self._plan_form(self._drafts.get(con_id, ())),
            **({"include_quote": False} if not include_quote else {}),
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
            state = self._view_model.select_position(
                con_id,
                self._plan_form(()),
                **({"include_quote": False} if not include_quote else {}),
            )
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()
        snapshot = self._view_model.latest_snapshot()
        if snapshot is not None and snapshot.complete and snapshot.fresh:
            self._session_contract_snapshots[con_id] = snapshot

    def _plan_form(self, layers: tuple[DraftLayerForm, ...]) -> PlanForm:
        stop_type, offset, unit = self._stop_configuration()
        return PlanForm(
            layers=layers,
            paper_execution_mode=self._paper_execution is not None,
            # With no draft, the planner's legacy percentage preview is only
            # observational. Do not validate a future layer's stop-limit
            # offset against that implicit preview.
            stop_order_type=stop_type if layers else "STP",
            stop_limit_offset=offset,
            stop_limit_unit=unit,
        )

    def _stop_configuration(self) -> tuple[str, str, str]:
        if self._selected_con_id is None:
            return (
                self._default_stop_type,
                self._default_stop_limit_offset,
                self._default_stop_limit_unit,
            )
        return self._position_stop_config.get(
            self._selected_con_id,
            (
                self._default_stop_type,
                self._default_stop_limit_offset,
                self._default_stop_limit_unit,
            ),
        )

    def _disarm_execution_locked(self) -> None:
        self._armed_execution = None
        self._armed_execution_deadline = None
        self._armed_market_exit = None
        self._armed_market_exits = ()
        self._armed_cancellation = None
        self._armed_cancellations = ()
        self._armed_trailing = None
        self._bulk_cancel_trace_id = None
        self._bulk_cancel_started_at = None
        self._active_action_verified = False
        self._review_all_active_exits = False
        self._armed_price_updates = ()
        self._warned_price_update_concerns = frozenset()
        self._price_update_retry_required = False
        self._armed_active_percentages = {}

    def _trace_bulk_cancel(self, event: str, **fields: Any) -> None:
        run_id = self._bulk_cancel_trace_id
        started = self._bulk_cancel_started_at
        if run_id is None or started is None:
            return
        record_cancellation_event(
            event,
            run_id=run_id,
            elapsed_ms=round(max(0.0, monotonic() - started) * 1000, 1),
            **fields,
        )

    def _expire_confirmation_locked(self, *, require_deadline: bool = False) -> bool:
        if not (
            self._armed_execution is not None
            or self._armed_price_updates
            or self._active_action_verified
        ):
            return False
        deadline = self._armed_execution_deadline
        if deadline is None and not require_deadline:
            return False
        if deadline is not None and monotonic() < deadline:
            return False
        if self._armed_price_updates:
            edited_percentages = dict(self._armed_active_percentages)
            self._disarm_execution_locked()
            self._armed_active_percentages = edited_percentages
        elif self._armed_execution is not None:
            self._disarm_execution_locked()
        else:
            # Keep the selected active-layer action available for a new Execute.
            self._active_action_verified = False
            self._armed_execution_deadline = None
        self._status_message = (
            "Review expired. Review the action again with fresh TWS data."
        )
        return True

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
        if self._armed_execution is not None and self._expire_confirmation_locked(
            require_deadline=True
        ):
            self._disarm_execution_locked()
            self._message = (
                "Paper bracket confirmation expired. Review the order again."
            )
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
        if armed.snapshot.quote.market_data_type == "NOT_REQUESTED":
            self._disarm_execution_locked()
            self._message = "Review the order with market data before confirming it."
            return
        state, candidate = self._view_model.prepare_paper_execution(
            self._plan_form(self._current_layers()), include_quote=False
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
                stop_limit_offset=(
                    Decimal(self._stop_configuration()[1])
                    if self._stop_configuration()[0] == "STP LMT"
                    else None
                ),
                stop_limit_unit=self._stop_configuration()[2],
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
                f"Paper submission acknowledged for {len(receipt.entry.order_ids)} orders.",
                include_quote=False,
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
            "Review the market sell to verify a fresh snapshot."
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
            "sell order will be sent. Review the cancellation to verify a fresh snapshot."
        )

    def _arm_all_cancellations_locked(self) -> None:
        """Stage every active, journal-proven OCA pair on this position."""
        self._disarm_execution_locked()
        self._bulk_cancel_trace_id = uuid4().hex
        self._bulk_cancel_started_at = monotonic()
        self._trace_bulk_cancel("trigger", action="cancel-all-active")
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Paper order management is disabled for this launch."
            self._trace_bulk_cancel("stage_blocked", reason="unavailable")
            return
        target_ids = self._active_target_perm_ids()
        snapshot = self._view_model.latest_snapshot()
        if not target_ids or snapshot is None:
            self._message = (
                "Bracket cancellation blocked: no active layers are available."
            )
            self._trace_bulk_cancel("stage_blocked", reason="no_active_layers")
            return
        try:
            candidates = self._paper_execution.prepare_market_exits(
                snapshot,
                target_perm_ids=target_ids,
                expected_client_id=self._settings.client_id,
            )
        except ExecutionBlocked as error:
            self._message = f"Bracket cancellation blocked: {error}"
            self._trace_bulk_cancel("stage_blocked", reason="verification")
            return
        self._armed_cancellations = candidates
        self._trace_bulk_cancel("stage_ready", bracket_count=len(candidates))
        self._set_review_status_locked(
            f"Review cancellation of {len(candidates)} active OCA brackets. "
            "The position will remain open. Review the cancellation to verify a fresh snapshot."
        )

    def _arm_trailing_conversion_locked(self, values: dict[str, str]) -> None:
        self._disarm_execution_locked()
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Paper order management is disabled for this launch."
            return
        snapshot = self._view_model.latest_snapshot()
        if snapshot is None:
            self._message = "Refresh this position before reviewing a trailing exit."
            return
        try:
            trail = Decimal(values.get("trail_value", ""))
            trail_unit = values.get("trail_unit", "dollars")
            if trail_unit == "dollars":
                multiplier = snapshot.contract.multiplier
                if not multiplier.is_finite() or multiplier <= 0:
                    raise ExecutionBlocked("the option contract multiplier is invalid")
                trail /= multiplier
            limit_text = values.get("trail_limit_value", "").strip()
            limit = Decimal(limit_text) if limit_text else None
            limit_unit = values.get("trail_limit_unit", "dollars")
            if limit is not None and limit_unit == "dollars":
                multiplier = snapshot.contract.multiplier
                if not multiplier.is_finite() or multiplier <= 0:
                    raise ExecutionBlocked("the option contract multiplier is invalid")
                limit /= multiplier
            request = TrailingRequest(
                trail_value=trail,
                trail_unit=trail_unit,
                limit_value=limit,
                limit_unit=limit_unit,
                tif="GTC",
            )
            self._armed_trailing = (
                self._paper_execution.prepare_entire_position_trailing(
                    snapshot,
                    target_perm_ids=self._active_target_perm_ids(),
                    expected_client_id=self._settings.client_id,
                    request=request,
                )
            )
        except (InvalidOperation, ExecutionBlocked) as error:
            self._message = f"Trailing exit blocked: {error}"
            return
        plan = self._armed_trailing
        self._set_review_status_locked(
            f"Review a trailing SELL for all {plan.quantity} held contracts."
            + (
                f" It will replace {len(plan.candidates)} app-owned "
                f"{'bracket' if len(plan.candidates) == 1 else 'brackets'}."
                if plan.candidates
                else ""
            )
        )

    def _confirm_trailing_conversion_locked(self) -> None:
        if self._expire_confirmation_locked(require_deadline=True):
            self._message = "Review expired. Review the trailing exit again."
            return
        plan = self._armed_trailing
        if (
            plan is None
            or self._paper_execution is None
            or self._selected_con_id is None
            or not self._active_action_verified
        ):
            self._message = "Review the trailing exit before confirming it."
            return
        cancelled = 0
        try:
            for index, candidate in enumerate(plan.candidates):
                state = self._view_model.select_position(
                    self._selected_con_id,
                    self._plan_form(self._drafts.get(self._selected_con_id, ())),
                    include_quote=False,
                )
                self._apply_state_locked(state)
                self._record_refresh_time_locked()
                self._announce_reconciliation_locked()
                snapshot = self._view_model.latest_snapshot()
                if snapshot is None:
                    raise ExecutionBlocked("the fresh bracket snapshot is unavailable")
                self._paper_execution.verify_trailing_baseline(snapshot, plan)
                remaining = plan.candidates[index:]
                if set(self._active_target_perm_ids()) != {
                    item.target_perm_id for item in remaining
                }:
                    raise ExecutionBlocked("the active bracket set changed")
                refreshed = self._paper_execution.prepare_market_exits(
                    snapshot,
                    target_perm_ids=tuple(item.target_perm_id for item in remaining),
                    expected_client_id=self._settings.client_id,
                )
                if (
                    refreshed != remaining
                    or snapshot.position.quantity != plan.quantity
                ):
                    raise ExecutionBlocked(
                        "a bracket or position changed during conversion"
                    )
                self._paper_execution.cancel_pair(
                    snapshot,
                    candidate,
                    host="127.0.0.1",
                    port=self._settings.port,
                    client_id=self._settings.client_id,
                    timeout_seconds=self._settings.timeout_seconds,
                )
                cancelled += 1
            state = self._view_model.select_position(
                self._selected_con_id,
                self._plan_form(self._drafts.get(self._selected_con_id, ())),
            )
            self._apply_state_locked(state)
            self._record_refresh_time_locked()
            self._announce_reconciliation_locked()
            snapshot = self._view_model.latest_snapshot()
            if snapshot is None:
                raise ExecutionBlocked("the final position snapshot is unavailable")
            self._paper_execution.submit_entire_position_trailing(
                snapshot,
                plan,
                host="127.0.0.1",
                port=self._settings.port,
                client_id=self._settings.client_id,
                timeout_seconds=self._settings.timeout_seconds,
            )
        except (ExecutionBlocked, ExecutionOutcomeUnknown) as error:
            self._message = (
                f"Trailing exit stopped after cancelling {cancelled} of "
                f"{len(plan.candidates)} brackets: {error}. Check TWS and refresh."
                if plan.candidates
                else f"Trailing exit stopped: {error}. Check TWS and refresh."
            )
        except Exception as error:
            self._message = (
                f"Trailing exit outcome is unknown after {cancelled} "
                f"cancellations: {error}. Check TWS and refresh."
                if plan.candidates
                else f"Trailing exit outcome is unknown: {error}. Check TWS and refresh."
            )
        else:
            self._refresh_after_acknowledged_write_locked(
                "TWS acknowledged the trailing exit. Check TWS for Transmit or fills.",
                include_quote=False,
            )
        finally:
            self._disarm_execution_locked()

    def _execute_active_action_locked(self) -> None:
        """Verify a reviewed cancellation or market exit before showing Confirm."""
        market_exits = self._armed_market_exits or (
            (self._armed_market_exit,) if self._armed_market_exit is not None else ()
        )
        cancellation = self._armed_cancellation
        cancellations = self._armed_cancellations
        trailing = self._armed_trailing
        if (
            self._paper_execution is None
            or self._selected_con_id is None
            or (
                not market_exits
                and cancellation is None
                and not cancellations
                and trailing is None
            )
        ):
            self._message = "Review an active-layer action before executing it."
            return
        self._active_action_verified = False
        if cancellations:
            self._trace_bulk_cancel(
                "review_triggered", bracket_count=len(cancellations)
            )
            self._trace_bulk_cancel("review_snapshot_start")
        snapshot_started = monotonic()
        trace_token = current_snapshot_context.set(
            (self._bulk_cancel_trace_id, "review", None)
            if cancellations and self._bulk_cancel_trace_id
            else None
        )
        try:
            state = self._view_model.select_position(
                self._selected_con_id,
                self._plan_form(self._drafts.get(self._selected_con_id, ())),
                **(
                    {"include_quote": False}
                    if cancellation is not None or cancellations
                    else {}
                ),
            )
        finally:
            current_snapshot_context.reset(trace_token)
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()
        snapshot = self._view_model.latest_snapshot()
        if cancellations:
            self._trace_bulk_cancel(
                "review_snapshot_complete",
                duration_ms=round((monotonic() - snapshot_started) * 1000, 1),
                available=snapshot is not None,
            )
        if snapshot is None:
            self._disarm_execution_locked()
            self._message = "Execution blocked: the fresh snapshot is unavailable."
            return
        try:
            if trailing is not None:
                self._paper_execution.verify_trailing_baseline(snapshot, trailing)
                if set(self._active_target_perm_ids()) != {
                    item.target_perm_id for item in trailing.candidates
                }:
                    raise ExecutionBlocked("the active bracket set changed")
                refreshed_trailing = (
                    self._paper_execution.prepare_entire_position_trailing(
                        snapshot,
                        target_perm_ids=tuple(
                            item.target_perm_id for item in trailing.candidates
                        ),
                        expected_client_id=self._settings.client_id,
                        request=trailing.request,
                    )
                )
                # The bid can move, and each fresh TWS capture has a new
                # connection epoch. Show the new stop and limit offset for
                # confirmation while keeping stable safety inputs identical.
                if (
                    replace(
                        refreshed_trailing,
                        reference_price=trailing.reference_price,
                        initial_stop=trailing.initial_stop,
                        limit_offset=trailing.limit_offset,
                        fingerprint=trailing.fingerprint,
                        connection_epoch=trailing.connection_epoch,
                    )
                    != trailing
                ):
                    raise ExecutionBlocked("the trailing plan changed after review")
                self._armed_trailing = refreshed_trailing
            elif cancellations:
                if set(self._active_target_perm_ids()) != {
                    candidate.target_perm_id for candidate in cancellations
                }:
                    raise ExecutionBlocked(
                        "the set of active OCA layers changed after review"
                    )
                refreshed_cancellations = self._paper_execution.prepare_market_exits(
                    snapshot,
                    target_perm_ids=tuple(
                        candidate.target_perm_id for candidate in cancellations
                    ),
                    expected_client_id=self._settings.client_id,
                )
                if refreshed_cancellations != cancellations:
                    raise ExecutionBlocked("the OCA layers changed after review")
            elif cancellation is not None:
                refreshed_cancellation = self._paper_execution.prepare_market_exit(
                    snapshot,
                    target_perm_id=cancellation.target_perm_id,
                    expected_client_id=self._settings.client_id,
                )
                if refreshed_cancellation != cancellation:
                    raise ExecutionBlocked("the OCA bracket changed after review")
            else:
                if self._review_all_active_exits and set(
                    self._active_target_perm_ids()
                ) != {candidate.target_perm_id for candidate in market_exits}:
                    raise ExecutionBlocked(
                        "the set of active OCA layers changed after review"
                    )
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
            self._message = (
                f"{'Trailing exit' if trailing is not None else 'Execution'} "
                f"blocked: {error}. Review the latest state again."
            )
            return
        self._active_action_verified = True
        if cancellations:
            self._trace_bulk_cancel("review_verified")
        self._armed_execution_deadline = monotonic() + 10
        self._set_review_status_locked(
            "Fresh paper snapshot verified. Review the action and confirm within 10 seconds."
        )

    def _confirm_cancellation_locked(self) -> None:
        """Cancel the staged pair after one more fresh-snapshot equality check."""
        if self._expire_confirmation_locked(require_deadline=True):
            self._message = "Review expired. Review the cancellation again."
            return
        candidate = self._armed_cancellation
        if (
            self._paper_execution is None
            or candidate is None
            or self._selected_con_id is None
            or not self._active_action_verified
        ):
            self._message = "Review the cancellation before confirming it."
            return
        state = self._view_model.select_position(
            self._selected_con_id,
            self._plan_form(self._drafts.get(self._selected_con_id, ())),
            include_quote=False,
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
            refresh_succeeded = self._refresh_after_acknowledged_write_locked(
                "TWS confirmed both OCA legs were cancelled.",
                include_quote=False,
            )
            observed = self._view_model.latest_snapshot()
            cancelled_ids = {candidate.target_perm_id, candidate.stop_perm_id}
            if refresh_succeeded and not (
                observed is not None
                and observed.selected.account == candidate.account
                and observed.selected.con_id == candidate.con_id
                and observed.complete
                and observed.fresh
                and not any(
                    order.perm_id in cancelled_ids for order in observed.working_orders
                )
            ):
                self._message = (
                    "Bracket cancellation needs verification: the refreshed TWS "
                    "snapshot still shows a selected order. Check TWS and refresh."
                )
        finally:
            self._disarm_execution_locked()

    def _confirm_all_cancellations_locked(self) -> None:
        """Cancel reviewed pairs one at a time, verifying TWS between writes."""
        self._trace_bulk_cancel("confirm_triggered")
        if self._expire_confirmation_locked(require_deadline=True):
            self._message = "Review expired. Review the cancellation again."
            return
        candidates = self._armed_cancellations
        if (
            self._paper_execution is None
            or self._selected_con_id is None
            or not candidates
            or not self._active_action_verified
        ):
            self._message = "Review the cancellation before confirming it."
            return
        cancelled = 0
        try:
            for index, candidate in enumerate(candidates):
                bracket_number = index + 1
                self._trace_bulk_cancel("snapshot_start", bracket_number=bracket_number)
                snapshot_started = monotonic()
                trace_token = current_snapshot_context.set(
                    (self._bulk_cancel_trace_id, "before_pair", bracket_number)
                    if self._bulk_cancel_trace_id
                    else None
                )
                try:
                    state = self._view_model.select_position(
                        self._selected_con_id,
                        self._plan_form(self._drafts.get(self._selected_con_id, ())),
                        include_quote=False,
                    )
                finally:
                    current_snapshot_context.reset(trace_token)
                self._apply_state_locked(state)
                self._record_refresh_time_locked()
                self._announce_reconciliation_locked()
                snapshot = self._view_model.latest_snapshot()
                self._trace_bulk_cancel(
                    "snapshot_complete",
                    bracket_number=bracket_number,
                    duration_ms=round((monotonic() - snapshot_started) * 1000, 1),
                    available=snapshot is not None,
                )
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
                self._trace_bulk_cancel(
                    "pair_cancel_start", bracket_number=bracket_number
                )
                pair_started = monotonic()
                context_token = current_cancellation_context.set(
                    (self._bulk_cancel_trace_id or uuid4().hex, bracket_number)
                )
                try:
                    self._paper_execution.cancel_pair(
                        snapshot,
                        candidate,
                        host="127.0.0.1",
                        port=self._settings.port,
                        client_id=self._settings.client_id,
                        timeout_seconds=self._settings.timeout_seconds,
                    )
                finally:
                    current_cancellation_context.reset(context_token)
                    self._trace_bulk_cancel(
                        "pair_cancel_complete",
                        bracket_number=bracket_number,
                        duration_ms=round((monotonic() - pair_started) * 1000, 1),
                    )
                cancelled += 1
        except (ExecutionBlocked, ExecutionOutcomeUnknown) as error:
            self._trace_bulk_cancel(
                "stopped", cancelled_count=cancelled, outcome=type(error).__name__
            )
            self._message = (
                f"Cancelled {cancelled} of {len(candidates)} brackets. Stopped: {error}. "
                "Refresh TWS before another action."
            )
        except Exception as error:
            self._trace_bulk_cancel(
                "stopped", cancelled_count=cancelled, outcome=type(error).__name__
            )
            self._message = (
                f"Cancelled {cancelled} of {len(candidates)} brackets. Outcome is unknown: "
                f"{error}. Refresh TWS before another action."
            )
        else:
            self._trace_bulk_cancel("final_refresh_start", cancelled_count=cancelled)
            refresh_started = monotonic()
            # Refresh disarms the action, including its trace context. Keep the
            # timing context locally so the final refresh and outcome are logged.
            trace_id = self._bulk_cancel_trace_id
            trace_started_at = self._bulk_cancel_started_at
            trace_token = current_snapshot_context.set(
                (trace_id, "final_refresh", None) if trace_id else None
            )
            try:
                refresh_succeeded = self._refresh_after_acknowledged_write_locked(
                    f"TWS confirmed cancellation of {cancelled} active OCA brackets.",
                    include_quote=False,
                )
            finally:
                current_snapshot_context.reset(trace_token)
            self._bulk_cancel_trace_id = trace_id
            self._bulk_cancel_started_at = trace_started_at
            self._trace_bulk_cancel(
                "final_refresh_complete",
                duration_ms=round((monotonic() - refresh_started) * 1000, 1),
                verified=refresh_succeeded,
            )
            if refresh_succeeded:
                observed = self._view_model.latest_snapshot()
                cancelled_ids = {
                    perm_id
                    for candidate in candidates
                    for perm_id in (candidate.target_perm_id, candidate.stop_perm_id)
                }
                if not (
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
                    self._message = (
                        "Bracket cancellation needs verification: refreshed TWS still "
                        "shows a selected order. Check TWS and refresh."
                    )
        finally:
            self._trace_bulk_cancel("finished", cancelled_count=cancelled)
            self._disarm_execution_locked()

    def _verify_bracket_exists_locked(self, values: dict[str, str]) -> None:
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Select a position before verifying its bracket."
            return
        fingerprint = values.get("fingerprint", "")
        self._recovery_requested_fingerprint = fingerprint
        index_text = values.get("layer_index", "")
        self._recovery_requested_layer_index = (
            int(index_text) if index_text.isdecimal() else None
        )
        state = self._view_model.select_position(
            self._selected_con_id, self._plan_form(()), include_quote=False
        )
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        snapshot = self._view_model.latest_snapshot()
        if (
            snapshot is None
            or not snapshot.connected
            or not snapshot.complete
            or not snapshot.fresh
            or snapshot.selected.account != self._settings.account
            or snapshot.selected.con_id != self._selected_con_id
        ):
            self._message = (
                "Bracket not verified: a fresh, complete broker read is required."
            )
            return
        self._announce_reconciliation_locked()
        outcomes = [
            outcome
            for entry, index, outcome in self._submission_outcomes()
            if entry.fingerprint == fingerprint
            and (
                self._recovery_requested_layer_index is None
                or index == self._recovery_requested_layer_index
            )
        ]
        if (outcomes and all(outcome.status == "ACTIVE" for outcome in outcomes)) or (
            outcomes
            and all(
                outcome.status == "ACTIVE" or outcome.status.startswith("CLOSED_")
                for outcome in outcomes
            )
        ):
            self._recovery_requested_fingerprint = None
            self._recovery_requested_layer_index = None
        else:
            self._message = (
                "Bracket not verified: the fresh snapshot did not establish a "
                "complete active or filled outcome. Check TWS and try again."
            )

    def _verify_management_locked(self, values: dict[str, str]) -> None:
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Select the locked contract before verifying it."
            return
        fingerprint = values.get("fingerprint", "")
        if values.get("confirmed") != "yes":
            self._message = "Confirm the contract's orders and fills in TWS first."
            return
        try:
            state = self._view_model.select_position(
                self._selected_con_id,
                self._plan_form(self._drafts.get(self._selected_con_id, ())),
                include_quote=False,
            )
            self._apply_state_locked(state)
            self._record_refresh_time_locked()
            snapshot = self._view_model.latest_snapshot()
            if snapshot is None:
                raise ExecutionBlocked("a fresh TWS snapshot is unavailable")
            self._paper_execution.confirm_unknown_management(
                snapshot, fingerprint, confirmed_in_tws=True
            )
        except ExecutionBlocked as error:
            self._message = f"Order status not verified: {error}"
            return
        except Exception:
            self._message = (
                "Order status not verified: TWS read failed. Refresh and try again."
            )
            return
        self._disarm_execution_locked()

    def _resolve_cancelled_bracket_locked(self, values: dict[str, str]) -> None:
        if self._paper_execution is None or self._selected_con_id is None:
            self._message = "Cancellation verification requires a selected position."
            return
        if values.get("confirmed") != "yes":
            self._message = (
                "Confirm neither bracket leg is working or filled in TWS first."
            )
            return
        con_id = self._selected_con_id
        state = self._view_model.select_position(
            con_id, self._plan_form(()), include_quote=False
        )
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        self._announce_reconciliation_locked()
        snapshot = self._view_model.latest_snapshot()
        if snapshot is None:
            self._message = "Cancellation verification needs a fresh TWS snapshot."
            return
        try:
            index_text = values.get("layer_index", "")
            if index_text.isdecimal():
                self._paper_execution.confirm_cancelled_layer(
                    snapshot,
                    values.get("fingerprint", ""),
                    int(index_text),
                    confirmed_in_tws=True,
                )
            else:
                raise ExecutionBlocked("select one bracket layer to verify")
        except ExecutionBlocked as error:
            self._message = f"Cancellation verification blocked: {error}"
            return
        self._recovery_requested_fingerprint = None
        self._recovery_requested_layer_index = None

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
            f"for {total} contracts, then review the market sell."
        )

    def _confirm_market_exit_locked(self) -> None:
        if self._expire_confirmation_locked(require_deadline=True):
            self._message = "Review expired. Review the market sell again."
            return
        armed = self._armed_market_exits or (
            (self._armed_market_exit,) if self._armed_market_exit is not None else ()
        )
        if (
            self._paper_execution is None
            or not armed
            or self._selected_con_id is None
            or not self._active_action_verified
        ):
            self._message = "Review the market sell before confirming it."
            return
        state = self._view_model.select_position(
            self._selected_con_id,
            self._plan_form(self._drafts.get(self._selected_con_id, ())),
            include_quote=False,
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
                raise ExecutionBlocked(
                    "the set of active OCA layers changed after review"
                )
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
                f"{sum((candidate.quantity for candidate in candidates), Decimal('0'))} contracts.",
                include_quote=False,
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
                exact_stop_text = values.get(
                    f"active_stop_price_{layer.target_perm_id}"
                )
                if exact_stop_text:
                    exact_stop = _decimal_value(exact_stop_text)
                    if (
                        exact_stop is None
                        or exact_stop <= 0
                        or round_up_price(exact_stop, calculator.bands) != exact_stop
                        or abs((exact_stop / basis - 1) * 100 - stop_percentage)
                        > Decimal("0.005001")
                    ):
                        raise ExecutionBlocked(
                            "bulk stop price does not match its displayed return"
                        )
                    desired_stop = exact_stop
                shown_target = _decimal_value(
                    _active_percentage_for_price(
                        target.limit_price,
                        basis,
                        target=True,
                        bands=calculator.bands,
                        presets=_parse_presets(
                            self._target_presets, maximum=Decimal("1000")
                        )
                        or (),
                    )
                )
                shown_stop = _decimal_value(
                    _active_percentage_for_price(
                        stop.stop_price,
                        basis,
                        target=True,
                        bands=calculator.bands,
                        presets=_parse_presets(
                            self._stop_presets, maximum=Decimal("100")
                        )
                        or (),
                    )
                )
                next_stop = (
                    desired_stop
                    if exact_stop_text and desired_stop != stop.stop_price
                    else _edited_active_price(
                        stop.stop_price, desired_stop, stop_percentage, shown_stop
                    )
                )
                saved_rule = (
                    self._paper_execution.stop_limit_rule(snapshot, layer)
                    if stop.order_type == "STP LMT"
                    else None
                )
                override_text = values.get(
                    f"active_stop_limit_offset_{layer.target_perm_id}", ""
                )
                override_offset = (
                    _decimal_value(override_text) if override_text else None
                )
                if override_text and override_offset is None:
                    raise ExecutionBlocked("new stop-limit offset is invalid")
                override_unit = values.get(
                    f"active_stop_limit_unit_{layer.target_perm_id}", ""
                )
                if (
                    stop.order_type == "STP LMT"
                    and saved_rule is not None
                    and override_offset is not None
                    and (
                        override_offset != saved_rule[0]
                        or (override_unit or saved_rule[1]) != saved_rule[1]
                    )
                    and next_stop is None
                ):
                    next_stop = stop.stop_price
                if next_stop is not None and stop.order_type == "STP LMT":
                    if saved_rule is None:
                        raise ExecutionBlocked(
                            "this STP LMT layer has no saved offset rule"
                        )
                    next_limit = stop_limit_price(
                        next_stop,
                        override_offset
                        if override_offset is not None
                        else saved_rule[0],
                        override_unit or saved_rule[1],
                        calculator.bands,
                    )
                else:
                    next_limit = None
                updates.append(
                    PriceUpdateCandidate(
                        layer=layer,
                        target_price=(
                            _edited_active_price(
                                target.limit_price,
                                desired_target,
                                target_percentage,
                                shown_target,
                            )
                        ),
                        stop_price=next_stop,
                        prior_target_price=target.limit_price,
                        prior_stop_price=stop.stop_price,
                        prior_stop_limit_price=(
                            stop.limit_price if stop.order_type == "STP LMT" else None
                        ),
                        stop_limit_price=next_limit,
                        stop_limit_offset=override_offset
                        if next_stop is not None
                        else None,
                        stop_limit_unit=override_unit if next_stop is not None else "",
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
            if prior_state not in {None, "RESOLVED"}:
                raise ExecutionBlocked(
                    "this exact amendment has already been sent or reserved; refresh TWS"
                )
            self._price_update_retry_required = prior_state == "RESOLVED"
            self._armed_active_percentages = edited_percentages
            self._warned_price_update_concerns = _price_update_impact(
                snapshot, self._armed_price_updates
            ).concerns
            self._armed_execution_deadline = monotonic() + 10
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
                "The earlier uncertain amendment was verified in TWS. Check that "
                "the old price remains and no change awaits Transmit before retrying."
            )
        else:
            self._set_review_status_locked(
                f"Fresh paper snapshot verified. Review {changed_legs} selected price "
                "amendments and confirm within 10 seconds."
            )

    def _confirm_price_updates_locked(self, values: dict[str, str]) -> None:
        if self._expire_confirmation_locked(require_deadline=True):
            self._message = "Review expired. Review the price changes again."
            return
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
            if not submitted or (
                isinstance(error, ExecutionBlocked)
                and not isinstance(error, ExecutionOutcomeUnknown)
                and not self._unresolved_management_entries()
            ):
                self._message = f"Price update blocked: {error}"
                record_price_update_event(
                    "ui_result", outcome="blocked", reason=str(error)
                )
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
                record_price_update_event(
                    "ui_result", outcome="filled", reason=str(error)
                )
            else:
                self._message = (
                    f"Price update outcome is unknown: {error}. Check TWS and refresh "
                    "before another action."
                )
                record_price_update_event(
                    "ui_result", outcome="unknown", reason=str(error)
                )
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
                    stop_limit_price=update.stop_limit_price,
                )
            refresh_succeeded = self._refresh_after_acknowledged_write_locked(
                f"{'Simulated broker' if self._demo_mode else 'TWS'} acknowledged "
                f"{len(receipt.entry.order_ids)} app-owned OCA "
                "price amendment(s).",
                include_quote=False,
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
            self._show_success_toast_locked(
                "Simulated price update acknowledged"
                if self._demo_mode
                else "Price update acknowledged by TWS",
                "Demo order state refreshed; no TWS order was sent."
                if refresh_succeeded and self._demo_mode
                else "Latest TWS state loaded. Check TWS for any required Transmit."
                if refresh_succeeded
                else "The automatic refresh could not verify broker state. Refresh before another order change.",
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
        stop_limit_price: Decimal | None = None,
    ) -> None:
        """Retain a TWS-acknowledged amendment through one lagging snapshot."""
        if target_price is None and stop_price is None and stop_limit_price is None:
            return
        prior = self._pending_active_prices.get(target_perm_id)
        self._pending_active_prices[target_perm_id] = (
            target_price if target_price is not None else (prior[0] if prior else None),
            stop_perm_id,
            stop_price if stop_price is not None else (prior[2] if prior else None),
            stop_limit_price
            if stop_limit_price is not None
            else (prior[3] if prior else None),
        )

    def _reconcile_pending_active_prices_locked(self) -> None:
        """Drop presentation overrides as soon as TWS confirms the new prices."""
        if not self._pending_active_prices:
            return
        orders_by_perm = {order.perm_id: order for order in self._state.working_orders}
        pending: dict[
            int, tuple[Decimal | None, int, Decimal | None, Decimal | None]
        ] = {}
        for target_perm_id, (
            target_price,
            stop_perm_id,
            stop_price,
            pending_stop_limit_price,
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
            unresolved_limit = (
                pending_stop_limit_price
                if pending_stop_limit_price is not None
                and stop.limit_price != pending_stop_limit_price
                else None
            )
            if any(
                price is not None
                for price in (unresolved_target, unresolved_stop, unresolved_limit)
            ):
                pending[target_perm_id] = (
                    unresolved_target,
                    stop_perm_id,
                    unresolved_stop,
                    unresolved_limit,
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
            record_trailing_prices = getattr(
                self._paper_execution, "record_trailing_prices", None
            )
            if callable(record_trailing_prices):
                record_trailing_prices(snapshot)
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
        if "draft_stop_type" in values:
            stop_type = values["draft_stop_type"]
            offset = values.get(
                "draft_stop_limit_offset", self._stop_configuration()[1]
            )
            unit = values.get("draft_stop_limit_unit", self._stop_configuration()[2])
            if stop_type not in {"STP", "STP LMT"} or (
                stop_type == "STP LMT" and not _valid_stop_limit_offset(offset, unit)
            ):
                self._message = (
                    "Enter a valid stop-limit offset before reviewing the draft."
                )
                return False
            if stop_type == "STP" and not _valid_stop_limit_offset(offset, unit):
                offset, unit = self._stop_configuration()[1:]
            self._position_stop_config[self._selected_con_id] = (
                stop_type,
                offset,
                unit,
            )
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
            exact_stop_text = values.get(f"draft_stop_price_{index}", "").strip()
            quantity = values.get(f"quantity_{index}", previous.quantity)
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
                exact_stop = Decimal(exact_stop_text) if exact_stop_text else None
                basis = self._state.unit_basis or Decimal("0")
                bands = (
                    self._state.quote_calculator.bands
                    if self._state.quote_calculator is not None
                    else ()
                )
                if exact_stop is not None and (
                    not exact_stop.is_finite()
                    or exact_stop <= 0
                    or round_up_price(exact_stop, bands) != exact_stop
                    or (
                        exact_stop != prices.stop_price
                        and abs((Decimal("1") - exact_stop / basis) * 100 - stop_value)
                        > Decimal("0.051")
                    )
                ):
                    raise ValueError("draft stop price and percentage disagree")
                chosen_stop = (
                    exact_stop if exact_stop is not None else prices.stop_price
                )
                actual_target = (
                    Decimal(previous.target_price)
                    if target_value == Decimal(previous.target_percentage)
                    else prices.target_price
                )
                if chosen_stop >= actual_target:
                    raise ValueError("stop must remain below target")
            except (InvalidOperation, ValueError):
                self._message = (
                    "Targets must be above 0%; stops must be below their targets."
                )
                return False
            layers.append(
                DraftLayerForm(
                    quantity=quantity,
                    target_price=(
                        previous.target_price
                        if target_value == Decimal(previous.target_percentage)
                        else format(prices.target_price, "f")
                    ),
                    stop_price=(
                        format(chosen_stop, "f")
                        if exact_stop is not None
                        else previous.stop_price
                        if stop_value == Decimal(previous.stop_percentage)
                        else format(prices.stop_price, "f")
                    ),
                    target_percentage=format(target_value, "f"),
                    stop_percentage=format(stop_value, "f"),
                    tif="GTC",
                )
            )
        self._drafts[self._selected_con_id] = tuple(layers)
        if not layers:
            self._position_stop_config.pop(self._selected_con_id, None)
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
                self._message = (
                    "A pending target price is invalid; review TWS and Refresh."
                )
                return
            if any(not price.is_finite() or price <= 0 for price in previous_targets):
                self._message = (
                    "A pending target price is invalid; review TWS and Refresh."
                )
                return
            target_presets = (
                _parse_presets(self._target_presets, maximum=Decimal("1000")) or ()
            )
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
            self._message = (
                "Refresh a position with available contracts before building a draft."
            )
            return
        if self._current_layers():
            self._message = (
                "A draft already exists. Edit its layers or remove them first."
            )
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
            self._message = (
                "The selected position does not have a usable price increment."
            )
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
        self._expire_confirmation_locked()
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
        elif (
            state.status is UiStatus.READY
            and not state.positions
            and not self._session_closed_positions
        ):
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
            )
            if self._observe_positions
            else None,
            self._header(),
            content,
            self._toast_component(),
            self._launch_connection_dialog(),
            self._submission_review_dialog(),
            recovery_dialog,
            Script(_busy_submit_script()),
            Script(_paper_confirmation_countdown_script()),
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
                H1(
                    "No option positions detected",
                    cls="mt-6 text-2xl font-semibold tracking-tight",
                ),
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
        notice = (
            self._toast
            if self._toast_revision > self._toast_rendered_revision
            else None
        )
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
                    else (
                        "Enter your paper account ID to connect to TWS."
                        if not self._settings.account
                        else "Check the account ID and TWS API settings, then retry."
                    ),
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
                            Label(
                                "Paper account ID",
                                fr="launch-account",
                                cls="text-sm font-medium",
                            ),
                            Input(
                                id="launch-account",
                                name="account",
                                value=self._settings.account,
                                required=True,
                                autocomplete="off",
                                aria_invalid="true"
                                if self._launch_account_error
                                else None,
                            ),
                            P(
                                self._launch_account_error,
                                role="alert",
                                cls="text-sm text-destructive",
                            )
                            if self._launch_account_error
                            else None,
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
                            cls="w-full space-y-3",
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
        elif self._observe_positions and self._observer_health in {
            "error",
            "disconnected",
            "client-id-in-use",
        }:
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
            if (
                self._observer_health in {"error", "disconnected", "client-id-in-use"}
                or self._launch_connection == "failed"
            ):
                initial_label = "TWS updates unavailable"
                initial_state = "warning"
            elif (
                self._launch_connection == "connecting"
                or self._observer_health == "connecting"
            ):
                initial_label = "Connecting to TWS"
                initial_state = "starting"
            else:
                initial_label = "Checking TWS updates"
                initial_state = "starting"
            connection = Div(
                Icon(
                    "lucide:radio",
                    cls="size-4 shrink-0 tws-updates-icon",
                    aria_hidden="true",
                ),
                Span(
                    initial_label,
                    id="tws-updates-label",
                    cls="text-xs font-medium whitespace-nowrap",
                    aria_live="polite",
                ),
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
                _button_tooltip(
                    Button(
                        "Refresh",
                        variant="outline",
                        size="sm",
                        type="submit",
                        data_busy_text="Refreshing…",
                    ),
                    "Get the latest positions and orders from TWS",
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
        entries = (
            reader(account=self._settings.account, con_id=con_id)
            if callable(reader)
            else ()
        )
        rows: list[Any] = []
        realized = Decimal("0")
        pnl_verified = True
        has_recorded_fill = any(entry.fills for entry in entries)
        for entry in entries:
            for index in range(len(entry.layers)):
                outcome = classify_journal_layer(
                    entry,
                    index,
                    active_perm_ids=frozenset(),
                    observed_perm_ids=frozenset(),
                )
                if outcome.status == "CANCELLED":
                    continue
                if outcome.status.startswith("CLOSED_"):
                    rows.append(
                        self._closed_layer_row(
                            len(rows) + 1,
                            entry,
                            index,
                            outcome,
                            recover_legacy=False,
                        )
                    )
                    if outcome.realized_pnl is not None and outcome.currency == "USD":
                        realized += outcome.realized_pnl
                    else:
                        pnl_verified = False
                else:
                    rows.append(
                        self._pending_layer_row(
                            len(rows) + 1,
                            entry,
                            index,
                            outcome,
                            read_only=True,
                        )
                    )
        for entry in self._trailing_entries(con_id):
            if entry.state != "SUBMITTED" or not entry.perm_ids:
                continue
            rows.append(
                self._trailing_layer_row(len(rows) + 1, entry, closed_position=True)
            )
            summary = _trailing_fill_summary(entry)
            if (
                summary is None
                or summary.quantity != entry.trailing_quantity
                or summary.realized_pnl is None
            ):
                pnl_verified = False
            else:
                realized += summary.realized_pnl
            has_recorded_fill = has_recorded_fill or bool(entry.fills)
        symbol, contract_detail = _position_identity(position.local_symbol)
        title = f"{symbol} {contract_detail}".strip()
        result = (
            _header_pnl(realized, "USD") if pnl_verified and has_recorded_fill else "—"
        )
        center = self._workspace_content(
            Div(
                H1(title, cls="min-w-0 text-2xl font-semibold tracking-tight"),
                Div(
                    self._add_layer_control(disabled=True),
                    cls="ml-auto flex flex-wrap items-center justify-end gap-2",
                ),
                cls="flex flex-wrap items-center gap-4",
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
            Div(
                ScrollArea(
                    ScrollArea(
                        Div(*rows, cls="oca-layer-list w-full"),
                        aria_label="Closed OCA layer rows",
                        orientation="horizontal",
                        cls="w-full",
                    )
                    if rows
                    else P(
                        "No closed fills or unresolved layers to show.",
                        cls="pt-8 text-sm text-muted-foreground",
                    ),
                    aria_label="OCA layers workspace",
                    orientation="vertical",
                    cls="h-full",
                ),
                cls="mt-5 min-h-0 flex-1 overflow-hidden",
            ),
            data_closed_session=True,
        )
        review = Div(
            Div(
                Span(
                    "ACTION REVIEW",
                    cls="text-xs font-semibold tracking-wide text-muted-foreground",
                ),
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
            Div(Button("Review order", disabled=True, cls="w-full"), cls="mx-4 mb-4"),
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
                                if position.con_id in self._position_changes.new_ids
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
                        P(
                            contract_detail,
                            cls="mt-1.5 w-full text-xs text-muted-foreground",
                        ),
                        variant="ghost",
                        type="submit",
                        cls=(
                            "h-auto min-h-20 w-full flex-col items-stretch justify-center gap-0 "
                            "rounded-none border-l-2 px-4 py-4 text-left hover:bg-accent "
                            + (
                                "border-emerald-400 bg-emerald-500/10"
                                if position.con_id == self._selected_closed_con_id
                                else "border-transparent"
                            )
                        ),
                    ),
                    HTMLInput(
                        type="hidden", name="action", value="select-session-closed"
                    ),
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
                    Span(
                        "CLOSED THIS SESSION",
                        cls="text-xs font-semibold tracking-wide text-muted-foreground",
                    ),
                    cls="border-t border-border px-3 py-4",
                )
                if closed_rows
                else None,
                *closed_rows,
                aria_label="Open option positions",
                cls="min-h-0 flex-1",
            ),
            cls="flex h-full min-h-0 flex-col overflow-hidden border-r border-border bg-card/30",
        )

    def _settings_dialog(self) -> Any:
        return Tooltip(
            TooltipTrigger(
                Dialog(
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
                                    Input(
                                        name="target_presets",
                                        value=self._target_presets,
                                    ),
                                ),
                                _field(
                                    "STP losses",
                                    Input(
                                        name="stop_presets", value=self._stop_presets
                                    ),
                                ),
                                cls="mt-3 grid grid-cols-2 gap-4",
                            ),
                            P(
                                "Comma-separated percentages. The final value repeats for later layers.",
                                cls="mt-3 text-xs leading-5 text-muted-foreground",
                            ),
                            Separator(cls="my-5"),
                            H3(
                                "Stop order for new layers", cls="text-sm font-semibold"
                            ),
                            Div(
                                ToggleGroup(
                                    ("STP", "STP"),
                                    ("STP LMT", "STP LMT"),
                                    type="single",
                                    value=self._default_stop_type,
                                    variant="outline",
                                    size="default",
                                    aria_label="Default stop order type for new layers",
                                    data_global_stop_type_group=True,
                                ),
                                Div(
                                    Label(
                                        "How far below the stop?",
                                        fr="global-stop-limit-offset",
                                        cls="text-xs font-medium text-muted-foreground",
                                    ),
                                    Div(
                                        Input(
                                            id="global-stop-limit-offset",
                                            name="global_stop_limit_offset",
                                            type="number",
                                            min="0.1"
                                            if self._default_stop_limit_unit
                                            == "percent"
                                            else "0.01",
                                            max="99.9"
                                            if self._default_stop_limit_unit
                                            == "percent"
                                            else None,
                                            step="any",
                                            value=self._default_stop_limit_offset,
                                            disabled=self._default_stop_type
                                            != "STP LMT",
                                            cls="w-24",
                                        ),
                                        Fieldset(
                                            ToggleGroup(
                                                ("percent", "%"),
                                                ("dollars", "$"),
                                                type="single",
                                                value=self._default_stop_limit_unit,
                                                variant="outline",
                                                size="default",
                                                aria_label="Default stop-limit offset unit",
                                                data_global_stop_unit_group=True,
                                            ),
                                            id="global-stop-limit-units",
                                            disabled=self._default_stop_type
                                            != "STP LMT",
                                            cls="contents",
                                        ),
                                        cls="mt-2 flex items-center gap-2",
                                    ),
                                    cls="min-w-0",
                                ),
                                cls="mt-3 flex flex-wrap items-end gap-3",
                            ),
                            HTMLInput(
                                type="hidden",
                                name="global_stop_type",
                                value=self._default_stop_type,
                            ),
                            HTMLInput(
                                type="hidden",
                                name="global_stop_limit_unit",
                                value=self._default_stop_limit_unit,
                            ),
                            Script(_global_stop_type_visual_script()),
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
                ),
                delay_duration=250,
            ),
            TooltipContent("Change connection and new layer defaults"),
        )

    def _selected_quantity_notice(self) -> Any:
        change = self._position_changes.selected_change
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

    def _workspace_content(self, *children: Any, **attributes: Any) -> Any:
        locked = bool(self._unresolved_management_entries())
        if not locked:
            return Div(
                *children,
                cls="workspace-content flex min-w-0 min-h-0 flex-col overflow-hidden px-8 py-6",
                **attributes,
            )
        return Div(
            self._management_lock_notice(),
            Fieldset(*children, disabled=True, cls="contents"),
            cls="workspace-content flex min-w-0 min-h-0 flex-col overflow-hidden px-8 py-6",
            **attributes,
        )

    def _management_lock_notice(self) -> Any:
        entries = self._unresolved_management_entries()
        if not entries:
            return None
        return Alert(
            AlertTitle("Order status is uncertain — contract locked"),
            AlertDescription(
                "Check this contract's orders and fills in TWS, including any "
                "change awaiting Transmit. Refresh, then verify the state here."
            ),
            Form(
                Label(
                    HTMLInput(
                        type="checkbox", name="confirmed", value="yes", required=True
                    ),
                    "I checked TWS: no change is awaiting Transmit, and the displayed "
                    "orders and fills match this contract.",
                    cls="mt-3 flex items-start gap-2 text-sm",
                ),
                HTMLInput(type="hidden", name="action", value="verify-management"),
                HTMLInput(
                    type="hidden", name="fingerprint", value=entries[0].fingerprint
                ),
                Button("Verify order status", type="submit", cls="mt-3"),
                action=f"/{self.session_token}/action",
                method="post",
            ),
            data_contract_lockdown=True,
            cls="mb-4 shrink-0 border-amber-500/50 bg-amber-500/10 text-amber-100",
        )

    def _add_layer_control(self, *, disabled: bool) -> Any:
        return Tooltip(
            TooltipTrigger(
                Button(
                    Icon("lucide:plus", cls="size-4", aria_hidden="true"),
                    "Add Layer",
                    variant="default",
                    size="default",
                    type="submit",
                    form="draft-form",
                    name="action",
                    value="add-layer",
                    aria_label="Create new OCA bracket",
                    disabled=disabled,
                ),
                delay_duration=250,
            ),
            TooltipContent("Add a draft layer"),
        )

    def _workspace(self, title: str) -> Any:
        coverage, _, _ = self._order_coverage()
        active_pairs = self._active_oca_pairs()
        outcomes = self._submission_outcomes()
        trailing_entries = self._trailing_entries()
        active_target_ids = {target.perm_id for _group, target, _stop in active_pairs}
        pending = tuple(
            item
            for item in outcomes
            if item[2].status
            in {
                "PENDING",
                "UNKNOWN",
                "PARTIAL",
                "NO_EXECUTION_EVIDENCE",
                "GROUP_COLLISION",
            }
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
            if outcome.status in {
                "PARTIAL",
                "UNKNOWN",
                "NO_EXECUTION_EVIDENCE",
                "GROUP_COLLISION",
            }:
                realized = None
                break
            if not outcome.status.startswith("CLOSED_"):
                continue
            if outcome.realized_pnl is None or outcome.currency != currency:
                realized = None
                break
            if realized is not None:
                realized += outcome.realized_pnl
        if any(entry.fills for entry in trailing_entries):
            # Partial trailing fills are not included in the held/total and
            # outcome projection yet; avoid presenting zero as a verified result.
            realized = None
        return self._workspace_content(
            self._selected_quantity_notice(),
            Div(
                H1(title, cls="min-w-0 text-2xl font-semibold tracking-tight"),
                Div(
                    Div(
                        self._set_stops_dialog(
                            active_pairs,
                            basis,
                            quote,
                            selected_snapshot.fresh
                            if selected_snapshot is not None
                            else False,
                        )
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
                                    type="button",
                                    data_move_stops_to_be=True,
                                    aria_label="Move all active stops to B/E",
                                    disabled=self._paper_execution is None
                                    or bool(self._armed_price_updates)
                                    or not self._active_stop_limits_editable(
                                        active_pairs
                                    ),
                                ),
                                delay_duration=250,
                            ),
                            TooltipContent("Move every active stop to break even"),
                        ),
                        Tooltip(
                            TooltipTrigger(
                                Button(
                                    Icon(
                                        "lucide:trash-2",
                                        cls="size-4",
                                        aria_hidden="true",
                                    ),
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
                            TooltipContent("Cancel every active layer"),
                        ),
                        Tooltip(
                            TooltipTrigger(
                                Button(
                                    Icon(
                                        "lucide:log-out",
                                        cls="size-4",
                                        aria_hidden="true",
                                    ),
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
                            TooltipContent("Sell every active layer now"),
                        ),
                        self._trailing_conversion_dialog(),
                        cls="flex items-center gap-2",
                    )
                    if active_pairs
                    else None,
                    self._trailing_conversion_dialog()
                    if not active_pairs
                    and selected_snapshot is not None
                    and selected_snapshot.position.quantity > 0
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
                            TooltipContent("Split contracts across draft layers"),
                        ),
                        self._add_layer_control(
                            disabled=not draft_allowed
                            or planning_available <= 0
                            or len(self._current_layers()) >= planning_available,
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
                    "Available",
                    f"{planning_available:g}" if draft_allowed else "—",
                    adornment=self._coverage_dialog(
                        coverage,
                        selected_snapshot,
                        uncertain_app_orders=any(
                            outcome.status in {"PENDING", "UNKNOWN", "GROUP_COLLISION"}
                            for _entry, _index, outcome in outcomes
                        ),
                    ),
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
            self._submission_attention(outcomes),
            Div(
                ScrollArea(
                    self._existing_layers_panel(active_pairs, outcomes)
                    if active_pairs or outcomes or trailing_entries
                    else None,
                    Div(
                        self._draft_panel(
                            show_empty_state=not (
                                active_pairs or outcomes or trailing_entries
                            )
                        ),
                        cls=(
                            "mt-5 border-t border-border pt-4"
                            if (active_pairs or outcomes or trailing_entries)
                            and self._current_layers()
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
        )

    def _cancelled_bracket_recovery(
        self, outcomes: tuple[tuple[JournalEntry, int, LayerOutcome], ...]
    ) -> Any:
        if not callable(
            getattr(self._paper_execution, "confirm_cancelled_unknown", None)
        ):
            return None
        requested = self._recovery_requested_fingerprint
        requested_index = self._recovery_requested_layer_index
        entry = next(
            (
                candidate
                for candidate, _index, outcome in outcomes
                if candidate.fingerprint == requested
                and (requested_index is None or _index == requested_index)
                and candidate.state
                in {
                    "SUBMISSION_UNKNOWN",
                    "PARTIALLY_RECONCILED",
                    "SUBMITTED",
                    "RECONCILED",
                }
                and outcome.status
                in {
                    "PENDING",
                    "UNKNOWN",
                    "PARTIAL",
                    "NO_EXECUTION_EVIDENCE",
                    "GROUP_COLLISION",
                    "CONFLICT",
                }
            ),
            None,
        )
        if entry is None:
            self._recovery_requested_fingerprint = None
            return None
        fingerprint = entry.fingerprint
        if requested_index is None and len(entry.layers) != 1:
            return None
        selected_index = requested_index if requested_index is not None else 0

        return Div(
            Dialog(
                DialogContent(
                    DialogHeader(
                        DialogTitle(
                            f"Verify layer {selected_index + 1} bracket status"
                        ),
                        DialogDescription(
                            "Check both listed orders in TWS. Refresh to check for "
                            "working orders or fills. Only clear this layer if neither "
                            "order is working and neither filled."
                        ),
                    ),
                    Div(
                        *(
                            Div(
                                P(
                                    "Tranche ID · OCA group",
                                    cls="text-xs text-muted-foreground",
                                ),
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
                                    Span(
                                        f"{layer.stop_order_type} loss",
                                        cls="text-muted-foreground",
                                    ),
                                    Span(
                                        f"{layer.stop_price}"
                                        + (
                                            f" (LMT {layer.stop_limit_price})"
                                            if layer.stop_limit_price
                                            else ""
                                        )
                                        + f" · {layer.quantity} contracts",
                                    ),
                                    cls="mt-2 flex justify-between gap-3 text-sm",
                                ),
                                cls="rounded-md border border-border p-4",
                            )
                            for index, layer in enumerate(entry.layers)
                            if index == selected_index
                        ),
                        cls="grid max-h-[40vh] gap-3 overflow-y-auto",
                    ),
                    P(
                        self._message,
                        role="alert",
                        cls="text-sm text-destructive",
                    )
                    if self._message.startswith(
                        (
                            "Cancellation verification blocked:",
                            "Bracket not verified:",
                            "Confirm neither bracket leg",
                        )
                    )
                    else None,
                    Form(
                        P(
                            "If either order filled, refresh to recover the execution. "
                            "If the fill still does not appear here, investigate it in TWS "
                            "and leave this layer unverified.",
                            cls="text-sm text-muted-foreground",
                        ),
                        Label(
                            Checkbox(
                                name="confirmed",
                                value="yes",
                                required=True,
                                signal="bracket_absence_confirmed",
                            ),
                            Span(
                                "I confirm neither order is working in TWS and neither filled"
                            ),
                            cls="mt-3 flex items-start gap-2 text-sm",
                        ),
                        HTMLInput(
                            type="hidden",
                            name="action",
                            value="resolve-cancelled-bracket",
                        ),
                        HTMLInput(type="hidden", name="fingerprint", value=fingerprint),
                        HTMLInput(
                            type="hidden", name="layer_index", value=str(selected_index)
                        ),
                        action=f"/{self.session_token}/action",
                        method="post",
                        id="bracket-clear-form",
                    ),
                    Div(
                        Form(
                            Button(
                                "Refresh layers",
                                type="submit",
                                variant="outline",
                                data_busy_text="Refreshing layers…",
                            ),
                            HTMLInput(
                                type="hidden",
                                name="action",
                                value="verify-bracket-exists",
                            ),
                            HTMLInput(
                                type="hidden", name="fingerprint", value=fingerprint
                            ),
                            HTMLInput(
                                type="hidden",
                                name="layer_index",
                                value=str(selected_index),
                            ),
                            action=f"/{self.session_token}/action",
                            method="post",
                            id="bracket-refresh-form",
                        ),
                        Div(
                            DialogClose("Cancel", variant="outline"),
                            Button(
                                f"Clear layer {selected_index + 1}",
                                type="submit",
                                form="bracket-clear-form",
                                disabled=True,
                                data_attr_disabled=~Signal(
                                    "bracket_absence_confirmed", False
                                ),
                                data_busy_text="Checking absence…",
                            ),
                            cls="flex gap-2",
                        ),
                        cls="flex w-full flex-wrap items-center justify-between gap-3",
                    ),
                ),
                signal="cancelled_bracket_recovery",
                default_open=True,
                dismissible=True,
                size="lg",
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
            if outcome.status in {
                "PARTIAL",
                "UNKNOWN",
                "NO_EXECUTION_EVIDENCE",
                "GROUP_COLLISION",
            }:
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
        self,
        pairs: tuple[tuple[str, Any, Any], ...],
        basis: Decimal,
        quote: Any,
        snapshot_fresh: bool,
    ) -> Any:
        has_stop_limit = any(
            stop.order_type == "STP LMT" for _group, _target, stop in pairs
        )
        current_prices = {stop.stop_price for _group, _target, stop in pairs}
        initial_price = next(iter(current_prices)) if len(current_prices) == 1 else None
        initial_return = (
            _price_percentage(initial_price, basis, target=True)
            if initial_price is not None
            else ""
        )
        latest_bid = (
            quote.bid
            if snapshot_fresh
            and quote is not None
            and quote.fresh
            and quote.bid is not None
            and quote.bid.is_finite()
            and quote.bid > 0
            else None
        )
        bid_text = (
            f"${latest_bid:,.{max(2, -latest_bid.normalize().as_tuple().exponent)}f}"
            if latest_bid is not None
            else "Unavailable"
        )
        return Tooltip(
            TooltipTrigger(
                Dialog(
                    DialogTrigger(
                        Icon("lucide:arrow-up", cls="size-4", aria_hidden="true"),
                        variant="outline",
                        size="icon",
                        aria_label="Set all active stops",
                        disabled=self._paper_execution is None
                        or bool(self._armed_price_updates)
                        or not self._active_stop_limits_editable(pairs),
                        data_on_click=evt.currentTarget.blur(),
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
                                Label(
                                    "Stop price",
                                    fr="all-stop-value",
                                    data_stop_input_label=True,
                                    cls="text-xs font-medium text-muted-foreground",
                                ),
                                Span(
                                    f"{initial_return}% from entry"
                                    if initial_return
                                    else "—",
                                    data_stop_dialog_inverse=True,
                                    aria_live="polite",
                                    cls="text-xs font-semibold text-foreground",
                                ),
                                cls="flex items-center justify-between gap-2",
                            ),
                            Div(
                                ToggleGroup(
                                    ToggleGroupItem(
                                        "%",
                                        value="return",
                                        aria_label="Enter return percentage from entry",
                                    ),
                                    ToggleGroupItem(
                                        "$",
                                        value="price",
                                        aria_label="Enter stop price in dollars",
                                    ),
                                    type="single",
                                    value="price",
                                    variant="outline",
                                    size="default",
                                    aria_label="Stop value unit",
                                    data_stop_mode_group=True,
                                ),
                                Input(
                                    id="all-stop-value",
                                    type="number",
                                    min="0",
                                    step="any",
                                    value=_price_text(initial_price)
                                    if initial_price is not None
                                    else "",
                                    data_stop_dialog_value=True,
                                    cls="min-w-0 flex-1",
                                ),
                                cls="mt-1 flex items-center gap-2",
                            ),
                            cls="space-y-0.5",
                        ),
                        Div(
                            *(
                                Button(
                                    f"{pct:+d}%" if pct > 0 else f"{pct}%",
                                    type="button",
                                    variant="outline",
                                    data_stop_preset=str(pct),
                                )
                                for pct in (20, 0, -20, -25, -35)
                            ),
                            cls="flex flex-wrap gap-2",
                        ),
                        Div(
                            Label(
                                HTMLInput(
                                    type="checkbox", data_stop_limit_override=True
                                ),
                                " Use a new STP LMT offset for these layers",
                                cls="flex items-center gap-2 text-sm",
                            ),
                            Div(
                                Input(
                                    type="number",
                                    min="0",
                                    step="any",
                                    value="5",
                                    data_stop_limit_override_value=True,
                                    aria_label="New stop-limit offset",
                                    cls="w-24",
                                ),
                                ToggleGroup(
                                    ToggleGroupItem("%", value="percent"),
                                    ToggleGroupItem("$", value="dollars"),
                                    type="single",
                                    value="percent",
                                    variant="outline",
                                    size="default",
                                    data_stop_limit_override_group=True,
                                    aria_label="New stop-limit offset unit",
                                ),
                                cls="mt-2 flex items-center gap-2",
                            ),
                            Span(
                                "Leave unchecked to keep each layer's saved offset rule.",
                                cls="text-xs text-muted-foreground",
                            ),
                            cls="space-y-1" if has_stop_limit else "hidden",
                        ),
                        Div(
                            Div(
                                Span(
                                    "Active layers", cls="text-xs text-muted-foreground"
                                ),
                                Span(str(len(pairs)), cls="text-sm font-semibold"),
                                cls="flex items-center justify-between gap-4",
                            ),
                            _dialog_context_row(
                                "Entry cost",
                                f"${basis.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}",
                            ),
                            _dialog_context_row(
                                "Latest bid at refresh"
                                if latest_bid is not None
                                else "Latest bid",
                                bid_text,
                            ),
                            Div(
                                Span("Stop price", cls="text-xs text-muted-foreground"),
                                Span(
                                    "—",
                                    data_stop_dialog_summary=True,
                                    aria_live="polite",
                                    cls="text-right text-sm font-semibold",
                                ),
                                cls="flex items-center justify-between gap-4",
                            ),
                            cls="space-y-2 rounded-md border border-border bg-muted/20 px-4 py-3",
                        ),
                        DialogFooter(
                            DialogClose("Cancel", variant="outline"),
                            Button(
                                "Apply to active layers",
                                type="button",
                                data_apply_all_stops=True,
                            ),
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
            TooltipContent(
                "Older STP LMT layers have no saved offset rule"
                if not self._active_stop_limits_editable(pairs)
                else "Set every active stop to one price"
            ),
        )

    def _active_stop_limits_editable(
        self, pairs: tuple[tuple[str, Any, Any], ...]
    ) -> bool:
        lookup = getattr(self._paper_execution, "saved_stop_limit_rule", None)
        for _group, target, stop in pairs:
            if stop.order_type != "STP LMT":
                continue
            if (
                not callable(lookup)
                or lookup(
                    account=self._verified_selected_account(),
                    con_id=self._selected_con_id or 0,
                    target_perm_id=target.perm_id,
                    stop_perm_id=stop.perm_id,
                )
                is None
            ):
                return False
        return True

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
            stops = [
                order for order in orders if order.order_type in {"STP", "STP LMT"}
            ]
            if (
                len(targets) == 1
                and len(stops) == 1
                and len(orders) == 2
                and all(
                    order.status in {"Submitted", "PreSubmitted"} for order in orders
                )
                and (
                    stops[0].order_type == "STP"
                    or (
                        stops[0].stop_price is not None
                        and stops[0].limit_price is not None
                        and 0 < stops[0].limit_price < stops[0].stop_price
                    )
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
                )
                in ambiguous
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
                ).status
                == "CANCELLED"
            )
        )

    def _unresolved_management_entries(self) -> tuple[JournalEntry, ...]:
        if self._paper_execution is None or self._selected_con_id is None:
            return ()
        reader = getattr(self._paper_execution, "unresolved_management_entries", None)
        if not callable(reader):
            return ()
        entries: tuple[JournalEntry, ...] = reader(
            account=self._verified_selected_account(), con_id=self._selected_con_id
        )
        return entries

    def _pending_submissions(
        self,
    ) -> tuple[tuple[JournalEntry, int, LayerOutcome], ...]:
        return tuple(
            item
            for item in self._submission_outcomes()
            if item[2].status
            in {
                "PENDING",
                "UNKNOWN",
                "PARTIAL",
                "NO_EXECUTION_EVIDENCE",
                "GROUP_COLLISION",
            }
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
        self,
        number: int,
        entry: JournalEntry,
        index: int,
        outcome: LayerOutcome,
        *,
        read_only: bool = False,
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
                _LAYER_TARGET_LABEL,
                value=layer.target_percentage,
                price=layer.target_price,
                input_id=f"verify-target-{number}",
            ),
            _sold_percentage_price_field(
                _LAYER_STOP_LABEL,
                value=layer.stop_percentage,
                price=layer.stop_price,
                input_id=f"verify-stop-{number}",
                limit_price=layer.stop_limit_price or None,
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
            _button_tooltip(
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
                    cls="relative z-[3] mt-5",
                ),
                "Remove this cancelled layer from view",
            )
            if outcome.status == "CANCELLED" and not read_only
            else _button_tooltip(
                Button(
                    Icon("lucide:check", cls="size-4", aria_hidden="true"),
                    variant="outline",
                    size="icon",
                    type="submit",
                    name="action",
                    value=f"verify-cancelled-bracket:{entry.fingerprint}:{index}",
                    aria_label=f"Verify bracket status of layer {number}",
                    cls="relative z-[3] mt-5",
                ),
                "Check this layer in TWS",
            )
            if outcome.status
            in {
                "PENDING",
                "UNKNOWN",
                "PARTIAL",
                "NO_EXECUTION_EVIDENCE",
                "GROUP_COLLISION",
                "CONFLICT",
            }
            and not read_only
            else Div(cls="min-w-0"),
            Div(
                Span(
                    "CANCELLED"
                    if outcome.status == "CANCELLED"
                    else "RESOLVE IN TWS"
                    if outcome.status == "GROUP_COLLISION"
                    else "VERIFY IN TWS",
                    cls="sold-layer-status",
                ),
                Span(heading, cls="sold-layer-result"),
                cls="sold-layer-badge",
                title=detail,
            ),
            data_layer_state="cancelled" if outcome.status == "CANCELLED" else "verify",
            data_result_tone="cancelled" if outcome.status == "CANCELLED" else "verify",
            cls="sold-layer-row layer-row-grid items-start gap-3 border-t border-border py-4",
        )

    def _closed_layer_row(
        self,
        number: int,
        entry: JournalEntry,
        index: int,
        outcome: LayerOutcome,
        *,
        recover_legacy: bool = True,
    ) -> Any:
        layer = entry.layers[index]
        recovered = None
        calculator = self._state.quote_calculator
        if (
            recover_legacy
            and (not layer.target_percentage or not layer.stop_percentage)
            and calculator is not None
        ):
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
                _LAYER_TARGET_LABEL,
                value=target_percentage,
                price=layer.target_price,
                input_id=f"sold-target-{number}",
                inferred=not layer.target_percentage and bool(recovered),
            ),
            _sold_percentage_price_field(
                _LAYER_STOP_LABEL,
                value=stop_percentage,
                price=layer.stop_price,
                input_id=f"sold-stop-{number}",
                inferred=not layer.stop_percentage and bool(recovered),
                limit_price=layer.stop_limit_price or None,
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
            Div(cls="min-w-0"),
            Div(
                Span("SOLD", cls="sold-layer-status"),
                Span(result, cls="sold-layer-result"),
                cls="sold-layer-badge",
            ),
            data_layer_state="sold",
            data_result_tone=result_tone,
            cls="sold-layer-row layer-row-grid items-start gap-3 border-t border-border py-4",
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

    def _trailing_entries(self, con_id: int | None = None) -> tuple[JournalEntry, ...]:
        selected = con_id if con_id is not None else self._selected_con_id
        if self._paper_execution is None or selected is None:
            return ()
        reader = getattr(self._paper_execution, "trailing_entries", None)
        if not callable(reader):
            return ()
        return cast(
            tuple[JournalEntry, ...],
            reader(
                account=self._verified_selected_account(),
                con_id=selected,
            ),
        )

    def _trailing_conversion_dialog(self) -> Any:
        snapshot = self._view_model.latest_snapshot()
        selected = (
            snapshot
            if snapshot is not None
            and snapshot.selected.con_id == self._selected_con_id
            else None
        )
        currency = selected.contract.currency if selected is not None else "USD"
        quote = selected.quote if selected is not None else None
        display_quote = (
            quote
            if selected is not None
            and selected.fresh
            and quote is not None
            and quote.fresh
            else None
        )
        multiplier = selected.contract.multiplier if selected is not None else None
        preview_bid = display_quote.bid if display_quote is not None else None
        preview_stop: Decimal | None = None
        if (
            selected is not None
            and preview_bid is not None
            and preview_bid.is_finite()
            and multiplier is not None
            and multiplier.is_finite()
            and multiplier > 0
            and selected.market_rule.exchange == selected.contract.exchange
        ):
            raw_stop = preview_bid - Decimal("25") / multiplier
            if raw_stop > 0:
                try:
                    preview_stop = round_down_price(
                        raw_stop, selected.market_rule.bands
                    )
                except ValueError:
                    preview_stop = None
        preview_config = (
            {
                "bid": format(preview_bid, "f") if preview_bid is not None else None,
                "multiplier": format(multiplier, "f")
                if multiplier is not None
                else None,
                "quantity": format(selected.position.quantity, "f"),
                "basis": format(selected.position.unit_basis, "f"),
                "bands": [
                    {
                        "low": format(band.low_edge, "f"),
                        "increment": format(band.increment, "f"),
                    }
                    for band in selected.market_rule.bands
                ],
            }
            if selected is not None
            else None
        )
        preview_change = (
            (preview_stop - selected.position.unit_basis)
            * multiplier
            * selected.position.quantity
            if preview_stop is not None
            and preview_bid is not None
            and multiplier is not None
            and selected is not None
            else None
        )
        outcome_text = (
            f"{_money(preview_change)} {'gain' if preview_change >= 0 else 'estimated loss at stop'}"
            if preview_change is not None
            else "—"
        )
        dialog = Dialog(
            DialogTrigger(
                Icon("lucide:route", cls="size-4", aria_hidden="true"),
                variant="outline",
                size="icon",
                aria_label="Set a trailing exit for this position",
                disabled=self._paper_execution is None,
            ),
            DialogContent(
                DialogHeader(
                    DialogTitle("Set a trailing exit"),
                    DialogDescription(
                        "Choose a trailing stop or trailing limit for all held "
                        "contracts. Review the order before submitting it."
                    ),
                ),
                Form(
                    Div(
                        Div(
                            Label(
                                "Trail amount per contract",
                                fr="trail-value",
                                data_trail_value_label=True,
                                cls="text-xs font-medium text-muted-foreground",
                            ),
                            Span(
                                _header_price(preview_stop, currency),
                                data_trail_stop_preview=True,
                                aria_live="polite",
                                cls="text-xs font-semibold text-foreground",
                            ),
                            cls="flex flex-wrap items-center justify-between gap-2",
                        ),
                        Div(
                            ToggleGroup(
                                ("dollars", "$"),
                                ("percent", "%"),
                                type="single",
                                signal="trailing_amount_unit",
                                value="dollars",
                                variant="outline",
                                size="default",
                                aria_label="Trail amount unit",
                                data_trail_unit_group=True,
                            ),
                            HTMLInput(
                                type="hidden",
                                name="trail_unit",
                                data_bind=Signal(
                                    "trailing_amount_unit", _ref_only=True
                                ),
                            ),
                            Input(
                                id="trail-value",
                                name="trail_value",
                                type="number",
                                min="0.01",
                                step="any",
                                value="25",
                                required=True,
                                cls="min-w-0 flex-1",
                            ),
                            cls="flex w-full items-center gap-2",
                        ),
                        Div(
                            Span(
                                outcome_text,
                                data_trail_outcome_preview=True,
                                aria_live="polite",
                                cls="text-xs text-emerald-400"
                                if preview_change is not None and preview_change >= 0
                                else "text-xs text-rose-400",
                            ),
                            cls="flex justify-end",
                        ),
                        cls="space-y-1",
                    ),
                    Div(
                        Div(
                            Label(
                                "Optional limit offset per contract",
                                fr="trail-limit-value",
                                data_trail_limit_value_label=True,
                                cls="text-xs font-medium text-muted-foreground",
                            ),
                            Span(
                                "—",
                                data_trail_limit_price_preview=True,
                                aria_live="polite",
                                cls="text-xs font-semibold text-foreground",
                            ),
                            cls="flex flex-wrap items-center justify-between gap-2",
                        ),
                        Div(
                            ToggleGroup(
                                ("dollars", "$"),
                                ("percent", "%"),
                                type="single",
                                signal="trailing_limit_unit",
                                value="dollars",
                                variant="outline",
                                size="default",
                                aria_label="Trailing limit offset unit",
                                data_trail_limit_unit_group=True,
                            ),
                            HTMLInput(
                                type="hidden",
                                name="trail_limit_unit",
                                data_bind=Signal("trailing_limit_unit", _ref_only=True),
                            ),
                            Input(
                                id="trail-limit-value",
                                name="trail_limit_value",
                                type="number",
                                min="0.01",
                                step="any",
                                placeholder="Blank for trailing stop",
                                cls="min-w-0 flex-1",
                            ),
                            cls="flex w-full items-center gap-2",
                        ),
                        Div(
                            Span(
                                "—",
                                data_trail_limit_outcome_preview=True,
                                aria_live="polite",
                                cls="text-xs text-muted-foreground",
                            ),
                            cls="flex justify-end",
                        ),
                        cls="space-y-1",
                    ),
                    _dialog_price_context(
                        (
                            "Average position price",
                            _header_price(
                                selected.position.unit_basis
                                if selected is not None
                                else None,
                                currency,
                            ),
                        ),
                        (
                            "Latest bid",
                            _header_price(
                                display_quote.bid
                                if display_quote is not None
                                else None,
                                currency,
                            ),
                        ),
                    ),
                    DialogFooter(
                        DialogClose("Cancel", variant="outline"),
                        Button(
                            "Review trailing exit",
                            type="submit",
                            name="action",
                            value="trailing-convert-arm",
                            data_busy_text="Checking…",
                        ),
                        cls="mt-4",
                    ),
                    action=f"/{self.session_token}/action",
                    method="post",
                    cls="grid gap-4",
                ),
                Script(_trailing_preview_script(preview_config)),
            ),
        )
        return Tooltip(
            TooltipTrigger(dialog, delay_duration=250),
            TooltipContent("Set a trailing exit for this position"),
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
        for entry in self._trailing_entries():
            if entry.state == "SUBMITTED" and entry.perm_ids:
                rows.append(self._trailing_layer_row(len(rows) + 1, entry))

        return Form(
            ScrollArea(
                Div(*rows, cls="oca-layer-list w-full"),
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

    def _trailing_layer_row(
        self, index: int, entry: JournalEntry, *, closed_position: bool = False
    ) -> Any:
        order = next(
            (
                order
                for order in self._state.working_orders
                if not closed_position
                and self._selected_con_id == entry.con_id
                and order.perm_id == entry.perm_ids[0]
                and order.order_type in {"TRAIL", "TRAIL LIMIT"}
            ),
            None,
        )
        working = order is not None
        summary = _trailing_fill_summary(entry)
        sold = (
            closed_position
            and summary is not None
            and summary.quantity == entry.trailing_quantity
        )
        kind = "TRAIL LIMIT" if entry.trailing_limit_offset else "TRAIL"
        recorded_stop = entry.trailing_last_stop
        recorded_limit = entry.trailing_last_limit
        result = (
            f"{_money(summary.realized_pnl)} USD"
            if sold and summary is not None and summary.realized_pnl is not None
            else "P&L pending"
            if sold
            else "Verify order and fills"
        )
        result_tone = (
            "profit"
            if summary is not None
            and summary.realized_pnl is not None
            and summary.realized_pnl > 0
            else "loss"
            if summary is not None
            and summary.realized_pnl is not None
            and summary.realized_pnl < 0
            else "unknown"
        )
        return Div(
            Div(
                Span(f"TRAIL {index}", cls="text-xs font-semibold"),
                P(
                    "Closed" if sold else "WORKING" if working else "CHECK TWS",
                    cls="mt-2 text-xs text-muted-foreground",
                ),
                cls="min-w-20",
            ),
            _trailing_readonly_field(
                "STP" if sold else kind,
                (recorded_stop or "—") if sold else entry.trailing_value,
                input_id=(
                    f"trailing-stop-{entry.perm_ids[0]}"
                    if sold
                    else f"trailing-value-{entry.perm_ids[0]}"
                ),
                suffix="USD"
                if sold and recorded_stop
                else "%"
                if not sold and entry.trailing_unit == "percent"
                else "USD"
                if not sold
                else None,
            ),
            _trailing_readonly_field(
                "LMT",
                recorded_limit or "—",
                input_id=f"trailing-limit-{entry.perm_ids[0]}",
                suffix="USD" if recorded_limit else None,
            )
            if sold and entry.trailing_limit_offset
            else Div(cls="min-w-0")
            if sold
            else Div(
                _trailing_readonly_field(
                    "Last recorded STP",
                    recorded_stop or "—",
                    input_id=f"trailing-stop-{entry.perm_ids[0]}",
                    suffix="USD" if recorded_stop else None,
                ),
                _trailing_readonly_field(
                    "Last recorded LMT",
                    recorded_limit or "—",
                    input_id=f"trailing-limit-{entry.perm_ids[0]}",
                    suffix="USD" if recorded_limit else None,
                )
                if entry.trailing_limit_offset
                else None,
                cls="min-w-0 space-y-2",
            ),
            _trailing_readonly_field(
                "Quantity",
                format(summary.quantity, "f")
                if sold and summary is not None
                else str(order.remaining if order else entry.trailing_quantity),
                input_id=f"trailing-quantity-{entry.perm_ids[0]}",
                detail=(
                    f"{format(summary.quantity, 'f')} sold"
                    if working and summary is not None and summary.quantity > 0
                    else "Original order"
                    if not working and not sold
                    else None
                ),
            ),
            Div(cls="min-w-0"),
            Div(
                Span("SOLD" if sold else "VERIFY IN TWS", cls="sold-layer-status"),
                Span(result, cls="sold-layer-result"),
                cls="sold-layer-badge",
            )
            if sold or not working
            else None,
            data_layer_state="sold-trailing" if sold else "trailing",
            data_result_tone=result_tone if sold else "verify",
            data_trailing_perm_id=entry.perm_ids[0],
            cls=(
                "layer-row-grid items-start gap-3 border-t border-border py-4 "
                "first:border-t-0 trailing-layer-row"
                + (" sold-layer-row" if sold or not working else "")
            ),
        )

    def _active_layer_row(self, index: int, group: str, target: Any, stop: Any) -> Any:
        calculator = self._state.quote_calculator
        bands = calculator.bands if calculator is not None else ()
        rule_lookup = getattr(self._paper_execution, "saved_stop_limit_rule", None)
        saved_rule = (
            rule_lookup(
                account=self._verified_selected_account(),
                con_id=self._selected_con_id or 0,
                target_perm_id=target.perm_id,
                stop_perm_id=stop.perm_id,
            )
            if callable(rule_lookup) and stop.order_type == "STP LMT"
            else None
        )
        pending = self._pending_active_prices.get(target.perm_id)
        display_target_price = (
            pending[0] if pending and pending[0] is not None else target.limit_price
        )
        display_stop_price = (
            pending[2] if pending and pending[2] is not None else stop.stop_price
        )
        display_stop_limit_price = (
            pending[3] if pending and pending[3] is not None else stop.limit_price
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
        initial_target_percentage = target_percentage
        initial_stop_percentage = stop_percentage
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
                _LAYER_TARGET_LABEL,
                Input(
                    name=f"active_target_{target.perm_id}",
                    id=f"active-target-{index}",
                    type="number",
                    value=target_percentage,
                    min="0.1",
                    step="0.1",
                    disabled=bool(self._armed_price_updates)
                    or (stop.order_type == "STP LMT" and saved_rule is None),
                    data_active_input="target",
                    data_active_perm_id=target.perm_id,
                    data_active_original=display_target_price,
                    data_active_initial=initial_target_percentage,
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
                _LAYER_STOP_LABEL,
                Div(
                    Input(
                        name=f"active_stop_{target.perm_id}",
                        id=f"active-stop-{index}",
                        type="number",
                        value=stop_percentage,
                        min="-99.9",
                        step="any",
                        disabled=bool(self._armed_price_updates)
                        or (stop.order_type == "STP LMT" and saved_rule is None),
                        data_active_input="stop",
                        data_active_perm_id=target.perm_id,
                        data_active_original=display_stop_price,
                        data_active_stop_limit_price=display_stop_limit_price
                        if stop.order_type == "STP LMT"
                        else None,
                        data_active_stop_limit_offset=saved_rule[0]
                        if saved_rule
                        else None,
                        data_active_stop_limit_unit=saved_rule[1]
                        if saved_rule
                        else None,
                        data_active_initial=initial_stop_percentage,
                        data_live_layer=index,
                        cls="pr-8",
                    ),
                    HTMLInput(
                        type="hidden",
                        name=f"active_stop_price_{target.perm_id}",
                        value="",
                        data_active_exact_stop=target.perm_id,
                    ),
                    HTMLInput(
                        type="hidden",
                        name=f"active_stop_limit_offset_{target.perm_id}",
                        value="",
                        data_active_stop_limit_offset=target.perm_id,
                    ),
                    HTMLInput(
                        type="hidden",
                        name=f"active_stop_limit_unit_{target.perm_id}",
                        value="",
                        data_active_stop_limit_unit=target.perm_id,
                    ),
                ),
                input_id=f"active-stop-{index}",
                price=(
                    _price_text(display_stop_price)
                    + f" (LMT ${_price_text(display_stop_limit_price)})"
                    if stop.order_type == "STP LMT"
                    else _price_text(display_stop_price)
                ),
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
            action_field=_button_tooltip(
                Button(
                    Icon("lucide:trash-2"),
                    variant="outline",
                    size="icon",
                    type="submit",
                    name="action",
                    value=f"cancel-pair-arm:{target.perm_id}",
                    disabled=self._paper_execution is None,
                    aria_label=f"Delete OCA layer {index}",
                    cls="mt-5",
                ),
                "Cancel this active layer",
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

    def _coverage_dialog(
        self,
        coverage: str,
        snapshot: BrokerSnapshot | None,
        *,
        uncertain_app_orders: bool = False,
    ) -> Any:
        if coverage not in {"mixed", "external"}:
            return None
        held = snapshot.position.quantity if snapshot is not None else None
        available = self._state.available_quantity
        reserved = held - available if held is not None else None
        message = (
            f"{reserved:g} {'contract already has' if reserved == 1 else 'contracts already have'} exit orders in TWS. "
            f"{available:g} {'remains' if available == 1 else 'remain'} available for new brackets. "
            if reserved is not None and reserved >= 0
            else f"{available:g} {'contract remains' if available == 1 else 'contracts remain'} available for new brackets. "
        )
        return Tooltip(
            TooltipTrigger(
                Dialog(
                    DialogTrigger(
                        Icon("lucide:triangle-alert", cls="size-4", aria_hidden="true"),
                        variant="ghost",
                        aria_label="Why are fewer contracts available?",
                        cls="available-warning-trigger",
                    ),
                    DialogContent(
                        DialogHeader(
                            DialogTitle("Existing TWS exit orders"),
                            DialogDescription(
                                message
                                + (
                                    "An app submission is still unverified. Inspect TWS before "
                                    "changing these orders."
                                    if uncertain_app_orders
                                    else "Orders placed outside this app are view-only here."
                                )
                            ),
                        ),
                        DialogFooter(DialogClose("Done", variant="outline")),
                    ),
                    signal="existing_exit_orders",
                    size="sm",
                ),
                delay_duration=250,
            ),
            TooltipContent("See exit orders already in TWS"),
        )

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
        stop_type, _offset, unit = self._stop_configuration()
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
            else Div(
                self._position_stop_type_control(),
                ScrollArea(
                    Div(
                        *[
                            self._draft_layer_row(index, layer)
                            for index, layer in enumerate(layers, start=1)
                        ],
                        cls="oca-layer-list w-full",
                    ),
                    aria_label="Draft layer rows",
                    orientation="horizontal",
                    cls="w-full",
                ),
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
            HTMLInput(type="hidden", name="draft_stop_type", value=stop_type),
            HTMLInput(type="hidden", name="draft_stop_limit_unit", value=unit),
            Script(_live_draft_script(self._live_draft_configuration())),
            Script(_stop_type_visual_script()) if layers else None,
            id="draft-form",
            action=f"/{self.session_token}/action",
            method="post",
            cls="h-full" if not layers and show_empty_state else "",
        )

    def _position_stop_type_control(self) -> Any:
        stop_type, offset, unit = self._stop_configuration()
        locked = self._armed_execution is not None
        layers = self._current_layers()
        basis = self._state.unit_basis
        stop_presets = _parse_presets(self._stop_presets, maximum=Decimal("100")) or ()
        default_return = format(-stop_presets[0], "f") if stop_presets else ""
        return Div(
            Div(
                Tooltip(
                    TooltipTrigger(
                        Dialog(
                            DialogTrigger(
                                Icon(
                                    "lucide:arrow-up", cls="size-4", aria_hidden="true"
                                ),
                                variant="outline",
                                size="icon",
                                aria_label="Set all draft stops",
                                disabled=locked or basis is None,
                                data_on_click=evt.currentTarget.blur(),
                            ),
                            DialogContent(
                                DialogHeader(
                                    DialogTitle("Set all draft stops"),
                                    DialogDescription(
                                        "Choose a stop price or return from entry for every draft layer."
                                    ),
                                ),
                                Div(
                                    Label(
                                        "Return from entry",
                                        fr="all-draft-stop-value",
                                        data_draft_stop_input_label=True,
                                        cls="text-xs font-medium text-muted-foreground",
                                    ),
                                    Span(
                                        "—",
                                        data_draft_stop_dialog_inverse=True,
                                        aria_live="polite",
                                        cls="text-xs font-semibold",
                                    ),
                                    cls="flex items-center justify-between gap-2",
                                ),
                                Div(
                                    ToggleGroup(
                                        ToggleGroupItem(
                                            "%",
                                            value="return",
                                            aria_label="Enter return percentage from entry",
                                        ),
                                        ToggleGroupItem(
                                            "$",
                                            value="price",
                                            aria_label="Enter stop price in dollars",
                                        ),
                                        type="single",
                                        value="return",
                                        variant="outline",
                                        size="default",
                                        aria_label="Stop value unit",
                                        data_draft_stop_mode_group=True,
                                    ),
                                    Input(
                                        id="all-draft-stop-value",
                                        type="number",
                                        step="any",
                                        value=default_return,
                                        data_draft_stop_dialog_value=True,
                                        cls="min-w-0 flex-1",
                                    ),
                                    cls="mt-1 flex items-center gap-2",
                                ),
                                Div(
                                    *(
                                        Button(
                                            f"{pct:+d}%" if pct > 0 else f"{pct}%",
                                            type="button",
                                            variant="outline",
                                            data_draft_stop_preset=str(pct),
                                        )
                                        for pct in (20, 0, -20, -25, -35)
                                    ),
                                    cls="mt-4 flex flex-wrap gap-2",
                                ),
                                Div(
                                    Div(
                                        Span(
                                            "Draft layers",
                                            cls="text-xs text-muted-foreground",
                                        ),
                                        Span(
                                            str(len(layers)),
                                            cls="text-sm font-semibold",
                                        ),
                                        cls="flex items-center justify-between gap-4",
                                    ),
                                    Div(
                                        Span(
                                            "Entry cost",
                                            cls="text-xs text-muted-foreground",
                                        ),
                                        Span(
                                            f"${basis:,.2f}"
                                            if basis is not None
                                            else "—",
                                            cls="text-sm font-semibold",
                                        ),
                                        cls="flex items-center justify-between gap-4",
                                    ),
                                    Div(
                                        Span(
                                            "Stop price",
                                            cls="text-xs text-muted-foreground",
                                        ),
                                        Span(
                                            "—",
                                            data_draft_stop_dialog_summary=True,
                                            aria_live="polite",
                                            cls="text-right text-sm font-semibold",
                                        ),
                                        cls="flex items-center justify-between gap-4",
                                    ),
                                    cls="mt-4 space-y-2 rounded-md border border-border bg-muted/20 px-4 py-3",
                                ),
                                DialogFooter(
                                    DialogClose("Cancel", variant="outline"),
                                    Button(
                                        "Apply to draft layers",
                                        type="button",
                                        data_apply_all_draft_stops=True,
                                        disabled=True,
                                    ),
                                    cls="mt-4",
                                ),
                                data_draft_stop_dialog=True,
                            ),
                            data_on_focusin=evt.stopPropagation(),
                            data_on_focusout=evt.stopPropagation(),
                        ),
                        delay_duration=250,
                    ),
                    TooltipContent("Set every draft stop to one price"),
                ),
                Tooltip(
                    TooltipTrigger(
                        Button(
                            Icon("lucide:equal", cls="size-4", aria_hidden="true"),
                            type="button",
                            variant="outline",
                            size="icon",
                            aria_label="Move all draft stops to B/E",
                            data_move_draft_stops_to_be=True,
                            disabled=locked or basis is None,
                        ),
                        delay_duration=250,
                    ),
                    TooltipContent("Move every draft stop to break even"),
                ),
                cls="flex items-center gap-2",
            ),
            Div(
                ToggleGroup(
                    ("STP", "STP"),
                    ("STP LMT", "STP LMT"),
                    type="single",
                    value=stop_type,
                    variant="outline",
                    size="default",
                    disabled=locked,
                    aria_label="Protective order type for new layers",
                    data_draft_stop_type_group=True,
                ),
                Tooltip(
                    TooltipTrigger(
                        Dialog(
                            DialogTrigger(
                                Svg(
                                    Polygon(
                                        points="10 2 14 2 14.5 4 17 5 19 4 21 6 20 8 21 10 23 10 23 14 21 14 20 16 21 18 19 20 17 19 14.5 20 14 22 10 22 9.5 20 7 19 5 20 3 18 4 16 3 14 1 14 1 10 3 10 4 8 3 6 5 4 7 5 9.5 4"
                                    ),
                                    Circle(cx="12", cy="12", r="3"),
                                    viewBox="0 0 24 24",
                                    fill="none",
                                    stroke="currentColor",
                                    stroke_width="2",
                                    stroke_linejoin="round",
                                    cls="size-4",
                                    aria_hidden="true",
                                ),
                                variant="outline",
                                size="icon",
                                aria_label="Stop-limit settings",
                                disabled=locked or stop_type != "STP LMT",
                                data_stop_limit_settings_trigger=True,
                            ),
                            DialogContent(
                                DialogHeader(
                                    DialogTitle("Stop-limit settings"),
                                    DialogDescription(
                                        "Set the minimum sell price below each layer's stop trigger."
                                    ),
                                ),
                                Div(
                                    Label(
                                        "How far below the stop?",
                                        fr="stop-limit-offset",
                                        cls="text-sm font-medium",
                                    ),
                                    Div(
                                        Input(
                                            id="stop-limit-offset",
                                            name="draft_stop_limit_offset",
                                            type="number",
                                            min="0.1",
                                            step="any",
                                            value=offset,
                                            disabled=locked,
                                            data_stop_limit_offset=True,
                                            cls="min-w-0 flex-1",
                                        ),
                                        ToggleGroup(
                                            ("percent", "%"),
                                            ("dollars", "$"),
                                            type="single",
                                            value=unit,
                                            variant="outline",
                                            size="default",
                                            disabled=locked,
                                            aria_label="Stop-limit offset unit",
                                            data_stop_limit_unit_group=True,
                                            data_selected_unit=unit,
                                        ),
                                        cls="mt-2 flex items-center gap-2",
                                    ),
                                    cls="mt-5",
                                ),
                                DialogFooter(
                                    DialogClose("Done", variant="outline"), cls="mt-6"
                                ),
                            ),
                        ),
                        delay_duration=250,
                    ),
                    TooltipContent("Set how far below the stop a limit order can sell"),
                ),
                cls="flex items-center gap-2",
            ),
            cls="mb-3 flex items-center justify-between gap-2",
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
        gain, loss = self._layer_projection(layer)
        return _layer_row_layout(
            index=index,
            state="draft",
            target_field=_percentage_price_field(
                _LAYER_TARGET_LABEL,
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
            stop_field=Div(
                _percentage_price_field(
                    _LAYER_STOP_LABEL,
                    Input(
                        name=f"stop_{index}",
                        id=f"stop_{index}",
                        type="number",
                        value=layer.stop_percentage,
                        max="99.999999",
                        step="any",
                        data_live_input="stop",
                        data_live_layer=index,
                        data_live_initial=layer.stop_percentage,
                        data_live_original=layer.stop_price,
                        cls="pr-8",
                    ),
                    input_id=f"stop_{index}",
                    price=layer.stop_price,
                    outcome=loss,
                    outcome_label="at stop trigger"
                    if self._stop_configuration()[0] == "STP LMT"
                    else "max loss",
                    tone="text-rose-400",
                    layer_index=index,
                    kind="stop",
                ),
                HTMLInput(
                    type="hidden",
                    name=f"draft_stop_price_{index}",
                    data_draft_exact_stop=index,
                ),
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
            action_field=_button_tooltip(
                Button(
                    Icon("lucide:trash-2"),
                    variant="outline",
                    size="icon",
                    type="submit",
                    name="action",
                    value=f"remove-layer:{index}",
                    aria_label=f"Remove layer {index}",
                    cls="mt-5",
                ),
                "Remove this draft layer",
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
        sold_quantity = Decimal("0")
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
                    sold_quantity += outcome.filled_quantity
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
        for draft_layer in self._current_layers():
            try:
                quantity = Decimal(draft_layer.quantity)
                target_price = Decimal(draft_layer.target_price)
                stop_price = Decimal(draft_layer.stop_price)
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
                "unitCost": format(basis * multiplier, "f")
                if basis is not None and multiplier is not None
                else None,
                "soldQuantity": format(sold_quantity, "f"),
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
        gain_value = (
            outcome.covered_gain if partial or estimate else outcome.expected_gain
        )
        loss_value = outcome.covered_loss if partial or estimate else outcome.max_loss
        baseline_gain = baseline.covered_gain if partial else baseline.expected_gain
        baseline_loss = baseline.covered_loss if partial else baseline.max_loss
        if estimate:
            baseline_gain = None
            baseline_loss = None
        if partial and baseline.covered_quantity != outcome.covered_quantity:
            baseline_gain = None
            baseline_loss = None
        unit_cost = (
            Decimal(config["unitCost"]) if config["unitCost"] is not None else None
        )
        covered_cost = (
            unit_cost * outcome.covered_quantity if unit_cost is not None else None
        )
        gain_cost = (
            covered_cost + unit_cost * Decimal(config["soldQuantity"])
            if covered_cost is not None and unit_cost is not None
            else None
        )
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
        return Div(
            H3("Outcome projection", cls="mb-3 text-sm font-semibold"),
            Div(
                _metric(
                    "Expected gain",
                    _projection_gain_value(gain_value, gain_delta, gain_cost),
                    "text-emerald-400",
                    live_key="gain",
                    help_text=(
                        "Return percentage uses the cost of shown contracts and verified sold contracts. "
                        "Realised P&L plus projected target results from shown "
                        "layers. Pending bracket prices assume TWS accepts "
                        "the submitted exits; other contracts are excluded."
                        if Decimal(config["pendingQuantity"])
                        else "Return percentage uses the cost of shown contracts and verified sold contracts. "
                        "Realised P&L plus projected gains from the shown layers. "
                        "Excludes contracts without a verified target and stop."
                        if partial
                        else "Return percentage uses the cost of held and verified sold contracts. "
                        "Realised P&L plus projected gains from the current layer plan."
                    ),
                ),
                _metric(
                    "Max loss",
                    _projection_loss_value(loss_value, loss_delta, covered_cost),
                    "text-rose-400",
                    live_key="loss",
                    help_text=(
                        "Return percentage uses the cost of shown held contracts. "
                        "Projected stop results from shown layers; excludes "
                        "realised P&L. Pending stops may not be working in TWS."
                        if Decimal(config["pendingQuantity"])
                        else "Return percentage uses the cost of shown held contracts. "
                        "Projected result at the shown layer stops; excludes "
                        "realised P&L and contracts without a verified target and stop."
                        if partial
                        else "Return percentage uses the cost of held contracts. "
                        "Projected losses at the current layer stops; excludes realised P&L."
                    ),
                ),
                cls="outcome-projection-pair grid grid-cols-2",
            ),
            data_outcome_projection=True,
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
        trailing = self._armed_trailing
        price_updates = self._armed_price_updates
        armed_execution = self._armed_execution
        action_rows: list[Any] = []
        if trailing is not None:
            action_rows = [self._review_trailing_plan(trailing)]
        elif market_exits:
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
            Badge("TRAILING EXIT", variant="outline", cls="text-[10px]")
            if trailing is not None
            else Badge("MKT EXIT", variant="outline", cls="text-[10px]")
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
            Div(
                Div(self._outcome_projection(projection), cls="mx-4 mb-3"),
                self._review_alert() if draft_rows else None,
                self._execution_control(),
                cls="border-t border-border pt-4",
            ),
            cls="flex min-h-0 flex-col overflow-hidden border-l border-border bg-card/30",
        )

    def _review_alert(self) -> Any:
        available = self._planning_available_quantity()
        drafted = sum(_int_or_zero(layer.quantity) for layer in self._current_layers())
        over = drafted > available
        stop_limit = self._stop_configuration()[0] == "STP LMT"
        stop_description = (
            "If the stop price is reached, the order will only sell at "
            "your limit price or higher. If the market drops below that "
            "price, you may still own the option and lose more than shown above. "
            "If your offset reaches $0 or less, the limit uses the lowest positive "
            "price allowed by this option's price increments."
        )
        quantity_description = (
            f"{drafted} contracts drafted; {available} available. "
            "Reduce a layer's quantity."
        )
        return Alert(
            AlertTitle(
                "Draft exceeds available contracts"
                if over
                else "Your stop may not sell the option",
                data_review_alert_title=True,
            ),
            AlertDescription(
                quantity_description if over else stop_description,
                data_review_alert_description=True,
            ),
            data_review_alert=True,
            data_quantity_over="true" if over else "false",
            data_quantity_description=quantity_description,
            data_stop_description=stop_description,
            data_stop_limit_warning=True,
            live=True,
            cls=(
                "mx-4 mb-3 w-[calc(100%-2rem)] min-w-0 break-words "
                + (
                    "border-destructive/70 bg-red-950 text-red-50 [&_p]:text-red-100/90"
                    if over
                    else "border-amber-500/40 bg-amber-500/10 text-amber-100"
                )
                + ("" if over or stop_limit else " hidden")
            ),
        )

    def _execution_control(self) -> Any:
        if self._unresolved_management_entries():
            return Div(
                Button(
                    "Order changes locked", type="button", disabled=True, cls="w-full"
                ),
                cls="mx-4 mb-4 w-[calc(100%-2rem)]",
            )
        if self._pending_submissions():
            return Div(
                Button(
                    "Review order",
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
        if (
            self._armed_cancellation is not None
            or self._armed_cancellations
            or market_exits
            or self._armed_trailing is not None
        ) and not self._active_action_verified:
            return self._reviewed_active_action_controls()
        if self._armed_trailing is not None:
            return self._staged_action_controls(
                confirm_action="trailing-convert-confirm",
                busy_text="Setting trail…",
                impact=(
                    "Set trailing exit",
                    (
                        "Confirm cancels every reviewed app-owned bracket, then "
                        "submits one trailing SELL for all held contracts after a "
                        "fresh position check. There is a period without bracket "
                        "protection. A trailing limit can remain unfilled.",
                    )
                    if self._armed_trailing.candidates
                    else (
                        "Confirm submits one trailing SELL for all held contracts "
                        "after a fresh position check. A trailing limit can remain "
                        "unfilled."
                    ),
                ),
            )
        if self._armed_cancellations:
            return self._staged_action_controls(
                confirm_action="cancel-all-confirm",
                busy_text="Cancelling…",
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
                impact=(impact.title, impact.details) if impact.concerns else None,
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
            self._paper_execution is not None and available > 0 and has_draft_layers
        )
        return Div(
            self._cancel_changes_control(staged=False) if has_active_layers else None,
            Div(
                Button(
                    "Review order",
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
                    "Review price changes",
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
        market_exits = self._armed_market_exits or (
            (self._armed_market_exit,) if self._armed_market_exit is not None else ()
        )
        return Form(
            self._cancel_changes_control(staged=True),
            Div(
                Button(
                    "Review trailing exit"
                    if self._armed_trailing is not None
                    else "Review market sell"
                    if market_exits
                    else "Review cancellation",
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
        countdown_ms = (
            max(0, round((self._armed_execution_deadline - monotonic()) * 1000))
            if self._armed_execution_deadline is not None
            else None
        )
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
                    f"Confirm ({(countdown_ms + 999) // 1000}s)"
                    if countdown_ms is not None
                    else "Confirm",
                    variant="destructive",
                    type="submit",
                    name="action",
                    value=confirm_action,
                    data_busy_text=busy_text,
                    data_paper_confirm=True,
                    data_confirm_countdown_ms=countdown_ms,
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
                            "SELL STP",
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
                        self._review_order_line(
                            "SELL STP LMT",
                            _price_transition(
                                stop.limit_price,
                                None,
                                tone="text-rose-400",
                                current_attributes={
                                    "data_active_review_stop_limit": target.perm_id
                                },
                            ),
                            "text-rose-400",
                            row_attributes={
                                "data_active_review_stop_limit_row": target.perm_id
                            },
                            hidden=True,
                        )
                        if stop.order_type == "STP LMT"
                        else None,
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
            cls="hidden min-h-full flex-1 flex-col"
            if hidden
            else "flex min-h-full flex-1 flex-col",
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
        stop_limit = self._stop_configuration()[0] == "STP LMT"
        reviewed_limit = None
        locked_limit = None
        if index <= len(self._state.pairs):
            preview_pair = self._state.pairs[index - 1]
            if (
                str(preview_pair.stop_price) == layer.stop_price
                and str(preview_pair.target_price) == layer.target_price
                and preview_pair.quantity == _int_or_zero(layer.quantity)
            ):
                reviewed_limit = preview_pair.stop_limit_price
        if self._armed_execution is not None and index <= len(
            self._armed_execution.plan.pairs
        ):
            locked_limit = self._armed_execution.plan.pairs[index - 1].stop.limit_price
            reviewed_limit = locked_limit
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
                    row_attributes={"data_draft_review_stop_row": index},
                ),
                self._review_order_line(
                    "SELL STP LMT",
                    Span(
                        f"${_price_text(reviewed_limit)}"
                        if reviewed_limit is not None
                        else "—",
                        data_live_review_price=f"stop-limit-{index}",
                        data_reviewed_limit_price=_price_text(locked_limit)
                        if locked_limit is not None
                        else None,
                        aria_live="polite",
                        cls="text-sm font-semibold text-rose-400",
                    ),
                    "text-rose-400",
                    row_attributes={"data_draft_review_stop_limit_row": index},
                    hidden=not stop_limit,
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
                    "SELL STP",
                    _price_transition(
                        update.prior_stop_price,
                        update.stop_price,
                        tone="text-rose-400",
                    ),
                    "text-rose-400",
                )
            )
        if update.stop_limit_price is not None:
            rows.append(
                self._review_order_line(
                    "SELL STP LMT",
                    _price_transition(
                        update.prior_stop_limit_price,
                        update.stop_limit_price,
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
        row_cls = "action-review-layer py-4"
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

    def _review_trailing_plan(self, plan: TrailingPlan) -> Any:
        kind = "TRAIL LIMIT" if plan.limit_offset is not None else "TRAIL"
        trail = (
            f"{plan.request.trail_value}%"
            if plan.request.trail_unit == "percent"
            else f"${plan.request.trail_value * plan.contract.multiplier:,.2f} per contract"
        )
        stop_limit = plan.limit_offset is not None
        initial_limit = (
            plan.initial_stop - plan.limit_offset
            if plan.limit_offset is not None
            else None
        )

        def term(label: str, value: str, detail: str | None = None) -> Any:
            return Div(
                Span(label, cls="text-xs text-muted-foreground"),
                Span(value, cls="font-mono text-sm font-semibold tabular-nums"),
                Span(detail, cls="text-xs text-muted-foreground") if detail else None,
                cls="flex min-w-0 flex-col gap-0.5",
            )

        return Div(
            Div(
                H3(f"SELL {kind}", cls="text-base font-semibold"),
                Span(
                    f"{plan.quantity} contracts · {plan.request.tif}",
                    cls="font-mono text-xs font-semibold tabular-nums",
                ),
                cls="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1",
            ),
            Div(
                term("Trail amount", trail),
                term(
                    "Initial stop estimate",
                    f"${plan.initial_stop}",
                    f"from bid ${plan.reference_price}",
                ),
                term(
                    "Limit offset",
                    f"${plan.limit_offset * plan.contract.multiplier:,.2f} per contract",
                    f"${plan.limit_offset} below moving stop",
                )
                if stop_limit
                else None,
                term("Initial limit estimate", f"${initial_limit}")
                if stop_limit
                else None,
                cls="mt-4 grid grid-cols-2 gap-x-4 gap-y-4 border-y border-border py-4",
            ),
            P(
                f"Replace {len(plan.candidates)} app-owned "
                f"{'bracket' if len(plan.candidates) == 1 else 'brackets'} and "
                f"include {plan.unassigned_quantity} available "
                f"{'contract' if plan.unassigned_quantity == 1 else 'contracts'} "
                f"in one trailing SELL for all {plan.quantity} held contracts."
                if plan.candidates and plan.unassigned_quantity
                else f"Replace {len(plan.candidates)} app-owned "
                f"{'bracket' if len(plan.candidates) == 1 else 'brackets'} "
                f"with one trailing SELL for all {plan.quantity} held contracts."
                if plan.candidates
                else f"Place one trailing SELL for all {plan.quantity} held contracts.",
                cls="mt-4 text-xs leading-5 text-muted-foreground",
            ),
            Div(
                P("Fill risk", cls="text-xs font-semibold text-amber-200"),
                P(
                    "After the stop triggers, the limit order may remain unfilled "
                    "if the option falls below its limit. You may still own the "
                    "contracts and lose more than the initial stop suggests."
                    if stop_limit
                    else "The stop submits a market sell. The fill price can "
                    "differ from the initial stop estimate.",
                    cls="mt-1 text-xs leading-5 text-amber-100",
                ),
                P(
                    "Cancelling the brackets creates a period without their protection.",
                    cls="mt-2 text-xs leading-5 text-amber-100",
                )
                if plan.candidates
                else None,
                cls="mt-4 border border-amber-500/40 bg-amber-500/10 p-3",
            ),
            cls="action-review-layer py-4",
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
                cls="action-review-layer py-4",
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


def _trailing_preview_script(configuration: dict[str, Any] | None) -> str:
    """Show an illustrative trigger from the displayed bid and verified tick bands."""
    payload = json.dumps(configuration, separators=(",", ":"))
    return f"""
(() => {{
  const config = {payload};
  const form = document.querySelector('input[name="trail_value"]')?.closest('form');
  if (!form) return;
  const input = form.elements['trail_value'];
  const label = form.querySelector('[data-trail-value-label]');
  const preview = form.querySelector('[data-trail-stop-preview]');
  const outcome = form.querySelector('[data-trail-outcome-preview]');
  const group = form.querySelector('[data-trail-unit-group]');
  const limitInput = form.elements['trail_limit_value'];
  const limitLabel = form.querySelector('[data-trail-limit-value-label]');
  const limitPrice = form.querySelector('[data-trail-limit-price-preview]');
  const limitOutcome = form.querySelector('[data-trail-limit-outcome-preview]');
  const limitGroup = form.querySelector('[data-trail-limit-unit-group]');
  if (!input || !label || !preview || !outcome || !group || !limitInput || !limitLabel ||
      !limitPrice || !limitOutcome || !limitGroup) return;
  const bands = (config?.bands || []).map(band => ({{
    low: Number(band.low), increment: Number(band.increment)
  }}));
  const bid = Number(config?.bid);
  const multiplier = Number(config?.multiplier);
  const quantity = Number(config?.quantity);
  const basis = Number(config?.basis);
  const roundDown = (value) => {{
    let candidate = value;
    for (let attempt = 0; attempt <= bands.length; attempt += 1) {{
      const applicable = bands.filter(band => band.low <= candidate + 1e-9);
      const band = applicable[applicable.length - 1];
      if (!band || !(band.increment > 0)) return NaN;
      const rounded = Math.floor(value / band.increment + 1e-9) * band.increment;
      const atRounded = bands.filter(item => item.low <= rounded + 1e-9);
      if (atRounded[atRounded.length - 1] === band) return rounded;
      candidate = rounded;
    }}
    return NaN;
  }};
  const roundUp = (value) => {{
    let candidate = value;
    for (let attempt = 0; attempt <= bands.length; attempt += 1) {{
      const applicable = bands.filter(band => band.low <= candidate + 1e-9);
      const band = applicable[applicable.length - 1];
      if (!band || !(band.increment > 0)) return NaN;
      const rounded = Math.ceil(value / band.increment - 1e-9) * band.increment;
      const atRounded = bands.filter(item => item.low <= rounded + 1e-9);
      if (atRounded[atRounded.length - 1] === band) return rounded;
      candidate = rounded;
    }}
    return NaN;
  }};
  const displayOutcome = (node, change, negativeLabel) => {{
    const gain = Number.isFinite(change) && change >= 0;
    node.textContent = Number.isFinite(change)
      ? `${{change >= 0 ? '+' : '-'}}$${{Math.abs(change).toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}} ${{gain ? 'gain' : negativeLabel}}`
      : '—';
    node.classList.toggle('text-emerald-400', Number.isFinite(change) && gain);
    node.classList.toggle('text-rose-400', Number.isFinite(change) && !gain);
    node.classList.toggle('text-muted-foreground', !Number.isFinite(change));
  }};
  const refresh = () => {{
    const unit = form.elements['trail_unit']?.value || 'dollars';
    label.textContent = unit === 'percent' ? 'Trail amount (%)' : 'Trail amount per contract';
    const amount = Number(input.value);
    let stop = NaN;
    if (input.value.trim() && amount > 0 && Number.isFinite(bid) && bid > 0) {{
      const trail = unit === 'percent' ? bid * amount / 100 : amount / multiplier;
      if (Number.isFinite(trail) && trail > 0 && bid - trail > 0) {{
        stop = roundDown(bid - trail);
      }}
    }}
    const valid = Number.isFinite(stop) && stop > 0 && stop < bid;
    preview.textContent = valid ? '$' + Number(stop.toFixed(6)).toString() : '—';
    const change = valid && Number.isFinite(multiplier) && multiplier > 0 &&
      Number.isFinite(quantity) && quantity > 0 && Number.isFinite(basis) && basis > 0
      ? (stop - basis) * multiplier * quantity : NaN;
    displayOutcome(outcome, change, 'estimated loss at stop');
    const offsetValue = Number(limitInput.value);
    const limitUnit = form.elements['trail_limit_unit']?.value || 'dollars';
    limitLabel.textContent = limitUnit === 'percent'
      ? 'Optional limit offset below stop (%)' : 'Optional limit offset per contract';
    const rawOffset = limitUnit === 'percent' ? stop * offsetValue / 100 : offsetValue / multiplier;
    const offset = valid && limitInput.value.trim() && offsetValue > 0 &&
      Number.isFinite(rawOffset) ? roundUp(rawOffset) : NaN;
    const initialLimit = Number.isFinite(offset) && stop - offset > 0 ? stop - offset : NaN;
    limitPrice.textContent = Number.isFinite(initialLimit)
      ? '$' + Number(initialLimit.toFixed(6)).toString() : '—';
    const limitChange = Number.isFinite(initialLimit) && Number.isFinite(change)
      ? (initialLimit - basis) * multiplier * quantity : NaN;
    displayOutcome(limitOutcome, limitChange, 'estimated loss at limit');
  }};
  input.addEventListener('input', refresh);
  limitInput.addEventListener('input', refresh);
  group.querySelectorAll('[data-value]').forEach(button => button.addEventListener('click', () => {{
    form.elements['trail_unit'].value = button.dataset.value;
    refresh();
  }}));
  limitGroup.querySelectorAll('[data-value]').forEach(button => button.addEventListener('click', () => {{
    form.elements['trail_limit_unit'].value = button.dataset.value;
    refresh();
  }}));
  refresh();
}})();
"""


def _global_stop_type_visual_script() -> str:
    return """
(() => {
  const start = () => {
    const buttons = document.querySelectorAll('[data-global-stop-type-group] [data-value]');
    const offset = document.getElementById('global-stop-limit-offset');
    const selection = document.querySelector('[name="global_stop_type"]');
    const unitSelection = document.querySelector('[name="global_stop_limit_unit"]');
    const unitFieldset = document.getElementById('global-stop-limit-units');
    const unitButtons = document.querySelectorAll('[data-global-stop-unit-group] [data-value]');
    buttons.forEach((button) => button.addEventListener('click', () => {
      if (selection) selection.value = button.dataset.value;
      if (offset) offset.disabled = button.dataset.value !== 'STP LMT';
      if (unitFieldset) unitFieldset.disabled = button.dataset.value !== 'STP LMT';
    }));
    unitButtons.forEach((button) => button.addEventListener('click', () => {
      if (unitSelection) unitSelection.value = button.dataset.value;
      if (offset) {
        offset.min = button.dataset.value === 'percent' ? '0.1' : '0.01';
        if (button.dataset.value === 'percent') offset.max = '99.9';
        else offset.removeAttribute('max');
      }
      unitButtons.forEach((choice) => {
        const selected = choice === button;
        choice.setAttribute('aria-checked', String(selected));
        choice.dataset.state = selected ? 'on' : 'off';
      });
    }));
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
"""


def _stop_type_visual_script() -> str:
    """Synchronize stop-limit draft controls before server-side validation."""
    return """
(() => {
  const start = () => {
    const form = document.getElementById('draft-form');
    if (!form) return;
    const mode = form.elements['draft_stop_type'];
    const settings = form.querySelector('[data-stop-limit-settings-trigger]');
    const group = form.querySelector('[data-draft-stop-type-group]');
    const unitGroup = form.querySelector('[data-stop-limit-unit-group]');
    const refreshPrices = () => form.dispatchEvent(new Event('stop-limit-settings-change'));
    const reviewAlert = document.querySelector('[data-review-alert]');
    const refreshAlert = () => {
      if (!reviewAlert) return;
      const over = reviewAlert.dataset.quantityOver === 'true';
      const stopLimit = mode.value === 'STP LMT';
      reviewAlert.classList.toggle('hidden', !over && !stopLimit);
      reviewAlert.classList.toggle('border-destructive/70', over);
      reviewAlert.classList.toggle('bg-red-950', over);
      reviewAlert.classList.toggle('text-red-50', over);
      reviewAlert.classList.toggle('[&_p]:text-red-100/90', over);
      reviewAlert.classList.toggle('border-amber-500/40', !over);
      reviewAlert.classList.toggle('bg-amber-500/10', !over);
      reviewAlert.classList.toggle('text-amber-100', !over);
      reviewAlert.querySelector('[data-review-alert-title]').textContent = over
        ? 'Draft exceeds available contracts' : 'Your stop may not sell the option';
      reviewAlert.querySelector('[data-review-alert-description]').textContent = over
        ? reviewAlert.dataset.quantityDescription : reviewAlert.dataset.stopDescription;
    };
    document.addEventListener('draft-quantity-change', refreshAlert);
    refreshAlert();
    group?.querySelectorAll('[data-value]').forEach((button) => {
      button.addEventListener('click', () => {
        mode.value = button.dataset.value;
        if (settings) settings.disabled = mode.value !== 'STP LMT';
        refreshAlert();
        refreshPrices();
      });
    });
    unitGroup?.querySelectorAll('[data-value]').forEach((button) => {
      button.addEventListener('click', () => {
        unitGroup.dataset.selectedUnit = button.dataset.value;
        form.elements['draft_stop_limit_unit'].value = button.dataset.value;
        refreshPrices();
      });
    });
    form.querySelector('[data-stop-limit-offset]')?.addEventListener('input', refreshPrices);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
"""


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
    const roundDown = (number) => {{
      let candidate = number;
      for (let attempt = 0; attempt <= bands.length; attempt += 1) {{
        const candidates = bands.filter((item) => item.low <= candidate + 1e-9);
        const band = candidates[candidates.length - 1];
        if (!band) return NaN;
        const rounded = Math.floor(number / band.increment + 1e-9) * band.increment;
        const roundedCandidates = bands.filter((item) => item.low <= rounded + 1e-9);
        if (roundedCandidates[roundedCandidates.length - 1] === band) return rounded;
        candidate = rounded;
      }}
      return NaN;
    }};
    const minimumPositivePrice = Math.min(...bands.map((band) => roundUp(Math.max(band.low, band.increment))));
    const update = () => {{
      const outcomes = [];
      let invalid = false;
      let assignedQuantity = 0;
      let quantitiesValid = true, limitsValid = true;
      const stopLimit = form.elements['draft_stop_type']?.value === 'STP LMT';
      const offset = Number(form.querySelector('[data-stop-limit-offset]')?.value);
      const unit = form.querySelector('[data-stop-limit-unit-group]')?.dataset.selectedUnit || 'percent';
      form.querySelectorAll('[data-live-input="target"]').forEach((input) => {{
        const index = input.dataset.liveLayer;
        const stopInput = form.elements[`stop_${{index}}`];
        const exactStopInput = form.elements[`draft_stop_price_${{index}}`];
        const target = value('target', index), stop = value('stop', index);
        const quantity = value('quantity', index);
        const quantityValid = Number.isInteger(quantity) && quantity >= 1 && quantity <= config.available;
        const ring = form.querySelector(`[data-quantity-ring="${{index}}"]`);
        const share = form.querySelector(`[data-quantity-share="${{index}}"]`);
        if (ring) ring.style.setProperty('--quantity-share', `${{quantityValid ? Math.min(100, quantity / config.available * 100) : 0}}%`);
        if (share) share.textContent = quantityValid ? `${{quantity}} of ${{config.available}} available contracts (${{Math.round(quantity / config.available * 100)}}%)` : `Enter 1 to ${{config.available}} available contracts`;
        if (Number.isInteger(quantity) && quantity > 0) assignedQuantity += quantity;
        if (!quantityValid) quantitiesValid = false;
        const inputValid = Number.isFinite(target) && target > 0 && Number.isFinite(stop) && stop < 100 && quantityValid;
        const targetPrice = inputValid ? (target === Number(input.dataset.liveInitial) ? Number(input.dataset.liveOriginal) : roundUp(basis * (1 + target / 100))) : NaN;
        const stopPrice = inputValid ? (exactStopInput?.value ? Number(exactStopInput.value) : stop === Number(stopInput?.dataset.liveInitial) ? Number(stopInput.dataset.liveOriginal) : roundUp(basis * (1 - stop / 100))) : NaN;
        const valid = inputValid && Number.isFinite(stopPrice) && Number.isFinite(targetPrice) && stopPrice < targetPrice;
        const rawLimit = unit === 'dollars' ? stopPrice - offset : stopPrice * (1 - offset / 100);
        const roundedLimit = rawLimit > 0 ? roundDown(rawLimit) : 0;
        const limitPrice = stopLimit && Number.isFinite(stopPrice) && Number.isFinite(offset) && offset > 0
          ? (roundedLimit > 0 ? roundedLimit : minimumPositivePrice) : NaN;
        if (stopLimit && (!Number.isFinite(limitPrice) || limitPrice <= 0 || limitPrice >= stopPrice)) limitsValid = false;
        const limitLabel = form.querySelector(`[data-live-stop-limit-price="${{index}}"]`);
        if (limitLabel) {{
          limitLabel.textContent = stopLimit ? `(LMT ${{Number.isFinite(limitPrice) ? priceText(limitPrice) : '—'}})` : '';
          limitLabel.classList.toggle('hidden', !stopLimit);
        }}
        document.querySelectorAll(`[data-draft-review-stop-limit-row="${{index}}"]`).forEach((row) => {{
          row.classList.toggle('hidden', !stopLimit);
        }});
        document.querySelectorAll(`[data-live-review-price="stop-limit-${{index}}"]`).forEach((node) => {{
          node.textContent = node.dataset.reviewedLimitPrice ? `$${{node.dataset.reviewedLimitPrice}}` : Number.isFinite(limitPrice) ? priceText(limitPrice) : '—';
        }});
        const gain = valid && Number.isFinite(targetPrice) ? (targetPrice - basis) * multiplier * quantity : NaN;
        const loss = valid && Number.isFinite(stopPrice) ? (stopPrice - basis) * multiplier * quantity : NaN;
        assigned(`[data-live-price="target-${{index}}"]`, Number.isFinite(targetPrice) ? (target === Number(input.dataset.liveInitial) ? `$${{input.dataset.liveOriginal}}` : priceText(targetPrice)) : '—');
        assigned(`[data-live-price="stop-${{index}}"]`, Number.isFinite(stopPrice) ? (exactStopInput?.value ? priceText(stopPrice) : stop === Number(stopInput?.dataset.liveInitial) ? `$${{stopInput.dataset.liveOriginal}}` : priceText(stopPrice)) : '—');
        assigned(`[data-live-review-price="target-${{index}}"]`, Number.isFinite(targetPrice) ? sellPriceText(targetPrice, target === Number(input.dataset.liveInitial) ? `$${{input.dataset.liveOriginal}}` : priceText(targetPrice)) : '—');
        assigned(`[data-live-review-price="stop-${{index}}"]`, Number.isFinite(stopPrice) ? sellPriceText(stopPrice, !exactStopInput?.value && stop === Number(stopInput?.dataset.liveInitial) ? `$${{stopInput.dataset.liveOriginal}}` : priceText(stopPrice)) : '—');
        assigned(`[data-live-outcome="target-${{index}}"]`, Number.isFinite(gain) ? `${{money(gain)}} gain` : '— gain');
        assigned(`[data-live-outcome="stop-${{index}}"]`, Number.isFinite(loss) ? `${{money(loss)}} ${{stopLimit ? 'at stop trigger' : 'max loss'}}` : `— ${{stopLimit ? 'at stop trigger' : 'max loss'}}`);
        assigned(`[data-live-review-quantity="${{index}}"]`, `${{quantityValid ? quantity : '—'}} contracts · GTC`);
        if (Number.isFinite(gain) && Number.isFinite(loss)) outcomes.push({{ quantity, gain, loss }});
        else invalid = true;
      }});
      const over = assignedQuantity > config.available;
      const quantityAlert = document.querySelector('[data-review-alert]');
      if (quantityAlert) {{
        quantityAlert.dataset.quantityOver = over ? 'true' : 'false';
        quantityAlert.dataset.quantityDescription = `${{assignedQuantity}} contracts drafted; ${{config.available}} available. Reduce a layer's quantity.`;
        document.dispatchEvent(new Event('draft-quantity-change'));
      }}
      const executeButton = document.querySelector('[data-draft-execute] [data-execute-enabled]');
      if (executeButton) executeButton.disabled = executeButton.dataset.executeEnabled !== 'true' || assignedQuantity <= 0 || over || !quantitiesValid || !limitsValid;
      invalid = invalid || !quantitiesValid || !limitsValid || over;
      window.ibkrProjection?.updateDraft(outcomes, invalid);
    }};
    document.querySelectorAll('[data-move-draft-stops-to-be]').forEach((button) => button.addEventListener('click', () => {{
      form.querySelectorAll('[data-live-input="stop"]').forEach((input) => {{ input.value = '0'; }});
      form.querySelectorAll('[data-draft-exact-stop]').forEach((input) => {{ input.value = ''; }});
      update();
    }}));
    const draftStopDialog = document.querySelector('[data-draft-stop-dialog]');
    if (draftStopDialog) {{
      const valueInput = draftStopDialog.querySelector('[data-draft-stop-dialog-value]');
      const inputLabel = draftStopDialog.querySelector('[data-draft-stop-input-label]');
      const inverse = draftStopDialog.querySelector('[data-draft-stop-dialog-inverse]');
      const summary = draftStopDialog.querySelector('[data-draft-stop-dialog-summary]');
      const apply = draftStopDialog.querySelector('[data-apply-all-draft-stops]');
      const modeGroup = draftStopDialog.querySelector('[data-draft-stop-mode-group]');
      let mode = 'return';
      const showMode = () => {{
        inputLabel.textContent = mode === 'price' ? 'Stop price' : 'Return from entry';
      }};
      const previewStop = () => {{
        const raw = valueInput.value;
        const number = Number(raw);
        const proposed = mode === 'price' ? number : basis * (1 + number / 100);
        const rounded = raw.trim() && Number.isFinite(proposed) && proposed > 0 ? roundUp(proposed) : NaN;
        const rate = Number.isFinite(rounded) ? (rounded / basis - 1) * 100 : NaN;
        const targets = [...form.querySelectorAll('[data-live-input="target"]')].map((input) => {{
          const target = Number(input.value);
          return target === Number(input.dataset.liveInitial) ? Number(input.dataset.liveOriginal) : roundUp(basis * (1 + target / 100));
        }});
        const valid = Number.isFinite(rounded) && targets.every((target) => Number.isFinite(target) && rounded < target);
        inverse.textContent = Number.isFinite(rounded) ? (mode === 'price' ? `${{rate >= 0 ? '+' : ''}}${{Number(rate.toFixed(2))}}% from entry` : priceText(rounded)) : '—';
        summary.textContent = Number.isFinite(rounded) ? `${{priceText(rounded)}} (${{rate >= 0 ? '+' : ''}}${{Number(rate.toFixed(2))}}%)` : '—';
        apply.disabled = !valid;
        return {{ rounded, valid }};
      }};
      valueInput.addEventListener('input', previewStop);
      modeGroup?.querySelectorAll('[data-value]').forEach((button) => button.addEventListener('click', () => {{
        const current = previewStop();
        if (Number.isFinite(current.rounded)) valueInput.value = button.dataset.value === 'price'
          ? String(Number(current.rounded.toFixed(6)))
          : String(Number(((current.rounded / basis - 1) * 100).toFixed(4)));
        mode = button.dataset.value;
        showMode(); previewStop(); valueInput.focus();
      }}));
      draftStopDialog.querySelectorAll('[data-draft-stop-preset]').forEach((button) => button.addEventListener('click', () => {{
        modeGroup?.querySelector('[data-value="return"]')?.click();
        mode = 'return'; valueInput.value = button.dataset.draftStopPreset;
        showMode(); previewStop();
      }}));
      apply.addEventListener('click', () => {{
        const choice = previewStop();
        if (!choice.valid) return;
        const stopLoss = mode === 'return'
          ? -Number(valueInput.value)
          : (1 - choice.rounded / basis) * 100;
        form.querySelectorAll('[data-live-input="stop"]').forEach((input) => {{
          input.value = String(Number(stopLoss.toFixed(1)));
          form.elements[`draft_stop_price_${{input.dataset.liveLayer}}`].value = String(choice.rounded);
        }});
        update();
        draftStopDialog.closest('dialog')?.close();
      }});
      showMode(); previewStop();
    }}
    form.querySelectorAll('[data-live-input]').forEach((input) => input.addEventListener('input', () => {{
      if (input.dataset.liveInput === 'stop') form.elements[`draft_stop_price_${{input.dataset.liveLayer}}`].value = '';
      update();
    }}));
    form.querySelectorAll('[data-live-input]').forEach((input) => input.addEventListener('change', update));
    form.addEventListener('stop-limit-settings-change', update);
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
    const roundDown = (number) => {{
      let candidate = number;
      for (let attempt = 0; attempt <= bands.length; attempt += 1) {{
        const candidates = bands.filter((item) => item.low <= candidate + 1e-9);
        const band = candidates[candidates.length - 1];
        if (!band) return NaN;
        const rounded = Math.floor(number / band.increment + 1e-9) * band.increment;
        const roundedCandidates = bands.filter((item) => item.low <= rounded + 1e-9);
        if (roundedCandidates[roundedCandidates.length - 1] === band) return rounded;
        candidate = rounded;
      }}
      return NaN;
    }};
    const minimumPositivePrice = Math.min(...bands.map((band) => roundUp(Math.max(band.low, band.increment))));
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
        const exactStop = form.querySelector(`[data-active-exact-stop="${{permId}}"]`);
        const quantity = Number(form.querySelector(`[data-active-quantity="${{permId}}"]`)?.value);
        const targetPrice = Number.isFinite(target) && target > 0 ? (target === Number(targetInput.dataset.activeInitial) ? Number(targetInput.dataset.activeOriginal) : roundUp(basis * (1 + target / 100))) : NaN;
        const stopPrice = Number.isFinite(stop) && stop > -100 ? (exactStop?.value ? Number(exactStop.value) : stop === Number(stopInput?.dataset.activeInitial) ? Number(stopInput?.dataset.activeOriginal) : roundUp(basis * (1 + stop / 100))) : NaN;
        const gain = Number.isFinite(targetPrice) && Number.isFinite(quantity) ? (targetPrice - basis) * multiplier * quantity : NaN;
        const loss = Number.isFinite(stopPrice) && Number.isFinite(quantity) ? (stopPrice - basis) * multiplier * quantity : NaN;
        if (Number.isFinite(gain) && Number.isFinite(loss) && quantity > 0) outcomes.push({{ id: Number(permId), quantity, gain, loss }});
        else invalid = true;
        assigned(`[data-live-price="active-target-${{index}}"]`, Number.isFinite(targetPrice) ? (target === Number(targetInput.dataset.activeInitial) ? `$${{targetInput.dataset.activeOriginal}}` : priceText(targetPrice)) : '—');
        const overrideOffset = form.querySelector(`[data-active-stop-limit-offset="${{permId}}"]`)?.value;
        const overrideUnit = form.querySelector(`[data-active-stop-limit-unit="${{permId}}"]`)?.value;
        const offset = Number(overrideOffset || stopInput?.dataset.activeStopLimitOffset);
        const unit = overrideUnit || stopInput?.dataset.activeStopLimitUnit;
        const rawLimit = unit === 'percent' ? stopPrice * (1 - offset / 100) : stopPrice - offset;
        const computedLimit = Number.isFinite(rawLimit) && Number.isFinite(stopPrice)
          ? (rawLimit > 0 ? roundDown(rawLimit) : minimumPositivePrice) : NaN;
        const stopLimitPrice = stopInput?.dataset.activeStopLimitPrice
          ? ((stopPrice !== Number(stopInput.dataset.activeOriginal) || Boolean(overrideOffset)) && Number.isFinite(computedLimit) && computedLimit > 0 && computedLimit < stopPrice
            ? priceText(computedLimit) : `$${{stopInput.dataset.activeStopLimitPrice}}`)
          : '';
        assigned(`[data-live-price="active-stop-${{index}}"]`, Number.isFinite(stopPrice) ? `${{exactStop?.value || stop !== Number(stopInput?.dataset.activeInitial) ? priceText(stopPrice) : `$${{stopInput.dataset.activeOriginal}}`}}${{stopLimitPrice ? ` (LMT ${{stopLimitPrice}})` : ''}}` : '—');
        assigned(`[data-live-outcome="active-target-${{index}}"]`, Number.isFinite(gain) ? `${{money(gain)}} gain` : '— gain');
        assigned(`[data-live-outcome="active-stop-${{index}}"]`, Number.isFinite(loss) ? `${{money(loss)}} at stop` : '— at stop');
        const originalTarget = Number(targetInput.dataset.activeOriginal);
        const originalStop = Number(stopInput?.dataset.activeOriginal);
        const targetEdited = targetInput.value.trim() !== (targetInput.dataset.activeInitial || '').trim();
        const stopEdited = Boolean(exactStop?.value) || stopInput?.value.trim() !== (stopInput?.dataset.activeInitial || '').trim();
        edited ||= targetEdited || stopEdited;
        const targetChanged = targetEdited && Number.isFinite(targetPrice) && Math.abs(targetPrice - originalTarget) > 1e-8;
        const ruleChanged = Boolean(stopInput?.dataset.activeStopLimitPrice) && Boolean(overrideOffset) &&
          (Number(overrideOffset) !== Number(stopInput.dataset.activeStopLimitOffset) ||
           overrideUnit !== stopInput.dataset.activeStopLimitUnit);
        edited ||= ruleChanged;
        const stopChanged = stopEdited && Number.isFinite(stopPrice) && Math.abs(stopPrice - originalStop) > 1e-8;
        const row = document.querySelector(`[data-active-review-row="${{permId}}"]`);
        const targetRow = document.querySelector(`[data-active-review-target-row="${{permId}}"]`);
        const stopRow = document.querySelector(`[data-active-review-stop-row="${{permId}}"]`);
        const targetText = document.querySelector(`[data-active-review-target="${{permId}}"]`);
        const stopText = document.querySelector(`[data-active-review-stop="${{permId}}"]`);
        if (targetText) targetText.textContent = targetChanged ? priceText(targetPrice) : '';
        const stopLimitRow = document.querySelector(`[data-active-review-stop-limit-row="${{permId}}"]`);
        const stopLimitText = document.querySelector(`[data-active-review-stop-limit="${{permId}}"]`);
        const limitChanged = Boolean(stopLimitRow) && Number.isFinite(computedLimit) &&
          stopLimitPrice !== `$${{stopInput.dataset.activeStopLimitPrice}}`;
        if (stopText) stopText.textContent = stopChanged ? priceText(stopPrice) : '';
        if (stopLimitText) stopLimitText.textContent = limitChanged ? stopLimitPrice : '';
        setHidden(targetRow, !targetChanged);
        setHidden(stopRow, !stopChanged);
        setHidden(stopLimitRow, !limitChanged);
        setHidden(row, !(targetChanged || stopChanged || limitChanged), 'block');
        changed ||= targetChanged || stopChanged || limitChanged;
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
    form.querySelectorAll('[data-active-input]').forEach((input) => input.addEventListener('input', () => {{
      if (input.dataset.activeInput === 'stop') {{
        const exact = form.querySelector(`[data-active-exact-stop="${{input.dataset.activePermId}}"]`);
        if (exact) exact.value = '';
      }}
      update();
    }}));
    document.querySelectorAll('[data-edit-active-prices]').forEach((button) => button.addEventListener('click', () => {{
      const firstPrice = form.querySelector('[data-active-input]:not(:disabled)');
      if (!firstPrice) return;
      firstPrice.scrollIntoView({{block: 'center', behavior: 'smooth'}});
      firstPrice.focus({{preventScroll: true}});
    }}));
    form.querySelectorAll('[data-active-input]').forEach((input) => input.addEventListener('change', update));
    document.querySelectorAll('[data-move-stops-to-be]').forEach((button) => button.addEventListener('click', () => {{
      form.querySelectorAll('[data-active-exact-stop]').forEach((input) => {{ input.value = ''; }});
      form.querySelectorAll('input[type="hidden"][data-active-stop-limit-offset], input[type="hidden"][data-active-stop-limit-unit]').forEach((input) => {{ input.value = ''; }});
      form.querySelectorAll('[data-active-input="stop"]').forEach((input) => {{
        input.value = '0';
        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
      }});
    }}));
    const stopDialog = document.querySelector('[data-stop-dialog]');
    if (stopDialog) {{
      const valueInput = stopDialog.querySelector('[data-stop-dialog-value]');
      const inputLabel = stopDialog.querySelector('[data-stop-input-label]');
      const inverse = stopDialog.querySelector('[data-stop-dialog-inverse]');
      const summary = stopDialog.querySelector('[data-stop-dialog-summary]');
      const apply = stopDialog.querySelector('[data-apply-all-stops]');
      const modeGroup = stopDialog.querySelector('[data-stop-mode-group]');
      const offsetOverride = stopDialog.querySelector('[data-stop-limit-override]');
      const offsetValue = stopDialog.querySelector('[data-stop-limit-override-value]');
      const offsetGroup = stopDialog.querySelector('[data-stop-limit-override-group]');
      let offsetUnit = 'percent';
      const dialogPriceText = (number) => `$${{number.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 6}})}}`;
      let mode = 'price';
      const showMode = () => {{
        inputLabel.textContent = mode === 'price' ? 'Stop price' : 'Return from entry';
        valueInput.min = mode === 'price' ? '0' : '-99.999999';
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
        const offset = Number(offsetValue?.value);
        apply.disabled = !Number.isFinite(rounded) || (offsetOverride?.checked &&
          (!offsetValue.value.trim() || !Number.isFinite(offset) || offset <= 0 ||
           (offsetUnit === 'percent' && offset >= 100)));
        return {{ rounded, rate }};
      }};
      valueInput.addEventListener('input', previewStop);
      offsetOverride?.addEventListener('change', previewStop);
      offsetValue?.addEventListener('input', previewStop);
      offsetGroup?.querySelectorAll('[data-value]').forEach((button) => button.addEventListener('click', () => {{
        offsetUnit = button.dataset.value;
        previewStop();
      }}));
      valueInput.addEventListener('change', () => {{
        const value = previewStop();
        if (mode === 'price' && Number.isFinite(value.rounded)) {{
          valueInput.value = String(Number(value.rounded.toFixed(6)));
          previewStop();
        }}
      }});
      modeGroup?.querySelectorAll('[data-value]').forEach((button) => button.addEventListener('click', () => {{
        const value = previewStop();
        if (Number.isFinite(value.rounded)) {{
          valueInput.value = button.dataset.value === 'price'
            ? String(Number(value.rounded.toFixed(6)))
            : String(Number(value.rate.toFixed(4)));
        }}
        mode = button.dataset.value;
        showMode();
        previewStop();
        valueInput.focus();
      }}));
      stopDialog.querySelectorAll('[data-stop-preset]').forEach((button) => button.addEventListener('click', () => {{
        modeGroup?.querySelector('[data-value="return"]')?.click();
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
          input.value = rate.toFixed(2);
          const exact = form.querySelector(`[data-active-exact-stop="${{input.dataset.activePermId}}"]`);
          if (exact) exact.value = String(value.rounded);
          const offsetField = form.querySelector(`[data-active-stop-limit-offset="${{input.dataset.activePermId}}"]`);
          const unitField = form.querySelector(`[data-active-stop-limit-unit="${{input.dataset.activePermId}}"]`);
          if (offsetField) offsetField.value = offsetOverride?.checked && input.dataset.activeStopLimitPrice ? offsetValue.value : '';
          if (unitField) unitField.value = offsetOverride?.checked && input.dataset.activeStopLimitPrice ? offsetUnit : '';
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
      form.querySelectorAll('[data-active-exact-stop]').forEach((input) => {{ input.value = ''; }});
      form.querySelectorAll('input[type="hidden"][data-active-stop-limit-offset], input[type="hidden"][data-active-stop-limit-unit]').forEach((input) => {{ input.value = ''; }});
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


def _trailing_readonly_field(
    label: str,
    value: str,
    *,
    input_id: str,
    suffix: str | None = None,
    detail: str | None = None,
) -> Any:
    """Use the same field rhythm as bracket layers for a recorded trail."""
    return Div(
        Label(label, fr=input_id, cls="text-xs font-medium text-muted-foreground"),
        Div(
            Input(
                id=input_id,
                value=value,
                disabled=True,
                cls="pr-12" if suffix else "",
            ),
            Span(
                suffix,
                cls=(
                    "pointer-events-none absolute right-3 top-1/2 "
                    "-translate-y-1/2 text-sm text-muted-foreground"
                ),
            )
            if suffix
            else None,
            cls="relative mt-1",
        ),
        P(detail, cls="mt-1 text-xs text-muted-foreground") if detail else None,
        cls="min-w-0 space-y-0.5",
    )


def _trailing_fill_summary(entry: JournalEntry) -> _TrailingFillSummary | None:
    """Accept only exact, nonduplicated SELL fills for this recorded trail."""
    if (
        entry.trailing_quantity <= 0
        or len(entry.perm_ids) != 1
        or entry.perm_ids[0] <= 0
    ):
        return None
    quantity = Decimal("0")
    proceeds = Decimal("0")
    pnl = Decimal("0")
    pnl_complete = True
    seen: set[str] = set()
    for fill in entry.fills:
        try:
            filled = Decimal(fill.quantity)
            price = Decimal(fill.price)
            reported_pnl = (
                Decimal(fill.realized_pnl) if fill.realized_pnl is not None else None
            )
        except InvalidOperation:
            return None
        if (
            not fill.exec_id
            or fill.exec_id in seen
            or fill.perm_id != entry.perm_ids[0]
            or fill.side.upper() not in {"SLD", "SELL"}
            or not filled.is_finite()
            or filled <= 0
            or not price.is_finite()
            or price <= 0
            or (reported_pnl is not None and not reported_pnl.is_finite())
        ):
            return None
        seen.add(fill.exec_id)
        quantity += filled
        proceeds += filled * price
        if reported_pnl is None or fill.currency != "USD":
            pnl_complete = False
        else:
            pnl += reported_pnl
    if quantity > entry.trailing_quantity:
        return None
    return _TrailingFillSummary(
        quantity=quantity,
        average_price=proceeds / quantity if quantity else None,
        realized_pnl=pnl if quantity and pnl_complete else None,
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
        action_field,
        data_layer_state=state,
        cls="layer-row-grid items-start gap-3 border-t border-border py-4 first:border-t-0",
    )


def _button_tooltip(control: Any, description: str) -> Any:
    return Tooltip(
        TooltipTrigger(control, delay_duration=250),
        TooltipContent(description),
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
            Label(
                label,
                fr=input_id,
                cls="text-xs font-medium text-muted-foreground",
            ),
            Div(
                Span(
                    f"${price}",
                    data_live_price=f"{kind}-{layer_index}",
                    aria_live="polite",
                    cls="text-xs font-semibold text-foreground",
                ),
                Span(
                    "",
                    data_live_stop_limit_price=layer_index if kind == "stop" else None,
                    aria_live="polite" if kind == "stop" else None,
                    cls="hidden text-xs text-muted-foreground"
                    if kind == "stop"
                    else "hidden",
                ),
                cls="flex flex-wrap items-baseline justify-end gap-x-1",
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
    label: str,
    *,
    value: str,
    price: str,
    input_id: str,
    inferred: bool = False,
    limit_price: str | None = None,
) -> Any:
    """Retain the active field geometry without inventing old percentages."""
    return Div(
        Div(
            Label(label, fr=input_id, cls="text-xs font-medium text-muted-foreground"),
            Span(
                f"${price} (LMT ${limit_price})" if limit_price else f"${price}",
                cls="text-xs font-semibold text-foreground",
            ),
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
        cls="inline-flex items-center gap-1.5 font-mono text-xs font-semibold tabular-nums",
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
        snapshot is None
        or not snapshot.connected
        or not snapshot.complete
        or not snapshot.fresh
        or not snapshot.executions_complete
    ):
        return False
    for update in updates:
        fills = (
            fill
            for fill in snapshot.executions
            if fill.exec_id not in prior_execution_ids
            and fill.account == update.layer.account
            and fill.con_id == update.layer.con_id
            and fill.side.upper() in {"SLD", "SELL"}
            and fill.perm_id
            in {
                perm_id
                for perm_id, price in (
                    (update.layer.target_perm_id, update.target_price),
                    (update.layer.stop_perm_id, update.stop_price),
                )
                if price is not None
            }
        )
        if (
            sum((fill.quantity for fill in fills), Decimal("0"))
            != update.layer.quantity
        ):
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
    bid = quote.bid if quote is not None else None
    bid = bid if bid is not None and bid.is_finite() and bid > 0 else None
    concerns: set[tuple[int, str]] = set()
    details: list[str] = []
    crosses_quote = False
    for update in updates:
        perm_id = update.layer.target_perm_id
        if update.stop_price is not None:
            if bid is not None and update.stop_price >= bid:
                concerns.add((perm_id, "stop-crosses-bid"))
                crosses_quote = True
            elif bid is None or not reliable:
                concerns.add((perm_id, "stop-quote-unknown"))
        if update.target_price is not None:
            if bid is not None and update.target_price <= bid:
                concerns.add((perm_id, "limit-crosses-bid"))
                crosses_quote = True
            elif bid is None or not reliable:
                concerns.add((perm_id, "limit-quote-unknown"))
    if crosses_quote:
        title = "Possible immediate sell"
        details.append(
            "Confirming may cause one or more of these sell orders to execute soon and close "
            "their OCA brackets. A stop does not guarantee its fill price."
        )
    elif concerns:
        title = "Immediate sell risk cannot be assessed"
        details.append(
            "A current live bid is unavailable for every modified sell leg. "
            "Check TWS before confirming; a changed order may execute soon."
        )
    else:
        title = ""
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


def _contract_header_metric(label: str, value: str, *, adornment: Any = None) -> Any:
    return Div(
        Span(label, cls="block text-xs text-muted-foreground"),
        Div(
            Span(value, cls="block text-sm font-medium tabular-nums"),
            adornment,
            cls="mt-1 flex items-center gap-1",
        )
        if adornment is not None
        else Span(value, cls="mt-1 block text-sm font-medium tabular-nums"),
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


def _dialog_context_row(label: str, value: str) -> Any:
    return Div(
        Span(label, cls="text-xs text-muted-foreground"),
        Span(value, cls="text-sm font-semibold"),
        cls="flex items-center justify-between gap-4",
    )


def _dialog_price_context(*rows: tuple[str, str]) -> Any:
    return Div(
        *(_dialog_context_row(label, value) for label, value in rows),
        cls="space-y-2 rounded-md border border-border bg-muted/20 px-4 py-3",
    )


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


def _valid_stop_limit_offset(value: str, unit: str) -> bool:
    if unit not in {"percent", "dollars"}:
        return False
    try:
        offset = Decimal(value)
    except InvalidOperation:
        return False
    return offset.is_finite() and offset > 0 and (unit != "percent" or offset < 100)


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


def _projection_gain_value(
    value: Decimal | None, delta: Decimal | None, cost: Decimal | None = None
) -> Any:
    return _projection_change_value(value, delta, metric="gain", cost=cost)


def _projection_loss_value(
    value: Decimal | None, delta: Decimal | None, cost: Decimal | None = None
) -> Any:
    return _projection_change_value(value, delta, metric="loss", cost=cost)


def _projection_change_value(
    value: Decimal | None,
    delta: Decimal | None,
    *,
    metric: str,
    cost: Decimal | None = None,
) -> Any:
    changed = value is not None and delta is not None and bool(delta)
    up = (delta > 0 if metric == "gain" else delta < 0) if delta is not None else False
    amount = f"${abs(delta):,.2f}" if changed and delta is not None else "—"
    baseline = value - delta if value is not None and delta is not None else None
    if metric == "gain":
        direction = "Expected gain increased" if up else "Expected gain decreased"
    elif (
        changed
        and value is not None
        and baseline is not None
        and (value < 0 or baseline < 0)
    ):
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
    percent = _projection_percent(value, cost)
    return Span(
        Span(
            _money(value) if value is not None else "— Unknown",
            cls="whitespace-nowrap",
            **{f"data_{metric}_value": True},
        ),
        Span(
            percent or "",
            cls="text-muted-foreground text-[11px] font-normal leading-4 whitespace-nowrap",
            **{f"data_{metric}_percent": True},
        ),
        Span(
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
            aria_label=comparison_label,
            cls="hidden text-muted-foreground text-[11px] font-normal leading-4 whitespace-nowrap",
            **{f"data_{metric}_change": True},
        ),
        cls="inline-flex flex-col items-start gap-0.5",
    )


def _projection_percent(value: Decimal | None, cost: Decimal | None) -> str | None:
    if value is None or cost is None or not cost.is_finite() or cost <= 0:
        return None
    percent = (value / cost * Decimal("100")).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return f"{percent:+,.1f}%"


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
    const updateMetric = (node, value, baseline, kind, cost) => {{
      if (!node) return;
      const select = (part) => node.querySelector('[data-' + kind + '-' + part + ']');
      const change = select('change');
      select('value').textContent = value === null ? '— Unknown' : money(value);
      select('percent').textContent = value !== null && Number.isFinite(cost) && cost > 0
        ? `${{new Intl.NumberFormat(undefined, {{minimumFractionDigits: 1, maximumFractionDigits: 1, signDisplay: 'always'}}).format(value / cost * 100)}}%` : '';
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
      const unitCost = config.unitCost === null ? NaN : Number(config.unitCost);
      const coveredCost = unitCost * covered;
      const gainCost = coveredCost + unitCost * Number(config.soldQuantity);
      updateMetric(gainNode, projected ? (config.marketExit ? (config.baselineGain === null ? null : Number(config.baselineGain)) : gain) : null, gainBaseline, 'gain', gainCost);
      updateMetric(lossNode, projected ? (config.marketExit ? (config.baselineLoss === null ? null : Number(config.baselineLoss)) : loss) : null, lossBaseline, 'loss', coveredCost);
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
            "Review the action again with current prices and quantities.",
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
            "blocked",
            "failed",
            "unavailable",
            "unknown",
            "disabled",
            "invalid",
            "not in the verified",
            "could not",
            "needs attention",
            "refresh required",
            "must be",
            "enter valid",
            "does not have a usable",
            "select at least",
            "press execute",
            "start execution first",
            "start a price update first",
            "state changed",
            "quote changes",
            "earlier price amendment",
            "targets must",
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
            "Target must be above 0%; stop must be below its target.",
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
        "Trailing exit blocked": "Couldn't review trailing exit",
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


def _paper_confirmation_countdown_script() -> str:
    """Display the remaining review window; the server enforces the deadline."""
    return """
    (() => {
      const button = document.querySelector('[data-confirm-countdown-ms]');
      if (!(button instanceof HTMLButtonElement)) return;
      const duration = Number(button.dataset.confirmCountdownMs);
      if (!Number.isFinite(duration)) return;
      const deadline = performance.now() + Math.max(0, duration);
      const render = () => {
        if (!button.isConnected || button.getAttribute('aria-busy') === 'true') return false;
        const seconds = Math.ceil(Math.max(0, deadline - performance.now()) / 1000);
        button.textContent = `Confirm (${seconds}s)`;
        if (seconds === 0) {
          button.disabled = true;
          window.setTimeout(() => window.location.reload(), 150);
        }
        return seconds > 0;
      };
      if (!render()) return;
      const interval = window.setInterval(() => {
        if (!render()) window.clearInterval(interval);
      }, 100);
    })();
    """


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
    return Div(
        label_node,
        P(
            value,
            data_live_metric=live_key,
            aria_live="polite" if live_key else None,
            cls=f"min-w-0 font-mono text-sm font-semibold tabular-nums {tone}",
        ),
        cls="outcome-metric-panel flex min-w-0 flex-col gap-3 rounded-md px-3 py-3",
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
