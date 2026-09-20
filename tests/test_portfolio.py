from dataclasses import replace
from decimal import Decimal

from ibkr_options_manager.broker import (
    PORTFOLIO_COMPLETIONS,
    BrokerCapture,
    CapturedContract,
    CapturedOrder,
    CapturedPosition,
    PortfolioRequest,
)
from ibkr_options_manager.portfolio import PortfolioCoordinator, PortfolioStatus


def capture() -> BrokerCapture:
    long_contract = CapturedContract(
        con_id=101,
        sec_type="OPT",
        expiry="20261016",
        strike=Decimal("7000"),
        right="C",
        multiplier=Decimal("100"),
        currency="USD",
        trading_class="SPXW",
        exchange="SMART",
        local_symbol="SPXW  261016C07000000",
    )
    short_contract = replace(
        long_contract,
        con_id=102,
        right="P",
        local_symbol="SPXW  261016P07000000",
    )
    stock_contract = replace(
        long_contract,
        con_id=103,
        sec_type="STK",
        local_symbol="AAPL",
    )
    return BrokerCapture(
        connection_epoch=4,
        connected=True,
        server_version=223,
        server_time=1_789_569_600,
        read_only_api=True,
        localhost_only=True,
        managed_accounts=("DU1234567",),
        positions=(
            CapturedPosition("DU1234567", long_contract, Decimal("3"), Decimal("250")),
            CapturedPosition(
                "DU1234567", short_contract, Decimal("-1"), Decimal("120")
            ),
            CapturedPosition(
                "DU1234567", stock_contract, Decimal("10"), Decimal("200")
            ),
        ),
        orders=(
            CapturedOrder(
                perm_id=9001,
                client_id=17,
                order_id=41,
                account="DU1234567",
                con_id=101,
                action="SELL",
                order_type="LMT",
                remaining=Decimal("1"),
                status="Submitted",
                oca_group=None,
                parent_id=0,
            ),
        ),
        contract_details=(),
        quote=None,
        market_rule=None,
        completed=PORTFOLIO_COMPLETIONS,
        completion_times=tuple(
            (name, Decimal("100")) for name in sorted(PORTFOLIO_COMPLETIONS)
        ),
        errors=(),
        captured_at=Decimal("100"),
    )


class FakePortfolioBroker:
    def __init__(self, value: BrokerCapture) -> None:
        self.value = value
        self.received: PortfolioRequest | None = None

    def capture(self, request: PortfolioRequest) -> BrokerCapture:
        self.received = request
        return self.value


def request() -> PortfolioRequest:
    return PortfolioRequest(
        host="127.0.0.1",
        port=7497,
        client_id=17,
        expected_account="DU1234567",
        timeout_seconds=10,
    )


def test_portfolio_lists_every_nonzero_option_and_marks_eligibility() -> None:
    broker = FakePortfolioBroker(capture())
    coordinator = PortfolioCoordinator(
        broker,
        max_age_seconds=Decimal("5"),
        clock=lambda: Decimal("101"),
    )

    result = coordinator.refresh(request())

    assert result.status is PortfolioStatus.READY
    assert result.snapshot is not None
    assert [position.key.con_id for position in result.snapshot.positions] == [101, 102]
    long, short = result.snapshot.positions
    assert long.eligible is True
    assert long.unit_basis == Decimal("2.5")
    assert long.working_orders[0].perm_id == 9001
    assert short.eligible is False
    assert short.eligibility == "Short position"
    assert broker.received == request()


def test_portfolio_preserves_the_received_position_order() -> None:
    received = capture()
    broker = FakePortfolioBroker(
        replace(
            received,
            positions=(
                received.positions[1],
                received.positions[0],
                received.positions[2],
            ),
        )
    )
    coordinator = PortfolioCoordinator(
        broker,
        max_age_seconds=Decimal("5"),
        clock=lambda: Decimal("101"),
    )

    result = coordinator.refresh(request())

    assert result.snapshot is not None
    assert [position.key.con_id for position in result.snapshot.positions] == [102, 101]


def test_portfolio_fails_closed_when_safety_evidence_is_missing() -> None:
    broker = FakePortfolioBroker(
        replace(
            capture(),
            read_only_api=False,
            managed_accounts=("DU7654321",),
            errors=("account DU1234567 position request timed out",),
        )
    )
    coordinator = PortfolioCoordinator(
        broker,
        max_age_seconds=Decimal("5"),
        clock=lambda: Decimal("101"),
    )

    result = coordinator.refresh(request())

    assert result.status is PortfolioStatus.BLOCKED
    assert result.snapshot is None
    assert "TWS API read-only mode was not verified" in result.errors
    assert "the configured paper account was not verified" in result.errors
    assert all("DU1234567" not in message for message in result.errors)


def test_portfolio_snapshot_expires_without_returning_cached_positions() -> None:
    now = Decimal("101")
    coordinator = PortfolioCoordinator(
        FakePortfolioBroker(capture()),
        max_age_seconds=Decimal("5"),
        clock=lambda: now,
    )
    assert coordinator.refresh(request()).status is PortfolioStatus.READY

    now = Decimal("105.1")
    result = coordinator.current()

    assert result.status is PortfolioStatus.STALE
    assert result.snapshot is None


def test_portfolio_request_rejects_a_non_paper_account() -> None:
    try:
        PortfolioRequest(
            host="127.0.0.1",
            port=7497,
            client_id=17,
            expected_account="U1234567",
        )
    except ValueError as error:
        assert str(error) == "expected_account must be a paper account ID"
    else:
        raise AssertionError("a non-paper account must fail closed")
