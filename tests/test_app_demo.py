import os
from dataclasses import replace
from decimal import Decimal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from starlette.testclient import TestClient

from ibkr_options_manager.app.demo import (
    DEMO_ACCOUNT,
    DEMO_CON_IDS,
    DemoPaperExecutionTransport,
    DemoReadOnlyBroker,
)
from ibkr_options_manager.app.main import build_parser, main
from ibkr_options_manager.app.web import StarUIWorkbench
from ibkr_options_manager.app.web.surface import _position_identity
from ibkr_options_manager.broker import PortfolioRequest, SnapshotRequest
from ibkr_options_manager.execution import ExecutionJournal, PaperExecutionService
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


def test_demo_launch_populates_the_starui_workbench_without_a_tws_refresh() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()

    assert len(workbench._state.positions) == 4
    assert workbench._state.selected_con_id is not None
    assert workbench._state.quote_calculator is not None
    assert workbench._state.available_quantity == 5


def test_position_identity_preserves_the_inventory_scan_order() -> None:
    assert _position_identity("MSTR  260925C00150000") == (
        "MSTR",
        "150 CALL · SEP 25 '26",
    )
    assert _position_identity("Unknown contract") == ("Unknown", "contract")


def test_demo_draft_has_no_separate_preview_action() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    page = client.get(workbench.path)

    assert workbench._state.selected_con_id is not None
    assert workbench._state.available_quantity == 5
    assert workbench._state.quote_calculator is not None
    assert "Preview current draft" not in page.text


def test_reconciled_app_orders_are_not_described_as_external_coverage() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._paper_execution = _OwnedOrderService({496_248_334})
    client = TestClient(workbench.app)

    page = client.get(workbench.path)

    assert "App-managed OCA coverage active" in page.text
    assert "1 app-created orders were reconciled with TWS" in page.text
    assert "Associated external orders remain inspect-only" not in page.text


def test_active_layers_show_complete_reconciled_lmt_stop_pairs() -> None:
    from ibkr_options_manager.app.view_model import WorkingOrderLine

    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._paper_execution = _OwnedOrderService({101, 102})
    workbench._state = replace(
        workbench._state,
        working_orders=(
            WorkingOrderLine(
                perm_id=101,
                order_id=11,
                action="SELL",
                order_type="LMT",
                remaining="4",
                status="Submitted",
                oca_group="3ad441753bb9/tranche-1",
                limit_price=Decimal("26.20"),
                tif="GTC",
            ),
            WorkingOrderLine(
                perm_id=102,
                order_id=12,
                action="SELL",
                order_type="STP",
                remaining="4",
                status="Submitted",
                oca_group="3ad441753bb9/tranche-1",
                stop_price=Decimal("16.40"),
                tif="GTC",
            ),
        ),
    )
    client = TestClient(workbench.app)

    page = client.get(workbench.path)

    assert "Layered OCA draft" in page.text
    active = client.post(
        workbench.path + "action",
        data={"action": "show-active"},
    )

    assert 'aria-label="Reconciled active OCA layers"' in active.text
    assert "OCA-1" in active.text
    assert "$26.20" in active.text
    assert "$16.40" in active.text
    assert "Move stop to B/E" in active.text
    assert "Update layers" in active.text
    assert "Sell layers" in active.text
    assert "active_layer_1" in active.text
    assert 'name="active_target_1"' in active.text
    assert 'name="active_stop_1"' in active.text
    assert 'id="active-quantity-1"' in active.text
    assert 'id="active-tif-1"' in active.text
    assert "State" not in active.text
    assert "requires a second confirmation" not in active.text
    assert "Layered OCA draft" not in active.text
    assert 'aria-current="page"' in active.text


def test_starui_workbench_renders_and_adds_a_layer_from_a_server_owned_form() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)

    page = client.get(workbench.path)
    assert page.status_code == 200
    assert "Layered OCA draft" in page.text
    assert "Last refreshed " in page.text
    assert "Verified 0.0 s" not in page.text
    assert "Each layer creates one SELL LMT + SELL STP OCA pair." not in page.text
    assert "Transmission locked" not in page.text
    assert "Preview only" not in page.text
    assert "SELL LMT" in page.text
    assert "SELL STP" in page.text
    assert "150 CALL · SEP 25 '26" in page.text
    assert "Connection &amp; layer defaults" in page.text
    assert "<dialog" in page.text
    assert "h-screen overflow-hidden" in page.text
    assert 'aria-label="Draft layer rows"' in page.text
    assert 'aria-label="Layer draft workspace"' in page.text
    assert 'aria-label="Planned order actions"' in page.text
    assert 'id="draft-form"' in page.text
    assert 'data-live-input="target"' in page.text
    assert 'data-live-review-price="target-1"' in page.text
    assert 'data-live-metric="gain"' in page.text
    assert "Preview current draft" not in page.text
    assert "cdn.jsdelivr.net" not in page.text
    assert "api.iconify.design" not in page.text
    assert "@starhtml/plugins/position" in page.text
    assert client.get("/_pkg/starhtml/plugins/position.js").status_code == 200

    draft = page.text.split("Layered OCA draft", maxsplit=1)[1].split(
        "Outcome projection", maxsplit=1
    )[0]
    assert 'data-slot="card-action"' in draft
    assert "Split all available" in draft
    assert "Split assigned" in draft
    assert "Add layer" in draft
    assert "Execute paper order" not in draft
    assert 'name="action" value="save-draft"' not in draft
    assert "font-mono" not in draft
    assert "text-xs font-semibold text-foreground" in draft
    assert "+$280.00 gain" in draft
    assert "-$340.00 max loss" in draft
    assert ">%</span>" in draft
    assert 'for="target_1"' in draft
    assert 'id="target_1"' in draft
    assert 'for="stop_1"' in draft
    assert 'id="stop_1"' in draft
    assert 'for="quantity_1"' in draft
    assert 'id="quantity_1"' in draft
    assert 'for="tif_1"' in draft
    assert 'id="tif_1"' in draft
    assert "w-full min-w-[41rem]" in draft
    assert "items-start gap-3" in draft
    assert (
        "grid-cols-[5rem_minmax(10rem,1fr)_minmax(10rem,1fr)_minmax(5rem,0.6fr)_5rem_2.25rem]"
        in draft
    )
    assert 'aria-label="Draft layer rows"' in draft
    assert 'overflow-x-auto overflow-y-hidden' in draft

    assert "mt-5" in draft

    response = client.post(
        workbench.path + "action",
        data={
            "action": "add-layer",
            "target_presets": "20, 40, 60, 100",
            "stop_presets": "25",
            "target_1": "20",
            "stop_1": "25",
            "quantity_1": "5",
            "tif_1": "DAY",
        },
    )

    assert response.status_code == 200
    assert [layer.quantity for layer in workbench._current_layers()] == ["3", "2"]
    assert workbench._current_layers()[0].tif == "DAY"

    response = client.post(
        workbench.path + "action",
        data={
            "action": "equal-split-assigned",
            "target_presets": "20, 40, 60, 100",
            "stop_presets": "25",
            "target_1": "20",
            "stop_1": "25",
            "quantity_1": "1",
            "target_2": "40",
            "stop_2": "25",
            "quantity_2": "2",
        },
    )

    assert response.status_code == 200
    assert [layer.quantity for layer in workbench._current_layers()] == ["2", "1"]

    response = client.post(
        workbench.path + "action",
        data={
            "action": "equal-split-available",
            "target_presets": "20, 40, 60, 100",
            "stop_presets": "25",
            "target_1": "20",
            "stop_1": "25",
            "quantity_1": "2",
            "target_2": "40",
            "stop_2": "25",
            "quantity_2": "1",
        },
    )

    assert response.status_code == 200
    assert [layer.quantity for layer in workbench._current_layers()] == ["3", "2"]

    response = client.post(
        workbench.path + "action",
        data={
            "action": "remove-layer:1",
            "target_presets": "20, 40, 60, 100",
            "stop_presets": "25",
            "target_1": "20",
            "stop_1": "25",
            "quantity_1": "3",
            "target_2": "40",
            "stop_2": "25",
            "quantity_2": "2",
        },
    )

    assert response.status_code == 200
    assert [layer.quantity for layer in workbench._current_layers()] == ["2"]


def test_paper_execution_control_submits_the_current_draft_form() -> None:
    workbench = _demo_workbench()
    workbench.load_demo_data()
    workbench._paper_execution = _OwnedOrderService(set())

    page = TestClient(workbench.app).get(workbench.path)

    assert 'id="draft-form"' in page.text
    assert 'form="draft-form"' in page.text
    assert 'name="action" value="execute-arm"' in page.text
    assert "Add a layer or modify an existing one to continue." not in page.text


def test_demo_execution_uses_the_same_two_click_flow_without_contacting_tws(
    tmp_path,
) -> None:
    def clock() -> Decimal:
        return Decimal("100")

    broker = DemoReadOnlyBroker(clock=clock, paper_execution_enabled=True)
    snapshots = SnapshotCoordinator(broker, max_age_seconds=Decimal("15"), clock=clock)
    portfolio = PortfolioCoordinator(
        broker,
        max_age_seconds=Decimal("15"),
        clock=clock,
        paper_execution_mode=True,
    )
    from ibkr_options_manager.app.view_model import PlannerViewModel

    workbench = StarUIWorkbench(
        PlannerViewModel(snapshots, portfolio=portfolio, clock=clock),
        initial_account=DEMO_ACCOUNT,
        demo_mode=True,
        paper_execution=PaperExecutionService(
            DemoPaperExecutionTransport(),
            ExecutionJournal(tmp_path / "paper-journal.json"),
        ),
    )
    workbench.load_demo_data()
    workbench._select_locked(1_002_100_161)  # NVDA has no associated demo order.
    client = TestClient(workbench.app)

    armed = client.post(
        workbench.path + "action",
        data={"action": "execute-arm"},
    )

    assert armed.status_code == 200
    assert "Click to confirm" in armed.text
    assert "Fresh paper snapshot verified" in armed.text

    submitted = client.post(
        workbench.path + "action",
        data={"action": "execute-confirm"},
    )

    assert submitted.status_code == 200
    assert "Paper submission acknowledged for 2 orders" in submitted.text
    assert "Execute paper order" in submitted.text


def test_main_builds_the_embedded_starui_window(monkeypatch: object) -> None:
    QApplication.instance() or QApplication([])
    created: list[_WindowStub] = []

    def window_factory(*args: object, **kwargs: object) -> _WindowStub:
        window = _WindowStub(*args, **kwargs)
        created.append(window)
        return window

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "ibkr_options_manager.app.main.StarUIPlannerWindow", window_factory
    )

    assert main(["--demo-data"]) == 0
    assert len(created) == 1
    assert created[0].demo_loaded is True


def _demo_workbench() -> StarUIWorkbench:
    def clock() -> Decimal:
        return Decimal("100")

    broker = DemoReadOnlyBroker(clock=clock)
    snapshots = SnapshotCoordinator(broker, max_age_seconds=Decimal("15"), clock=clock)
    portfolio = PortfolioCoordinator(broker, max_age_seconds=Decimal("15"), clock=clock)
    from ibkr_options_manager.app.view_model import PlannerViewModel

    return StarUIWorkbench(
        PlannerViewModel(snapshots, portfolio=portfolio, clock=clock),
        initial_account=DEMO_ACCOUNT,
        demo_mode=True,
    )


class _WindowStub:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.demo_loaded = False

    def show(self) -> None:
        pass

    def load_demo_data(self) -> None:
        self.demo_loaded = True


class _OwnedOrderService:
    def __init__(self, perm_ids: set[int]) -> None:
        self._perm_ids = perm_ids

    def owned_perm_ids(self, *, account: str, con_id: int) -> frozenset[int]:
        del account, con_id
        return frozenset(self._perm_ids)
