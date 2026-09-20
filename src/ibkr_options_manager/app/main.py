from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from decimal import Decimal
from time import monotonic

from PySide6.QtWidgets import QApplication

from ..broker import IbkrSnapshotBroker
from ..portfolio import PortfolioCoordinator
from ..snapshot import SnapshotCoordinator
from .demo import DEMO_ACCOUNT, DEMO_CON_IDS, DemoReadOnlyBroker, DemoSnapshotSource
from .view_model import PlannerViewModel
from .web_window import StarUIPlannerWindow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ibkr-options-manager-gui",
        description="Read-only paper-TWS option exit preview",
    )
    parser.add_argument("--account", default="", help="paper account to prefill")
    parser.add_argument("--con-id", type=int, help="option conId to prefill")
    parser.add_argument("--max-age", type=float, default=15.0)
    parser.add_argument(
        "--demo-data",
        action="store_true",
        help="launch with deterministic simulated data; never contacts TWS",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.demo_data and args.con_id is not None and args.con_id not in DEMO_CON_IDS:
        build_parser().error("--con-id is not present in the simulated data")
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(sys.argv[:1])
    app.setApplicationName("IBKR Options Manager")
    app.setOrganizationName("Local")

    def clock() -> Decimal:
        return Decimal(str(monotonic()))

    initial_account = args.account or (DEMO_ACCOUNT if args.demo_data else "")
    broker: DemoReadOnlyBroker | IbkrSnapshotBroker
    coordinator: DemoSnapshotSource | SnapshotCoordinator
    if args.demo_data:
        broker = DemoReadOnlyBroker(clock=clock)
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
    )
    view_model = PlannerViewModel(coordinator, portfolio=portfolio, clock=clock)
    window = StarUIPlannerWindow(
        view_model,
        initial_account=initial_account,
        initial_con_id=args.con_id,
        demo_mode=args.demo_data,
    )
    window.show()
    if args.demo_data:
        window.load_demo_data()
    if owns_app:
        return app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
