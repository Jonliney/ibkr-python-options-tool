import ast
import unittest
from pathlib import Path

from ibkr_options_manager.broker import IbkrSnapshotBroker
from ibkr_options_manager.ibkr_probe import IbkrReadOnlyBroker

_FORBIDDEN_CALLS = {
    "cancelOrder",
    "exerciseOptions",
    "placeOrder",
    "reqAutoOpenOrders",
    "reqGlobalCancel",
    "reqOpenOrders",
}


class ProductionSourceSafetyTests(unittest.TestCase):
    def test_broker_interface_exposes_only_observation(self) -> None:
        public_methods = {
            name
            for name, value in vars(IbkrReadOnlyBroker).items()
            if not name.startswith("_") and callable(value)
        }

        self.assertEqual(public_methods, {"observe"})

    def test_snapshot_broker_interface_exposes_only_capture(self) -> None:
        public_methods = {
            name
            for name, value in vars(IbkrSnapshotBroker).items()
            if not name.startswith("_") and callable(value)
        }

        self.assertEqual(public_methods, {"capture"})

    def test_production_source_never_calls_a_forbidden_order_method(self) -> None:
        package = Path(__file__).parents[1] / "src" / "ibkr_options_manager"
        violations: list[str] = []
        sources = sorted(package.rglob("*.py"))

        self.assertTrue(sources, "production source scan must not be empty")

        for source in sources:
            tree = ast.parse(source.read_text(), filename=str(source))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _called_name(node.func)
                if name in _FORBIDDEN_CALLS:
                    violations.append(f"{source.name}:{node.lineno}:{name}")

        self.assertEqual(violations, [])

    def test_domain_has_no_broker_or_gui_dependencies(self) -> None:
        domain = Path(__file__).parents[1] / "src" / "ibkr_options_manager" / "domain"
        violations: list[str] = []

        for source in sorted(domain.rglob("*.py")):
            tree = ast.parse(source.read_text(), filename=str(source))
            for node in ast.walk(tree):
                module = _imported_module(node)
                if module and module.split(".", 1)[0] in {"ibapi", "PySide6"}:
                    violations.append(f"{source.name}:{node.lineno}:{module}")

        self.assertEqual(violations, [])


def _called_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _imported_module(node: ast.AST) -> str | None:
    if isinstance(node, ast.ImportFrom):
        return node.module
    if isinstance(node, ast.Import) and node.names:
        return node.names[0].name
    return None


if __name__ == "__main__":
    unittest.main()
