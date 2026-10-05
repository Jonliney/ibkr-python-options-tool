"""Store the paper account identifier used to prefill the desktop connection."""

from PySide6.QtCore import QSettings

_ACCOUNT_KEY = "connection/paper_account"


def _preferences() -> QSettings:
    return QSettings("Local", "IBKR Options Manager")


def load_saved_account() -> str:
    value = _preferences().value(_ACCOUNT_KEY, "")
    account = value.strip() if isinstance(value, str) else ""
    return account.upper() if account.upper().startswith("DU") else ""


def save_paper_account(account: str) -> None:
    account = account.strip().upper()
    if not account.startswith("DU"):
        raise ValueError("A paper account ID starting with DU is required")
    settings = _preferences()
    settings.setValue(_ACCOUNT_KEY, account)
    settings.sync()
    if settings.status() != QSettings.Status.NoError:
        raise OSError("Could not save the paper account ID")
