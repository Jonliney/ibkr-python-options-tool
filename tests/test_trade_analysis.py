from decimal import Decimal

import pytest

from ibkr_options_manager.domain.trade_analysis import (
    PriceMinute,
    TradeFill,
    analyse_prices,
    group_trades,
)

D = Decimal
T = 1_800_000_000


def fill(identity, time, side, qty, price):
    return TradeFill(identity, time, side, D(qty), D(price))


def test_reentries_partial_fills_and_weighted_basis_are_distinct():
    trades = group_trades(
        (
            fill("a.01", T, "BOT", "2", "1"),
            fill("b.01", T + 10, "BOT", "1", "2"),
            fill("c.01", T + 120, "SLD", "1", "3"),
            fill("d.01", T + 180, "SLD", "2", "1.5"),
            fill("e.01", T + 300, "BOT", "1", "2"),
        )
    )
    assert len(trades) == 2
    assert trades[0].quantity == 3
    assert trades[0].entry_price == D(4) / 3
    assert trades[0].realized(D(100)) == D(200)
    assert trades[1].remaining == 1
    assert trades[1].realized(D(100)) is None


def test_correction_replaces_original_without_duplicate_quantity():
    (trade,) = group_trades(
        (
            fill("a.01", T, "BOT", "2", "1"),
            fill("a.02", T, "BOT", "1", "1.25"),
            fill("a.02", T, "BOT", "1", "1.25"),
            fill("b.01", T + 60, "SLD", "1", "2"),
        )
    )
    assert trade.quantity == 1
    assert trade.realized(D(100)) == 75


@pytest.mark.parametrize(
    "fills",
    [
        (fill("sell.01", T, "SLD", "1", "2"), fill("buy.01", T + 60, "BOT", "1", "1")),
        (fill("buy.01", T, "BOT", "1", "1"), fill("sell.01", T, "SLD", "1", "2")),
        (fill("buy.01", T, "BOT", "1", "1"), fill("sell.01", T + 60, "SLD", "2", "2")),
    ],
)
def test_unsupported_or_ambiguous_fill_stream_is_not_invented(fills):
    assert not group_trades(fills)


def test_entry_minute_excluded_and_zero_price_preserved():
    (trade,) = group_trades((fill("a.01", T + 20, "BOT", "2", "1"),))
    result = analyse_prices(
        trade,
        (
            PriceMinute(T, D(99), D(0), D(5)),
            PriceMinute(T + 60, D(".8"), D(0), D(".4")),
            PriceMinute(T + 120, D("1.5"), D(1), D("1.25")),
        ),
    )
    assert result.high == D("1.5")
    assert result.low == 0
    assert result.average == D(".825")
    assert result.stop_then_target is True
    assert result.sequence == "Low before high"


def test_same_minute_high_low_and_threshold_order_is_unknown():
    (trade,) = group_trades((fill("a.01", T, "BOT", "1", "1"),))
    result = analyse_prices(trade, (PriceMinute(T + 60, D("1.5"), D(".5"), D(1)),))
    assert result.stop_then_target is None
    assert "unknown" in result.sequence
    assert "unknown" in result.threshold_sequence


def test_target_before_stop_and_only_observed_after_last_scale_in():
    (trade,) = group_trades(
        (fill("a.01", T, "BOT", "1", "1"), fill("b.01", T + 120, "BOT", "1", "1"))
    )
    result = analyse_prices(
        trade,
        (
            PriceMinute(T + 60, D(10), D(0), D(1)),
            PriceMinute(T + 180, D("1.3"), D(1), D("1.2")),
            PriceMinute(T + 240, D(1), D(".7"), D(".8")),
        ),
    )
    assert result.high == D("1.3")
    assert result.stop_then_target is False
    assert result.threshold_sequence == "Target before stop level"
    assert result.warning
