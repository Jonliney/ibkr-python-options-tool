from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from decimal import Decimal
from time import monotonic

from PySide6.QtWidgets import QApplication

from ..broker import IbkrSnapshotBroker
from ..snapshot import SnapshotCoordinator
from .view_model import PlannerViewModel
from .window import PlannerWindow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ibkr-options-manager-gui",
        description="Read-only paper-TWS option exit preview",
    )
    parser.add_argument("--account", default="", help="paper account to prefill")
    parser.add_argument("--con-id", type=int, help="option conId to prefill")
    parser.add_argument("--max-age", type=float, default=15.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(sys.argv[:1])
    app.setApplicationName("IBKR Options Manager")
    app.setOrganizationName("Local")

    def clock() -> Decimal:
        return Decimal(str(monotonic()))

    coordinator = SnapshotCoordinator(
        IbkrSnapshotBroker(),
        max_age_seconds=Decimal(str(args.max_age)),
        clock=clock,
    )
    view_model = PlannerViewModel(coordinator, clock=clock)
    window = PlannerWindow(
        view_model,
        initial_account=args.account,
        initial_con_id=args.con_id,
    )
    window.show()
    if owns_app:
        return app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
