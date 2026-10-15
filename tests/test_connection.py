import pytest

from ibkr_options_manager.connection import validate_paper_connection


@pytest.mark.parametrize(
    "field,value",
    [
        ("port", 0),
        ("port", 65536),
        ("expected_account", " "),
        ("timeout_seconds", 0),
        ("timeout_seconds", -1),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", float("inf")),
    ],
)
def test_invalid_connection_envelope_fails_closed(field, value):
    settings = dict(
        host="127.0.0.1",
        port=7497,
        client_id=17,
        expected_account="DU1234567",
        timeout_seconds=1,
    )
    settings[field] = value
    with pytest.raises(ValueError):
        validate_paper_connection(**settings)


@pytest.mark.parametrize("port", [1, 65535])
def test_valid_port_boundaries_are_accepted(port):
    validate_paper_connection(
        host="127.0.0.1",
        port=port,
        client_id=17,
        expected_account="DU1234567",
        timeout_seconds=1,
    )
