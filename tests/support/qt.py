"""Bounded browser readiness checks driven by observable JavaScript state."""

from time import monotonic

from PySide6.QtCore import QTimer


def wait_for_javascript(view, script, callback, *, timeout=3):
    deadline = monotonic() + timeout

    def inspect():
        view.page().runJavaScript(script, observed)

    def observed(value):
        if value or monotonic() >= deadline:
            callback(value)
        else:
            QTimer.singleShot(10, inspect)

    inspect()
