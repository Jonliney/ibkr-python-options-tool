from dataclasses import replace
from decimal import Decimal

from ibkr_options_manager.broker import (
    REQUIRED_COMPLETIONS,
    BrokerCapture,
    CapturedContract,
    CapturedMarketRule,
    CapturedOrder,
    CapturedPosition,
    CapturedQuote,
    SnapshotRequest,
)
from ibkr_options_manager.domain import PriceBand
from ibkr_options_manager.snapshot import SnapshotCoordinator, SnapshotStatus


def complete_capture() -> BrokerCapture:
    contract = CapturedContract(
        con_id=917864414,
        sec_type="OPT",
        expiry="20260916",
        strike=Decimal("7605"),
        right="C",
        multiplier=Decimal("100"),
        currency="USD",
        trading_class="SPXW",
        exchange="SMART",
        local_symbol="SPXW  260916C07605000",
    )
    return BrokerCapture(
        connection_epoch=7,
        connected=True,
        server_version=223,
        server_time=1_789_569_600,
        read_only_api=True,
        localhost_only=True,
        managed_accounts=("DU1234567",),
        positions=(
            CapturedPosition(
                account="DU1234567",
                contract=contract,
                quantity=Decimal("10"),
                average_cost=Decimal("100"),
            ),
        ),
        orders=(
            CapturedOrder(
                perm_id=1197098753,
                client_id=0,
                order_id=0,
                account="DU1234567",
                con_id=917864414,
                action="SELL",
                order_type="LMT",
                remaining=Decimal("1"),
                status="Submitted",
                oca_group=None,
                parent_id=0,
            ),
        ),
        contract_details=(contract,),
        quote=CapturedQuote(
            bid=Decimal("0.95"),
            ask=Decimal("1.05"),
            last=Decimal("1.00"),
            close=Decimal("0.90"),
            market_data_type="DELAYED",
            observed_at=Decimal("100.0"),
        ),
        market_rule=CapturedMarketRule(
            exchange="SMART",
            bands=(PriceBand(Decimal("0"), Decimal("0.05")),),
        ),
        completed=REQUIRED_COMPLETIONS,
        completion_times=tuple(
            (name, Decimal("100.0")) for name in sorted(REQUIRED_COMPLETIONS)
        ),
        errors=(),
        captured_at=Decimal("100.0"),
    )


class FakeReadOnlyBroker:
    def __init__(self, capture: BrokerCapture) -> None:
        self.capture_value = capture
        self.received: SnapshotRequest | None = None

    def capture(self, request: SnapshotRequest) -> BrokerCapture:
        self.received = request
        return self.capture_value


def request() -> SnapshotRequest:
    return SnapshotRequest(
        host="127.0.0.1",
        port=7497,
        client_id=17,
        expected_account="DU1234567",
        option_con_id=917864414,
        timeout_seconds=10,
    )


def test_snapshot_request_rejects_a_non_paper_account_id() -> None:
    try:
        SnapshotRequest(
            host="127.0.0.1",
            port=7496,
            client_id=17,
            expected_account="U1234567",
            option_con_id=917864414,
        )
    except ValueError as error:
        assert str(error) == "expected_account must be a paper account ID"
    else:
        raise AssertionError("a non-paper account ID must fail closed")


def test_complete_capture_publishes_one_immutable_domain_snapshot() -> None:
    broker = FakeReadOnlyBroker(complete_capture())
    coordinator = SnapshotCoordinator(
        broker, max_age_seconds=Decimal("5"), clock=lambda: Decimal("101")
    )

    result = coordinator.refresh(request())

    assert result.status is SnapshotStatus.READY
    assert result.errors == ()
    assert result.snapshot is not None
    assert result.snapshot.connection_epoch == 7
    assert result.snapshot.server_version == 223
    assert result.snapshot.server_time == 1_789_569_600
    assert result.snapshot.position.unit_basis == Decimal("1")
    assert result.snapshot.contract.con_id == 917864414
    assert result.snapshot.working_orders[0].perm_id == 1197098753
    assert result.snapshot.quote.market_data_type == "DELAYED"
    assert broker.received == request()
    assert coordinator.current() == result


def test_incomplete_refresh_invalidates_previous_snapshot_and_redacts_errors() -> None:
    broker = FakeReadOnlyBroker(complete_capture())
    coordinator = SnapshotCoordinator(
        broker, max_age_seconds=Decimal("5"), clock=lambda: Decimal("101")
    )
    assert coordinator.refresh(request()).status is SnapshotStatus.READY
    broker.capture_value = replace(
        complete_capture(),
        completed=REQUIRED_COMPLETIONS - {"quote"},
        errors=("account DU1234567 quote request timed out",),
    )

    result = coordinator.refresh(request())

    assert result.status is SnapshotStatus.BLOCKED
    assert result.snapshot is None
    assert result.errors == (
        "missing completion barriers: quote",
        "account ***4567 quote request timed out",
    )
    assert coordinator.current() == result


def test_position_and_contract_details_identity_must_match_exactly() -> None:
    capture = complete_capture()
    mismatched_position = replace(
        capture.positions[0],
        contract=replace(capture.positions[0].contract, trading_class="SPX"),
    )
    broker = FakeReadOnlyBroker(replace(capture, positions=(mismatched_position,)))
    coordinator = SnapshotCoordinator(
        broker, max_age_seconds=Decimal("5"), clock=lambda: Decimal("101")
    )

    result = coordinator.refresh(request())

    assert result.status is SnapshotStatus.BLOCKED
    assert result.snapshot is None
    assert result.errors == (
        "position contract identity does not match contract details",
    )


def test_current_snapshot_expires_without_returning_cached_state() -> None:
    now = Decimal("101")
    broker = FakeReadOnlyBroker(complete_capture())
    coordinator = SnapshotCoordinator(
        broker, max_age_seconds=Decimal("5"), clock=lambda: now
    )
    assert coordinator.refresh(request()).status is SnapshotStatus.READY

    now = Decimal("105.001")
    result = coordinator.current()

    assert result.status is SnapshotStatus.STALE
    assert result.snapshot is None
    assert result.errors == ("snapshot exceeded its freshness limit",)


def test_invalid_multiplier_blocks_normalization_instead_of_raising() -> None:
    capture = complete_capture()
    invalid_contract = replace(capture.contract_details[0], multiplier=Decimal("0"))
    invalid_position = replace(capture.positions[0], contract=invalid_contract)
    broker = FakeReadOnlyBroker(
        replace(
            capture,
            positions=(invalid_position,),
            contract_details=(invalid_contract,),
        )
    )
    coordinator = SnapshotCoordinator(
        broker, max_age_seconds=Decimal("5"), clock=lambda: Decimal("101")
    )

    result = coordinator.refresh(request())

    assert result.status is SnapshotStatus.BLOCKED
    assert result.snapshot is None
    assert result.errors == ("contract multiplier is invalid",)


def test_callback_order_does_not_change_the_published_snapshot() -> None:
    capture = complete_capture()
    second_order = replace(
        capture.orders[0],
        perm_id=1197098754,
        order_id=1,
        remaining=Decimal("2"),
    )
    first = replace(capture, orders=(second_order, capture.orders[0]))
    second = replace(capture, orders=(capture.orders[0], second_order))
    broker = FakeReadOnlyBroker(first)
    coordinator = SnapshotCoordinator(
        broker, max_age_seconds=Decimal("5"), clock=lambda: Decimal("101")
    )

    first_result = coordinator.refresh(request())
    broker.capture_value = second
    second_result = coordinator.refresh(request())

    assert first_result.status is SnapshotStatus.READY
    assert second_result.status is SnapshotStatus.READY
    assert first_result.snapshot == second_result.snapshot
