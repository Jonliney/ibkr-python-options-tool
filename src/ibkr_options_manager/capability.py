from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    """Connection inputs that cannot be widened beyond the paper-TWS probe."""

    host: str
    port: int
    client_id: int
    expected_account: str
    timeout_seconds: float = 10.0
    option_con_id: int | None = None
    expected_manual_order_perm_id: int | None = None

    def __post_init__(self) -> None:
        try:
            address = ip_address(self.host)
        except ValueError as error:
            raise ValueError("host must be a literal loopback address") from error
        if not address.is_loopback:
            raise ValueError("host must be a literal loopback address")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.client_id == 0:
            raise ValueError("client_id must be nonzero to avoid binding TWS orders")
        if self.client_id < 0:
            raise ValueError("client_id must be positive")
        if not self.expected_account.strip():
            raise ValueError("expected_account is required")
        if not self.expected_account.strip().upper().startswith("DU"):
            raise ValueError("expected_account must be a paper account ID")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.option_con_id is not None and self.option_con_id <= 0:
            raise ValueError("option_con_id must be positive")
        if (
            self.expected_manual_order_perm_id is not None
            and self.expected_manual_order_perm_id <= 0
        ):
            raise ValueError("expected_manual_order_perm_id must be positive")


@dataclass(frozen=True, slots=True)
class ProbeObservation:
    connected: bool
    server_version: int | None
    server_time_received: bool
    read_only_api: bool | None
    localhost_only: bool | None
    managed_accounts: tuple[str, ...]
    positions_complete: bool
    open_orders_complete: bool
    option_con_id: int | None
    contract_details_count: int
    quote_received: bool
    market_rule_received: bool
    observed_order_perm_ids: tuple[int, ...]
    blocking_errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProbeReport:
    observation: ProbeObservation
    blockers: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.blockers


class ReadOnlyBroker(Protocol):
    """The only broker seam available to the capability probe."""

    def observe(self, config: ProbeConfig) -> ProbeObservation:
        """Collect a completed read-only observation or fail closed."""


def assess_capabilities(
    config: ProbeConfig, observation: ProbeObservation
) -> ProbeReport:
    """Turn raw probe evidence into a fail-closed compatibility decision."""

    blockers: list[str] = []
    if not observation.connected:
        blockers.append("paper TWS connection was not established")
    if observation.server_version is None:
        blockers.append("TWS server version was not observed")
    if not observation.server_time_received:
        blockers.append("TWS server time was not received")
    if observation.read_only_api is not True:
        blockers.append("TWS did not prove that API read-only mode is enabled")
    if observation.localhost_only is not True:
        blockers.append("TWS did not prove that localhost-only mode is enabled")
    if config.expected_account not in observation.managed_accounts:
        blockers.append(
            f"expected paper account {config.expected_account} was not managed"
        )
    if not observation.positions_complete:
        blockers.append("position snapshot did not complete")
    if not observation.open_orders_complete:
        blockers.append("open-order snapshot did not complete")
    if observation.option_con_id is None:
        blockers.append("no eligible long option position was observed")
    if observation.contract_details_count != 1:
        blockers.append("option contract did not resolve to exactly one contract")
    if not observation.quote_received:
        blockers.append("option quote snapshot was not received")
    if not observation.market_rule_received:
        blockers.append("option market rule was not received")
    expected_perm_id = config.expected_manual_order_perm_id
    if expected_perm_id is None:
        blockers.append("an expected manual TWS order permId was not supplied")
    elif expected_perm_id not in observation.observed_order_perm_ids:
        blockers.append(
            f"expected manual TWS order permId {expected_perm_id} was not visible"
        )
    blockers.extend(observation.blocking_errors)
    return ProbeReport(observation=observation, blockers=tuple(blockers))


def run_capability_probe(config: ProbeConfig, broker: ReadOnlyBroker) -> ProbeReport:
    return assess_capabilities(config, broker.observe(config))
