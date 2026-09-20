from __future__ import annotations

# ruff: noqa: E501
import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
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
    Signal,
    Span,
    star_app,
)
from starhtml import (
    Input as HTMLInput,
)
from starhtml.icons import resolver
from starlette.requests import Request

from ...domain import preview_reference_prices
from ..view_model import (
    ConnectionSettings,
    DraftLayerForm,
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
from .components.ui.tabs import Tabs, TabsContent, TabsList, TabsTrigger

_STATIC_DIR = Path(__file__).with_name("static")
_ASSETS_DIR = Path(__file__).with_name("assets")
_DEFAULT_HELP = "Each layer creates one SELL LMT + SELL STP OCA pair."


class StarUIWorkbench:
    """Server-owned StarUI view over the existing, write-free planner seam."""

    def __init__(
        self,
        view_model: PlannerViewModel,
        *,
        initial_account: str = "",
        initial_con_id: int | None = None,
        demo_mode: bool = False,
    ) -> None:
        _register_bundled_icons()
        self._view_model = view_model
        self._demo_mode = demo_mode
        self._preferred_con_id = initial_con_id
        self._selected_con_id: int | None = None
        self._state = view_model.empty()
        self._drafts: dict[int, tuple[DraftLayerForm, ...]] = {}
        self._settings = ConnectionSettings(account=initial_account)
        self._target_presets = "20, 40, 60, 100"
        self._stop_presets = "25"
        self._message = "Refresh and select a position to build a read-only draft."
        self._lock = RLock()
        self.session_token = token_urlsafe(24)
        self.app, route = star_app(
            title="IBKR Options Manager — Read-only preview",
            static_path=str(_STATIC_DIR),
            secret_key=token_urlsafe(32),
            inline_icons=True,
            hdrs=(Link(rel="stylesheet", href="/starui.css"),),
            htmlkw={"lang": "en", "data_theme": "dark"},
            bodykw={"cls": "min-h-screen bg-background text-foreground"},
        )
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
                self._save_form_locked(values)
                self._select_locked(_positive_int(values.get("con_id"), 0))
            else:
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
                elif action == "preview":
                    self._preview_locked()
            return self._page()

    def _refresh_locked(self) -> None:
        state = self._view_model.refresh_portfolio(self._settings)
        self._apply_state_locked(state)
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
        state = self._view_model.select_position(
            con_id, PlanForm(layers=self._drafts.get(con_id, ()))
        )
        self._apply_state_locked(state)

    def _preview_locked(self) -> None:
        if self._selected_con_id is None:
            self._message = "Select a verified option before previewing its draft."
            return
        state = self._view_model.preview_action(PlanForm(layers=self._current_layers()))
        self._apply_state_locked(state)
        self._message = "Preview refreshed from the current verified snapshot. No order was placed."

    def _apply_state_locked(self, state: ViewState) -> None:
        self._state = state
        self._selected_con_id = state.selected_con_id
        self._ensure_draft_locked()
        if state.status is UiStatus.READY:
            self._message = "Verified broker state is ready for read-only planning."
        elif state.status is not UiStatus.EMPTY:
            self._message = state.status_message

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
            Badge("SIMULATED DATA" if self._demo_mode else "READ-ONLY", variant="outline"),
            Span(f"Account {self._state.account or '—'}", cls="text-xs text-muted-foreground"),
            Span(cls="flex-1"),
            Span(f"Verified {self._state.snapshot_age}", cls="text-xs text-muted-foreground"),
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
        external = self._state.working_orders
        return Div(
            Alert(
                AlertTitle("Existing order coverage detected"),
                AlertDescription("Associated open orders remain inspect-only. Only verified available contracts can be bracketed."),
                cls="mb-5 border-amber-500/40 bg-amber-500/10 text-amber-100" if external else "hidden",
            ),
            Div(
                Div(H1(title, cls="text-2xl font-semibold tracking-tight"), P(self._message, cls="mt-2 text-sm text-muted-foreground")),
                Div(Span("Cost basis / Ask", cls="text-xs text-muted-foreground"), P(" / ".join(fact.value for fact in self._state.quote[:2]) or "—", cls="mt-1 font-mono text-sm"), cls="text-right"),
                cls="flex items-start justify-between gap-6",
            ),
            P(f"{self._state.available_quantity} contracts verified available to bracket", cls="mt-2 text-sm font-medium text-emerald-400"),
            Tabs(
                TabsList(
                    TabsTrigger("Draft layers", id="draft"),
                    TabsTrigger("Active layers", id="active", disabled=True),
                    variant="line",
                ),
                TabsContent(
                    self._draft_panel(),
                    id="draft",
                    cls="mt-5 min-h-0 flex-1",
                    style="overflow: hidden",
                ),
                TabsContent(
                    P(
                        "Active layer management arrives with the future transmission milestone."
                    ),
                    id="active",
                    cls="mt-5 min-h-0 flex-1",
                    style="overflow: hidden",
                ),
                value="draft",
                variant="line",
                cls="mt-5 flex min-h-0 flex-1 flex-col",
            ),
            cls="flex min-w-0 min-h-0 flex-col overflow-hidden px-8 py-6",
        )

    def _draft_panel(self) -> Any:
        layers = self._current_layers()
        return Form(
            Card(
                CardHeader(
                    Div(
                        CardTitle("Layered OCA draft"),
                        CardDescription(_DEFAULT_HELP, cls="mt-1"),
                    ),
                    CardAction(
                        Div(
                            DropdownMenu(
                                DropdownMenuTrigger(
                                    "Equal split",
                                    Icon("lucide:chevron-down", cls="size-4"),
                                    variant="outline",
                                    size="sm",
                                ),
                                DropdownMenuContent(
                                    DropdownMenuItem(
                                        "All available contracts",
                                        type="submit",
                                        name="action",
                                        value="equal-split-available",
                                    ),
                                    DropdownMenuItem(
                                        "Already assigned contracts",
                                        type="submit",
                                        name="action",
                                        value="equal-split-assigned",
                                    ),
                                    align="end",
                                ),
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
                            cls="flex items-center gap-2",
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
                                        cls="text-xs text-muted-foreground",
                                    ),
                                    cls="mt-5 flex justify-end",
                                ),
                                cls="w-full min-w-[41rem]",
                            ),
                            aria_label="Draft layer rows",
                            orientation="both",
                            cls="max-h-[25rem] w-full",
                        ),
                        cls="min-w-0",
                    ),
                ),
            ),
            self._outcome_projection(layers),
            HTMLInput(type="hidden", name="target_presets", value=self._target_presets),
            HTMLInput(type="hidden", name="stop_presets", value=self._stop_presets),
            action=f"/{self.session_token}/action",
            method="post",
        )

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
                    cls="pr-8",
                ),
                input_id=f"target_{index}",
                price=layer.target_price,
                outcome=gain,
                outcome_label="gain",
                tone="text-emerald-400",
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
                    cls="pr-8",
                ),
                input_id=f"stop_{index}",
                price=layer.stop_price,
                outcome=loss,
                outcome_label="max loss",
                tone="text-rose-400",
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
                Div(_metric("Expected gain", _money(gain), "text-emerald-400"), _metric("Max loss", _money(loss), "text-rose-400"), _metric("Breakeven after", _breakeven(outcomes), "text-amber-300"), cls="grid grid-cols-3 gap-8"),
            ),
            cls="mt-5",
        )

    def _review(self) -> Any:
        layers = self._current_layers()
        action_rows = [
            self._review_pair(index, layer)
            for index, layer in enumerate(layers, start=1)
        ]
        return Div(
            Div(Span("ACTION REVIEW", cls="text-xs font-semibold tracking-wide text-muted-foreground"), Badge("DRAFT", variant="outline", cls="text-[10px]"), cls="flex items-center justify-between px-4 py-4"),
            ScrollArea(
                *action_rows,
                aria_label="Planned order actions",
                cls="min-h-0 flex-1 px-4",
            ),
            Form(Button("Preview current draft", variant="outline", type="submit", cls="w-full"), HTMLInput(type="hidden", name="action", value="preview"), action=f"/{self.session_token}/action", method="post", cls="border-t border-border p-4"),
            Button("Transmission locked", disabled=True, cls="mx-4 mb-4 w-[calc(100%-2rem)]"),
            cls="flex min-h-0 flex-col overflow-hidden border-l border-border bg-card/30",
        )

    def _review_pair(self, index: int, layer: DraftLayerForm) -> Any:
        return Div(
            Div(
                P(f"OCA-{index}", cls="text-xs font-semibold"),
                P(
                    f"{layer.quantity} contracts · {layer.tif}",
                    cls="mt-0.5 text-xs text-muted-foreground",
                ),
            ),
            Div(
                Div(
                    Span("SELL LMT", cls="text-xs font-semibold text-emerald-400"),
                    Span(f"${layer.target_price}", cls="text-sm font-semibold text-emerald-400"),
                    cls="flex items-center justify-between gap-3",
                ),
                Div(
                    Span("SELL STP", cls="text-xs font-semibold text-rose-400"),
                    Span(f"${layer.stop_price}", cls="text-sm font-semibold text-rose-400"),
                    cls="flex items-center justify-between gap-3",
                ),
                cls="mt-3 space-y-3 border-l-2 border-border pl-3",
            ),
            cls="border-b border-border py-4",
        )


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
) -> Any:
    """Render a percentage input with its calculated price and layer outcome."""
    return Div(
        Div(
            Label(label, fr=input_id, cls="text-xs font-medium text-muted-foreground"),
            Span(f"${price}", cls=f"text-xs font-semibold {tone}"),
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
            Span(f"{outcome} {outcome_label}", cls=f"text-xs {tone}"),
            cls="mt-1 flex justify-end",
        ),
        cls="min-w-0 space-y-0.5",
    )


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


def _metric(label: str, value: str, tone: str) -> Any:
    return Div(P(label, cls="text-xs font-semibold uppercase tracking-wide text-muted-foreground"), P(value, cls=f"mt-1 font-mono text-lg font-semibold {tone}"))


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
