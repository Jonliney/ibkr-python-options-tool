import unittest

from ibkr_options_manager.capability import ProbeObservation, ProbeReport
from ibkr_options_manager.cli import (
    build_parser,
    report_to_dict,
    snapshot_result_to_dict,
)
from ibkr_options_manager.snapshot import SnapshotResult, SnapshotStatus


class ProbeCliTests(unittest.TestCase):
    def test_defaults_to_the_paper_tws_port_and_nonzero_client(self) -> None:
        args = build_parser().parse_args(
            [
                "probe",
                "--account",
                "DU1234567",
                "--manual-order-perm-id",
                "9001",
            ]
        )

        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 7497)
        self.assertEqual(args.client_id, 17)

    def test_json_report_redacts_managed_account_ids(self) -> None:
        observation = ProbeObservation(
            connected=False,
            server_version=None,
            server_time_received=False,
            read_only_api=None,
            localhost_only=None,
            managed_accounts=("DU1234567",),
            positions_complete=False,
            open_orders_complete=False,
            option_con_id=None,
            contract_details_count=0,
            quote_received=False,
            market_rule_received=False,
            observed_order_perm_ids=(),
            blocking_errors=("connection failed",),
        )

        result = report_to_dict(ProbeReport(observation, ("blocked",)))

        self.assertEqual(result["managed_accounts"], ["***4567"])
        self.assertNotIn("DU1234567", repr(result))


class SnapshotCliTests(unittest.TestCase):
    def test_snapshot_defaults_to_paper_loopback_and_nonzero_client(self) -> None:
        args = build_parser().parse_args(
            [
                "snapshot",
                "--account",
                "DU1234567",
                "--con-id",
                "917864414",
            ]
        )

        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 7497)
        self.assertEqual(args.client_id, 17)
        self.assertEqual(args.max_age, 5.0)

    def test_blocked_snapshot_output_redacts_the_expected_account(self) -> None:
        result = SnapshotResult(
            SnapshotStatus.BLOCKED,
            None,
            ("account DU1234567 did not match",),
        )

        output = snapshot_result_to_dict(result, "DU1234567")

        self.assertEqual(output["account"], "***4567")
        self.assertEqual(output["errors"], ["account ***4567 did not match"])
        self.assertNotIn("DU1234567", repr(output))


if __name__ == "__main__":
    unittest.main()
