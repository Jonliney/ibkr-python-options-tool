from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from PySide6.QtWidgets import QApplication

from ..broker import IbkrPaperExecutionBroker, IbkrSnapshotBroker
from ..execution import (
    ExecutionJournal,
    PaperExecutionService,
    default_paper_journal_path,
)
from ..portfolio import PortfolioCoordinator
from ..snapshot import SnapshotCoordinator
from .account_preferences import load_saved_account, save_paper_account
from .demo import (
    DEMO_ACCOUNT,
    DEMO_CON_IDS,
    DEMO_SCENARIOS,
    DEMO_TRAILING_CON_ID,
    DemoPaperExecutionTransport,
    DemoReadOnlyBroker,
    DemoSnapshotSource,
    seed_demo_journal,
)
from .view_model import PlannerViewModel
from .web_window import StarUIPlannerWindow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ibkr-options-manager-gui",
        description="Paper-TWS option OCA bracket manager",
    )
    parser.add_argument("--account", default="", help="paper account to prefill")
    parser.add_argument("--con-id", type=int, help="option conId to prefill")
    parser.add_argument("--max-age", type=float, default=15.0)
    parser.add_argument(
        "--observer-client-id",
        type=int,
        default=18,
        help="dedicated nonzero TWS client ID for position observation",
    )
    parser.add_argument(
        "--demo-data",
        action="store_true",
        help="launch with deterministic simulated data; never contacts TWS",
    )
    parser.add_argument(
        "--demo-scenario",
        choices=DEMO_SCENARIOS,
        default="standard",
        help="named, isolated trailing workflow to rehearse with --demo-data",
    )
    parser.add_argument(
        "--enable-paper-execution",
        action="store_true",
        help=(
            "enable two-click paper-order submission; requires a DU account and "
            "TWS API read-only mode disabled"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.demo_scenario != "standard" and not args.demo_data:
        build_parser().error("--demo-scenario requires --demo-data")
    if args.demo_data and args.con_id is not None and args.con_id not in DEMO_CON_IDS:
        build_parser().error("--con-id is not present in the simulated data")
    demo_execution = args.enable_paper_execution or args.demo_scenario != "standard"
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(sys.argv[:1])
    app.setApplicationName("IBKR Options Manager")
    app.setOrganizationName("Local")

    def clock() -> Decimal:
        return Decimal(str(monotonic()))

    initial_account = args.account.strip().upper() or (
        DEMO_ACCOUNT if args.demo_data else load_saved_account()
    )
    broker: DemoReadOnlyBroker | IbkrSnapshotBroker
    coordinator: DemoSnapshotSource | SnapshotCoordinator
    if args.demo_data:
        broker = DemoReadOnlyBroker(
            clock=clock,
            paper_execution_enabled=demo_execution,
            scenario=args.demo_scenario,
        )
        coordinator = DemoSnapshotSource(broker, clock=clock)
        portfolio_max_age = Decimal("31536000")
    else:
        broker = IbkrSnapshotBroker()
        coordinator = SnapshotCoordinator(
            broker,
            max_age_seconds=Decimal(str(args.max_age)),
            clock=clock,
        )
        portfolio_max_age = Decimal(str(args.max_age))
    portfolio = PortfolioCoordinator(
        broker,
        max_age_seconds=portfolio_max_age,
        clock=clock,
        paper_execution_mode=demo_execution,
    )
    view_model = PlannerViewModel(coordinator, portfolio=portfolio, clock=clock)
    paper_execution: PaperExecutionService | None = None
    demo_journal_dir: TemporaryDirectory[str] | None = None
    if demo_execution:
        journal_path = default_paper_journal_path()
        if args.demo_data:
            if args.demo_scenario == "standard":
                # The original demo retains its separate journal across launches.
                journal_path = journal_path.with_name("demo-execution-journal.json")
            else:
                # Named examples start clean and never touch a saved journal.
                demo_journal_dir = TemporaryDirectory(prefix="ibkr-trailing-demo-")
                journal_path = Path(demo_journal_dir.name) / "scenario-journal.json"
        journal = (
            seed_demo_journal(journal_path, scenario=args.demo_scenario)
            if args.demo_data
            else ExecutionJournal(journal_path)
        )
        if isinstance(broker, DemoReadOnlyBroker):
            broker.use_journal(journal)
        transport = (
            DemoPaperExecutionTransport(journal)
            if args.demo_data
            else IbkrPaperExecutionBroker()
        )
        paper_execution = PaperExecutionService(
            transport,
            journal,
        )
    window = StarUIPlannerWindow(
        view_model,
        initial_account=initial_account,
        initial_con_id=args.con_id or (
            DEMO_TRAILING_CON_ID if args.demo_scenario != "standard" else None
        ),
        demo_mode=args.demo_data,
        paper_execution=paper_execution,
        observe_positions=not args.demo_data,
        observer_client_id=args.observer_client_id,
        save_account=save_paper_account if not args.demo_data else None,
        demo_journal_dir=demo_journal_dir,
    )
    window.refresh_on_launch()
    window.show()
    if owns_app:
        return app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
