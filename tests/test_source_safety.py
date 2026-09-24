import ast
import unittest
from pathlib import Path

from ibkr_options_manager.broker import IbkrPaperExecutionBroker, IbkrSnapshotBroker
from ibkr_options_manager.ibkr_probe import IbkrReadOnlyBroker

_FORBIDDEN_CALLS = {
    "cancelOrder",
    "exerciseOptions",
    "placeOrder",
    "reqAutoOpenOrders",
    "reqGlobalCancel",
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

    def test_paper_writer_exposes_only_explicit_paper_write_methods(self) -> None:
        public_methods = {
            name
            for name, value in vars(IbkrPaperExecutionBroker).items()
            if not name.startswith("_") and callable(value)
        }

        self.assertEqual(
            public_methods,
            {
                "submit",
                "cancel_pair",
                "cancel_pair_then_submit_market",
                "cancel_pairs_then_submit_market",
                "modify_prices",
            },
        )

    def test_production_source_never_calls_a_forbidden_order_method(self) -> None:
        package = Path(__file__).parents[1] / "src" / "ibkr_options_manager"
        violations: list[str] = []
        sources = sorted(package.rglob("*.py"))

        self.assertTrue(sources, "production source scan must not be empty")

        writer = package / "broker" / "execution.py"
        self.assertTrue(writer.is_file(), "the isolated paper writer must exist")
        for source in sources:
            if source == writer:
                continue
            tree = ast.parse(source.read_text(), filename=str(source))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _called_name(node.func)
                if name in _FORBIDDEN_CALLS:
                    violations.append(f"{source.name}:{node.lineno}:{name}")

        self.assertEqual(violations, [])

    def test_isolated_writer_cancels_only_the_explicit_selected_pair(self) -> None:
        package = Path(__file__).parents[1] / "src" / "ibkr_options_manager"
        writer = package / "broker" / "execution.py"
        tree = ast.parse(writer.read_text(), filename=str(writer))
        calls = {
            _called_name(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }

        self.assertIn("placeOrder", calls)
        self.assertIn("cancelOrder", calls)
        self.assertIn("reqOpenOrders", calls)
        self.assertNotIn("reqGlobalCancel", calls)
        self.assertNotIn("reqAutoOpenOrders", calls)

        cancel_calls = [
            call
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and _called_name(call.func) == "cancelOrder"
        ]
        cancel_arguments = {
            _attribute_name(call.args[0])
            for call in cancel_calls
            if len(call.args) == 2
        }
        self.assertEqual(cancel_arguments, {None})
        self.assertTrue(
            all(
                isinstance(call.args[1], ast.Call)
                and _called_name(call.args[1].func) == "OrderCancel"
                for call in cancel_calls
            )
        )

    def test_only_the_snapshot_and_writer_may_request_client_bound_orders(self) -> None:
        package = Path(__file__).parents[1] / "src" / "ibkr_options_manager"
        allowed_sources = {
            package / "broker" / "ibkr.py",
            package / "broker" / "execution.py",
        }
        violations: list[str] = []

        for source in sorted(package.rglob("*.py")):
            if source in allowed_sources:
                continue
            tree = ast.parse(source.read_text(), filename=str(source))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and _called_name(node.func) == "reqOpenOrders"
                ):
                    violations.append(f"{source.name}:{node.lineno}:reqOpenOrders")

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


def _attribute_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _imported_module(node: ast.AST) -> str | None:
    if isinstance(node, ast.ImportFrom):
        return node.module
    if isinstance(node, ast.Import) and node.names:
        return node.names[0].name
    return None


if __name__ == "__main__":
    unittest.main()
