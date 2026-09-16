import unittest

from ibkr_options_manager.capability import (
    ProbeConfig,
    ProbeObservation,
    assess_capabilities,
    run_capability_probe,
)


class ProbeConfigTests(unittest.TestCase):
    def test_accepts_only_loopback_with_nonzero_client_id(self) -> None:
        config = ProbeConfig(
            host="127.0.0.1",
            port=7497,
            client_id=17,
            expected_account="DU1234567",
        )

        self.assertEqual(config.host, "127.0.0.1")

        for host in ("localhost", "192.0.2.1", "0.0.0.0"):
            with (
                self.subTest(host=host),
                self.assertRaisesRegex(ValueError, "literal loopback"),
            ):
                ProbeConfig(
                    host=host,
                    port=7497,
                    client_id=17,
                    expected_account="DU1234567",
                )

        with self.assertRaisesRegex(ValueError, "nonzero"):
            ProbeConfig(
                host="127.0.0.1",
                port=7497,
                client_id=0,
                expected_account="DU1234567",
            )

        with self.assertRaisesRegex(ValueError, "paper account"):
            ProbeConfig(
                host="127.0.0.1",
                port=7496,
                client_id=17,
                expected_account="U1234567",
            )


class CapabilityAssessmentTests(unittest.TestCase):
    def test_passes_only_when_every_read_only_capability_is_observed(self) -> None:
        config = ProbeConfig(
            host="127.0.0.1",
            port=7497,
            client_id=17,
            expected_account="DU1234567",
            expected_manual_order_perm_id=9001,
        )
        observation = ProbeObservation(
            connected=True,
            server_version=204,
            server_time_received=True,
            read_only_api=True,
            localhost_only=True,
            managed_accounts=("DU1234567",),
            positions_complete=True,
            open_orders_complete=True,
            option_con_id=123456,
            contract_details_count=1,
            quote_received=True,
            market_rule_received=True,
            observed_order_perm_ids=(9001,),
            blocking_errors=(),
        )

        report = assess_capabilities(config, observation)

        self.assertTrue(report.passed)
        self.assertEqual(report.blockers, ())

    def test_blocks_unknown_read_only_state_and_unseen_manual_order(self) -> None:
        config = ProbeConfig(
            host="::1",
            port=7497,
            client_id=17,
            expected_account="DU1234567",
            expected_manual_order_perm_id=9001,
        )
        observation = ProbeObservation(
            connected=True,
            server_version=204,
            server_time_received=True,
            read_only_api=None,
            localhost_only=True,
            managed_accounts=("DU1234567",),
            positions_complete=True,
            open_orders_complete=True,
            option_con_id=123456,
            contract_details_count=1,
            quote_received=True,
            market_rule_received=True,
            observed_order_perm_ids=(),
            blocking_errors=(),
        )

        report = assess_capabilities(config, observation)

        self.assertFalse(report.passed)
        self.assertEqual(
            report.blockers,
            (
                "TWS did not prove that API read-only mode is enabled",
                "expected manual TWS order permId 9001 was not visible",
            ),
        )

    def test_runs_through_the_read_only_broker_seam(self) -> None:
        config = ProbeConfig(
            host="127.0.0.1",
            port=7497,
            client_id=17,
            expected_account="DU1234567",
            expected_manual_order_perm_id=9001,
        )
        observation = ProbeObservation(
            connected=True,
            server_version=204,
            server_time_received=True,
            read_only_api=True,
            localhost_only=True,
            managed_accounts=("DU1234567",),
            positions_complete=True,
            open_orders_complete=True,
            option_con_id=123456,
            contract_details_count=1,
            quote_received=True,
            market_rule_received=True,
            observed_order_perm_ids=(9001,),
            blocking_errors=(),
        )

        class FakeReadOnlyBroker:
            def __init__(self) -> None:
                self.received: ProbeConfig | None = None

            def observe(self, received: ProbeConfig) -> ProbeObservation:
                self.received = received
                return observation

        broker = FakeReadOnlyBroker()

        report = run_capability_probe(config, broker)

        self.assertTrue(report.passed)
        self.assertIs(broker.received, config)


if __name__ == "__main__":
    unittest.main()
