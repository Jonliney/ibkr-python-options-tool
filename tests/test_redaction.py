import pytest

from ibkr_options_manager.redaction import redact_accounts


@pytest.mark.parametrize(
    "message,accounts,expected",
    [
        ("DU1234567 DU1234567", ("DU1234567",), "***4567 ***4567"),
        ("DU1234567 DU1234", ("DU1234", "DU1234567"), "***4567 ***1234"),
        ("DU1 DU12", ("DU1", "DU12", ""), "**** ****"),
    ],
)
def test_account_redaction_handles_overlap_repetition_and_short_ids(
    message, accounts, expected
):
    assert redact_accounts(message, accounts) == expected
