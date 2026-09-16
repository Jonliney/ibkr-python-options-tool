import unittest
from types import SimpleNamespace

from ibkr_options_manager.ibkr_probe import (
    IbkrReadOnlyBroker,
    extract_api_safety_settings,
)


class IbkrReadOnlyBrokerTests(unittest.TestCase):
    def test_module_is_importable_without_ibapi_installed(self) -> None:
        broker = IbkrReadOnlyBroker()

        self.assertTrue(callable(broker.observe))

    def test_extracts_safety_flags_from_official_config_response(self) -> None:
        response = SimpleNamespace(
            api=SimpleNamespace(
                settings=SimpleNamespace(
                    readOnlyApi=True,
                    allowLocalhostOnly=True,
                )
            )
        )

        settings = extract_api_safety_settings(response)

        self.assertEqual(settings, (True, True))


if __name__ == "__main__":
    unittest.main()
