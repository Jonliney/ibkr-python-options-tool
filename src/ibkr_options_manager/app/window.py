from __future__ import annotations

# ruff: noqa: E501
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from PySide6.QtCore import (
    Property,
    QObject,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..domain import preview_reference_prices
from .view_model import (
    ConnectionSettings,
    DraftLayerForm,
    Fact,
    PlanForm,
    PlannerViewModel,
    QuoteCalculatorLine,
    RouteMark,
    UiStatus,
    ViewState,
)


class _ThreadPool(Protocol):
    def start(self, runnable: QRunnable) -> None: ...


@dataclass(slots=True)
class _LayerWidgets:
    target_percentage: QDoubleSpinBox
    stop_percentage: QDoubleSpinBox
    target_price: QLabel
    stop_price: QLabel
    target_outcome: QLabel
    stop_outcome: QLabel
    quantity: QLineEdit
    tif: QComboBox


class _PercentageSpinBox(QDoubleSpinBox):
    """A percentage editor that preserves compact, human-readable whole values."""

    def textFromValue(self, value: float) -> str:
        return format(Decimal(str(value)).normalize(), "f")


class _RefreshSignals(QObject):
    finished = Signal(object)


class _RefreshTask(QRunnable):
    def __init__(self, operation: Callable[[], ViewState]) -> None:
        super().__init__()
        self._operation = operation
        self.signals = _RefreshSignals()

    @Slot()
    def run(self) -> None:
        self.signals.finished.emit(self._operation())


class _PositionRowButton(QPushButton):
    """A compact inventory row with the same scan order as the workbench brief."""

    def __init__(
        self,
        local_symbol: str,
        quantity: str,
        unit_basis: str,
        eligibility: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        pieces = local_symbol.split(maxsplit=1)
        self._symbol = pieces[0] if pieces else local_symbol
        self._contract = pieces[1] if len(pieces) > 1 else "Option contract"
        self._quantity = quantity
        self._basis = unit_basis
        self._eligibility = eligibility
        self.setText("")
        self.setMinimumHeight(88)

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        foreground = QColor("#edf3ec" if self.isEnabled() else "#7c867c")
        muted = QColor("#8f9b8f" if self.isEnabled() else "#687168")
        rect = self.contentsRect().adjusted(12, 0, -12, 0)

        painter.setPen(foreground)
        painter.setFont(QFont("Avenir Next", 11, QFont.Weight.Bold))
        painter.drawText(
            QRectF(rect.left(), 11, rect.width() * 0.55, 17),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._symbol,
        )
        painter.setFont(QFont("SF Mono", 9, QFont.Weight.Bold))
        painter.drawText(
            QRectF(rect.left() + rect.width() * 0.55, 11, rect.width() * 0.45, 17),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"{self._quantity} / {self._quantity}",
        )
        painter.setPen(muted)
        painter.setFont(QFont("SF Mono", 9))
        painter.drawText(
            QRectF(rect.left(), 31, rect.width(), 16),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._contract,
        )
        painter.drawText(
            QRectF(rect.left(), 56, rect.width() * 0.55, 16),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"Basis  {self._basis}",
        )
        painter.setPen(QColor("#59d477") if self.isEnabled() else muted)
        painter.setFont(QFont("Avenir Next", 9, QFont.Weight.Bold))
        painter.drawText(
            QRectF(rect.left() + rect.width() * 0.55, 56, rect.width() * 0.45, 16),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            self._eligibility,
        )


class PriceRouteWidget(QWidget):
    """Draws the observed and planned option prices on one calibrated route."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._marks: tuple[RouteMark, ...] = ()
        self._reveal = 1.0
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Price route")

    def marks(self) -> tuple[RouteMark, ...]:
        return self._marks

    def set_marks(self, marks: tuple[RouteMark, ...]) -> None:
        self._marks = marks
        self.setAccessibleDescription(
            "; ".join(f"{mark.label} at {mark.price}" for mark in marks)
            if marks
            else "No verified price route is available"
        )
        self.update()

    def get_reveal(self) -> float:
        return self._reveal

    def set_reveal(self, value: float) -> None:
        self._reveal = max(0.0, min(1.0, value))
        self.update()

    reveal = Property(float, get_reveal, set_reveal)

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#eaf1f2"))
        if not self._marks:
            painter.setPen(QColor("#536b78"))
            painter.setFont(QFont("Avenir Next", 13))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Refresh a verified position to plot its price route",
            )
            return

        values = [mark.price for mark in self._marks if mark.price.is_finite()]
        if not values:
            return
        low = min(values)
        high = max(values)
        span = max(high - low, Decimal("1"))
        low -= span * Decimal("0.08")
        high += span * Decimal("0.08")
        plot = QRectF(
            56,
            54,
            max(40, self.width() - 88),
            max(100, self.height() - 142),
        )

        painter.setFont(QFont("SF Mono", 9))
        for index in range(6):
            ratio = index / 5
            x = plot.left() + plot.width() * ratio
            price = low + (high - low) * Decimal(str(ratio))
            painter.setPen(QPen(QColor("#bdd0d4"), 1))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(QColor("#536b78"))
            painter.drawText(
                QRectF(x - 34, plot.bottom() + 8, 68, 20),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                f"{price:.2f}",
            )

        basis = next((mark for mark in self._marks if mark.kind == "BASIS"), None)
        targets = {
            mark.label[1:]: mark
            for mark in self._marks
            if mark.kind == "TARGET"
            and mark.label.startswith("T")
            and mark.label[1:].isdigit()
        }
        stops = {
            mark.label[1:]: mark
            for mark in self._marks
            if mark.kind == "STOP"
            and mark.label.startswith("S")
            and mark.label[1:].isdigit()
        }
        if basis is not None:
            basis_x = _price_x(basis.price, low, high, plot)
            for lane, key in enumerate(sorted(targets, key=int)):
                target = targets[key]
                stop = stops.get(key)
                if stop is None:
                    continue
                y = plot.top() + 96 + lane * 34
                if y > plot.bottom() - 18:
                    break
                path = QPainterPath(QPointF(_price_x(stop.price, low, high, plot), y))
                path.lineTo(QPointF(basis_x, y))
                path.lineTo(QPointF(_price_x(target.price, low, high, plot), y))
                painter.save()
                painter.setClipRect(
                    QRectF(
                        plot.left(),
                        plot.top(),
                        plot.width() * self._reveal,
                        plot.height(),
                    )
                )
                painter.setPen(QPen(QColor("#8aa7ad"), 1.5))
                painter.drawPath(path)
                painter.restore()

        grouped: dict[tuple[str, Decimal], list[RouteMark]] = {}
        for mark in self._marks:
            grouped.setdefault((mark.kind, mark.price), []).append(mark)
        ordered = sorted(
            grouped.values(),
            key=lambda marks: (marks[0].price, marks[0].kind, marks[0].label),
        )
        lanes = {"QUOTE": 0, "BASIS": 1, "TARGET": 2, "STOP": 3}
        colors = {
            "QUOTE": QColor("#177e89"),
            "BASIS": QColor("#071a2b"),
            "TARGET": QColor("#d34174"),
            "STOP": QColor("#b04a35"),
        }
        for marks in ordered:
            mark = marks[0]
            display_label = (
                mark.label if len(marks) == 1 else f"{marks[0].label}-{marks[-1].label}"
            )
            x = _price_x(mark.price, low, high, plot)
            lane = lanes.get(mark.kind, 0)
            top = plot.top() + lane * 22
            bottom = plot.bottom() - lane * 8
            pen = QPen(colors.get(mark.kind, QColor("#071a2b")), 2)
            if mark.kind == "STOP":
                pen.setStyle(Qt.PenStyle.DashLine)
            elif mark.kind == "BASIS":
                pen.setWidthF(3.0)
            painter.setPen(pen)
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
            painter.setPen(colors.get(mark.kind, QColor("#071a2b")))
            painter.setFont(QFont("SF Mono", 9, QFont.Weight.DemiBold))
            align = (
                Qt.AlignmentFlag.AlignLeft
                if x < plot.center().x()
                else Qt.AlignmentFlag.AlignRight
            )
            label_rect = QRectF(
                x + 5 if x < plot.center().x() else x - 125,
                top - 21,
                120,
                19,
            )
            painter.drawText(
                label_rect, align | Qt.AlignmentFlag.AlignVCenter, display_label
            )


def _price_x(price: Decimal, low: Decimal, high: Decimal, plot: QRectF) -> float:
    ratio = float((price - low) / (high - low))
    return plot.left() + plot.width() * ratio


class PlannerWindow(QMainWindow):
    def __init__(
        self,
        view_model: PlannerViewModel,
        *,
        initial_account: str = "",
        initial_con_id: int | None = None,
        thread_pool: _ThreadPool | None = None,
        demo_mode: bool = False,
    ) -> None:
        super().__init__()
        self._view_model = view_model
        self._thread_pool = thread_pool or QThreadPool.globalInstance()
        self._state = view_model.empty()
        self._quote_calculator: QuoteCalculatorLine | None = None
        self._draft_by_con_id: dict[int, tuple[DraftLayerForm, ...]] = {}
        self._layer_widgets: list[_LayerWidgets] = []
        self._preferred_con_id = initial_con_id
        self._selected_con_id: int | None = None
        self._active_task: _RefreshTask | None = None
        self._busy_kind = ""
        self._updating = False
        self._demo_mode = demo_mode
        self._route_animation: QPropertyAnimation | None = None
        self.setWindowTitle(
            "IBKR Options Manager — Simulated data"
            if demo_mode
            else "IBKR Options Manager — Read-only preview"
        )
        self.resize(1500, 920)
        self.setMinimumSize(1120, 720)
        self._build_ui(initial_account)
        self.setStyleSheet(_STYLE)
        self._apply_state(self._state)

    def _build_ui(self, initial_account: str) -> None:
        root = QWidget()
        root.setObjectName("root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 8, 16, 8)
        self.connection_dot = QLabel()
        self.connection_dot.setObjectName("connectionDot")
        header_layout.addWidget(self.connection_dot)
        self.status_label = QLabel()
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAccessibleName("Planner status")
        header_layout.addWidget(self.status_label)
        self.demo_indicator = QLabel("SIMULATED DATA · NO TWS")
        self.demo_indicator.setObjectName("demoIndicator")
        self.demo_indicator.setVisible(self._demo_mode)
        header_layout.addWidget(self.demo_indicator)
        self.account_label = QLabel("Account —")
        self.account_label.setObjectName("accountLabel")
        self.account_label.setAccessibleName("Selected account")
        header_layout.addWidget(self.account_label)
        header_layout.addStretch()
        self.verified_label = QLabel("Not verified")
        self.verified_label.setObjectName("verifiedLabel")
        header_layout.addWidget(self.verified_label)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("refreshButton")
        self.refresh_button.setToolTip("Refresh open option positions")
        self.refresh_button.clicked.connect(self._start_refresh)
        header_layout.addWidget(self.refresh_button)
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setText("Settings")
        self.settings_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        self.settings_button.setCheckable(True)
        self.settings_button.setAccessibleName("Connection settings")
        self.settings_button.setToolTip("Show connection settings")
        header_layout.addWidget(self.settings_button)
        root_layout.addWidget(header)

        root_layout.addWidget(self._build_workspace(initial_account), 1)

        self.setCentralWidget(root)

    def _build_workspace(self, initial_account: str) -> QWidget:
        workspace = QSplitter(Qt.Orientation.Horizontal)
        workspace.setObjectName("workspace")
        workspace.setChildrenCollapsible(False)

        inventory = QFrame()
        inventory.setObjectName("inventoryPane")
        inventory_layout = QVBoxLayout(inventory)
        inventory_layout.setContentsMargins(0, 0, 0, 0)
        inventory_layout.setSpacing(0)
        inventory_header = QHBoxLayout()
        inventory_header.setContentsMargins(12, 12, 12, 10)
        inventory_header.addWidget(_section_title("Long positions"))
        inventory_header.addStretch()
        self.position_count_label = QLabel("0 active")
        self.position_count_label.setObjectName("positionCount")
        inventory_header.addWidget(self.position_count_label)
        inventory_layout.addLayout(inventory_header)
        self.positions_scroll = QScrollArea()
        self.positions_scroll.setObjectName("positionsScroll")
        self.positions_scroll.setWidgetResizable(True)
        self.positions_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.positions_panel = QWidget()
        self.positions_panel.setObjectName("positionsPanel")
        self.positions_layout = QVBoxLayout(self.positions_panel)
        self.positions_layout.setContentsMargins(0, 0, 0, 0)
        self.positions_layout.setSpacing(0)
        self.positions_layout.addStretch()
        self.positions_scroll.setWidget(self.positions_panel)
        inventory_layout.addWidget(self.positions_scroll, 1)

        self.connection_settings = QFrame()
        self.connection_settings.setObjectName("connectionSettings")
        settings_layout = QGridLayout(self.connection_settings)
        settings_layout.setContentsMargins(12, 12, 12, 12)
        settings_layout.setHorizontalSpacing(8)
        settings_layout.setVerticalSpacing(6)
        connection_grid = QGridLayout()
        connection_grid.setHorizontalSpacing(12)
        connection_grid.setVerticalSpacing(6)
        self.account_input = QLineEdit(initial_account)
        self.account_input.setObjectName("accountInput")
        self.account_input.setPlaceholderText("Full DU paper account ID")
        self.account_input.setClearButtonEnabled(True)
        self.port_input = QSpinBox()
        self.port_input.setObjectName("portInput")
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(7497)
        self.client_id_input = QSpinBox()
        self.client_id_input.setObjectName("clientIdInput")
        self.client_id_input.setRange(1, 2_147_483_647)
        self.client_id_input.setValue(17)
        self.timeout_input = QDoubleSpinBox()
        self.timeout_input.setObjectName("timeoutInput")
        self.timeout_input.setRange(1, 60)
        self.timeout_input.setValue(20)
        self.timeout_input.setSuffix(" s")
        self.lmt_target_presets_input = QLineEdit("20, 40, 60, 100")
        self.lmt_target_presets_input.setObjectName("lmtTargetPresetsInput")
        self.lmt_target_presets_input.setPlaceholderText("20, 40, 60, 100")
        self.lmt_target_presets_input.setToolTip(
            "Comma-separated LMT target percentages. The final value repeats "
            "for any additional layer."
        )
        self.stp_loss_presets_input = QLineEdit("25")
        self.stp_loss_presets_input.setObjectName("stpLossPresetsInput")
        self.stp_loss_presets_input.setPlaceholderText("25")
        self.stp_loss_presets_input.setToolTip(
            "Comma-separated STP loss percentages. The final value repeats "
            "for any additional layer."
        )
        _add_field(connection_grid, 0, "Account", self.account_input)
        _add_field(connection_grid, 1, "Port", self.port_input)
        _add_field(connection_grid, 2, "Client ID", self.client_id_input)
        _add_field(connection_grid, 3, "Timeout", self.timeout_input)
        settings_layout.addLayout(connection_grid, 0, 0)
        preset_grid = QGridLayout()
        preset_grid.setHorizontalSpacing(12)
        preset_grid.setVerticalSpacing(6)
        _add_field(preset_grid, 0, "LMT targets", self.lmt_target_presets_input)
        _add_field(preset_grid, 1, "STP losses", self.stp_loss_presets_input)
        settings_layout.addLayout(preset_grid, 1, 0)
        inventory_layout.addWidget(self.connection_settings)
        self.settings_button.toggled.connect(self.connection_settings.setVisible)
        self.connection_settings.setVisible(not bool(initial_account))
        self.settings_button.setChecked(not bool(initial_account))

        center_scroll = QScrollArea()
        self.workspace_scroll = center_scroll
        center_scroll.setObjectName("workspaceScroll")
        center_scroll.setWidgetResizable(True)
        center_scroll.setFrameShape(QFrame.Shape.NoFrame)
        center = QWidget()
        center.setObjectName("workspacePanel")
        layout = QVBoxLayout(center)
        layout.setContentsMargins(32, 18, 32, 32)
        layout.setSpacing(16)
        self.external_notice = QLabel()
        self.external_notice.setObjectName("externalNotice")
        self.external_notice.setWordWrap(True)
        self.external_notice.setVisible(False)
        layout.addWidget(self.external_notice)
        selected_header = QHBoxLayout()
        self.position_title = QLabel("Select an open option position")
        self.position_title.setObjectName("positionTitle")
        selected_header.addWidget(self.position_title)
        selected_header.addStretch()
        self.position_overview = QLabel("Cost basis / Ask —")
        self.position_overview.setObjectName("positionOverview")
        self.position_overview.setAlignment(Qt.AlignmentFlag.AlignRight)
        selected_header.addWidget(self.position_overview)
        layout.addLayout(selected_header)
        self.available_label = QLabel("Refresh and select a position to build a draft.")
        self.available_label.setObjectName("availableLabel")
        layout.addWidget(self.available_label)
        layout.addWidget(self._build_bracket_page())
        layout.addWidget(self._build_outcome_projection())
        layout.addStretch(1)
        center_scroll.setWidget(center)

        self.action_extended_view = self._build_action_extended_view()
        workspace.addWidget(inventory)
        workspace.addWidget(center_scroll)
        workspace.addWidget(self.action_extended_view)
        workspace.setSizes([250, 940, 300])
        workspace.setStretchFactor(1, 1)

        for widget in (
            self.account_input,
            self.port_input,
            self.client_id_input,
            self.timeout_input,
        ):
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._mark_connection_dirty)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.valueChanged.connect(self._mark_connection_dirty)

        for widget in (self.lmt_target_presets_input, self.stp_loss_presets_input):
            widget.textChanged.connect(self._preset_settings_changed)

        QWidget.setTabOrder(self.account_input, self.port_input)
        QWidget.setTabOrder(self.port_input, self.client_id_input)
        QWidget.setTabOrder(self.client_id_input, self.timeout_input)
        QWidget.setTabOrder(self.timeout_input, self.lmt_target_presets_input)
        QWidget.setTabOrder(self.lmt_target_presets_input, self.stp_loss_presets_input)
        return workspace

    def _build_inline_orders(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("inlineOrdersPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        layout.addWidget(_section_title("Active orders", dark=False))

        self.inline_orders_table = QTableWidget(0, 5)
        self.inline_orders_table.setObjectName("inlineOrdersTable")
        self.inline_orders_table.setHorizontalHeaderLabels(
            ["Perm ID", "Action", "Type", "Remaining", "Status"]
        )
        self.inline_orders_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.inline_orders_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.inline_orders_table.verticalHeader().setVisible(False)
        self.inline_orders_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.inline_orders_table.horizontalHeader().setStretchLastSection(True)
        self.inline_orders_table.setMinimumHeight(76)
        self.inline_orders_table.setMaximumHeight(112)
        layout.addWidget(self.inline_orders_table)
        return panel

    def _build_bracket_page(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("draftPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        heading = QHBoxLayout()
        heading.setContentsMargins(16, 13, 16, 12)
        heading.setSpacing(8)
        title_group = QVBoxLayout()
        title = QLabel("Layered OCA draft")
        title.setObjectName("panelTitle")
        title_group.addWidget(title)
        self.bracket_help = QLabel(
            "Each row creates one equal-quantity SELL LMT + SELL STP pair."
        )
        self.bracket_help.setObjectName("panelHelp")
        title_group.addWidget(self.bracket_help)
        heading.addLayout(title_group)
        heading.addStretch()
        self._equal_split_mode = "available"
        self.equal_split_button = QToolButton()
        self.equal_split_button.setObjectName("equalSplitButton")
        self.equal_split_button.setProperty("secondary", True)
        self.equal_split_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup
        )
        self.equal_split_menu = QMenu(self.equal_split_button)
        self.equal_split_available_action = QAction(
            "Equal split available contracts", self.equal_split_menu
        )
        self.equal_split_assigned_action = QAction(
            "Equal split assigned contracts", self.equal_split_menu
        )
        self.equal_split_available_action.triggered.connect(
            lambda: self._set_equal_split_mode("available")
        )
        self.equal_split_assigned_action.triggered.connect(
            lambda: self._set_equal_split_mode("assigned")
        )
        self.equal_split_menu.addAction(self.equal_split_available_action)
        self.equal_split_menu.addAction(self.equal_split_assigned_action)
        self.equal_split_button.setMenu(self.equal_split_menu)
        self._set_equal_split_mode("available")
        self.equal_split_button.clicked.connect(self._equal_split_layers)
        heading.addWidget(self.equal_split_button)
        heading.addSpacing(4)
        self.add_layer_button = QPushButton("Add layer")
        self.add_layer_button.setObjectName("addLayerButton")
        self.add_layer_button.setProperty("secondary", True)
        self.add_layer_button.setToolTip(
            "Each layer needs at least one contract; the layer count cannot "
            "exceed the verified available quantity."
        )
        self.add_layer_button.clicked.connect(self._add_layer)
        heading.addWidget(self.add_layer_button)
        layout.addLayout(heading)

        self.layer_rows_widget = QWidget()
        self.layer_rows_widget.setObjectName("layerRows")
        self.layer_rows_layout = QVBoxLayout(self.layer_rows_widget)
        self.layer_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.layer_rows_layout.setSpacing(0)
        layout.addWidget(self.layer_rows_widget)
        footer = QHBoxLayout()
        footer.setContentsMargins(16, 9, 16, 9)
        footer.addStretch()
        self.draft_quantity_label = QLabel("0 / 0 contracts")
        self.draft_quantity_label.setObjectName("draftQuantity")
        footer.addWidget(self.draft_quantity_label)
        layout.addLayout(footer)
        return panel

    def _build_outcome_projection(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("outcomeProjection")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(9)
        title_row = QHBoxLayout()
        title = QLabel("Outcome projection")
        title.setObjectName("panelTitle")
        title_row.addWidget(title)
        title_row.addStretch()
        layout.addLayout(title_row)
        self.outcome_summary_label = QLabel("Waiting for a verified draft layer.")
        self.outcome_summary_label.setObjectName("outcomeSummary")
        layout.addWidget(self.outcome_summary_label)
        metrics = QHBoxLayout()
        metrics.setSpacing(28)
        gain = QVBoxLayout()
        gain.setSpacing(2)
        gain_label = QLabel("Expected gain")
        gain_label.setObjectName("outcomeMetricLabel")
        gain.addWidget(gain_label)
        self.outcome_gain_label = QLabel("—")
        self.outcome_gain_label.setObjectName("outcomeGain")
        gain.addWidget(self.outcome_gain_label)
        metrics.addLayout(gain)
        loss = QVBoxLayout()
        loss.setSpacing(2)
        loss_label = QLabel("Max loss")
        loss_label.setObjectName("outcomeMetricLabel")
        loss.addWidget(loss_label)
        self.outcome_loss_label = QLabel("—")
        self.outcome_loss_label.setObjectName("outcomeLoss")
        loss.addWidget(self.outcome_loss_label)
        metrics.addLayout(loss)
        breakeven = QVBoxLayout()
        breakeven.setSpacing(2)
        breakeven_label = QLabel("Breakeven after")
        breakeven_label.setObjectName("outcomeMetricLabel")
        breakeven.addWidget(breakeven_label)
        self.outcome_breakeven_label = QLabel("—")
        self.outcome_breakeven_label.setObjectName("outcomeBreakeven")
        breakeven.addWidget(self.outcome_breakeven_label)
        metrics.addLayout(breakeven)
        metrics.addStretch()
        layout.addLayout(metrics)
        return panel

    def _update_outcome_projection(self) -> None:
        basis = self._state.unit_basis
        multiplier = self._state.multiplier
        layers = self._draft_layer_forms()
        if (
            self._selected_con_id is None
            or basis is None
            or multiplier is None
            or not layers
        ):
            self.outcome_summary_label.setText(
                "Refresh a verified position to project this draft's outcomes."
            )
            self.outcome_gain_label.setText("—")
            self.outcome_loss_label.setText("—")
            self.outcome_breakeven_label.setText("—")
            return
        outcomes = self._draft_layer_outcomes()
        if outcomes is None:
            self.outcome_summary_label.setText(
                "Complete every layer with valid prices and quantity to project outcomes."
            )
            self.outcome_gain_label.setText("—")
            self.outcome_loss_label.setText("—")
            self.outcome_breakeven_label.setText("—")
            return
        quantity = sum(layer_quantity for layer_quantity, _, _ in outcomes)
        if quantity > self._state.available_quantity:
            self.outcome_summary_label.setText(
                "Draft quantity exceeds the verified available contracts."
            )
            self.outcome_gain_label.setText("—")
            self.outcome_loss_label.setText("—")
            self.outcome_breakeven_label.setText("—")
            return
        target_pnl = sum((target for _, target, _ in outcomes), Decimal("0"))
        stop_pnl = sum((stop for _, _, stop in outcomes), Decimal("0"))
        cost = basis * multiplier * quantity
        self.outcome_summary_label.setText(
            f"{quantity} assigned contract(s) · Basis {_price_text(basis)} · "
            f"multiplier {format(multiplier, 'f')}"
        )
        self.outcome_gain_label.setText(
            f"{_signed_money_text(target_pnl)} ({_return_percentage_text(target_pnl, cost)})"
        )
        self.outcome_loss_label.setText(
            f"{_signed_money_text(stop_pnl)} ({_return_percentage_text(stop_pnl, cost)})"
        )
        breakeven = _modeled_breakeven(outcomes)
        if breakeven is None:
            self.outcome_breakeven_label.setText("—")
        else:
            layer_index, floor = breakeven
            self.outcome_breakeven_label.setText(
                f"Layer {layer_index} ({_signed_money_text(floor)})"
            )

    def _build_action_extended_view(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("reviewPane")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        heading = QHBoxLayout()
        heading.setContentsMargins(14, 13, 14, 12)
        title = QLabel("Action review")
        title.setObjectName("reviewTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.review_status_label = QLabel("Draft")
        self.review_status_label.setObjectName("reviewStatus")
        heading.addWidget(self.review_status_label)
        layout.addLayout(heading)
        self.preview_notice = QLabel(
            "Preview only\nNo order will be placed, modified, or cancelled."
        )
        self.preview_notice.setObjectName("previewNotice")
        self.preview_notice.setWordWrap(True)
        layout.addWidget(self.preview_notice)
        self.review_scroll = QScrollArea()
        self.review_scroll.setObjectName("reviewScroll")
        self.review_scroll.setWidgetResizable(True)
        self.review_scroll.setFrameShape(QFrame.Shape.NoFrame)
        review_content = QWidget()
        review_content.setObjectName("reviewContent")
        self.review_actions_layout = QVBoxLayout(review_content)
        self.review_actions_layout.setContentsMargins(18, 16, 14, 16)
        self.review_actions_layout.setSpacing(0)
        self.review_actions_layout.addStretch()
        self.review_scroll.setWidget(review_content)
        layout.addWidget(self.review_scroll, 1)
        footer = QVBoxLayout()
        footer.setContentsMargins(14, 12, 14, 14)
        footer.setSpacing(8)
        self.review_quantity_label = QLabel("Draft quantity 0 / 0")
        self.review_quantity_label.setObjectName("reviewQuantity")
        footer.addWidget(self.review_quantity_label)
        self.preview_button = QPushButton("Preview current draft")
        self.preview_button.setObjectName("previewButton")
        self.preview_button.setProperty("secondary", True)
        self.preview_button.clicked.connect(self._preview)
        footer.addWidget(self.preview_button)
        self.transmission_locked = QPushButton("Transmission locked")
        self.transmission_locked.setObjectName("transmissionLocked")
        self.transmission_locked.setEnabled(False)
        footer.addWidget(self.transmission_locked)
        layout.addLayout(footer)
        return panel

    def load_demo_data(self) -> None:
        """Populate the simulated workbench without using the refresh worker."""
        if not self._demo_mode:
            return
        portfolio_state = self._view_model.refresh_portfolio(self._connection_settings())
        self._apply_state(portfolio_state)
        target_con_id = self._preferred_con_id
        if target_con_id is None and self._state.positions:
            target_con_id = self._state.positions[0].con_id
        if target_con_id is None:
            return
        if target_con_id not in {position.con_id for position in self._state.positions}:
            return
        self._preferred_con_id = None
        self._selected_con_id = target_con_id
        form = PlanForm(layers=self._draft_by_con_id.get(target_con_id, ()))
        self._apply_state(self._view_model.select_position(target_con_id, form))

    @Slot()
    def _start_refresh(self) -> None:
        if self._demo_mode:
            self.load_demo_data()
            return
        settings = self._connection_settings()
        self._selected_con_id = None
        self._start_task(
            "portfolio",
            lambda: self._view_model.refresh_portfolio(settings),
        )

    @Slot(object)
    def _task_finished(self, state: object) -> None:
        kind = self._busy_kind
        self._active_task = None
        self._set_busy(False)
        if isinstance(state, ViewState):
            self._apply_state(state)
        if kind != "portfolio":
            return
        target_con_id = self._preferred_con_id
        if target_con_id is None and self._state.positions:
            target_con_id = self._state.positions[0].con_id
        if target_con_id is None:
            return
        if target_con_id not in {position.con_id for position in self._state.positions}:
            return
        self._preferred_con_id = None
        self._select_position(target_con_id)

    def _select_position(self, con_id: int) -> None:
        if self._active_task is not None or con_id == self._selected_con_id:
            return
        self._selected_con_id = con_id
        form = PlanForm(layers=self._draft_by_con_id.get(con_id, ()))
        self._start_task(
            "position",
            lambda: self._view_model.select_position(con_id, form),
        )

    def _start_task(self, kind: str, operation: Callable[[], ViewState]) -> None:
        if self._active_task is not None:
            return
        self._busy_kind = kind
        self._set_busy(True)
        task = _RefreshTask(operation)
        self._active_task = task
        task.signals.finished.connect(self._task_finished)
        self._thread_pool.start(task)

    @Slot()
    def _preview(self, *, focus_result: bool = True) -> None:
        if self._state.selected_con_id is None or self._active_task is not None:
            return
        self._apply_state(self._view_model.preview_action(self._form()))
        if focus_result:
            self.review_scroll.verticalScrollBar().setValue(0)

    @Slot()
    def _mark_connection_dirty(self) -> None:
        if self._updating or self._state.status is UiStatus.EMPTY:
            return
        self._selected_con_id = None
        self.preview_button.setEnabled(False)
        self.status_label.setText("INPUTS CHANGED · REFRESH REQUIRED")
        self.status_label.setProperty("state", "blocked")
        _repolish(self.status_label)
        self._clear_position_buttons()
        self._quote_calculator = None
        self._update_outcome_projection()

    @Slot()
    def _preset_settings_changed(self) -> None:
        """Validate layer defaults without invalidating the broker snapshot."""
        presets_valid = self._layer_preset_values() is not None
        for widget in (self.lmt_target_presets_input, self.stp_loss_presets_input):
            widget.setProperty("presetInvalid", not presets_valid)
            _repolish(widget)
        if not presets_valid:
            self.bracket_help.setText(
                "Use comma-separated LMT targets (above 0%) and STP losses "
                "(between 0% and 100%)."
            )
        elif not self._updating:
            self.bracket_help.setText(
                "Each row creates one equal-quantity SELL LMT + SELL STP pair."
            )
        self._update_draft_totals()

    def _set_busy(self, busy: bool) -> None:
        self.refresh_button.setEnabled(not busy)
        self.positions_scroll.setEnabled(not busy)
        self.preview_button.setEnabled(not busy and self._state.can_preview)
        if busy:
            message = (
                "REFRESHING POSITIONS · PRIOR STATE INVALIDATED"
                if self._busy_kind == "portfolio"
                else "VERIFYING POSITION · PRIOR PREVIEW INVALIDATED"
            )
            self.status_label.setText(message)
            self.status_label.setProperty("state", "loading")
            _repolish(self.status_label)
            self._quote_calculator = None
            self._update_outcome_projection()

    def _connection_settings(self) -> ConnectionSettings:
        return ConnectionSettings(
            account=self.account_input.text().strip(),
            port=self.port_input.value(),
            client_id=self.client_id_input.value(),
            timeout_seconds=self.timeout_input.value(),
        )

    def _form(self) -> PlanForm:
        return PlanForm(
            layers=self._draft_layer_forms(),
        )

    def _apply_state(self, state: ViewState) -> None:
        self._updating = True
        try:
            self._state = state
            self._selected_con_id = state.selected_con_id
            status_prefix = "SIMULATED · " if self._demo_mode else ""
            self.status_label.setText(
                f"{status_prefix}{state.status.value} · {state.status_message.upper()}"
            )
            self.status_label.setAccessibleDescription(state.status_message)
            self.status_label.setProperty("state", state.status.value.lower())
            _repolish(self.status_label)
            self.connection_dot.setProperty(
                "state", "ready" if state.status is UiStatus.READY else "blocked"
            )
            _repolish(self.connection_dot)
            self.account_label.setText(f"Account {state.account}")
            self.verified_label.setText(f"Verified {state.snapshot_age}")
            if state.status is UiStatus.READY and self.settings_button.isChecked():
                self.settings_button.setChecked(False)
            self.preview_button.setEnabled(state.can_preview)
            self.position_title.setText(state.position_title)
            self.position_overview.setText(
                "Cost basis / Ask  "
                f"{_price_text(state.unit_basis)} / "
                f"{_price_text(None if state.quote_calculator is None else state.quote_calculator.ask)}"
            )
            self._populate_positions(state)
            self._quote_calculator = state.quote_calculator
            self._ensure_draft(state)
            self._render_draft_layers()
            self.available_label.setText(
                f"{state.available_quantity} contracts verified available to bracket"
                if state.selected_con_id is not None
                else "Refresh and select a position to build a draft."
            )
            self.external_notice.setVisible(bool(state.working_orders))
            if state.working_orders:
                self.external_notice.setText(
                    "External order coverage detected. Some held contracts are "
                    "already associated with orders and remain inspect-only."
                )
            self._update_action_review()
        finally:
            self._updating = False

    def _populate_positions(self, state: ViewState) -> None:
        self._clear_position_buttons()
        self.position_count_label.setText(f"{len(state.positions)} active")
        for position in state.positions:
            button = _PositionRowButton(
                position.local_symbol,
                position.quantity,
                position.unit_basis,
                position.eligibility,
            )
            button.setObjectName("positionButton")
            button.setProperty("selected", position.con_id == state.selected_con_id)
            button.setProperty("eligible", position.eligible)
            button.setAccessibleName(
                f"Select {position.local_symbol}, {position.quantity} contracts, "
                f"basis {position.unit_basis}, {position.eligibility}"
            )
            button.setEnabled(position.eligible)
            button.clicked.connect(
                lambda _checked=False, con_id=position.con_id: self._select_position(
                    con_id
                )
            )
            self.positions_layout.insertWidget(self.positions_layout.count() - 1, button)

    def _clear_position_buttons(self) -> None:
        while self.positions_layout.count() > 1:
            item = self.positions_layout.takeAt(0)
            widget = None if item is None else item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _ensure_draft(self, state: ViewState) -> None:
        con_id = state.selected_con_id
        if con_id is None:
            return
        if con_id in self._draft_by_con_id:
            return
        if state.bracket_form.layers:
            self._draft_by_con_id[con_id] = state.bracket_form.layers
            return
        if (
            state.unit_basis is None
            or state.quote_calculator is None
            or state.available_quantity <= 0
        ):
            self._draft_by_con_id[con_id] = ()
            return
        percentages = self._layer_preset_for_index(0)
        if percentages is None:
            self._draft_by_con_id[con_id] = ()
            return
        target_percentage, stop_percentage = percentages
        try:
            preview = preview_reference_prices(
                state.unit_basis,
                target_percentage,
                stop_percentage,
                state.quote_calculator.bands,
            )
        except ValueError:
            self._draft_by_con_id[con_id] = ()
            return
        self._draft_by_con_id[con_id] = (
            DraftLayerForm(
                quantity=str(state.available_quantity),
                target_price=format(preview.target_price, "f"),
                stop_price=format(preview.stop_price, "f"),
                target_percentage=format(target_percentage, "f"),
                stop_percentage=format(stop_percentage, "f"),
            ),
        )

    def _layer_preset_values(
        self,
    ) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...]] | None:
        targets = _parse_percentage_presets(
            self.lmt_target_presets_input.text(), maximum=Decimal("1000")
        )
        stops = _parse_percentage_presets(
            self.stp_loss_presets_input.text(), maximum=Decimal("100")
        )
        if targets is None or stops is None:
            return None
        return targets, stops

    def _layer_preset_for_index(self, index: int) -> tuple[Decimal, Decimal] | None:
        presets = self._layer_preset_values()
        if presets is None:
            return None
        targets, stops = presets
        return (
            targets[min(index, len(targets) - 1)],
            stops[min(index, len(stops) - 1)],
        )

    def _draft_layer_forms(self) -> tuple[DraftLayerForm, ...]:
        if not self._layer_widgets:
            return ()
        return tuple(
            DraftLayerForm(
                quantity=widgets.quantity.text(),
                target_price=_raw_price_text(widgets.target_price.text()),
                stop_price=_raw_price_text(widgets.stop_price.text()),
                target_percentage=widgets.target_percentage.text(),
                tif=widgets.tif.currentText(),
                runner=False,
                stop_percentage=widgets.stop_percentage.text(),
            )
            for widgets in self._layer_widgets
        )

    def _draft_quantity(self) -> int:
        quantity = 0
        for layer in self._draft_layer_forms():
            try:
                value = int(layer.quantity.strip())
            except ValueError:
                continue
            if value > 0:
                quantity += value
        return quantity

    def _draft_layer_outcomes(
        self,
    ) -> tuple[tuple[int, Decimal, Decimal], ...] | None:
        """Return the target and stop P&L for each complete draft layer."""
        basis = self._state.unit_basis
        multiplier = self._state.multiplier
        if basis is None or multiplier is None:
            return None
        outcomes: list[tuple[int, Decimal, Decimal]] = []
        try:
            for layer in self._draft_layer_forms():
                quantity = int(layer.quantity.strip())
                target_price = Decimal(layer.target_price)
                stop_price = Decimal(layer.stop_price)
                if (
                    quantity <= 0
                    or not target_price.is_finite()
                    or not stop_price.is_finite()
                    or target_price <= 0
                    or stop_price <= 0
                ):
                    return None
                outcomes.append(
                    (
                        quantity,
                        (target_price - basis) * multiplier * quantity,
                        (stop_price - basis) * multiplier * quantity,
                    )
                )
        except (InvalidOperation, ValueError):
            return None
        return tuple(outcomes)

    def _update_layer_outcomes(self) -> None:
        outcomes = self._draft_layer_outcomes()
        if outcomes is None:
            for widgets in self._layer_widgets:
                widgets.target_outcome.setText("—")
                widgets.stop_outcome.setText("—")
            return
        for widgets, (_, target_pnl, stop_pnl) in zip(
            self._layer_widgets, outcomes, strict=True
        ):
            widgets.target_outcome.setText(_signed_money_text(target_pnl))
            widgets.stop_outcome.setText(_signed_money_text(stop_pnl))

    def _render_draft_layers(self) -> None:
        forms = (
            self._draft_by_con_id.get(self._selected_con_id, ())
            if self._selected_con_id is not None
            else ()
        )
        self._updating = True
        try:
            _clear_layout(self.layer_rows_layout)
            self._layer_widgets = []
            for index, layer in enumerate(forms, start=1):
                row = QFrame()
                row.setObjectName("layerRow")
                row_layout = QGridLayout(row)
                row_layout.setContentsMargins(16, 10, 16, 10)
                row_layout.setHorizontalSpacing(10)
                row_layout.setVerticalSpacing(4)
                tag = QLabel(f"Layer {index}")
                tag.setObjectName("layerTag")
                row_layout.addWidget(tag, 0, 0)
                group = QLabel(f"OCA-{index}")
                group.setObjectName("layerGroup")
                row_layout.addWidget(group, 1, 0)
                target_percentage = _percentage_spin_box(
                    layer.target_percentage,
                    maximum=1000,
                )
                target_percentage.setObjectName("layerTargetPercentageInput")
                target_percentage.setAccessibleName(f"Layer {index} limit target percentage")
                stop_percentage = _percentage_spin_box(
                    layer.stop_percentage or self._stop_percentage_text(layer),
                    maximum=100,
                )
                stop_percentage.setObjectName("layerStopPercentageInput")
                stop_percentage.setAccessibleName(f"Layer {index} stop loss percentage")
                target_price = QLabel(_price_text_from_raw(layer.target_price))
                target_price.setObjectName("layerTargetPrice")
                target_price.setAccessibleName(f"Layer {index} calculated limit target price")
                stop_price = QLabel(_price_text_from_raw(layer.stop_price))
                stop_price.setObjectName("layerStopPrice")
                stop_price.setAccessibleName(f"Layer {index} calculated stop price")
                target_outcome = QLabel("—")
                target_outcome.setObjectName("layerTargetOutcome")
                target_outcome.setAccessibleName(
                    f"Layer {index} dollar outcome if its limit target fills"
                )
                stop_outcome = QLabel("—")
                stop_outcome.setObjectName("layerStopOutcome")
                stop_outcome.setAccessibleName(
                    f"Layer {index} dollar outcome if its stop loss fills"
                )
                quantity = QLineEdit(layer.quantity)
                quantity.setObjectName("layerQuantityInput")
                quantity.setAccessibleName(f"Layer {index} quantity")
                tif = QComboBox()
                tif.setObjectName("layerTifInput")
                tif.addItems(["GTC", "DAY"])
                tif.setCurrentText(layer.tif)
                remove = QPushButton()
                remove.setObjectName("removeLayerButton")
                remove.setProperty("secondary", True)
                remove.setIcon(
                    self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon)
                )
                remove.setIconSize(QSize(14, 14))
                remove.setFixedSize(32, 33)
                remove.setAccessibleName(f"Remove layer {index}")
                remove.setToolTip(remove.accessibleName())
                remove.setEnabled(len(forms) > 1)
                remove.clicked.connect(
                    lambda _checked=False, row_index=index - 1: self._remove_layer(
                        row_index
                    )
                )
                _add_percentage_field(
                    row_layout,
                    0,
                    1,
                    "LMT target",
                    target_percentage,
                    target_price,
                    target_outcome,
                    "target",
                    2,
                )
                _add_percentage_field(
                    row_layout,
                    0,
                    3,
                    "STP loss",
                    stop_percentage,
                    stop_price,
                    stop_outcome,
                    "stop",
                    2,
                )
                _add_compact_field(row_layout, 0, 5, "Qty", quantity, None, 2)
                _add_compact_field(row_layout, 0, 7, "TIF", tif, None, 2)
                row_layout.addWidget(remove, 1, 9, 1, 1, Qt.AlignmentFlag.AlignVCenter)
                for widget in (quantity,):
                    widget.textChanged.connect(self._draft_changed)
                target_percentage.valueChanged.connect(
                    lambda _value: self._reprice_draft_from_percentages()
                )
                stop_percentage.valueChanged.connect(
                    lambda _value: self._reprice_draft_from_percentages()
                )
                tif.currentIndexChanged.connect(self._draft_changed)
                self._layer_widgets.append(
                    _LayerWidgets(
                        target_percentage=target_percentage,
                        stop_percentage=stop_percentage,
                        target_price=target_price,
                        stop_price=stop_price,
                        target_outcome=target_outcome,
                        stop_outcome=stop_outcome,
                        quantity=quantity,
                        tif=tif,
                    )
                )
                row.setProperty("targetPercentage", layer.target_percentage)
                self.layer_rows_layout.addWidget(row)
        finally:
            self._updating = False
        self._update_draft_totals()

    def _draft_changed(self, *_: object) -> None:
        if self._updating or self._selected_con_id is None:
            return
        forms = []
        for widgets in self._layer_widgets:
            forms.append(
                DraftLayerForm(
                    quantity=widgets.quantity.text(),
                    target_price=_raw_price_text(widgets.target_price.text()),
                    stop_price=_raw_price_text(widgets.stop_price.text()),
                    target_percentage=widgets.target_percentage.text(),
                    tif=widgets.tif.currentText(),
                    runner=False,
                    stop_percentage=widgets.stop_percentage.text(),
                )
            )
        self._draft_by_con_id[self._selected_con_id] = tuple(forms)
        self._update_draft_totals()
        self._update_action_review()

    def _update_draft_totals(self) -> None:
        quantity = self._draft_quantity()
        available = self._state.available_quantity
        self.draft_quantity_label.setText(f"{quantity} / {available} contracts")
        self.review_quantity_label.setText(f"Draft quantity  {quantity} / {available}")
        invalid = quantity > available or quantity <= 0
        self.draft_quantity_label.setProperty("invalid", invalid)
        _repolish(self.draft_quantity_label)
        can_add_pair = (
            available > 0
            and len(self._layer_widgets) < available
            and self._layer_preset_values() is not None
        )
        self.add_layer_button.setEnabled(can_add_pair)
        self.equal_split_button.setEnabled(bool(self._layer_widgets))
        self._update_layer_outcomes()
        self._update_outcome_projection()

    def _equal_split_layers(self) -> None:
        forms = list(self._draft_layer_forms())
        if not forms or self._selected_con_id is None:
            return
        total = (
            self._state.available_quantity
            if self._equal_split_mode == "available"
            else self._draft_quantity()
        )
        split = _split_quantity(total, len(forms))
        self._draft_by_con_id[self._selected_con_id] = tuple(
            DraftLayerForm(
                quantity=str(split[index]),
                target_price=layer.target_price,
                stop_price=layer.stop_price,
                target_percentage=layer.target_percentage,
                tif=layer.tif,
                runner=False,
                stop_percentage=layer.stop_percentage,
            )
            for index, layer in enumerate(forms)
        )
        self._render_draft_layers()
        self._update_action_review()

    def _add_layer(self) -> None:
        if self._selected_con_id is None or self._state.available_quantity <= 0:
            return
        forms = list(self._draft_layer_forms())
        if not forms or len(forms) >= self._state.available_quantity:
            return
        percentages = self._layer_preset_for_index(len(forms))
        if percentages is None:
            self.bracket_help.setText(
                "Set valid comma-separated LMT targets and STP losses in Settings "
                "before adding a layer."
            )
            return
        target_percentage, stop_percentage = percentages
        if self._state.unit_basis is None or self._quote_calculator is None:
            return
        try:
            preview = preview_reference_prices(
                self._state.unit_basis,
                target_percentage,
                stop_percentage,
                self._quote_calculator.bands,
            )
        except ValueError:
            return
        # Adding a layer is the primary "cover this position" flow.  It must
        # split the verified available position, never a reduced remnant left
        # after an earlier row was removed.
        total = self._state.available_quantity
        forms.append(
            DraftLayerForm(
                quantity="0",
                target_price=format(preview.target_price, "f"),
                stop_price=format(preview.stop_price, "f"),
                target_percentage=format(target_percentage, "f"),
                runner=False,
                stop_percentage=format(stop_percentage, "f"),
            )
        )
        split = _split_quantity(total, len(forms))
        self._draft_by_con_id[self._selected_con_id] = tuple(
            DraftLayerForm(
                quantity=str(split[index]),
                target_price=layer.target_price,
                stop_price=layer.stop_price,
                target_percentage=layer.target_percentage,
                tif=layer.tif,
                runner=False,
                stop_percentage=layer.stop_percentage,
            )
            for index, layer in enumerate(forms)
        )
        self._render_draft_layers()
        self._update_action_review()

    def _set_equal_split_mode(self, mode: str) -> None:
        if mode not in {"available", "assigned"}:
            return
        self._equal_split_mode = mode
        self.equal_split_button.setText(
            "Equal split available contracts"
            if mode == "available"
            else "Equal split assigned contracts"
        )
        self.equal_split_button.setAccessibleName(self.equal_split_button.text())
        self.equal_split_button.setToolTip(
            "Redistribute every verified available contract across the draft"
            if mode == "available"
            else "Redistribute only the quantity already assigned to draft layers"
        )

    def _remove_layer(self, row_index: int) -> None:
        forms = list(self._draft_layer_forms())
        if self._selected_con_id is None or len(forms) <= 1:
            return
        del forms[row_index]
        self._draft_by_con_id[self._selected_con_id] = tuple(forms)
        self._render_draft_layers()
        self._update_action_review()

    def _stop_percentage_text(self, layer: DraftLayerForm) -> str:
        """Recover a legacy draft's stop percentage for the percentage-first UI."""
        basis = self._state.unit_basis
        if basis is None or basis <= 0:
            return "25"
        try:
            stop = Decimal(layer.stop_price)
        except InvalidOperation:
            return "25"
        percentage = (Decimal("1") - stop / basis) * Decimal("100")
        return format(max(Decimal("0"), percentage).quantize(Decimal("0.1")), "f")

    def _reprice_draft_from_percentages(self) -> None:
        if self._updating or self._selected_con_id is None:
            return
        if self._state.unit_basis is None or self._quote_calculator is None:
            return
        forms: list[DraftLayerForm] = []
        for layer in self._draft_layer_forms():
            try:
                target_percentage = Decimal(layer.target_percentage.strip())
                stop_percentage = Decimal(layer.stop_percentage.strip())
                preview = preview_reference_prices(
                    self._state.unit_basis,
                    target_percentage,
                    stop_percentage,
                    self._quote_calculator.bands,
                )
            except (InvalidOperation, ValueError):
                self.bracket_help.setText(
                    "Target must be above 0%; stop loss must be between 0% and 100%."
                )
                return
            forms.append(
                DraftLayerForm(
                    quantity=layer.quantity,
                    target_price=format(preview.target_price, "f"),
                    stop_price=format(preview.stop_price, "f"),
                    target_percentage=format(target_percentage, "f"),
                    tif=layer.tif,
                    runner=False,
                    stop_percentage=format(stop_percentage, "f"),
                )
            )
        self.bracket_help.setText(
            "Each row creates one equal-quantity SELL LMT + SELL STP pair."
        )
        self._draft_by_con_id[self._selected_con_id] = tuple(forms)
        for widgets, layer in zip(self._layer_widgets, forms, strict=True):
            widgets.target_price.setText(_price_text_from_raw(layer.target_price))
            widgets.stop_price.setText(_price_text_from_raw(layer.stop_price))
        self._update_outcome_projection()
        self._update_action_review()

    def _update_action_review(self) -> None:
        _clear_layout(self.review_actions_layout)
        forms = self._draft_layer_forms()
        if not forms:
            empty = QLabel("Select an eligible position to see its read-only action plan.")
            empty.setObjectName("reviewEmpty")
            empty.setWordWrap(True)
            self.review_actions_layout.addWidget(empty)
            self.review_actions_layout.addStretch()
            return
        action_index = 1
        symbol = self._state.position_title.split(maxsplit=1)[0]
        for index, layer in enumerate(forms, start=1):
            for order_type, price, tone in (
                ("LMT", layer.target_price, "target"),
                ("STP", layer.stop_price, "stop"),
            ):
                self.review_actions_layout.addWidget(
                    _review_timeline_item(
                        f"Action {action_index:02d}",
                        f"Create SELL {order_type} · {layer.quantity} {symbol}",
                        f"{_price_text_from_raw(price)} · {layer.tif}",
                        f"OCA-{index}",
                        tone,
                    )
                )
                action_index += 1
        check = _review_timeline_item(
            "Final safety check",
            "Refresh account, position, orders, tick rule and connection before future execution.",
            "",
            "",
            "warning",
        )
        self.review_actions_layout.addWidget(check)
        self.review_actions_layout.addStretch()


def _section_title(text: str, *, dark: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionTitleDark" if dark else "sectionTitleLight")
    return label


def _rule() -> QFrame:
    line = QFrame()
    line.setObjectName("rule")
    line.setFrameShape(QFrame.Shape.HLine)
    return line


def _add_field(
    layout: QGridLayout,
    row: int,
    label: str,
    widget: QWidget,
    *,
    suffix: str | None = None,
) -> None:
    field_label = QLabel(label)
    field_label.setObjectName("fieldLabel")
    field_label.setBuddy(widget)
    layout.addWidget(field_label, row, 0)
    layout.addWidget(widget, row, 1)
    if suffix is not None:
        suffix_label = QLabel(suffix)
        suffix_label.setObjectName("fieldSuffix")
        layout.addWidget(suffix_label, row, 2)


def _add_compact_field(
    layout: QGridLayout,
    row: int,
    column: int,
    label: str,
    widget: QWidget,
    prefix: str | None,
    span: int,
) -> None:
    field_label = QLabel(label)
    field_label.setObjectName("compactFieldLabel")
    layout.addWidget(field_label, row, column, 1, span)
    if prefix is None:
        layout.addWidget(widget, row + 1, column, 1, span)
        return
    wrapped = QWidget()
    wrapped_layout = QHBoxLayout(wrapped)
    wrapped_layout.setContentsMargins(0, 0, 0, 0)
    wrapped_layout.setSpacing(4)
    symbol = QLabel(prefix)
    symbol.setObjectName("pricePrefix")
    wrapped_layout.addWidget(symbol)
    wrapped_layout.addWidget(widget, 1)
    layout.addWidget(wrapped, row + 1, column, 1, span)


def _add_percentage_field(
    layout: QGridLayout,
    row: int,
    column: int,
    label: str,
    percentage: QDoubleSpinBox,
    price: QLabel,
    outcome: QLabel,
    tone: str,
    span: int,
) -> None:
    """Render an editable percentage with its derived dollar price in one field."""
    field_label = QLabel(label)
    field_label.setObjectName("compactFieldLabel")
    layout.addWidget(field_label, row, column, 1, span)
    surface = QFrame()
    surface.setObjectName("percentagePriceField")
    surface.setProperty("tone", tone)
    surface_layout = QHBoxLayout(surface)
    surface_layout.setContentsMargins(7, 0, 7, 0)
    surface_layout.setSpacing(3)
    surface_layout.addWidget(percentage, 1)
    suffix = QLabel("%")
    suffix.setObjectName("percentageSuffix")
    surface_layout.addWidget(suffix)
    surface_layout.addStretch()
    price.setObjectName("layerTargetPrice" if tone == "target" else "layerStopPrice")
    price.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    surface_layout.addWidget(price)
    layout.addWidget(surface, row + 1, column, 1, span)
    outcome.setObjectName(
        "layerTargetOutcome" if tone == "target" else "layerStopOutcome"
    )
    outcome.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    layout.addWidget(outcome, row + 2, column, 1, span)


def _percentage_spin_box(value: str, *, maximum: float) -> _PercentageSpinBox:
    """Create a bounded percentage control with arrow-key increments."""
    field = _PercentageSpinBox()
    field.setDecimals(1)
    field.setRange(0.1, maximum)
    field.setSingleStep(1.0)
    field.setAccelerated(True)
    try:
        initial_value = float(Decimal(value))
    except (InvalidOperation, OverflowError, ValueError):
        initial_value = field.minimum()
    field.setValue(max(field.minimum(), min(initial_value, field.maximum())))
    return field


def _review_timeline_item(
    title: str,
    action: str,
    detail: str,
    group: str,
    tone: str,
) -> QFrame:
    item = QFrame()
    item.setObjectName("reviewTimelineItem")
    item.setProperty("tone", tone)
    layout = QHBoxLayout(item)
    layout.setContentsMargins(0, 0, 0, 14)
    layout.setSpacing(9)
    rail = QFrame()
    rail.setObjectName("reviewTimelineRail")
    rail.setProperty("tone", tone)
    rail.setFixedWidth(8)
    dot = QFrame(rail)
    dot.setObjectName("reviewTimelineDot")
    dot.setProperty("tone", tone)
    dot.setGeometry(0, 2, 7, 7)
    layout.addWidget(rail, 0)
    content = QVBoxLayout()
    content.setContentsMargins(0, 0, 0, 0)
    content.setSpacing(3)
    title_label = QLabel(title)
    title_label.setObjectName("reviewActionEyebrow")
    title_label.setProperty("tone", tone)
    content.addWidget(title_label)
    action_label = QLabel(action)
    action_label.setObjectName("reviewActionTitle")
    action_label.setWordWrap(True)
    content.addWidget(action_label)
    if detail:
        detail_label = QLabel(detail)
        detail_label.setObjectName("reviewActionDetail")
        detail_label.setProperty("tone", tone)
        content.addWidget(detail_label)
    if group:
        group_label = QLabel(group)
        group_label.setObjectName("reviewActionGroup")
        content.addWidget(group_label)
    layout.addLayout(content, 1)
    return item


def _split_quantity(total: int, count: int) -> tuple[int, ...]:
    if total <= 0 or count <= 0:
        return ()
    each, remainder = divmod(total, count)
    return tuple(each + int(index < remainder) for index in range(count))


def _parse_percentage_presets(
    raw: str, *, maximum: Decimal
) -> tuple[Decimal, ...] | None:
    """Parse comma-separated defaults without accepting an unsafe fallback."""
    values: list[Decimal] = []
    for value_text in raw.split(","):
        try:
            value = Decimal(value_text.strip())
        except InvalidOperation:
            return None
        if not value.is_finite() or value <= 0 or value > maximum:
            return None
        values.append(value)
    return tuple(values) if values else None


def _selected_reference_price(
    quote: QuoteCalculatorLine,
    mode: str,
    manual_value: str,
) -> Decimal | None:
    if mode == "BID":
        return quote.bid
    if mode == "ASK":
        return quote.ask
    if mode == "LAST":
        return quote.last
    if mode == "MANUAL":
        try:
            value = Decimal(manual_value.strip())
        except InvalidOperation:
            return None
        return value if value.is_finite() and value > 0 else None
    if quote.bid is None or quote.ask is None:
        return None
    return (quote.bid + quote.ask) / Decimal("2")


def _price_text(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"${format(value.normalize(), 'f')}"


def _price_text_from_raw(value: str) -> str:
    try:
        price = Decimal(value.strip())
    except InvalidOperation:
        return "$—"
    return _price_text(price) if price.is_finite() and price > 0 else "$—"


def _raw_price_text(value: str) -> str:
    return value.strip().removeprefix("$")


def _money_text(value: Decimal) -> str:
    return f"${format(value.quantize(Decimal('0.01')), 'f')}"


def _signed_money_text(value: Decimal) -> str:
    if value > 0:
        return f"+{_money_text(value)}"
    if value < 0:
        return f"-{_money_text(abs(value))}"
    return "$0.00"


def _return_percentage_text(pnl: Decimal, cost: Decimal) -> str:
    if cost <= 0:
        return "—"
    return f"{pnl / cost * Decimal('100'):+.1f}%"


def _modeled_breakeven(
    outcomes: tuple[tuple[int, Decimal, Decimal], ...],
) -> tuple[int, Decimal] | None:
    """Find the first target sequence that covers every remaining modeled stop."""
    remaining_stops = sum((stop for _, _, stop in outcomes), Decimal("0"))
    secured_outcome = Decimal("0")
    for index, (_, target, stop) in enumerate(outcomes[:-1], start=1):
        secured_outcome += target
        remaining_stops -= stop
        floor = secured_outcome + remaining_stops
        if floor >= 0:
            return index, floor
    return None


def _populate_facts(
    layout: QGridLayout, facts: tuple[Fact, ...], *, dark: bool
) -> None:
    _clear_layout(layout)
    for row, fact in enumerate(facts):
        name = QLabel(fact.label)
        name.setObjectName("factNameDark" if dark else "factNameLight")
        value = QLabel(fact.value)
        value.setObjectName("factValueDark" if dark else "factValueLight")
        value.setProperty("state", fact.state.value.lower())
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(name, row, 0)
        layout.addWidget(value, row, 1, Qt.AlignmentFlag.AlignRight)


def _clear_layout(layout: QVBoxLayout | QGridLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue
        widget = item.widget()
        child_layout = item.layout()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        elif child_layout is not None:
            _clear_layout(child_layout)  # type: ignore[arg-type]


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


_STYLE = """
QWidget#root { background: #071a2b; color: #eaf1f2; }
QFrame#safetyBanner { background: #d34174; border: 0; }
QLabel#safetyLabel { color: white; font: 700 14px "Avenir Next"; letter-spacing: 1px; }
QLabel#safetyScope { color: #ffeaf1; font: 12px "Avenir Next"; }
QFrame#header { background: #071a2b; border-bottom: 1px solid #244355; }
QLabel#statusLabel { min-width: 280px; padding: 8px 12px; border: 1px solid #466675; color: #b9ccd2; font: 700 11px "SF Mono"; letter-spacing: 0.5px; }
QLabel#statusLabel[state="ready"] { background: #0d3c46; border-color: #7fc8c8; color: #d6ffff; }
QLabel#statusLabel[state="blocked"], QLabel#statusLabel[state="stale"] { background: #49283a; border-color: #d34174; color: #ffeaf1; }
QLabel#statusLabel[state="loading"] { background: #173b50; border-color: #7fc8c8; color: #e5ffff; }
QLabel#accountLabel { color: #c5d9de; padding-left: 16px; font: 12px "SF Mono"; }
QToolButton#settingsButton { background: transparent; color: #cfe0e4; border: 1px solid #557786; padding: 10px 12px; font: 700 12px "Avenir Next"; }
QToolButton#settingsButton:hover, QToolButton#settingsButton:checked { background: #16394b; border-color: #7fc8c8; }
QScrollArea#workspaceScroll, QWidget#workspacePanel { background: #eaf1f2; color: #071a2b; border: 0; }
QFrame#connectionSettings { background: #0a2233; border: 1px solid #315062; padding: 12px; }
QFrame#positionSummary { background: #173447; border: 1px solid #315062; }
QLabel#positionOverview { color: #c9dde0; font: 12px "Avenir Next"; }
QFrame#actionExtendedView { background: #f3f7f7; border: 1px solid #b7cdd1; }
QFrame#detailsPanel { border-top: 1px solid #b7cdd1; }
QLabel#sectionTitleDark { color: #d9e8eb; font: 700 12px "Avenir Next"; letter-spacing: 0.8px; margin-top: 6px; }
QLabel#sectionTitleLight { color: #173447; font: 700 12px "Avenir Next"; letter-spacing: 0.8px; margin-top: 6px; }
QLabel#fieldLabel, QLabel#factNameDark { color: #8fa8b4; font: 12px "Avenir Next"; }
QLabel#fieldSuffix { color: #8fa8b4; }
QLabel#factValueDark { color: #e3eef0; font: 12px "SF Mono"; }
QLabel#factValueDark[state="pass"] { color: #a8e0df; }
QLabel#factValueDark[state="blocked"] { color: #ff9dba; }
QLabel#factNameLight { color: #627985; font: 12px "Avenir Next"; }
QLabel#factValueLight { color: #173447; font: 12px "SF Mono"; }
QLabel#positionTitle { color: #ffffff; font: 600 16px "SF Mono"; padding: 3px 0 7px 0; }
QFrame#rule { color: #244355; margin: 5px 0; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #102d40; color: #f1f7f8; border: 1px solid #365a6c; padding: 7px 8px; min-height: 20px; selection-background-color: #d34174; selection-color: white; font: 12px "Avenir Next"; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border: 2px solid #7fc8c8; padding: 6px 7px; }
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled { color: #78909b; background: #0d2637; }
QComboBox QAbstractItemView { background: #102d40; color: white; selection-background-color: #245d70; }
QPushButton { background: #7fc8c8; color: #071a2b; border: 1px solid #9edcdc; padding: 10px 14px; min-height: 20px; font: 700 12px "Avenir Next"; }
QPushButton:hover { background: #a0dedd; }
QPushButton:pressed { background: #63aeb0; }
QPushButton:focus { border: 2px solid white; padding: 9px 13px; }
QPushButton[actionTab="true"]:checked { background: #173447; color: #eaf1f2; border-color: #173447; }
QPushButton[secondary="true"] { background: transparent; color: #173447; border-color: #557786; }
QPushButton[secondary="true"]:hover, QPushButton[secondary="true"]:checked { background: #d3e8e8; border-color: #177e89; }
QPushButton:disabled { background: #173447; color: #627d88; border-color: #294b5c; }
QLabel#routeTitle { color: #071a2b; font: 600 20px "Avenir Next"; }
QLabel#ageLabel { color: #536b78; font: 11px "SF Mono"; }
QWidget#priceRoute { border: 1px solid #b7cdd1; background: #eaf1f2; }
QLabel#allocationValue { color: #173447; border-top: 1px solid #9fb8bd; padding: 8px 20px 2px 0; font: 700 12px "SF Mono"; }
QTableWidget#planTable { background: #f3f7f7; alternate-background-color: #e4edef; color: #102b3a; border: 1px solid #b7cdd1; gridline-color: #cbdadc; selection-background-color: #c3e4e3; selection-color: #071a2b; font: 11px "SF Mono"; }
QTableWidget#planTable::item { padding: 6px; }
QTableWidget#positionsTable, QTableWidget#workingOrdersTable { background: #ffffff; color: #102b3a; border: 1px solid #b7cdd1; gridline-color: #cbdadc; selection-background-color: #2e8dc0; selection-color: white; font: 11px "SF Mono"; }
QTableWidget#positionsTable::item, QTableWidget#workingOrdersTable::item { padding: 7px; }
QHeaderView::section { background: #173447; color: #eaf1f2; border: 0; border-right: 1px solid #315568; padding: 7px; font: 700 11px "Avenir Next"; }
QLabel#marketRule { color: #536b78; font: 11px "SF Mono"; padding-top: 7px; }
QLabel#validationLine { color: #173447; border-top: 1px solid #b7cdd1; padding: 7px 0; font: 11px "Avenir Next"; }
QLabel#validationLine[state="blocked"] { color: #8e2449; }
QLabel#validationLine[state="pass"] { color: #17676a; }
QFrame#riskStrip { background: #061622; border-top: 1px solid #284657; }
QLabel#riskTitle { color: #f2c14e; font: 700 11px "SF Mono"; padding-right: 16px; }
QFrame#riskStrip QLabel { color: #b7c9cf; font: 11px "Avenir Next"; }
QScrollBar:vertical { width: 10px; background: #dce8ea; }
QScrollBar::handle:vertical { background: #769aa5; min-height: 30px; }
QToolTip { background: #071a2b; color: #f4f8f8; border: 1px solid #7fc8c8; padding: 5px; }
"""

_STYLE += """
QWidget#root, QSplitter#workspace { background: #080b09; color: #e6ebe4; }
QFrame#header { background: #0c100d; border-bottom: 1px solid #222a23; }
QLabel#connectionDot { min-width: 8px; max-width: 8px; min-height: 8px; max-height: 8px; border-radius: 4px; background: #3ec765; }
QLabel#connectionDot[state="blocked"] { background: #d97a55; }
QLabel#statusLabel { min-width: 0; padding: 0; border: 0; background: transparent; color: #d8dfd7; font: 700 11px "SF Mono"; }
QLabel#statusLabel[state="ready"] { background: transparent; border: 0; color: #e6ebe4; }
QLabel#statusLabel[state="blocked"], QLabel#statusLabel[state="stale"] { background: transparent; border: 0; color: #d9a055; }
QLabel#statusLabel[state="loading"] { background: transparent; border: 0; color: #aab6aa; }
QLabel#demoIndicator { color: #d6a949; background: #211b0b; border: 1px solid #57471b; border-radius: 2px; padding: 3px 6px; font: 700 9px "SF Mono"; }
QLabel#accountLabel, QLabel#verifiedLabel { color: #899589; padding-left: 16px; font: 10px "SF Mono"; }
QLabel#verifiedLabel { padding-left: 0; }
QPushButton#refreshButton, QToolButton#settingsButton { background: #151b16; color: #d8dfd7; border: 1px solid #2a342b; border-radius: 3px; padding: 7px 10px; min-height: 0; font: 700 11px "Avenir Next"; }
QPushButton#refreshButton:hover, QToolButton#settingsButton:hover, QToolButton#settingsButton:checked { background: #202a21; border-color: #405142; }
QSplitter::handle { background: #222a23; width: 1px; }
QFrame#inventoryPane, QFrame#reviewPane { background: #0c100d; border: 0; }
QScrollArea#positionsScroll, QScrollArea#workspaceScroll, QScrollArea#reviewScroll { background: #080b09; border: 0; }
QWidget#positionsPanel, QWidget#workspacePanel, QWidget#reviewContent { background: #0c100d; }
QLabel#sectionTitleDark { color: #8d9a8c; margin: 0; font: 700 10px "Avenir Next"; letter-spacing: 0.7px; text-transform: uppercase; }
QLabel#positionCount { color: #50cf70; background: #12351a; border-radius: 2px; padding: 3px 6px; font: 700 9px "SF Mono"; }
QPushButton#positionButton { background: transparent; color: #dfe6de; border: 0; border-left: 2px solid transparent; border-bottom: 1px solid #202720; border-radius: 0; padding: 14px 12px 14px 12px; min-height: 72px; text-align: left; font: 700 11px "SF Mono"; }
QPushButton#positionButton:hover { background: #151d16; border-left-color: #304234; }
QPushButton#positionButton[selected="true"] { background: #112417; border-left-color: #3ec765; color: #f2f7f0; }
QPushButton#positionButton[selected="true"]:hover { background: #15301c; border-left-color: #59d477; }
QPushButton#positionButton[eligible="false"] { color: #7c867c; }
QFrame#connectionSettings { background: #101610; border-top: 1px solid #273027; padding: 0; }
QLabel#fieldLabel, QLabel#compactFieldLabel { color: #758173; font: 700 9px "Avenir Next"; text-transform: uppercase; letter-spacing: 0.55px; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #080b09; color: #e6ebe4; border: 1px solid #2b362d; border-radius: 3px; padding: 6px 7px; min-height: 19px; selection-background-color: #3ec765; selection-color: #061008; font: 11px "SF Mono"; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border: 1px solid #59d477; padding: 6px 7px; }
QLineEdit[presetInvalid="true"] { border-color: #ef6b62; }
QComboBox QAbstractItemView { background: #101610; color: #e6ebe4; selection-background-color: #1f3d25; }
QLabel#positionTitle { color: #f0f4ef; padding: 0; font: 700 22px "Avenir Next"; letter-spacing: -0.4px; }
QLabel#positionOverview { color: #aeb9ad; font: 11px "SF Mono"; }
QLabel#availableLabel { color: #54cf73; font: 700 11px "Avenir Next"; }
QLabel#externalNotice { color: #d6a949; background: #211b0b; border: 1px solid #57471b; border-radius: 3px; padding: 10px 12px; font: 11px "Avenir Next"; }
QFrame#draftPanel, QFrame#outcomeProjection { background: #101610; border: 1px solid #273127; border-radius: 4px; }
QLabel#panelTitle { color: #e9efea; font: 700 14px "Avenir Next"; }
QLabel#panelHelp, QLabel#outcomeSummary { color: #7f8b80; font: 10px "Avenir Next"; }
QLabel#compactFieldLabel { min-width: 0; }
QPushButton[secondary="true"], QToolButton#equalSplitButton { background: #151b16; color: #cbd4ca; border: 1px solid #2a352b; border-radius: 3px; padding: 7px 10px; min-height: 0; font: 700 10px "Avenir Next"; }
QPushButton[secondary="true"]:hover, QToolButton#equalSplitButton:hover { background: #202a21; border-color: #4f6251; color: #f0f6ef; }
QPushButton[secondary="true"]:disabled, QToolButton#equalSplitButton:disabled { background: #101410; color: #596359; border-color: #202820; }
QPushButton#removeLayerButton { padding: 0; min-width: 32px; max-width: 32px; min-height: 33px; max-height: 33px; }
QToolButton#equalSplitButton::menu-button { border-left: 1px solid #2a352b; width: 18px; }
QFrame#layerRow { border-top: 1px solid #242e25; background: #0e130f; }
QLabel#layerTag { color: #cdd6cc; background: #202920; border-radius: 2px; padding: 3px 5px; font: 700 9px "Avenir Next"; text-transform: uppercase; }
QLabel#layerGroup { color: #657165; font: 9px "SF Mono"; }
QLabel#pricePrefix { color: #56d274; font: 700 12px "SF Mono"; }
QLineEdit#layerStopInput + QWidget QLabel#pricePrefix { color: #ef6b62; }
QFrame#percentagePriceField { background: #080b09; border: 1px solid #2b362d; border-radius: 3px; min-height: 31px; }
QFrame#percentagePriceField:focus-within { border-color: #59d477; }
QFrame#percentagePriceField[tone="stop"]:focus-within { border-color: #ef6b62; }
QFrame#percentagePriceField QDoubleSpinBox, QFrame#percentagePriceField QDoubleSpinBox QLineEdit { background: transparent; border: 0; border-radius: 0; padding: 0; min-height: 0; color: #e8f0e7; font: 700 11px "SF Mono"; }
QFrame#percentagePriceField QDoubleSpinBox:focus, QFrame#percentagePriceField QDoubleSpinBox QLineEdit:focus { border: 0; padding: 0; }
QFrame#percentagePriceField QDoubleSpinBox::up-button, QFrame#percentagePriceField QDoubleSpinBox::down-button { width: 12px; border: 0; background: transparent; }
QLabel#percentageSuffix { color: #7f8b80; font: 10px "SF Mono"; }
QLabel#layerTargetPrice, QLabel#layerStopPrice { color: #5bd578; min-width: 60px; max-width: 60px; font: 700 11px "SF Mono"; padding-left: 4px; }
QLabel#layerStopPrice { color: #ef6b62; }
QLabel#layerTargetOutcome, QLabel#layerStopOutcome { color: #748073; padding-top: 1px; font: 9px "SF Mono"; }
QLabel#layerTargetOutcome { color: #58c974; }
QLabel#layerStopOutcome { color: #db766d; }
QLabel#draftQuantity, QLabel#reviewQuantity { color: #d7dfd7; font: 700 10px "SF Mono"; }
QLabel#draftQuantity[invalid="true"] { color: #ef6b62; }
QLabel#outcomeMetricLabel { color: #8b9789; font: 700 9px "Avenir Next"; text-transform: uppercase; letter-spacing: 0.55px; }
QLabel#outcomeGain { color: #5bd578; font: 700 19px "SF Mono"; }
QLabel#outcomeLoss { color: #ef6b62; font: 700 19px "SF Mono"; }
QLabel#outcomeBreakeven { color: #d6a949; font: 700 19px "SF Mono"; }
QLabel#reviewTitle { color: #8f9a90; font: 700 10px "Avenir Next"; text-transform: uppercase; letter-spacing: 0.7px; }
QLabel#reviewStatus { color: #778277; font: 9px "SF Mono"; text-transform: uppercase; }
QLabel#previewNotice { margin: 0 14px 12px 14px; color: #78d88c; background: #102517; border: 1px solid #285d35; border-radius: 3px; padding: 10px; font: 10px "Avenir Next"; }
QFrame#reviewTimelineItem { background: transparent; border: 0; }
QFrame#reviewTimelineRail { background: transparent; border-left: 1px solid #2c352d; }
QFrame#reviewTimelineDot { background: #57d477; border: 2px solid #0c100d; border-radius: 4px; }
QFrame#reviewTimelineDot[tone="stop"] { background: #ef6b62; }
QFrame#reviewTimelineDot[tone="warning"] { background: #d6a949; }
QLabel#reviewActionEyebrow { color: #839083; font: 700 9px "Avenir Next"; text-transform: uppercase; letter-spacing: 0.65px; }
QLabel#reviewActionEyebrow[tone="warning"] { color: #d6a949; }
QLabel#reviewActionTitle { color: #e1e8e0; font: 700 10px "SF Mono"; }
QLabel#reviewActionDetail { color: #57d477; font: 700 10px "SF Mono"; }
QLabel#reviewActionDetail[tone="stop"] { color: #ef6b62; }
QLabel#reviewActionDetail[tone="warning"] { color: #d6a949; }
QLabel#reviewActionGroup { color: #667267; font: 9px "SF Mono"; }
QLabel#reviewEmpty { color: #788378; font: 11px "Avenir Next"; }
QPushButton#transmissionLocked:disabled { background: #15331a; color: #63ca78; border: 1px solid #2c6439; border-radius: 3px; padding: 9px; font: 700 11px "Avenir Next"; }
QScrollBar:vertical { width: 8px; background: #090d0a; }
QScrollBar::handle:vertical { background: #334036; min-height: 30px; border-radius: 4px; }
QToolTip { background: #101610; color: #e8eee7; border: 1px solid #4c5c4e; padding: 5px; }
"""


__all__ = ["PlannerWindow", "PriceRouteWidget"]
