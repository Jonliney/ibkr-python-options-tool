from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from decimal import Decimal
from time import monotonic
from typing import Any

from .broker import IbkrSnapshotBroker, SnapshotRequest
from .capability import ProbeConfig, ProbeReport, run_capability_probe
from .ibkr_probe import IbapiUnavailableError, IbkrReadOnlyBroker
from .snapshot import SnapshotCoordinator, SnapshotResult, SnapshotStatus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ibkr-options-manager",
        description="Read-only IBKR option-exit planning tools",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser(
        "probe",
        help="prove read-only paper-TWS capabilities without sending orders",
    )
    probe.set_defaults(host="127.0.0.1")
    probe.add_argument(
        "--port",
        type=int,
        default=7497,
        help="paper TWS socket port (default: 7497)",
    )
    probe.add_argument(
        "--client-id",
        type=int,
        default=17,
        help="nonzero API client ID (default: 17)",
    )
    probe.add_argument(
        "--account",
        required=True,
        help="exact paper account ID expected from TWS",
    )
    probe.add_argument(
        "--con-id",
        type=int,
        help="existing long option conId; required if more than one is held",
    )
    probe.add_argument(
        "--manual-order-perm-id",
        type=int,
        help="permId of a manual paper-TWS option order that must be visible",
    )
    probe.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="overall probe timeout in seconds (default: 10)",
    )
    snapshot = commands.add_parser(
        "snapshot",
        help="capture one coherent read-only paper-TWS snapshot",
    )
    snapshot.set_defaults(host="127.0.0.1")
    snapshot.add_argument(
        "--port",
        type=int,
        default=7497,
        help="paper TWS socket port (default: 7497)",
    )
    snapshot.add_argument(
        "--client-id",
        type=int,
        default=17,
        help="nonzero API client ID (default: 17)",
    )
    snapshot.add_argument(
        "--account",
        required=True,
        help="exact paper account ID expected from TWS",
    )
    snapshot.add_argument(
        "--con-id",
        required=True,
        type=int,
        help="existing long option contract ID",
    )
    snapshot.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="overall snapshot timeout in seconds (default: 10)",
    )
    snapshot.add_argument(
        "--max-age",
        type=float,
        default=5.0,
        help="maximum snapshot age in seconds (default: 5)",
    )
    snapshot.add_argument(
        "--diagnostic",
        action="store_true",
        help="include local contract and order details; account remains redacted",
    )
    return parser


def report_to_dict(report: ProbeReport) -> dict[str, Any]:
    observation = report.observation
    accounts = observation.managed_accounts
    redacted_accounts = [_redact_account(account) for account in accounts]

    def redact_message(message: str) -> str:
        result = message
        for account, redacted in zip(accounts, redacted_accounts, strict=True):
            result = result.replace(account, redacted)
        return result

    return {
        "status": "PASS" if report.passed else "BLOCKED",
        "connected": observation.connected,
        "server_version": observation.server_version,
        "server_time_received": observation.server_time_received,
        "read_only_api": observation.read_only_api,
        "localhost_only": observation.localhost_only,
        "managed_accounts": redacted_accounts,
        "positions_complete": observation.positions_complete,
        "open_orders_complete": observation.open_orders_complete,
        "option_con_id": observation.option_con_id,
        "contract_details_count": observation.contract_details_count,
        "quote_received": observation.quote_received,
        "market_rule_received": observation.market_rule_received,
        "observed_order_perm_ids": list(observation.observed_order_perm_ids),
        "blocking_errors": [
            redact_message(message) for message in observation.blocking_errors
        ],
        "blockers": [redact_message(message) for message in report.blockers],
    }


def snapshot_result_to_dict(
    result: SnapshotResult,
    expected_account: str,
    *,
    diagnostic: bool = False,
) -> dict[str, Any]:
    redacted_account = _redact_account(expected_account)

    def redact_message(message: str) -> str:
        return message.replace(expected_account, redacted_account)

    output: dict[str, Any] = {
        "status": result.status.value,
        "account": redacted_account,
        "errors": [redact_message(message) for message in result.errors],
    }
    snapshot = result.snapshot
    if snapshot is None:
        return output

    output.update(
        {
            "connected": snapshot.connected,
            "read_only_api": snapshot.read_only_api,
            "localhost_only": snapshot.localhost_only,
            "paper_account_verified": snapshot.paper_account_verified,
            "complete": snapshot.complete,
            "fresh": snapshot.fresh,
            "connection_epoch": snapshot.connection_epoch,
            "server_version": snapshot.server_version,
            "server_time": snapshot.server_time,
            "captured_at": str(snapshot.captured_at),
            "completion_times": {
                name: str(completed_at)
                for name, completed_at in snapshot.completion_times
            },
            "option_con_id": snapshot.contract.con_id,
            "position": {
                "quantity": str(snapshot.position.quantity),
                "raw_average_cost": str(snapshot.position.raw_average_cost),
                "unit_basis": str(snapshot.position.unit_basis),
            },
            "quote": {
                "bid": _decimal_text(snapshot.quote.bid),
                "ask": _decimal_text(snapshot.quote.ask),
                "last": _decimal_text(snapshot.quote.last),
                "close": _decimal_text(snapshot.quote.close),
                "market_data_type": snapshot.quote.market_data_type,
                "fresh": snapshot.quote.fresh,
                "observed_at": str(snapshot.quote.observed_at),
            },
            "market_rule": {
                "exchange": snapshot.market_rule.exchange,
                "bands": [
                    {
                        "low_edge": str(band.low_edge),
                        "increment": str(band.increment),
                    }
                    for band in snapshot.market_rule.bands
                ],
            },
            "working_order_count": len(snapshot.working_orders),
        }
    )
    if diagnostic:
        output["contract"] = {
            "sec_type": snapshot.contract.sec_type,
            "expiry": snapshot.contract.expiry,
            "strike": str(snapshot.contract.strike),
            "right": snapshot.contract.right,
            "multiplier": str(snapshot.contract.multiplier),
            "currency": snapshot.contract.currency,
            "trading_class": snapshot.contract.trading_class,
            "exchange": snapshot.contract.exchange,
            "local_symbol": snapshot.contract.local_symbol,
        }
        output["working_orders"] = [
            {
                "perm_id": order.perm_id,
                "client_id": order.client_id,
                "order_id": order.order_id,
                "con_id": order.key.con_id,
                "action": order.action,
                "order_type": order.order_type,
                "remaining": str(order.remaining),
                "status": order.status,
                "oca_group": order.oca_group,
                "parent_id": order.parent_id,
                "observed_at": str(order.observed_at),
            }
            for order in snapshot.working_orders
        ]
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "snapshot":
            request = SnapshotRequest(
                host=args.host,
                port=args.port,
                client_id=args.client_id,
                expected_account=args.account,
                option_con_id=args.con_id,
                timeout_seconds=args.timeout,
            )
            coordinator = SnapshotCoordinator(
                IbkrSnapshotBroker(),
                max_age_seconds=Decimal(str(args.max_age)),
                clock=_now_decimal,
            )
            result = coordinator.refresh(request)
            print(
                json.dumps(
                    snapshot_result_to_dict(
                        result,
                        args.account,
                        diagnostic=args.diagnostic,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0 if result.status is SnapshotStatus.READY else 2

        config = ProbeConfig(
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            expected_account=args.account,
            timeout_seconds=args.timeout,
            option_con_id=args.con_id,
            expected_manual_order_perm_id=args.manual_order_perm_id,
        )
        report = run_capability_probe(config, IbkrReadOnlyBroker())
    except (IbapiUnavailableError, ValueError) as error:
        print(json.dumps({"status": "BLOCKED", "error": str(error)}, indent=2))
        return 2

    print(json.dumps(report_to_dict(report), indent=2, sort_keys=True))
    return 0 if report.passed else 2


def _redact_account(account: str) -> str:
    if len(account) <= 4:
        return "****"
    return f"***{account[-4:]}"


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _now_decimal() -> Decimal:
    return Decimal(str(monotonic()))


if __name__ == "__main__":
    sys.exit(main())
