import os
from decimal import Decimal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("IBKR_OPTIONS_MANAGER_REDUCE_MOTION", "1")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
)

from ibkr_options_manager.app.view_model import (
    Fact,
    FactState,
    PlanForm,
    PlanPairLine,
    PortfolioPositionLine,
    PreviewRow,
    QuoteCalculatorLine,
    RouteMark,
    UiStatus,
    ValidationLine,
    ViewState,
    WorkingOrderLine,
)
from ibkr_options_manager.app.window import PlannerWindow
from ibkr_options_manager.domain import PriceBand


def state(
    *,
    status: UiStatus = UiStatus.READY,
    selected: bool = True,
    working_orders: bool = False,
) -> ViewState:
    blocked = status is not UiStatus.READY
    pairs = (
        ()
        if blocked or not selected
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
                logical_group=f"abc123/tranche-{index}",
            )
            for index, quantity, percentage, target in (
                (1, 2, Decimal("20"), Decimal("28.6")),
                (2, 2, Decimal("40"), Decimal("33.4")),
                (3, 1, Decimal("60"), Decimal("38.2")),
            )
        )
    )
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
        position_title=(
            "No verified position"
            if blocked or not selected
            else "SPXW  260916C07605000"
        ),
        position=(
            () if blocked or not selected else (Fact("Position", "5", FactState.PASS),)
        ),
        quote=(
            ()
            if blocked or not selected
            else (Fact("Bid", "$23.3"), Fact("Ask", "$23.4"))
        ),
        market_rule=(
            () if blocked or not selected else ("0+ · tick 0.05", "3+ · tick 0.1")
        ),
        snapshot_age="—" if blocked or not selected else "1.0 s",
        allocation=(
            ("Position —", "Allocated —", "Planned —")
            if blocked or not selected
            else ("Position 5", "Allocated 0", "Planned 5")
        ),
        pairs=pairs,
        route_marks=()
        if blocked or not selected
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
        fingerprint=None if blocked or not selected else "a" * 64,
        can_preview=not blocked and selected,
        positions=(
            ()
            if blocked
            else (
                PortfolioPositionLine(
                    con_id=917864414,
                    local_symbol="SPXW  260916C07605000",
                    quantity="5",
                    unit_basis="$23.816303",
                    working_order_count=1,
                    eligible=True,
                    eligibility="Eligible",
                ),
                PortfolioPositionLine(
                    con_id=917864415,
                    local_symbol="SPXW  260916P07500000",
                    quantity="-1",
                    unit_basis="$12.4",
                    working_order_count=0,
                    eligible=False,
                    eligibility="Short position",
                ),
            )
        ),
        selected_con_id=917864414 if selected and not blocked else None,
        working_orders=(
            ()
            if blocked or not selected or not working_orders
            else (
                WorkingOrderLine(
                perm_id=1197098753,
                action="SELL",
                order_type="LMT",
                remaining="1",
                status="Submitted",
                ),
            )
        ),
        preview_rows=tuple(
            PreviewRow(
                (
                    f"{pair.index:02d}",
                    str(pair.quantity),
                    f"{pair.target_price}  (+{pair.target_percentage}%)",
                    str(pair.stop_price),
                    pair.tif,
                    pair.logical_group,
                )
            )
            for pair in pairs
        ),
        quote_calculator=(
            None
            if blocked or not selected
            else QuoteCalculatorLine(
                bid=Decimal("23.3"),
                ask=Decimal("23.4"),
                last=Decimal("23.3"),
                market_data_type="LIVE",
                fresh=True,
                bands=(
                    PriceBand(Decimal("0"), Decimal("0.05")),
                    PriceBand(Decimal("3"), Decimal("0.1")),
                ),
            )
        ),
        available_quantity=0 if blocked or not selected else 5,
        unit_basis=None if blocked or not selected else Decimal("23.816303"),
        multiplier=None if blocked or not selected else Decimal("100"),
    )


class FakeViewModel:
    def __init__(self) -> None:
        self.refresh_state = state()
        self.preview_state = state()
        self.select_state = state()
        self.refresh_count = 0
        self.preview_count = 0
        self.select_count = 0
        self.last_form: PlanForm | None = None

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

    def refresh_portfolio(self, settings: object) -> ViewState:
        del settings
        self.refresh_count += 1
        return self.refresh_state

    def select_position(self, con_id: int, form: PlanForm) -> ViewState:
        del con_id, form
        self.select_count += 1
        return self.select_state

    def preview_action(self, form: PlanForm) -> ViewState:
        self.last_form = form
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


def test_window_has_a_read_only_action_review_and_no_write_controls() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window.show()
    qt_app.processEvents()

    notice = window.findChild(QLabel, "previewNotice")
    assert notice is not None
    assert "No order will be placed" in notice.text()
    button_text = {button.text() for button in window.findChildren(QPushButton)}
    assert {"Refresh", "Preview current draft", "Transmission locked"} <= button_text
    assert not any(
        word in label.lower()
        for label in button_text
        for word in ("submit", "confirm", "cancel", "modify", "execute")
    )
    assert window.transmission_locked.isEnabled() is False
    window.close()


def test_outcome_projection_uses_the_draft_layers_and_cost_basis() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    qt_app.processEvents()

    assert window.findChild(QLabel, "quoteSnapshot") is None
    assert window.outcome_summary_label.text().startswith("5 assigned contract(s)")
    assert window.outcome_gain_label.text() == "+$2391.85 (+20.1%)"
    assert window.outcome_loss_label.text() == "-$2958.15 (-24.8%)"

    target = window.findChildren(QDoubleSpinBox, "layerTargetPercentageInput")[0]
    target.setValue(40)
    qt_app.processEvents()

    assert window.findChildren(QLabel, "layerTargetPrice")[0].text() == "$33.4"
    assert window.outcome_gain_label.text() == "+$4791.85 (+40.2%)"
    assert window.outcome_loss_label.text() == "-$2958.15 (-24.8%)"
    window.close()


def test_layer_outcomes_show_each_layer_impact_and_modeled_breakeven() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    QTest.mouseClick(window.add_layer_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    target_outcomes = window.findChildren(QLabel, "layerTargetOutcome")
    stop_outcomes = window.findChildren(QLabel, "layerStopOutcome")
    assert [label.text() for label in target_outcomes] == [
        "+$1435.11",
        "+$1916.74",
    ]
    assert [label.text() for label in stop_outcomes] == [
        "-$1774.89",
        "-$1183.26",
    ]
    assert window.outcome_breakeven_label.text() == "Layer 1 (+$251.85)"
    assert window.findChild(QLabel, "outcomeBreakevenDetail") is None
    remove = next(
        button
        for button in window.findChildren(QPushButton, "removeLayerButton")
        if button.isVisible()
    )
    assert remove.text() == ""
    assert remove.icon().isNull() is False
    assert remove.accessibleName() == "Remove layer 1"
    price_field = window.findChild(QFrame, "percentagePriceField")
    assert price_field is not None
    assert remove.height() == price_field.height()
    window.close()


def test_layer_percentages_are_editable_and_reprice_the_dollar_reference() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    target = window.findChildren(QDoubleSpinBox, "layerTargetPercentageInput")[0]
    stop = window.findChildren(QDoubleSpinBox, "layerStopPercentageInput")[0]
    target.setValue(40)
    qt_app.processEvents()
    stop = window.findChildren(QDoubleSpinBox, "layerStopPercentageInput")[0]
    stop.setValue(25)
    qt_app.processEvents()

    assert window.findChildren(QLabel, "layerTargetPrice")[0].text() == "$33.4"
    assert window.findChildren(QLabel, "layerStopPrice")[0].text() == "$17.9"
    window.close()


def test_layer_percentage_inputs_are_equal_width_numeric_spinners() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    QTest.mouseClick(window.add_layer_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()
    targets = window.findChildren(QDoubleSpinBox, "layerTargetPercentageInput")
    stops = window.findChildren(QDoubleSpinBox, "layerStopPercentageInput")
    target_prices = window.findChildren(QLabel, "layerTargetPrice")
    stop_prices = window.findChildren(QLabel, "layerStopPrice")

    assert [field.singleStep() for field in [*targets, *stops]] == [1.0] * 4
    assert len({price.width() for price in [*target_prices, *stop_prices]}) == 1

    targets[0].setFocus()
    QTest.keyClick(targets[0], Qt.Key.Key_Up)
    qt_app.processEvents()

    assert targets[0].value() == 21
    assert targets[0].hasFocus()
    assert target_prices[0].text() == "$28.9"
    window.close()


def test_repeated_preview_uses_the_draft_without_refreshing_transport() -> None:
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
    assert view_model.last_form is not None
    assert len(view_model.last_form.layers) == 1
    assert view_model.last_form.layers[0].quantity == "5"
    window.close()


def test_selecting_a_position_button_requests_that_contract_detail() -> None:
    qt_app = app()
    view_model = FakeViewModel()
    window = PlannerWindow(  # type: ignore[arg-type]
        view_model,
        thread_pool=InlineThreadPool(),  # type: ignore[arg-type]
    )
    window._apply_state(state(selected=False))
    window.show()
    qt_app.processEvents()

    button = window.findChildren(QPushButton, "positionButton")[0]
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    assert view_model.select_count == 1
    window.close()


def test_refresh_selects_the_first_position_and_marks_external_order_coverage() -> None:
    qt_app = app()
    view_model = FakeViewModel()
    view_model.refresh_state = state(selected=False)
    view_model.select_state = state(selected=True, working_orders=True)
    window = PlannerWindow(  # type: ignore[arg-type]
        view_model,
        thread_pool=InlineThreadPool(),  # type: ignore[arg-type]
    )
    window.show()
    qt_app.processEvents()

    QTest.mouseClick(window.refresh_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    assert view_model.select_count == 1
    assert len(window.findChildren(QPushButton, "positionButton")) == 2
    assert window.external_notice.isVisible()
    assert "External order coverage" in window.external_notice.text()
    window.close()


def test_second_refresh_reselects_and_verifies_the_first_position() -> None:
    qt_app = app()
    view_model = FakeViewModel()
    view_model.refresh_state = state(selected=False)
    view_model.select_state = state(selected=True)
    window = PlannerWindow(  # type: ignore[arg-type]
        view_model,
        thread_pool=InlineThreadPool(),  # type: ignore[arg-type]
    )
    window.show()
    qt_app.processEvents()

    QTest.mouseClick(window.refresh_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()
    QTest.mouseClick(window.refresh_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    assert view_model.select_count == 2
    assert window.preview_button.isEnabled()
    window.close()


def test_preview_uses_the_current_layer_configuration() -> None:
    qt_app = app()
    view_model = FakeViewModel()
    window = PlannerWindow(view_model)  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    window.findChildren(QLineEdit, "layerQuantityInput")[0].setText("3")

    QTest.mouseClick(window.preview_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    assert view_model.preview_count == 1
    assert view_model.last_form is not None
    assert view_model.last_form.layers[0].quantity == "3"
    window.close()


def test_add_layer_uses_the_next_target_and_redistributes_the_draft() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    QTest.mouseClick(window.add_layer_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    targets = window.findChildren(QDoubleSpinBox, "layerTargetPercentageInput")
    quantities = window.findChildren(QLineEdit, "layerQuantityInput")
    assert [target.text() for target in targets] == ["20", "40"]
    prices = window.findChildren(QLabel, "layerTargetPrice")
    assert [price.text() for price in prices] == ["$28.6", "$33.4"]
    assert [quantity.text() for quantity in quantities] == ["3", "2"]
    assert window.findChild(QLineEdit, "nextTargetInput") is None
    window.close()


def test_add_layer_uses_the_layer_preset_even_when_an_existing_target_changes() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    target = window.findChildren(QDoubleSpinBox, "layerTargetPercentageInput")[0]
    target.setValue(60)
    QTest.mouseClick(window.add_layer_button, Qt.MouseButton.LeftButton)

    targets = window.findChildren(QDoubleSpinBox, "layerTargetPercentageInput")
    assert [field.text() for field in targets] == ["60", "40"]
    window.close()


def test_layer_presets_set_new_layer_defaults_and_repeat_the_final_value() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    window.findChild(QLineEdit, "lmtTargetPresetsInput").setText("20, 40, 60")  # type: ignore[union-attr]
    window.findChild(QLineEdit, "stpLossPresetsInput").setText("25, 35")  # type: ignore[union-attr]
    for _ in range(4):
        QTest.mouseClick(window.add_layer_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    targets = window.findChildren(QDoubleSpinBox, "layerTargetPercentageInput")
    stops = window.findChildren(QDoubleSpinBox, "layerStopPercentageInput")
    assert [field.text() for field in targets] == ["20", "40", "60", "60", "60"]
    assert [field.text() for field in stops] == ["25", "35", "35", "35", "35"]
    window.close()


def test_invalid_layer_presets_block_new_layers_without_staling_the_position() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    lmt_presets = window.findChild(QLineEdit, "lmtTargetPresetsInput")
    assert lmt_presets is not None
    lmt_presets.setText("20, not-a-percentage")
    qt_app.processEvents()

    assert window.add_layer_button.isEnabled() is False
    assert window.preview_button.isEnabled() is True
    assert "comma-separated" in window.bracket_help.text()
    window.close()


def test_add_layer_after_removal_redistributes_all_available_contracts() -> None:
    """Removing a row must not silently shrink the quantity used by the next split."""
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    QTest.mouseClick(window.add_layer_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()
    QTest.mouseClick(
        window.findChildren(QPushButton, "removeLayerButton")[1],
        Qt.MouseButton.LeftButton,
    )
    qt_app.processEvents()
    QTest.mouseClick(window.add_layer_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    quantities = window.findChildren(QLineEdit, "layerQuantityInput")
    assert [quantity.text() for quantity in quantities] == ["3", "2"]
    window.close()


def test_layer_count_cannot_exceed_available_contracts() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    for _ in range(4):
        QTest.mouseClick(window.add_layer_button, Qt.MouseButton.LeftButton)
    qt_app.processEvents()

    assert len(window.findChildren(QLineEdit, "layerQuantityInput")) == 5
    assert window.add_layer_button.isEnabled() is False
    assert window.findChild(QPushButton, "addRunnerButton") is None

    QTest.mouseClick(window.add_layer_button, Qt.MouseButton.LeftButton)
    assert len(window.findChildren(QLineEdit, "layerQuantityInput")) == 5
    window.close()


def test_equal_split_can_apply_to_all_available_or_assigned_contracts() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    QTest.mouseClick(window.add_layer_button, Qt.MouseButton.LeftButton)
    quantities = window.findChildren(QLineEdit, "layerQuantityInput")
    quantities[0].setText("1")
    quantities[1].setText("1")
    qt_app.processEvents()
    assert window.equal_split_button.text() == "Equal split available contracts"
    window.equal_split_assigned_action.trigger()
    assert window.equal_split_button.text() == "Equal split assigned contracts"
    QTest.mouseClick(window.equal_split_button, Qt.MouseButton.LeftButton)
    quantities = window.findChildren(QLineEdit, "layerQuantityInput")
    assert [field.text() for field in quantities] == ["1", "1"]

    window.equal_split_available_action.trigger()
    assert window.equal_split_button.text() == "Equal split available contracts"
    QTest.mouseClick(window.equal_split_button, Qt.MouseButton.LeftButton)
    quantities = window.findChildren(QLineEdit, "layerQuantityInput")
    assert [field.text() for field in quantities] == ["3", "2"]
    window.close()


def test_working_orders_are_shown_as_external_coverage_in_the_workspace() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state(working_orders=True))
    window.show()
    qt_app.processEvents()

    assert window.preview_button.isEnabled()
    assert window.external_notice.isVisible()
    window.close()


def test_blocked_portfolio_refresh_clears_the_previous_plan() -> None:
    qt_app = app()
    view_model = FakeViewModel()
    view_model.refresh_state = state(status=UiStatus.BLOCKED, selected=False)
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
    assert window.preview_button.isEnabled() is False
    assert window.available_label.text().startswith("Refresh and select")
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
    assert window.port_input.hasFocus() or window.port_input.lineEdit().hasFocus()
    window.close()


def test_dynamic_state_and_draft_controls_have_accessible_text_equivalents() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.show()
    qt_app.processEvents()

    field_labels = window.findChildren(QLabel, "fieldLabel")
    assert any(label.buddy() is window.account_input for label in field_labels)
    assert window.status_label.accessibleName() == "Planner status"
    assert "Synthetic review fixture" in window.status_label.accessibleDescription()
    target_percentages = window.findChildren(
        QDoubleSpinBox, "layerTargetPercentageInput"
    )
    assert target_percentages[0].accessibleName()
    assert window.findChildren(QPushButton, "positionButton")[0].accessibleName()
    window.close()


def test_minimum_window_size_keeps_inventory_and_action_preview_visible() -> None:
    qt_app = app()
    window = PlannerWindow(FakeViewModel())  # type: ignore[arg-type]
    window._apply_state(state())
    window.resize(window.minimumSize())
    window.show()
    qt_app.processEvents()

    assert window.positions_scroll.isVisible()
    assert window.review_scroll.isVisible()
    assert window.layer_rows_widget.isVisible()
    window.close()
