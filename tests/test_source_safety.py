import ast
import unittest
from functools import cache
from importlib.util import resolve_name
from pathlib import Path

import pytest

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
                "submit_market",
                "submit_trailing",
                "change_trailing",
                "cancel_pair",
                "cancel_market",
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
            tree = _source_tree(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _called_name(node.func)
                if name in _FORBIDDEN_CALLS:
                    violations.append(f"{source.name}:{node.lineno}:{name}")

        self.assertEqual(violations, [])

    def test_writer_uses_explicit_cancel_arguments_and_no_global_cancel(self) -> None:
        package = Path(__file__).parents[1] / "src" / "ibkr_options_manager"
        writer = package / "broker" / "execution.py"
        tree = _source_tree(writer)
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
            if isinstance(call, ast.Call) and _called_name(call.func) == "cancelOrder"
        ]
        self.assertTrue(cancel_calls)
        self.assertTrue(all(len(call.args) == 2 for call in cancel_calls))
        self.assertTrue(
            all(
                isinstance(call.args[1], ast.Call)
                and _called_name(call.args[1].func) == "OrderCancel"
                for call in cancel_calls
            )
        )

    def test_only_leased_adapters_and_nonzero_listener_request_own_orders(self) -> None:
        package = Path(__file__).parents[1] / "src" / "ibkr_options_manager"
        allowed_sources = {
            package / "broker" / "ibkr.py",
            package / "broker" / "execution.py",
            # Observation-only same-submitter listener: client 0 is rejected
            # at construction and its lease is released before captures/writes.
            package / "order_observation.py",
        }
        violations: list[str] = []

        for source in sorted(package.rglob("*.py")):
            if source in allowed_sources:
                continue
            tree = _source_tree(source)
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
            tree = _source_tree(source)
            package = ".".join(source.parent.relative_to(domain.parent.parent).parts)
            violations.extend(_domain_import_violations(tree, package, source.name))

        self.assertEqual(violations, [])


def _called_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


@cache
def _source_tree(source: Path) -> ast.Module:
    return ast.parse(source.read_text(), filename=str(source))


def _domain_import_violations(tree, package, filename):
    forbidden = (
        "ibapi",
        "PySide6",
        "starhtml",
        "starlette",
        "uvicorn",
        "ibkr_options_manager.app",
        "ibkr_options_manager.broker",
        "ibkr_options_manager.execution",
        "ibkr_options_manager.ibkr_probe",
    )
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            modules = [module, *(f"{module}.{a.name}" for a in node.names)]
        else:
            continue
        for module in modules:
            if any(
                module == root or module.startswith(root + ".") for root in forbidden
            ):
                violations.append(f"{filename}:{node.lineno}:{module}")
    return violations


@pytest.mark.parametrize(
    "statement",
    [
        "import os, ibapi.client",
        "from ..broker import IbkrSnapshotBroker",
        "from .. import app",
        "from ibkr_options_manager import execution",
        "from PySide6 import QtWidgets",
    ],
)
def test_domain_guard_detects_transport_and_gui_import_forms(statement):
    assert _domain_import_violations(
        ast.parse(statement), "ibkr_options_manager.domain", "synthetic.py"
    )


def test_domain_guard_allows_stdlib_and_domain_imports():
    assert not _domain_import_violations(
        ast.parse("import decimal, typing\nfrom .model import BrokerSnapshot"),
        "ibkr_options_manager.domain",
        "synthetic.py",
    )


if __name__ == "__main__":
    unittest.main()
