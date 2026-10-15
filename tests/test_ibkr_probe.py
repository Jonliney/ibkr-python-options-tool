import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from ibkr_options_manager.ibkr_probe import (
    extract_api_safety_settings,
)


class IbkrReadOnlyBrokerTests(unittest.TestCase):
    def test_module_is_importable_without_ibapi_installed(self) -> None:
        source = Path(__file__).parents[1] / "src"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])
class MissingIbapi(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'ibapi' or fullname.startswith('ibapi.'):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, MissingIbapi())
from ibkr_options_manager.ibkr_probe import (
    IbkrReadOnlyBroker, IbapiUnavailableError, _load_ibapi,
)
assert callable(IbkrReadOnlyBroker().observe)
try:
    _load_ibapi()
except IbapiUnavailableError:
    pass
else:
    raise AssertionError('IBAPI unexpectedly available')
""",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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
