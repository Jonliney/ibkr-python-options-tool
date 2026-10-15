from decimal import Decimal
from pathlib import Path

from ibkr_options_manager.app.demo import (
    DEMO_ACCOUNT,
    DemoPaperExecutionTransport,
    DemoReadOnlyBroker,
    seed_demo_journal,
)
from ibkr_options_manager.app.web import StarUIWorkbench
from ibkr_options_manager.execution import (
    PaperExecutionService,
)
from ibkr_options_manager.portfolio import PortfolioCoordinator
from ibkr_options_manager.snapshot import SnapshotCoordinator


def _trailing_scenario_workbench(tmp_path: Path, scenario: str) -> StarUIWorkbench:
    def clock() -> Decimal:
        return Decimal("100")

    from ibkr_options_manager.app.view_model import PlannerViewModel

    journal = seed_demo_journal(tmp_path / "scenario.json", scenario=scenario)
    broker = DemoReadOnlyBroker(
        clock=clock, paper_execution_enabled=True, scenario=scenario
    )
    broker.use_journal(journal)
    workbench = StarUIWorkbench(
        PlannerViewModel(
            SnapshotCoordinator(broker, max_age_seconds=Decimal("15"), clock=clock),
            portfolio=PortfolioCoordinator(
                broker,
                max_age_seconds=Decimal("15"),
                clock=clock,
                paper_execution_mode=True,
            ),
            clock=clock,
        ),
        initial_account=DEMO_ACCOUNT,
        initial_con_id=1_004_470_201,
        demo_mode=True,
        demo_scenario=scenario,
        paper_execution=PaperExecutionService(
            DemoPaperExecutionTransport(journal), journal
        ),
    )
    workbench.load_demo_data()
    return workbench


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
    def __init__(self, *_args: object, **kwargs: object) -> None:
        self.demo_loaded = False
        self.launch_refresh_requested = False
        self.initial_account = kwargs.get("initial_account")
        self.initial_con_id = kwargs.get("initial_con_id")
        self.paper_execution = kwargs.get("paper_execution")
        self.demo_journal_dir = kwargs.get("demo_journal_dir")

    def show(self) -> None:
        pass

    def load_demo_data(self) -> None:
        self.demo_loaded = True

    def refresh_on_launch(self) -> None:
        self.launch_refresh_requested = True
        self.load_demo_data()


class _OwnedOrderService:
    def __init__(self, perm_ids: set[int]) -> None:
        self._perm_ids = perm_ids

    def owned_perm_ids(self, *, account: str, con_id: int) -> frozenset[int]:
        del account, con_id
        return frozenset(self._perm_ids)
