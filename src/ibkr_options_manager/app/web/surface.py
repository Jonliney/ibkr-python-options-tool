from __future__ import annotations

# ruff: noqa: E501
import json
from dataclasses import replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from secrets import token_urlsafe
from threading import RLock
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

from ...domain import preview_reference_prices
from ...execution import (
    ExecutionBlocked,
    ExecutionOutcomeUnknown,
    MarketExitCandidate,
    PaperExecutionService,
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
from .components.ui.button import Button
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

_STATIC_DIR = Path(__file__).with_name("static")
_ASSETS_DIR = Path(__file__).with_name("assets")
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
        self._preferred_con_id = initial_con_id
        self._selected_con_id: int | None = None
        self._state = view_model.empty()
        self._drafts: dict[int, tuple[DraftLayerForm, ...]] = {}
        self._settings = ConnectionSettings(account=initial_account)
        self._target_presets = "20, 40, 60, 100"
        self._stop_presets = "25"
        self._last_refreshed_at = "—"
        self._workspace_tab = "draft"
        self._message = "Refresh and select a position to build a draft."
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
        route(f"/{self.session_token}/action", methods=["POST"])(self._action)

    @property
    def path(self) -> str:
        return f"/{self.session_token}/"

    def load_demo_data(self) -> None:
        """Load the existing deterministic demo without starting a browser worker."""
        if not self._demo_mode:
            return
        with self._lock:
            self._refresh_locked()

    def _home(self) -> Any:
        with self._lock:
            return self._page()

    async def _action(self, request: Request) -> Any:
        form = await request.form()
        values = {str(key): str(value) for key, value in form.items()}
        action = values.get("action", "save-draft")
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
            elif action == "show-draft":
                self._workspace_tab = "draft"
            elif action == "show-active":
                self._workspace_tab = "active"
            elif action.startswith("market-exit-arm:"):
                _, _, perm_id = action.partition(":")
                self._arm_market_exit_locked(_positive_int(perm_id, 0))
            elif action == "market-exit-selected":
                self._arm_selected_market_exit_locked(values)
            elif action == "market-exit-confirm":
                self._confirm_market_exit_locked()
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
        self._apply_state_locked(state)
        self._record_refresh_time_locked()
        target = self._preferred_con_id
        if target is None and state.positions:
            target = state.positions[0].con_id
        self._preferred_con_id = None
        if target is None:
            return
        self._select_locked(target)

    def _select_locked(self, con_id: int) -> None:
        if con_id not in {position.con_id for position in self._state.positions}:
            self._message = "The selected contract is not in the verified portfolio."
            return
        self._selected_con_id = con_id
        self._workspace_tab = "draft"
        state = self._view_model.select_position(
            con_id, self._plan_form(self._drafts.get(con_id, ()))
        )
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
        self._message = "Fresh paper snapshot verified. Click to confirm sends the reviewed OCA orders."

    def _confirm_execution_locked(self) -> None:
        armed = self._armed_execution
        if self._paper_execution is None or armed is None or armed.plan.fingerprint is None:
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
            self._message = "Execution blocked: the fresh snapshot is no longer sendable."
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
            self._message = (
                f"Submission outcome is unknown: {error}. If TWS shows the complete "
                "bracket, approve it there if required, then Refresh to reconcile it "
                "into Active layers. Do not retry this draft."
            )
        except ExecutionBlocked as error:
            self._message = f"Execution blocked: {error}"
        except Exception as error:  # the isolated writer must never crash the UI
            self._message = f"Submission outcome is unknown: {error}"
        else:
            self._message = (
                f"Paper submission acknowledged for {len(receipt.entry.order_ids)} orders. "
                "Refresh to reconcile them into Active layers; no automatic retry will occur."
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
            self._message = "Market exit blocked: a fresh selected-position snapshot is required."
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
        self._workspace_tab = "active"
        self._message = (
            f"Fresh paper snapshot verified. Review the MKT exit for "
            f"{candidate.quantity} contracts, then click to confirm."
        )

    def _arm_selected_market_exit_locked(self, values: dict[str, str]) -> None:
        """Arm the existing one-layer paper exit through the global selection UI.

        Multi-layer exits remain deliberately unavailable until their own
        ordered cancellation/recheck protocol exists.  Selecting more than one
        must therefore fail before any broker action, rather than silently
        selling only the first checked layer.
        """
        selected = {
            _positive_int(value, 0)
            for name, value in values.items()
            if name.startswith("active_layer_")
        }
        selected.discard(0)
        if len(selected) != 1:
            self._message = (
                "Select exactly one active layer for the staged paper MKT exit. "
                "Bulk exits are not enabled yet."
            )
            return
        self._arm_market_exit_locked(selected.pop())

    def _confirm_market_exit_locked(self) -> None:
        armed = self._armed_market_exit
        if self._paper_execution is None or armed is None or self._selected_con_id is None:
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
            candidate = self._paper_execution.prepare_market_exit(
                snapshot,
                target_perm_id=armed.target_perm_id,
                expected_client_id=self._settings.client_id,
            )
            if candidate != armed:
                raise ExecutionBlocked("the OCA layer changed after review")
            self._paper_execution.cancel_pair_then_submit_market(
                snapshot,
                candidate,
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
            self._message = (
                f"TWS confirmed both selected OCA legs were cancelled and "
                f"acknowledged the standalone MKT sell for {candidate.quantity} contracts. "
                "Refresh to verify the outcome."
            )
        finally:
            self._workspace_tab = "active"
            self._disarm_execution_locked()

    def _apply_state_locked(self, state: ViewState) -> None:
        self._state = state
        self._selected_con_id = state.selected_con_id
        self._ensure_draft_locked()
        if state.status is UiStatus.READY:
            self._message = "Verified broker state is ready for read-only planning."
        elif state.status is not UiStatus.EMPTY:
            self._message = state.status_message

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
            reconciled = self._paper_execution.reconcile_snapshot(snapshot)
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
        if con_id is None or con_id in self._drafts or self._state.bracket_form.layers:
            if con_id is not None and self._state.bracket_form.layers:
                self._drafts[con_id] = self._state.bracket_form.layers
            return
        basis = self._state.unit_basis
        calculator = self._state.quote_calculator
        presets = self._preset_for_index(0)
        if basis is None or calculator is None or self._state.available_quantity <= 0 or presets is None:
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
                self._message = "Targets must be above 0%; stops must be between 0% and 100%."
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
            self._message = "Enter valid comma-separated LMT and STP preset percentages first."
            return
        target, stop = presets
        try:
            prices = preview_reference_prices(basis, target, stop, calculator.bands)
        except ValueError:
            self._message = "The selected position does not have a usable price increment."
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
                layers, _split_quantity(self._state.available_quantity, len(layers)), strict=True
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
        title = state.position_title if self._selected_con_id is not None else "Select an option position"
        return Div(
            self._header(ready),
            Div(
                self._inventory(),
                self._workspace(title),
                self._review(),
                cls="grid h-[calc(100vh-3.5rem)] min-h-0 grid-cols-[16rem_minmax(0,1fr)_19rem] overflow-hidden border-t border-border",
            ),
            cls="h-screen overflow-hidden bg-background text-foreground selection:bg-primary selection:text-primary-foreground",
        )

    def _header(self, ready: bool) -> Any:
        return Div(
            Div(cls="size-2 rounded-full " + ("bg-emerald-500" if ready else "bg-amber-400")),
            Span("CONNECTED" if ready else self._state.status, cls="text-xs font-semibold tracking-wide"),
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
            Span(f"Account {self._state.account or '—'}", cls="text-xs text-muted-foreground"),
            Span(cls="flex-1"),
            Span(
                f"Last refreshed {self._last_refreshed_at}",
                cls="text-xs text-muted-foreground",
            ),
            Form(
                Button("Refresh", variant="outline", size="sm", type="submit"),
                HTMLInput(type="hidden", name="action", value="refresh"),
                HTMLInput(type="hidden", name="account", value=self._settings.account),
                HTMLInput(type="hidden", name="port", value=str(self._settings.port)),
                HTMLInput(type="hidden", name="client_id", value=str(self._settings.client_id)),
                HTMLInput(type="hidden", name="timeout", value=str(self._settings.timeout_seconds)),
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
                Span("LONG POSITIONS", cls="text-xs font-semibold tracking-wide text-muted-foreground"),
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
                            Input(
                                name="target_presets", value=self._target_presets
                            ),
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
                        Button("Refresh with settings", type="submit"),
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
        show_active = self._workspace_tab == "active" and bool(active_pairs)
        return Div(
            self._coverage_alert(coverage, app_order_count, order_count),
            Div(
                Div(H1(title, cls="text-2xl font-semibold tracking-tight"), P(self._message, cls="mt-2 text-sm text-muted-foreground")),
                Div(Span("Cost basis / Ask", cls="text-xs text-muted-foreground"), P(" / ".join(fact.value for fact in self._state.quote[:2]) or "—", cls="mt-1 font-mono text-sm"), cls="text-right"),
                cls="flex items-start justify-between gap-6",
            ),
            P(f"{self._state.available_quantity} contracts verified available to bracket", cls="mt-2 text-sm font-medium text-emerald-400"),
            Div(
                self._workspace_tab_control(
                    "Draft layers",
                    "show-draft",
                    selected=not show_active,
                ),
                self._workspace_tab_control(
                    "Active layers",
                    "show-active",
                    selected=show_active,
                    disabled=not active_pairs,
                ),
                cls="mt-5 flex h-9 items-center gap-4",
            ),
            Div(
                ScrollArea(
                    self._active_layers_panel() if show_active else self._draft_panel(),
                    aria_label=(
                        "Reconciled active OCA layers"
                        if show_active
                        else "Layer draft workspace"
                    ),
                    orientation="vertical",
                    cls="h-full",
                ),
                cls="mt-5 min-h-0 flex-1 overflow-hidden",
            ),
            cls="flex min-w-0 min-h-0 flex-col overflow-hidden px-8 py-6",
        )

    def _workspace_tab_control(
        self,
        label: str,
        action: str,
        *,
        selected: bool,
        disabled: bool = False,
    ) -> Any:
        """Render a server-owned workspace tab to avoid WebView visibility drift."""
        return Form(
            Button(
                label,
                variant="ghost",
                size="sm",
                type="submit",
                disabled=disabled,
                aria_current="page" if selected else None,
                cls=(
                    "h-9 rounded-none border-b-2 border-foreground px-0 text-foreground "
                    "hover:bg-transparent hover:text-foreground"
                    if selected
                    else "h-9 rounded-none border-b-2 border-transparent px-0 "
                    "hover:bg-transparent"
                ),
            ),
            HTMLInput(type="hidden", name="action", value=action),
            action=f"/{self.session_token}/action",
            method="post",
            cls="contents",
        )

    def _order_coverage(self) -> tuple[str, int, int]:
        """Classify displayed order coverage using durable app ownership proof."""
        orders = self._state.working_orders
        if not orders or self._selected_con_id is None:
            return "none", 0, 0
        if self._paper_execution is None:
            return "external", 0, len(orders)
        owned_perm_ids = self._paper_execution.owned_perm_ids(
            account=self._settings.account,
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
            account=self._settings.account,
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
            if len(targets) == 1 and len(stops) == 1 and len(orders) == 2:
                pairs.append((group, targets[0], stops[0]))
        return tuple(pairs)

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
                            Button("Move stop to B/E", variant="outline", size="sm", disabled=True),
                            Button("Update layers", variant="outline", size="sm", disabled=True),
                            Button(
                                "Sell layers",
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
            action=f"/{self.session_token}/action",
            method="post",
        )

    def _active_layer_row(self, index: int, _group: str, target: Any, stop: Any) -> Any:
        target_percentage = _price_percentage(
            target.limit_price,
            self._state.unit_basis,
            target=True,
        )
        stop_percentage = _price_percentage(
            stop.stop_price,
            self._state.unit_basis,
            target=False,
        )
        gain, loss = self._active_layer_projection(target, stop)
        checkbox_id = f"active-layer-{index}"
        return Div(
            Div(
                HTMLInput(
                    type="checkbox",
                    id=checkbox_id,
                    name=f"active_layer_{index}",
                    value=str(target.perm_id),
                    checked=True,
                    cls="mt-0.5 size-4 accent-primary",
                ),
                Div(
                    Label(f"LAYER {index}", fr=checkbox_id, cls="text-xs font-semibold"),
                    P(f"OCA-{index}", cls="mt-2 text-xs text-muted-foreground"),
                    cls="min-w-20",
                ),
                cls="flex items-start gap-3",
            ),
            _percentage_price_field(
                "LMT target",
                Input(
                    name=f"active_target_{index}",
                    id=f"active-target-{index}",
                    type="number",
                    value=target_percentage,
                    min="0.1",
                    step="0.1",
                    cls="pr-8",
                ),
                input_id=f"active-target-{index}",
                price=_price_text(target.limit_price),
                outcome=gain,
                outcome_label="gain",
                tone="text-emerald-400",
                layer_index=index,
                kind=f"active-target-{index}",
            ),
            _percentage_price_field(
                "STP loss",
                Input(
                    name=f"active_stop_{index}",
                    id=f"active-stop-{index}",
                    type="number",
                    value=stop_percentage,
                    min="0.1",
                    max="100",
                    step="0.1",
                    cls="pr-8",
                ),
                input_id=f"active-stop-{index}",
                price=_price_text(stop.stop_price),
                outcome=loss,
                outcome_label="max loss",
                tone="text-rose-400",
                layer_index=index,
                kind=f"active-stop-{index}",
            ),
            _field(
                "Quantity",
                Input(
                    id=f"active-quantity-{index}",
                    type="number",
                    value=str(target.remaining),
                    readonly=True,
                ),
                input_id=f"active-quantity-{index}",
            ),
            _field(
                "TIF",
                Input(
                    id=f"active-tif-{index}",
                    value=target.tif or "—",
                    readonly=True,
                ),
                input_id=f"active-tif-{index}",
            ),
            # Reuse the exact, precompiled StarUI grid utility used by draft
            # rows.  A new arbitrary Tailwind grid class is not available in
            # the bundled stylesheet and silently collapses to one column.
            Div(aria_hidden="true"),
            cls="grid grid-cols-[5rem_minmax(10rem,1fr)_minmax(10rem,1fr)_minmax(5rem,0.6fr)_5rem_2.25rem] items-start gap-3 border-t border-border py-4 first:border-t-0",
        )

    def _active_layer_projection(self, target: Any, stop: Any) -> tuple[str, str]:
        basis = self._state.unit_basis
        multiplier = self._state.multiplier
        if basis is None or multiplier is None:
            return "—", "—"
        if target.limit_price is None or stop.stop_price is None:
            return "—", "—"
        try:
            quantity = Decimal(str(target.remaining))
        except InvalidOperation:
            return "—", "—"
        if quantity <= 0:
            return "—", "—"
        gain = (target.limit_price - basis) * multiplier * quantity
        loss = (stop.stop_price - basis) * multiplier * quantity
        return _money(gain), _money(loss)

    def _coverage_alert(
        self,
        coverage: str,
        app_order_count: int,
        order_count: int,
    ) -> Any:
        if coverage == "app":
            return Alert(
                AlertTitle("App-managed OCA coverage active"),
                AlertDescription(
                    f"{app_order_count} app-created orders were reconciled with TWS. "
                    "This position is fully covered; a new bracket will not be created."
                ),
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
                                    self._layer_row(index, layer, len(layers))
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
            self._outcome_projection(layers),
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
                {"low": format(band.low_edge, "f"), "increment": format(band.increment, "f")}
                for band in calculator.bands
            ],
        }

    def _layer_row(self, index: int, layer: DraftLayerForm, count: int) -> Any:
        tif_signal = Signal(f"tif_{index}_value", _ref_only=True)
        gain, loss = self._layer_projection(layer)
        return Div(
            Div(
                Span(f"LAYER {index}", cls="text-xs font-semibold"),
                P(f"OCA-{index}", cls="mt-2 text-xs text-muted-foreground"),
                cls="min-w-20",
            ),
            _percentage_price_field(
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
            _percentage_price_field(
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
            _field(
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
            _field(
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
            Button(
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
            cls="grid grid-cols-[5rem_minmax(10rem,1fr)_minmax(10rem,1fr)_minmax(5rem,0.6fr)_5rem_2.25rem] items-start gap-3 border-t border-border py-4 first:border-t-0",
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
                    target = (Decimal(layer.target_price) - basis) * multiplier * quantity
                    stop = (Decimal(layer.stop_price) - basis) * multiplier * quantity
                except InvalidOperation:
                    continue
                outcomes.append((target, stop))
        gain = sum((target for target, _ in outcomes), Decimal("0"))
        loss = sum((stop for _, stop in outcomes), Decimal("0"))
        return Card(
            CardHeader(CardTitle("Outcome projection"), CardDescription("Based on the current cost basis and planned layer prices."), cls="gap-1"),
            CardContent(
                Div(
                    _metric("Expected gain", _money(gain), "text-emerald-400", live_key="gain"),
                    _metric("Max loss", _money(loss), "text-rose-400", live_key="loss"),
                    _metric("Breakeven after", _breakeven(outcomes), "text-amber-300", live_key="breakeven"),
                    cls="flex flex-wrap items-start gap-x-16 gap-y-5",
                ),
            ),
            cls="mt-5",
        )

    def _review(self) -> Any:
        market_exit = self._armed_market_exit
        armed_execution = self._armed_execution
        action_rows: list[Any] = []
        if market_exit is not None:
            action_rows = [self._review_market_exit(market_exit)]
        elif armed_execution is not None:
            action_rows = [
                self._review_pair(index, layer)
                for index, layer in enumerate(self._current_layers(), start=1)
            ]
        elif self._workspace_tab == "draft" and self._current_layers():
            # A draft is an untransmitted order plan.  Keep it continuously
            # visible in the review panel while its editable inputs change.
            action_rows = [
                self._review_pair(index, layer)
                for index, layer in enumerate(self._current_layers(), start=1)
            ]
        return Div(
            Div(
                Span("ACTION REVIEW", cls="text-xs font-semibold tracking-wide text-muted-foreground"),
                Badge("MKT EXIT", variant="outline", cls="text-[10px]")
                if market_exit is not None
                else Badge("DRAFT", variant="outline", cls="text-[10px]")
                if action_rows
                else None,
                cls="flex items-center justify-between px-4 py-4",
            ),
            ScrollArea(*action_rows, aria_label="Planned order actions", cls="min-h-0 flex-1 px-4")
            if action_rows
            else Div(
                P(
                    "Add a layer or modify an existing one to continue.",
                    cls="text-center text-sm leading-6 text-muted-foreground",
                ),
                cls="flex min-h-0 flex-1 items-center justify-center px-6",
            ),
            self._execution_control(),
            cls="flex min-h-0 flex-col overflow-hidden border-l border-border bg-card/30",
        )

    def _execution_control(self) -> Any:
        market_exit = self._armed_market_exit
        if market_exit is not None:
            return Form(
                Button(
                    "Click to confirm cancel + MKT sell",
                    variant="destructive",
                    type="submit",
                    cls="w-full",
                ),
                HTMLInput(type="hidden", name="action", value="market-exit-confirm"),
                action=f"/{self.session_token}/action",
                method="post",
                cls="mx-4 mb-4 w-[calc(100%-2rem)]",
            )
        if self._armed_execution is not None:
            return Div(
                Button(
                    "Click to confirm",
                    variant="destructive",
                    type="submit",
                    name="action",
                    value="execute-confirm",
                    form="draft-form",
                    cls="w-full",
                ),
                cls="mx-4 mb-4 w-[calc(100%-2rem)]",
            )
        can_execute = (
            self._paper_execution is not None
            and self._workspace_tab == "draft"
            and self._state.available_quantity > 0
            and bool(self._current_layers())
        )
        return Div(
            Button(
                "Execute paper order",
                variant="default",
                type="submit",
                name="action",
                value="execute-arm",
                form="draft-form",
                disabled=not can_execute,
                cls="w-full",
            ),
            cls="mx-4 mb-4 w-[calc(100%-2rem)]",
        )

    def _review_pair(self, index: int, layer: DraftLayerForm) -> Any:
        return Div(
            Div(
                P(f"OCA-{index}", cls="text-xs font-semibold"),
                P(
                    f"{layer.quantity} contracts · {layer.tif}",
                    data_live_review_quantity=index,
                    cls="mt-0.5 text-xs text-muted-foreground",
                ),
            ),
            Div(
                Div(
                    Span("SELL LMT", cls="text-xs font-semibold text-emerald-400"),
                    Span(
                        f"${layer.target_price}",
                        data_live_review_price=f"target-{index}",
                        aria_live="polite",
                        cls="text-sm font-semibold text-emerald-400",
                    ),
                    cls="flex items-center justify-between gap-3",
                ),
                Div(
                    Span("SELL STP", cls="text-xs font-semibold text-rose-400"),
                    Span(
                        f"${layer.stop_price}",
                        data_live_review_price=f"stop-{index}",
                        aria_live="polite",
                        cls="text-sm font-semibold text-rose-400",
                    ),
                    cls="flex items-center justify-between gap-3",
                ),
                cls="mt-3 space-y-3 border-l-2 border-border pl-3",
            ),
            cls="border-b border-border py-4",
        )

    def _review_market_exit(self, candidate: MarketExitCandidate) -> Any:
        return Div(
            Div(
                P("Selected app-owned OCA layer", cls="text-xs font-semibold"),
                P(
                    f"{candidate.quantity} contracts · {candidate.tif}",
                    cls="mt-0.5 text-xs text-muted-foreground",
                ),
            ),
            Div(
                Div(
                    Span("CANCEL SELL LMT", cls="text-xs font-semibold text-amber-300"),
                    Span(str(candidate.target_order_id), cls="text-sm font-semibold text-amber-300"),
                    cls="flex items-center justify-between gap-3",
                ),
                Div(
                    Span("CANCEL SELL STP", cls="text-xs font-semibold text-rose-400"),
                    Span(str(candidate.stop_order_id), cls="text-sm font-semibold text-rose-400"),
                    cls="flex items-center justify-between gap-3",
                ),
                Div(
                    Span("CREATE STANDALONE SELL MKT", cls="text-xs font-semibold text-amber-300"),
                    Span(
                        f"{candidate.quantity} contracts",
                        cls="text-sm font-semibold text-amber-300",
                    ),
                    cls="flex items-center justify-between gap-3",
                ),
                P(
                    "Wait for both cancellation confirmations, then create one standalone SELL MKT. No other OCA group is modified.",
                    cls="pt-1 text-xs text-muted-foreground",
                ),
                cls="mt-3 space-y-3 border-l-2 border-amber-400/60 pl-3",
            ),
            cls="border-b border-border py-4",
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


def _field(
    label: str,
    control: Any,
    *,
    input_id: str | None = None,
    suffix: str | None = None,
) -> Any:
    return Div(
        Label(label, fr=input_id, cls="text-xs font-medium text-muted-foreground"),
        Div(control, Span(suffix, cls="shrink-0 font-mono text-xs text-muted-foreground") if suffix else None, cls="mt-1 flex items-center gap-2"),
        cls="min-w-0 space-y-0.5",
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


def _int_or_zero(value: str) -> int:
    try:
        return max(0, int(value))
    except ValueError:
        return 0


def _money(value: Decimal) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _metric(label: str, value: str, tone: str, *, live_key: str | None = None) -> Any:
    return Div(
        P(label, cls="text-xs font-semibold uppercase tracking-wide text-muted-foreground"),
        P(
            value,
            data_live_metric=live_key,
            aria_live="polite" if live_key else None,
            cls=f"mt-1 font-mono text-lg font-semibold {tone}",
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
