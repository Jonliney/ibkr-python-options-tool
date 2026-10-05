import pytest
from PySide6.QtCore import QSettings

from ibkr_options_manager.app import account_preferences


def test_paper_account_round_trips_in_local_preferences(tmp_path, monkeypatch) -> None:
    path = tmp_path / "account.ini"
    monkeypatch.setattr(
        account_preferences,
        "_preferences",
        lambda: QSettings(str(path), QSettings.Format.IniFormat),
    )

    assert account_preferences.load_saved_account() == ""
    account_preferences.save_paper_account(" du1234567 ")
    assert account_preferences.load_saved_account() == "DU1234567"


def test_non_paper_account_is_neither_saved_nor_loaded(tmp_path, monkeypatch) -> None:
    path = tmp_path / "account.ini"
    monkeypatch.setattr(
        account_preferences,
        "_preferences",
        lambda: QSettings(str(path), QSettings.Format.IniFormat),
    )

    with pytest.raises(ValueError, match="paper account"):
        account_preferences.save_paper_account("U1234567")
    assert account_preferences.load_saved_account() == ""
