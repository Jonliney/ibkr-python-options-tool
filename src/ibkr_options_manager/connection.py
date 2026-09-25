from __future__ import annotations

from ipaddress import ip_address


def validate_paper_connection(
    *,
    host: str,
    port: int,
    client_id: int,
    expected_account: str,
    timeout_seconds: float,
) -> None:
    """Enforce the connection envelope shared by every paper-TWS reader."""
    try:
        address = ip_address(host)
    except ValueError as error:
        raise ValueError("host must be a literal loopback address") from error
    if not address.is_loopback:
        raise ValueError("host must be a literal loopback address")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if client_id <= 0:
        raise ValueError("client_id must be positive and nonzero")
    if not expected_account.strip():
        raise ValueError("expected_account is required")
    if not expected_account.strip().upper().startswith("DU"):
        raise ValueError("expected_account must be a paper account ID")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")


__all__ = ["validate_paper_connection"]
