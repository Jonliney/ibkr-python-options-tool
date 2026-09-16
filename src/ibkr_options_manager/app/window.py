from __future__ import annotations

# ruff: noqa: E501
import os
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Protocol

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QObject,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QRunnable,
    Qt,
    QThreadPool,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
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
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..domain import RemainderPolicy, TriggerMethod
from .view_model import (
    ConnectionSelection,
    Fact,
    PlanForm,
    PlannerViewModel,
    RouteMark,
    UiStatus,
    ViewState,
)


class _ThreadPool(Protocol):
    def start(self, runnable: QRunnable) -> None: ...


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
            mark.label[1:]: mark for mark in self._marks if mark.kind == "TARGET"
        }
        stops = {mark.label[1:]: mark for mark in self._marks if mark.kind == "STOP"}
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
    ) -> None:
        super().__init__()
        self._view_model = view_model
        self._thread_pool = thread_pool or QThreadPool.globalInstance()
        self._state = view_model.empty()
        self._route_animation: QPropertyAnimation | None = None
        self.setWindowTitle("IBKR Options Manager — Read-only preview")
        self.resize(1380, 900)
        self.setMinimumSize(1120, 820)
        self._build_ui(initial_account, initial_con_id)
        self.setStyleSheet(_STYLE)
        self._apply_state(self._state)

    def _build_ui(self, initial_account: str, initial_con_id: int | None) -> None:
        root = QWidget()
        root.setObjectName("root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        safety = QFrame()
        safety.setObjectName("safetyBanner")
        safety_layout = QHBoxLayout(safety)
        safety_layout.setContentsMargins(24, 11, 24, 11)
        safety_label = QLabel("READ-ONLY PREVIEW — ORDERS CANNOT BE SENT")
        safety_label.setObjectName("safetyLabel")
        safety_label.setAccessibleName("Permanent read-only safety notice")
        safety_layout.addWidget(safety_label)
        safety_layout.addStretch()
        safety_scope = QLabel("Paper TWS · localhost · no trading controls")
        safety_scope.setObjectName("safetyScope")
        safety_layout.addWidget(safety_scope)
        root_layout.addWidget(safety)

        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(24, 16, 24, 16)
        title_stack = QVBoxLayout()
        title_stack.setSpacing(2)
        title = QLabel("Option exit passage plan")
        title.setObjectName("appTitle")
        subtitle = QLabel("Observe the position. Plot the route. Inspect every gate.")
        subtitle.setObjectName("appSubtitle")
        title_stack.addWidget(title)
        title_stack.addWidget(subtitle)
        header_layout.addLayout(title_stack)
        header_layout.addStretch()
        self.status_label = QLabel()
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAccessibleName("Planner status")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(self.status_label)
        root_layout.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("workspace")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_left_panel(initial_account, initial_con_id))
        splitter.addWidget(self._build_route_panel())
        splitter.setSizes([420, 960])
        root_layout.addWidget(splitter, 1)

        risk = QFrame()
        risk.setObjectName("riskStrip")
        risk_layout = QHBoxLayout(risk)
        risk_layout.setContentsMargins(24, 10, 24, 10)
        risk_title = QLabel("EXECUTION RISK")
        risk_title.setObjectName("riskTitle")
        risk_copy = QLabel(
            "Stops can fill below their trigger. Paper behavior does not prove live execution."
        )
        risk_copy.setWordWrap(True)
        risk_layout.addWidget(risk_title)
        risk_layout.addWidget(risk_copy, 1)
        root_layout.addWidget(risk)
        self.setCentralWidget(root)

    def _build_left_panel(
        self, initial_account: str, initial_con_id: int | None
    ) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("leftScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(380)
        scroll.setMaximumWidth(480)
        panel = QWidget()
        panel.setObjectName("leftPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 22, 24, 28)
        layout.setSpacing(14)

        layout.addWidget(_section_title("Paper TWS connection"))
        connection_grid = QGridLayout()
        connection_grid.setHorizontalSpacing(12)
        connection_grid.setVerticalSpacing(10)
        self.account_input = QLineEdit(initial_account)
        self.account_input.setObjectName("accountInput")
        self.account_input.setPlaceholderText("Full paper account ID")
        self.account_input.setClearButtonEnabled(True)
        self.con_id_input = QSpinBox()
        self.con_id_input.setObjectName("conIdInput")
        self.con_id_input.setRange(0, 2_147_483_647)
        self.con_id_input.setSpecialValueText("Required")
        self.con_id_input.setGroupSeparatorShown(True)
        if initial_con_id is not None:
            self.con_id_input.setValue(initial_con_id)
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
        _add_field(connection_grid, 0, "Paper account", self.account_input)
        _add_field(connection_grid, 1, "Option conId", self.con_id_input)
        _add_field(connection_grid, 2, "Paper port", self.port_input)
        _add_field(connection_grid, 3, "Client ID", self.client_id_input)
        _add_field(connection_grid, 4, "Timeout", self.timeout_input)
        layout.addLayout(connection_grid)
        self.refresh_button = QPushButton("Refresh paper snapshot")
        self.refresh_button.setObjectName("refreshButton")
        self.refresh_button.clicked.connect(self._start_refresh)
        layout.addWidget(self.refresh_button)

        layout.addWidget(_rule())
        layout.addWidget(_section_title("Connection evidence"))
        self.connection_facts = QGridLayout()
        self.connection_facts.setVerticalSpacing(7)
        layout.addLayout(self.connection_facts)

        layout.addWidget(_rule())
        layout.addWidget(_section_title("Verified position"))
        self.position_title = QLabel("No verified position")
        self.position_title.setObjectName("positionTitle")
        self.position_title.setWordWrap(True)
        layout.addWidget(self.position_title)
        self.position_facts = QGridLayout()
        self.position_facts.setVerticalSpacing(7)
        layout.addLayout(self.position_facts)

        layout.addWidget(_rule())
        layout.addWidget(_section_title("Plan request"))
        plan_grid = QGridLayout()
        plan_grid.setHorizontalSpacing(12)
        plan_grid.setVerticalSpacing(10)
        self.tranche_input = QLineEdit("2")
        self.tranche_input.setObjectName("trancheInput")
        self.target_input = QLineEdit("20, 40, 60, 80, 100")
        self.target_input.setObjectName("targetInput")
        self.stop_input = QLineEdit("20")
        self.stop_input.setObjectName("stopInput")
        self.remainder_input = QComboBox()
        self.remainder_input.setObjectName("remainderInput")
        self.remainder_input.addItem(
            "Remainder becomes next rung", RemainderPolicy.NEXT_RUNG
        )
        self.remainder_input.addItem(
            "Add remainder to final rung", RemainderPolicy.ADD_TO_LAST
        )
        self.tif_input = QComboBox()
        self.tif_input.setObjectName("tifInput")
        self.tif_input.addItems(["GTC", "DAY"])
        self.trigger_input = QComboBox()
        self.trigger_input.setObjectName("triggerInput")
        for method in TriggerMethod:
            if method is TriggerMethod.DEFAULT:
                continue
            self.trigger_input.addItem(method.value.replace("_", " ").title(), method)
        trigger_index = self.trigger_input.findData(TriggerMethod.DOUBLE_BID_ASK)
        self.trigger_input.setCurrentIndex(trigger_index)
        _add_field(plan_grid, 0, "Tranche size", self.tranche_input)
        _add_field(plan_grid, 1, "Target percentages", self.target_input)
        _add_field(plan_grid, 2, "Stop loss", self.stop_input, suffix="%")
        _add_field(plan_grid, 3, "Remainder", self.remainder_input)
        _add_field(plan_grid, 4, "Time in force", self.tif_input)
        _add_field(plan_grid, 5, "Stop trigger", self.trigger_input)
        layout.addLayout(plan_grid)
        self.preview_button = QPushButton("Rebuild preview")
        self.preview_button.setObjectName("previewButton")
        self.preview_button.setProperty("secondary", True)
        self.preview_button.clicked.connect(self._preview)
        layout.addWidget(self.preview_button)
        layout.addStretch()
        scroll.setWidget(panel)

        for widget in (
            self.account_input,
            self.con_id_input,
            self.port_input,
            self.client_id_input,
            self.timeout_input,
        ):
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._mark_connection_dirty)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.valueChanged.connect(self._mark_connection_dirty)

        QWidget.setTabOrder(self.account_input, self.con_id_input)
        QWidget.setTabOrder(self.con_id_input, self.port_input)
        QWidget.setTabOrder(self.port_input, self.client_id_input)
        QWidget.setTabOrder(self.client_id_input, self.timeout_input)
        QWidget.setTabOrder(self.timeout_input, self.refresh_button)
        QWidget.setTabOrder(self.refresh_button, self.tranche_input)
        QWidget.setTabOrder(self.tranche_input, self.target_input)
        QWidget.setTabOrder(self.target_input, self.stop_input)
        QWidget.setTabOrder(self.stop_input, self.remainder_input)
        QWidget.setTabOrder(self.remainder_input, self.tif_input)
        QWidget.setTabOrder(self.tif_input, self.trigger_input)
        QWidget.setTabOrder(self.trigger_input, self.preview_button)
        return scroll

    def _build_route_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("routePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(14)

        route_header = QHBoxLayout()
        route_title = QLabel("Observed state and proposed route")
        route_title.setObjectName("routeTitle")
        route_header.addWidget(route_title)
        route_header.addStretch()
        self.age_label = QLabel("Snapshot age —")
        self.age_label.setObjectName("ageLabel")
        route_header.addWidget(self.age_label)
        layout.addLayout(route_header)

        self.route_widget = PriceRouteWidget()
        self.route_widget.setObjectName("priceRoute")
        layout.addWidget(self.route_widget, 4)

        self.allocation_layout = QHBoxLayout()
        self.allocation_labels: list[QLabel] = []
        for _ in range(3):
            label = QLabel()
            label.setObjectName("allocationValue")
            self.allocation_labels.append(label)
            self.allocation_layout.addWidget(label)
        self.allocation_layout.addStretch()
        layout.addLayout(self.allocation_layout)

        self.plan_table = QTableWidget(0, 7)
        self.plan_table.setObjectName("planTable")
        self.plan_table.setHorizontalHeaderLabels(
            ["Pair", "Qty", "Target", "Stop", "TIF", "Trigger", "Logical OCA"]
        )
        self.plan_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.plan_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.plan_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.plan_table.verticalHeader().setVisible(False)
        self.plan_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.plan_table.horizontalHeader().setStretchLastSection(True)
        self.plan_table.setMinimumHeight(125)
        layout.addWidget(self.plan_table, 2)

        lower = QHBoxLayout()
        lower.setSpacing(28)
        quote_column = QVBoxLayout()
        quote_column.addWidget(_section_title("Quote and market rule", dark=False))
        self.quote_facts = QGridLayout()
        self.quote_facts.setVerticalSpacing(5)
        quote_column.addLayout(self.quote_facts)
        self.market_rule_label = QLabel("—")
        self.market_rule_label.setObjectName("marketRule")
        self.market_rule_label.setWordWrap(True)
        quote_column.addWidget(self.market_rule_label)
        quote_column.addStretch()
        lower.addLayout(quote_column, 1)

        validation_column = QVBoxLayout()
        validation_column.addWidget(_section_title("Validation register", dark=False))
        self.validation_layout = QVBoxLayout()
        self.validation_layout.setSpacing(6)
        validation_column.addLayout(self.validation_layout)
        validation_column.addStretch()
        lower.addLayout(validation_column, 2)
        layout.addLayout(lower, 2)
        return panel

    @Slot()
    def _start_refresh(self) -> None:
        selection = self._selection()
        form = self._form()
        self._set_busy(True)
        task = _RefreshTask(lambda: self._view_model.refresh(selection, form))
        task.signals.finished.connect(self._refresh_finished)
        self._thread_pool.start(task)

    @Slot(object)
    def _refresh_finished(self, state: object) -> None:
        self._set_busy(False)
        if isinstance(state, ViewState):
            self._apply_state(state)

    @Slot()
    def _preview(self) -> None:
        self._apply_state(self._view_model.preview(self._form()))

    @Slot()
    def _mark_connection_dirty(self) -> None:
        if self._state.status is UiStatus.EMPTY:
            return
        self.preview_button.setEnabled(False)
        self.status_label.setText("INPUTS CHANGED · REFRESH REQUIRED")
        self.status_label.setProperty("state", "blocked")
        _repolish(self.status_label)
        self.plan_table.setRowCount(0)
        self.route_widget.set_marks(())

    def _set_busy(self, busy: bool) -> None:
        self.refresh_button.setEnabled(not busy)
        self.preview_button.setEnabled(False if busy else self._state.can_preview)
        if busy:
            self.status_label.setText("REFRESHING · PRIOR SNAPSHOT INVALIDATED")
            self.status_label.setProperty("state", "loading")
            _repolish(self.status_label)
            self.plan_table.setRowCount(0)
            self.route_widget.set_marks(())

    def _selection(self) -> ConnectionSelection:
        return ConnectionSelection(
            account=self.account_input.text().strip(),
            con_id=self.con_id_input.value(),
            port=self.port_input.value(),
            client_id=self.client_id_input.value(),
            timeout_seconds=self.timeout_input.value(),
        )

    def _form(self) -> PlanForm:
        remainder = self.remainder_input.currentData()
        trigger = self.trigger_input.currentData()
        return PlanForm(
            tranche_size=self.tranche_input.text(),
            target_percentages=self.target_input.text(),
            stop_loss_percentage=self.stop_input.text(),
            remainder_policy=(
                remainder
                if isinstance(remainder, RemainderPolicy)
                else RemainderPolicy.NEXT_RUNG
            ),
            tif=self.tif_input.currentText(),
            trigger_method=(
                trigger
                if isinstance(trigger, TriggerMethod)
                else TriggerMethod.DOUBLE_BID_ASK
            ),
        )

    def _apply_state(self, state: ViewState) -> None:
        self._state = state
        self.status_label.setText(
            f"{state.status.value} · {state.status_message.upper()}"
        )
        self.status_label.setAccessibleDescription(state.status_message)
        self.status_label.setProperty("state", state.status.value.lower())
        _repolish(self.status_label)
        self.preview_button.setEnabled(state.can_preview)
        self.age_label.setText(f"Snapshot age {state.snapshot_age}")
        self.position_title.setText(state.position_title)
        _populate_facts(self.connection_facts, state.connection, dark=True)
        _populate_facts(self.position_facts, state.position, dark=True)
        _populate_facts(self.quote_facts, state.quote, dark=False)
        self.market_rule_label.setText(
            "Market rule\n" + "\n".join(state.market_rule)
            if state.market_rule
            else "Market rule —"
        )
        for label, value in zip(self.allocation_labels, state.allocation, strict=True):
            label.setText(value)
        self._populate_pairs(state)
        self._populate_validations(state)
        self.route_widget.set_marks(state.route_marks)
        self._animate_route(bool(state.route_marks))

    def _populate_pairs(self, state: ViewState) -> None:
        self.plan_table.setRowCount(len(state.pairs))
        for row, pair in enumerate(state.pairs):
            values = (
                f"{pair.index:02d}",
                str(pair.quantity),
                f"{pair.target_price}  (+{pair.target_percentage}%)",
                str(pair.stop_price),
                pair.tif,
                pair.trigger_method.replace("_", " "),
                pair.logical_group,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in {0, 1, 2, 3}:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.plan_table.setItem(row, column, item)

    def _populate_validations(self, state: ViewState) -> None:
        _clear_layout(self.validation_layout)
        if not state.validations:
            message = (
                "All planner checks passed · preview remains non-trading"
                if state.status is UiStatus.READY
                else "Waiting for a coherent paper-TWS snapshot"
            )
            label = QLabel(message)
            label.setObjectName("validationLine")
            label.setAccessibleName("Validation status")
            label.setProperty(
                "state", "pass" if state.status is UiStatus.READY else "info"
            )
            label.setWordWrap(True)
            self.validation_layout.addWidget(label)
            return
        for validation in state.validations:
            label = QLabel(f"{validation.code}\n{validation.message}")
            label.setObjectName("validationLine")
            label.setAccessibleName(
                f"{'Blocking' if validation.blocking else 'Informational'} validation"
            )
            label.setAccessibleDescription(validation.message)
            label.setProperty("state", "blocked" if validation.blocking else "info")
            label.setWordWrap(True)
            self.validation_layout.addWidget(label)

    def _animate_route(self, has_marks: bool) -> None:
        if not has_marks or os.environ.get("IBKR_OPTIONS_MANAGER_REDUCE_MOTION"):
            self.route_widget.set_reveal(1.0)
            return
        animation = QPropertyAnimation(self.route_widget, b"reveal", self)
        animation.setDuration(460)
        animation.setStartValue(0.08)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.start()
        self._route_animation = animation


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
QLabel#appTitle { color: #f4f8f8; font: 600 26px "Avenir Next"; }
QLabel#appSubtitle { color: #93abb6; font: 13px "Avenir Next"; }
QLabel#statusLabel { min-width: 280px; padding: 8px 12px; border: 1px solid #466675; color: #b9ccd2; font: 700 11px "SF Mono"; letter-spacing: 0.5px; }
QLabel#statusLabel[state="ready"] { background: #0d3c46; border-color: #7fc8c8; color: #d6ffff; }
QLabel#statusLabel[state="blocked"], QLabel#statusLabel[state="stale"] { background: #49283a; border-color: #d34174; color: #ffeaf1; }
QLabel#statusLabel[state="loading"] { background: #173b50; border-color: #7fc8c8; color: #e5ffff; }
QScrollArea#leftScroll, QWidget#leftPanel { background: #0a2233; border: 0; }
QWidget#routePanel { background: #eaf1f2; color: #071a2b; border-left: 1px solid #315062; }
QSplitter::handle { background: #315062; width: 1px; }
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
QPushButton[secondary="true"] { background: transparent; color: #cfe0e4; border-color: #557786; }
QPushButton[secondary="true"]:hover { background: #16394b; border-color: #7fc8c8; }
QPushButton:disabled { background: #173447; color: #627d88; border-color: #294b5c; }
QLabel#routeTitle { color: #071a2b; font: 600 20px "Avenir Next"; }
QLabel#ageLabel { color: #536b78; font: 11px "SF Mono"; }
QWidget#priceRoute { border: 1px solid #b7cdd1; background: #eaf1f2; }
QLabel#allocationValue { color: #173447; border-top: 1px solid #9fb8bd; padding: 8px 20px 2px 0; font: 700 12px "SF Mono"; }
QTableWidget#planTable { background: #f3f7f7; alternate-background-color: #e4edef; color: #102b3a; border: 1px solid #b7cdd1; gridline-color: #cbdadc; selection-background-color: #c3e4e3; selection-color: #071a2b; font: 11px "SF Mono"; }
QTableWidget#planTable::item { padding: 6px; }
QHeaderView::section { background: #173447; color: #eaf1f2; border: 0; border-right: 1px solid #315568; padding: 7px; font: 700 11px "Avenir Next"; }
QLabel#marketRule { color: #536b78; font: 11px "SF Mono"; padding-top: 7px; }
QLabel#validationLine { color: #173447; border-top: 1px solid #b7cdd1; padding: 7px 0; font: 11px "Avenir Next"; }
QLabel#validationLine[state="blocked"] { color: #8e2449; }
QLabel#validationLine[state="pass"] { color: #17676a; }
QFrame#riskStrip { background: #061622; border-top: 1px solid #284657; }
QLabel#riskTitle { color: #f2c14e; font: 700 11px "SF Mono"; padding-right: 16px; }
QFrame#riskStrip QLabel { color: #b7c9cf; font: 11px "Avenir Next"; }
QScrollBar:vertical { width: 10px; background: #0a2233; }
QScrollBar::handle:vertical { background: #365a6c; min-height: 30px; }
QToolTip { background: #071a2b; color: #f4f8f8; border: 1px solid #7fc8c8; padding: 5px; }
"""


__all__ = ["PlannerWindow", "PriceRouteWidget"]
