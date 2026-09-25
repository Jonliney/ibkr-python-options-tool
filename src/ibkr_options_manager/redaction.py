from __future__ import annotations

from collections.abc import Iterable


def redact_account(account: str) -> str:
    """Return the stable account representation used at every output seam."""
    return "****" if len(account) <= 4 else f"***{account[-4:]}"


def redact_accounts(message: str, accounts: Iterable[str]) -> str:
    """Remove every known account ID from a broker or application message."""
    result = message
    known_accounts = {value for value in accounts if value}
    for account in sorted(known_accounts, key=len, reverse=True):
        result = result.replace(account, redact_account(account))
    return result


__all__ = ["redact_account", "redact_accounts"]
