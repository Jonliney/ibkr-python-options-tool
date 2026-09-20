import os
from decimal import Decimal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ibkr_options_manager.app.demo import DEMO_ACCOUNT, DEMO_CON_IDS, DemoReadOnlyBroker
from ibkr_options_manager.app.main import build_parser, main
from ibkr_options_manager.app.window import PlannerWindow
from ibkr_options_manager.broker import PortfolioRequest, SnapshotRequest
from ibkr_options_manager.portfolio import PortfolioCoordinator, PortfolioStatus
from ibkr_options_manager.snapshot import SnapshotCoordinator, SnapshotStatus


def test_demo_flag_is_explicit_and_does_not_require_account_input() -> None:
    args = build_parser().parse_args(["--demo-data"])

    assert args.demo_data is True
    assert args.account == ""
    assert args.con_id is None


def test_demo_broker_exercises_inventory_and_reserved_quantity_without_tws() -> None:
    def clock() -> Decimal:
        return Decimal("100")

    broker = DemoReadOnlyBroker(clock=clock)
    portfolio = PortfolioCoordinator(
        broker, max_age_seconds=Decimal("15"), clock=clock
    )
    inventory = portfolio.refresh(
        PortfolioRequest(
            host="127.0.0.1",
            port=7497,
            client_id=17,
            expected_account=DEMO_ACCOUNT,
        )
    )

    assert inventory.status is PortfolioStatus.READY
    assert inventory.snapshot is not None
    assert (
        set(position.key.con_id for position in inventory.snapshot.positions)
        == DEMO_CON_IDS
    )
    assert inventory.snapshot.positions[0].quantity == Decimal("10")
    assert inventory.snapshot.positions[0].working_orders[0].remaining == Decimal("5")

    snapshots = SnapshotCoordinator(
        broker, max_age_seconds=Decimal("15"), clock=clock
    )
    selected = inventory.snapshot.positions[0].key.con_id
    snapshot = snapshots.refresh(
        SnapshotRequest(
            host="127.0.0.1",
            port=7497,
            client_id=17,
            expected_account=DEMO_ACCOUNT,
            option_con_id=selected,
        )
    )

    assert snapshot.status is SnapshotStatus.READY
    assert snapshot.snapshot is not None
    assert snapshot.snapshot.quote.market_data_type == "FROZEN"
    assert snapshot.snapshot.position.unit_basis == Decimal("2.74")


def test_demo_launch_populates_the_first_contract_without_a_tws_refresh() -> None:
    application = QApplication.instance() or QApplication([])

    assert main(["--demo-data"]) == 0
    window = next(
        widget
        for widget in application.topLevelWidgets()
        if isinstance(widget, PlannerWindow)
    )

    assert window.demo_indicator.isVisible()
    assert len(window._state.positions) == 4
    assert window._state.selected_con_id is not None
    assert window._state.quote_calculator is not None
    window.close()


def test_demo_preview_does_not_expire_using_the_live_snapshot_age_setting() -> None:
    application = QApplication.instance() or QApplication([])

    assert main(["--demo-data", "--max-age", "0.001"]) == 0
    window = next(
        widget
        for widget in application.topLevelWidgets()
        if isinstance(widget, PlannerWindow)
    )
    QTest.qWait(20)
    window._preview()

    assert window._state.selected_con_id is not None
    assert window._state.available_quantity == 5
    assert window._state.quote_calculator is not None
    window.close()
