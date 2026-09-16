import os
from decimal import Decimal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("IBKR_OPTIONS_MANAGER_REDUCE_MOTION", "1")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from ibkr_options_manager.app.view_model import (
    Fact,
    FactState,
    PlanPairLine,
    RouteMark,
    UiStatus,
    ValidationLine,
    ViewState,
)
from ibkr_options_manager.app.window import PlannerWindow


def state(*, status: UiStatus = UiStatus.READY) -> ViewState:
    blocked = status is not UiStatus.READY
    return ViewState(
        status=status,
        status_message=(
            "Broker state is not ready"
            if blocked
            else "Synthetic review fixture · plan ready for inspection"
        ),
        account="***4567",
        connection=(
            Fact("Endpoint", "127.0.0.1:7497", FactState.PASS),
            Fact("Read-only API", "verified", FactState.PASS),
        ),
        position_title=("No verified position" if blocked else "SPXW  260916C07605000"),
        position=() if blocked else (Fact("Position", "5", FactState.PASS),),
        quote=() if blocked else (Fact("Bid", "$23.3"), Fact("Ask", "$23.4")),
        market_rule=() if blocked else ("0+ · tick 0.05", "3+ · tick 0.1"),
        snapshot_age="—" if blocked else "1.0 s",
        allocation=(
            ("Position —", "Allocated —", "Planned —")
            if blocked
            else ("Position 5", "Allocated 0", "Planned 5")
        ),
        pairs=()
        if blocked
        else tuple(
            PlanPairLine(
                index=index,
                quantity=quantity,
                target_percentage=percentage,
                target_raw=target,
                target_price=target,
                stop_raw=Decimal("19.1"),
                stop_price=Decimal("19.1"),
                tif="GTC",
                trigger_method="DOUBLE_BID_ASK",
                logical_group=f"abc123/tranche-{index}",
            )
            for index, quantity, percentage, target in (
                (1, 2, Decimal("20"), Decimal("28.6")),
                (2, 2, Decimal("40"), Decimal("33.4")),
                (3, 1, Decimal("60"), Decimal("38.2")),
            )
        ),
        route_marks=()
        if blocked
        else (
            RouteMark("BASIS", Decimal("23.816303"), "BASIS", "unit premium"),
            RouteMark("T1", Decimal("28.6"), "TARGET", "+20% · 2 contracts"),
            RouteMark("S1", Decimal("19.1"), "STOP", "2 contracts"),
            RouteMark("T2", Decimal("33.4"), "TARGET", "+40% · 2 contracts"),
            RouteMark("S2", Decimal("19.1"), "STOP", "2 contracts"),
            RouteMark("T3", Decimal("38.2"), "TARGET", "+60% · 1 contract"),
            RouteMark("S3", Decimal("19.1"), "STOP", "1 contract"),
        ),
        validations=(
            (ValidationLine("SNAPSHOT_BLOCKED", "position request timed out"),)
            if blocked
            else ()
        ),
        fingerprint=None if blocked else "a" * 64,
        can_preview=not blocked,
    )


class FakeViewModel:
    def __init__(self) -> None:
        self.refresh_state = state()
        self.preview_state = state()
        self.refresh_count = 0
        self.preview_count = 0

    def empty(self) -> ViewState:
        return ViewState(
            status=UiStatus.EMPTY,
            status_message="Enter connection details",
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

    def refresh(self, selection: object, form: object) -> ViewState:
        del selection, form
        self.refresh_count += 1
        return self.refresh_state

    def preview(self, form: object) -> ViewState:
        del form
        self.preview_count += 1
        return self.preview_state


class InlineThreadPool:
    def start(self, runnable: object) -> None:
        runnable.run()  # type: ignore[attr-defined]


def app() -> QApplication:
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing
    return QApplication([])


def test_window_has_a_permanent_notice_and_no_order_action_control() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window.show()
    qt_app.processEvents()

    notice = window.findChild(QLabel, "safetyLabel")
    assert notice is not None
    assert notice.text() == "READ-ONLY PREVIEW — ORDERS CANNOT BE SENT"
    button_text = {button.text() for button in window.findChildren(QPushButton)}
    assert button_text == {"Refresh paper snapshot", "Rebuild preview"}
    assert not any(
        word in label.lower()
        for label in button_text
        for word in ("submit", "confirm", "cancel", "modify", "transmit")
    )
    window.close()


def test_repeated_preview_updates_the_table_without_refreshing_transport() -> None:
    qt_app = app()
    view_model = FakeViewModel()
    window = PlannerWindow(view_model)  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    QTest.mouseClick(window.preview_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(window.preview_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    assert view_model.preview_count == 2
    assert view_model.refresh_count == 0
    assert window.plan_table.rowCount() == 3
    assert window.plan_table.item(0, 2).text() == "28.6  (+20%)"
    assert window.plan_table.item(2, 1).text() == "1"
    window.close()


def test_blocked_refresh_clears_the_previous_plan() -> None:
    qt_app = app()
    view_model = FakeViewModel()
    view_model.refresh_state = state(status=UiStatus.BLOCKED)
    window = PlannerWindow(  # type: ignore[arg-type]
        view_model,
        initial_account="DU1234567",
        initial_con_id=917864414,
        thread_pool=InlineThreadPool(),  # type: ignore[arg-type]
    )
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    QTest.mouseClick(window.refresh_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    assert view_model.refresh_count == 1
    assert window.plan_table.rowCount() == 0
    assert window.preview_button.isEnabled() is False
    labels = [label.text() for label in window.findChildren(QLabel)]
    assert any("position request timed out" in label for label in labels)
    window.close()


def test_changing_connection_selection_invalidates_the_visible_plan() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    window.account_input.setText("DU7654321")
    qt_app.processEvents()

    assert window.preview_button.isEnabled() is False
    assert window.plan_table.rowCount() == 0
    assert window.route_widget.marks() == ()
    assert "REFRESH REQUIRED" in window.status_label.text()
    window.close()


def test_keyboard_flow_starts_with_account_then_contract() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window.show()
    window.account_input.setFocus()
    qt_app.processEvents()

    QTest.keyClick(window.account_input, Qt.Key.Key_Tab)
    qt_app.processEvents()
    assert window.con_id_input.hasFocus() or window.con_id_input.lineEdit().hasFocus()
    window.close()


def test_dynamic_state_and_route_have_accessible_text_equivalents() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    field_labels = window.findChildren(QLabel, "fieldLabel")
    assert any(label.buddy() is window.account_input for label in field_labels)
    assert window.status_label.accessibleName() == "Planner status"
    assert "Synthetic review fixture" in window.status_label.accessibleDescription()
    assert "BASIS at 23.816303" in window.route_widget.accessibleDescription()
    validation_labels = window.findChildren(QLabel, "validationLine")
    assert any(
        label.accessibleName() == "Validation status" for label in validation_labels
    )
    window.close()


def test_minimum_window_size_keeps_route_allocation_and_table_separate() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.resize(window.minimumSize())
    window.show()
    qt_app.processEvents()

    allocation_top = min(label.geometry().top() for label in window.allocation_labels)
    allocation_bottom = max(
        label.geometry().bottom() for label in window.allocation_labels
    )
    assert window.route_widget.geometry().bottom() < allocation_top
    assert allocation_bottom < window.plan_table.geometry().top()
    window.close()
