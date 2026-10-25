from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from starhtml import to_xml

from ibkr_options_manager.app.demo import DEMO_ACCOUNT
from ibkr_options_manager.app.demo_history import seed_demo_history
from ibkr_options_manager.app.web.journal_view import _metric_sparkline
from ibkr_options_manager.domain.trade_analysis import PriceMinute
from ibkr_options_manager.journal import load_journal
from ibkr_options_manager.journal_trends import MetricTrend, weekly_metric_trends
from ibkr_options_manager.trade_history import TradeHistoryStore

D = Decimal


def ts(day):
    return int(datetime(2026, 10, day, 12, tzinfo=UTC).timestamp())


@pytest.fixture
def rows(tmp_path):
    path = tmp_path / "history.sqlite3"
    seed_demo_history(path)
    store = TradeHistoryStore(path)
    try:
        return load_journal(store, DEMO_ACCOUNT)
    finally:
        store.close()


def test_weekly_results_use_close_dates_rolling_windows_and_separate_currencies(rows):
    base = next(r for r in rows if r.realized_pnl is not None)
    win = replace(base, exit_at=ts(13), realized_pnl=D(60))
    loss = replace(base, exit_at=ts(15), realized_pnl=D(-30))
    old = replace(base, exit_at=ts(1) - 35 * 86400, realized_pnl=D(900))
    future = replace(base, exit_at=ts(26), realized_pnl=D(9999))
    gbp = replace(
        win, contract=replace(base.contract, currency="GBP"), realized_pnl=D(100)
    )
    partial = replace(
        next(r for r in rows if r.trade and r.trade.remaining > 0),
        exit_at=ts(14),
        realized_pnl=D(8888),
    )
    trends = weekly_metric_trends((win, loss, old, future, gbp, partial), as_of=ts(25))
    assert all(len(t.values) == 12 for series in trends.values() for t in series)
    pnl = {t.currency: t.values for t in trends["pnl"]}
    assert pnl["USD"][-1] == 930  # includes the old gain inside the 12-week window
    assert pnl["GBP"][-1] == 100
    rr = {t.currency: t.values for t in trends["rr"]}
    assert rr["USD"][-1] == 2  # old and future trades are outside the rolling window
    assert rr["GBP"][-1] is None
    assert trends["win"][0].values[-1] == D(2) / 3 * 100
    single = weekly_metric_trends((win,), as_of=ts(25))
    assert single["win"][0].values[-1] is None
    assert single["rr"][0].values[-1] is None


def test_excursions_do_not_look_ahead_and_omit_incomplete_or_sparse_samples(rows):
    base = next(r for r in rows if r.analysis and r.entry_price)
    bars = (
        PriceMinute(ts(14), D(120), D(90), D(100)),
        PriceMinute(ts(22), D(200), D(20), D(100)),
        PriceMinute(ts(26), D(1000), D(1), D(100)),
    )
    entry = replace(
        base,
        entry_at=ts(13),
        entry_price=D(100),
        coverage="Observed minutes",
        analysis=replace(base.analysis, bars=bars),
    )
    other = replace(entry, row_id="second")
    gapped = replace(entry, coverage="Gaps in history", entry_price=D(1))
    trends = weekly_metric_trends((entry, other, gapped), as_of=ts(25))
    assert trends["dip"][0].values[-2:] == (D(-10), D(-80))
    assert trends["peak"][0].values[-2:] == (D(20), D(100))
    sparse = weekly_metric_trends((entry,), as_of=ts(25))
    assert all(v is None for v in sparse["dip"][0].values)
    empty = weekly_metric_trends((), as_of=ts(25))
    assert empty["pnl"] == ()


def test_sparkline_gaps_are_unconnected_and_graphs_are_decorative():
    series = (MetricTrend("USD", (D(1), D(2), None, D(4), D(5), *([None] * 7))),)
    html = to_xml(_metric_sparkline(series))
    assert html.count("<polyline") == 2
    assert 'aria-hidden="true"' in html
    assert 'focusable="false"' in html
    assert "tabindex" not in html and "<circle" not in html
    assert _metric_sparkline((MetricTrend("", (D(1), None)),)) is None


def test_short_history_spans_tile_width_instead_of_hugging_right_edge():
    series = (MetricTrend("", (*([None] * 9), D(1), D(2), D(3))),)
    html = to_xml(_metric_sparkline(series))
    assert 'points="0.00,68.00 110.00,38.00 220.00,8.00"' in html
