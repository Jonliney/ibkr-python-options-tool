from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ibkr_options_manager.broker.read_only import (
    CapturedContract,
    CapturedExecution,
    PortfolioRequest,
)
from ibkr_options_manager.trade_history import (
    Discovery,
    MinuteBar,
    TradeHistoryCollector,
    TradeHistoryStore,
)

D = Decimal
NOW = int(datetime(2026, 10, 16, 15, 30, tzinfo=UTC).timestamp())
ACCOUNT = "DU1234567"
CONTRACT = CapturedContract(
    123,
    "OPT",
    "20261023",
    D("100"),
    "C",
    D("100"),
    "USD",
    "XYZ",
    "SMART",
    "XYZ C100",
)
FILL = CapturedExecution(
    "fill.01",
    ACCOUNT,
    123,
    11,
    "BOT",
    D("2"),
    D("1.25"),
    "20261016 14:00:00 Europe/London",
    currency="USD",
)
REQUEST = PortfolioRequest("127.0.0.1", 7497, 19, ACCOUNT)


class Source:
    def __init__(self):
        self.discovery = Discovery((CONTRACT,), (FILL,))
        self.calls = []
        self.fail = False

    def discover(self, request):
        return self.discovery

    def history(self, request, contract, start, end):
        self.calls.append((contract.con_id, start, end))
        if self.fail:
            raise RuntimeError("disconnected before historicalDataEnd")
        return (MinuteBar(end - 60, D("2"), D("0"), D("1.5")),)


@pytest.fixture
def store(tmp_path):
    db = TradeHistoryStore(tmp_path / "history.sqlite3")
    yield db
    db.close()


def rows(store, table):
    return store.db.execute(f"SELECT * FROM {table}").fetchall()


def test_completed_minutes_prices_and_deduplication_survive_restart(tmp_path):
    path = tmp_path / "history.sqlite3"
    source = Source()
    store = TradeHistoryStore(path)
    TradeHistoryCollector(store, source).collect(REQUEST, NOW + 35)
    assert len(rows(store, "fills")) == 1
    assert all(r[1] < NOW for r in rows(store, "bars"))
    store.close()
    store = TradeHistoryStore(path)
    try:
        collector = TradeHistoryCollector(store, source)
        collector.collect(REQUEST, NOW + 60)
        collector.collect(REQUEST, NOW + 60)
        assert len(rows(store, "fills")) == 1
        bars = rows(store, "bars")
        assert len({(b[0], b[1], b[5]) for b in bars}) == len(bars)
        assert {b[3] for b in bars} == {"0"}  # zero bid is valid
        assert source.calls[-1][1] == NOW
    finally:
        store.close()


def test_closed_positions_remain_tracked(store):
    source = Source()
    collector = TradeHistoryCollector(store, source)
    collector.collect(REQUEST, NOW)
    source.discovery = Discovery((), ())
    result = collector.collect(REQUEST, NOW + 60)
    assert result["contracts"] == 1
    assert source.calls[-1][0] == CONTRACT.con_id


def test_failure_preserves_cursor_and_reconnect_retries(store):
    source = Source()
    source.fail = True
    collector = TradeHistoryCollector(store, source)
    result = collector.collect(REQUEST, NOW)
    cursor = store.tracked(ACCOUNT)[0][1]
    assert result["errors"] > 0
    assert cursor == NOW - 86400
    assert rows(store, "bars") == []
    assert {r[4] for r in rows(store, "requests")} == {"error"}
    source.fail = False
    collector.collect(REQUEST, NOW + 60)
    assert store.tracked(ACCOUNT)[0][1] > cursor
    assert rows(store, "bars")


def test_long_outage_requests_recent_data_as_well_as_old_gap(store):
    source = Source()
    collector = TradeHistoryCollector(store, source)
    collector.collect(REQUEST, NOW)
    source.calls.clear()
    collector.collect(REQUEST, NOW + 3 * 86400)
    assert len(source.calls) == 2
    assert all(end - start <= 86400 for _, start, end in source.calls)
    assert source.calls[-1][2] == NOW + 3 * 86400
    assert store.tracked(ACCOUNT)[0][1] < NOW + 3 * 86400


def test_expiration_preserves_data_without_inventing_settlement(store):
    source = Source()
    collector = TradeHistoryCollector(store, source)
    collector.collect(REQUEST, NOW)
    bars = rows(store, "bars")
    source.calls.clear()
    source.discovery = Discovery((), ())
    result = collector.collect(REQUEST, NOW + 10 * 86400)
    assert result["expired"] == 1
    assert not source.calls
    assert rows(store, "bars") == bars
    assert rows(store, "contracts")[0][5] == "expired_unverified"


@pytest.mark.parametrize(
    "field,value",
    [
        ("strike", D("101")),
        ("right", "P"),
        ("multiplier", D("10")),
        ("expiry", "20261030"),
        ("currency", "GBP"),
        ("trading_class", "OTHER"),
    ],
)
def test_identity_conflicts_cannot_relabel_saved_prices(store, field, value):
    store.register(ACCOUNT, Discovery((CONTRACT,), (FILL,)), NOW)
    with pytest.raises(ValueError, match="identity conflict"):
        store.register(
            ACCOUNT, Discovery((replace(CONTRACT, **{field: value}),), ()), NOW
        )
    assert store.tracked(ACCOUNT)[0][0] == CONTRACT


def test_execution_conflict_rolls_back_entire_registration(store):
    store.register(ACCOUNT, Discovery((CONTRACT,), (FILL,)), NOW)
    other = replace(CONTRACT, con_id=456)
    with pytest.raises(ValueError, match="conflicting evidence"):
        store.register(
            ACCOUNT,
            Discovery((CONTRACT, other), (replace(FILL, quantity=D("3")),)),
            NOW,
        )
    assert len(rows(store, "contracts")) == 1


def test_partial_fills_and_corrections_are_individual_evidence(store):
    fills = (
        FILL,
        replace(FILL, exec_id="other.01", quantity=D("1")),
        replace(FILL, exec_id="fill.02", quantity=D("1")),
        replace(FILL, exec_id="sell.01", side="SLD", price=D("1.5")),
    )
    for _ in range(2):
        store.register(ACCOUNT, Discovery((CONTRACT,), fills), NOW)
    assert len(rows(store, "fills")) == 4  # corrections are not summed as more buys


def test_cross_account_fill_is_rejected_before_any_write(store):
    with pytest.raises(ValueError, match="invalid execution"):
        store.register(
            ACCOUNT, Discovery((CONTRACT,), (replace(FILL, account="DU9999999"),)), NOW
        )
    assert not rows(store, "contracts")


@pytest.mark.parametrize(
    "high,low,close",
    [
        ("NaN", "0", "1"),
        ("2", "-1", "1"),
        ("2", "1", "3"),
        ("Infinity", "0", "1"),
    ],
)
def test_invalid_prices_rejected(high, low, close):
    with pytest.raises(ValueError):
        MinuteBar(NOW, D(high), D(low), D(close))


def test_empty_response_is_explicit_and_not_synthetic_zero(store):
    store.register(ACCOUNT, Discovery((CONTRACT,), ()), NOW)
    store.record(ACCOUNT, CONTRACT, NOW - 60, NOW, ())
    assert not rows(store, "bars")
    assert rows(store, "requests")[0][4] == "empty"


def test_invalid_response_cannot_advance_cursor_or_publish_partial_bars(store):
    source = Source()
    source.history = lambda *args: (MinuteBar(NOW, D("2"), D("1"), D("1.5")),)
    result = TradeHistoryCollector(store, source).collect(REQUEST, NOW)
    assert result["errors"] > 0
    assert not rows(store, "bars")
    assert store.tracked(ACCOUNT)[0][1] == NOW - 86400


def test_vendor_revision_replaces_same_minute(store):
    store.register(ACCOUNT, Discovery((CONTRACT,), ()), NOW)
    for high in ("2", "3"):
        store.record(
            ACCOUNT,
            CONTRACT,
            NOW - 60,
            NOW,
            (MinuteBar(NOW - 60, D(high), D("1"), D("1.5")),),
        )
    assert len(rows(store, "bars")) == 1
    assert rows(store, "bars")[0][2] == "3"


def test_position_changes_save_quantities_without_inventing_an_entry(store):
    from ibkr_options_manager.broker.read_only import CapturedPosition

    source = Source()
    source.discovery = Discovery(
        (CONTRACT,),
        (),
        positions=(CapturedPosition(ACCOUNT, CONTRACT, D("2"), D("125")),),
    )
    collector = TradeHistoryCollector(store, source)
    collector.collect(REQUEST, NOW)
    collector.collect(REQUEST, NOW + 60)
    assert len(rows(store, "position_observations")) == 1
    source.discovery = replace(
        source.discovery,
        positions=(CapturedPosition(ACCOUNT, CONTRACT, D("1"), D("125")),),
    )
    collector.collect(REQUEST, NOW + 120)
    assert [r[3] for r in rows(store, "position_observations")] == ["2", "1"]
    assert not rows(store, "fills")


def test_known_older_entry_moves_backfill_start_to_entry(store):
    source = Source()
    source.discovery = Discovery(
        (CONTRACT,),
        (
            replace(
                FILL,
                time="20261012 12:00:00 UTC",
            ),
        ),
    )
    TradeHistoryCollector(store, source).collect(REQUEST, NOW)
    expected = int(datetime(2026, 10, 12, 12, tzinfo=UTC).timestamp())
    assert source.calls[0][1] == expected - 60
    assert rows(store, "execution_times")[0][2] == expected


@pytest.mark.parametrize(
    "raw,zone,known",
    [
        ("20261016 14:00:00", None, False),
        ("20261016 14:00:00", "Europe/London", True),
        ("20261016 14:00:00 UTC", None, True),
        ("20261025 01:30:00 Europe/London", None, False),
        ("20260329 01:30:00 Europe/London", None, False),
        ("garbage", "UTC", False),
    ],
)
def test_execution_time_never_guesses_a_zone_or_dst_fold(raw, zone, known):
    from ibkr_options_manager.trade_history import execution_epoch

    assert (execution_epoch(raw, zone) is not None) == known


def test_timezone_configuration_cannot_rewrite_existing_fill_time(tmp_path):
    path = tmp_path / "history.sqlite3"
    fill = replace(FILL, time="20261016 14:00:00")
    store = TradeHistoryStore(path, "UTC")
    store.register(ACCOUNT, Discovery((CONTRACT,), (fill,)), NOW)
    store.close()
    store = TradeHistoryStore(path, "Europe/London")
    try:
        with pytest.raises(ValueError, match="timezone conflicts"):
            store.register(ACCOUNT, Discovery((CONTRACT,), (fill,)), NOW)
    finally:
        store.close()
