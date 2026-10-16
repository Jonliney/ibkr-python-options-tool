from dataclasses import replace
from decimal import Decimal

from starlette.testclient import TestClient

from ibkr_options_manager.app.demo import DEMO_ACCOUNT
from ibkr_options_manager.app.demo_history import seed_demo_history
from ibkr_options_manager.app.web import StarUIWorkbench
from ibkr_options_manager.broker.read_only import CapturedPosition
from ibkr_options_manager.journal import load_journal, summarize
from ibkr_options_manager.trade_history import Discovery, TradeHistoryStore
from support.workbench import _demo_workbench
from test_trade_history import ACCOUNT, CONTRACT, NOW

D = Decimal


def test_demo_data_is_idempotent_durable_and_account_scoped(tmp_path):
    path = tmp_path / "history.sqlite3"
    for _ in range(2):
        seed_demo_history(path)
    store = TradeHistoryStore(path)
    try:
        rows = load_journal(store, DEMO_ACCOUNT)
        assert len(rows) == 13
        summary = summarize(rows)
        assert summary.wins == 6 and summary.losses == 2
        assert summary.win_rate == 75
        assert summary.realized == D("788.5")
        assert summary.priced_count == 11  # one gap, one unknown entry
        assert summary.ambiguous_count == 1
        assert summary.stop_then_target > 0
        assert not load_journal(store, "DU1111111")
        assert any(r.observations and r.observations[-1].quantity == 0 for r in rows)
        assert sum(r.final_price is not None for r in rows) == 4
    finally:
        store.close()


def test_complete_portfolio_records_disappearance_and_restart_without_pnl(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = TradeHistoryStore(path)
    position = CapturedPosition(ACCOUNT, CONTRACT, D(3), D(125))
    store.register(
        ACCOUNT,
        Discovery((CONTRACT,), (), positions=(position,), positions_complete=True),
        NOW,
    )
    # An incomplete discovery must not close the holding.
    store.register(ACCOUNT, Discovery((), ()), NOW + 60)
    assert (
        store.db.execute("SELECT closed_at FROM portfolio_state").fetchone()[0] is None
    )
    store.register(ACCOUNT, Discovery((), (), positions_complete=True), NOW + 120)
    store.close()
    store = TradeHistoryStore(path)
    try:
        (row,) = load_journal(store, ACCOUNT)
        assert row.status == "Closed · entry unknown"
        assert row.realized_pnl is None
        assert row.analysis is None
        assert row.observations[-1].quantity == 0
    finally:
        store.close()


def test_v1_position_observations_migrate_without_losing_history(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = TradeHistoryStore(path)
    store.register(
        ACCOUNT,
        Discovery(
            (CONTRACT,),
            (),
            positions=(CapturedPosition(ACCOUNT, CONTRACT, D(2), D(125)),),
        ),
        NOW,
    )
    with store.db:
        store.db.execute("DROP TABLE portfolio_state")
        store.db.execute("PRAGMA user_version=1")
    store.close()
    store = TradeHistoryStore(path)
    try:
        (row,) = load_journal(store, ACCOUNT)
        assert row.quantity == 2
        assert len(row.observations) == 1
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        store.close()


def test_dashboard_filters_analysis_and_management_roundtrip():
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    try:
        html = client.post(
            workbench.path + "action", data={"action": "show-dashboard"}
        ).text
        assert ">Dashboard</h1>" in html and "75%" in html
        assert "Illustrative history" not in html
        assert "Saved account history" not in html
        assert 'name="journal_status"' not in html
        assert 'name="journal_currency"' not in html
        assert ">Apply<" not in html
        assert 'data-slot="dropdown-menu-trigger"' in html
        assert "Cumulative gross realized" in html
        assert "Entry unknown" in html
        assert "Order changes" not in html
        rows = workbench._journal_rows()
        selected = next(
            r for r in rows if r.contract.con_id == workbench._selected_con_id
        )
        html = client.post(
            workbench.path + "action",
            data={"action": "open-trade", "trade_id": selected.row_id},
        ).text
        assert "data-position-analysis" in html
        assert "Order of extremes" in html and "Average observed price" in html
        assert "Contract position observations" in html
        html = client.post(
            workbench.path + "action", data={"action": "show-management"}
        ).text
        assert 'id="position-inventory"' in html
        assert "Build your exit draft" in html
        assert "data-position-analysis" not in html
        html = client.post(
            workbench.path + "action", data={"action": "show-dashboard"}
        ).text
        html = client.post(
            workbench.path + "action",
            data={
                "action": "journal-filter",
                "journal_status": "closed",
                "journal_currency": "USD",
                "journal_stop": "20",
                "journal_target": "40",
            },
        ).text
        assert "13 trades" in html  # analysis settings never filter trade history
        assert workbench._journal_target == 40
        html = client.post(
            workbench.path + "action",
            data={
                "action": "journal-filter",
                "journal_status": "closed",
                "journal_currency": "USD",
                "journal_stop": "NaN",
                "journal_target": "40",
            },
        ).text
        assert "Choose a stop" in html
        assert workbench._journal_stop == 20
    finally:
        workbench.close()


def test_saved_closed_trade_reopens_after_workbench_restart(tmp_path):
    path = tmp_path / "journal.sqlite3"
    original = _demo_workbench()
    try:
        # A new workbench shares only the database, not session dictionaries.
        seed_demo_history(path)
        reopened = StarUIWorkbench(
            original._view_model,
            initial_account=DEMO_ACCOUNT,
            demo_mode=True,
            history_path=path,
        )
        try:
            closed = next(r for r in reopened._journal_rows() if r.status == "Closed")
            assert not reopened._session_closed_positions
            html = (
                TestClient(reopened.app)
                .post(
                    reopened.path + "action",
                    data={"action": "open-trade", "trade_id": closed.row_id},
                )
                .text
            )
            assert "data-position-analysis" in html
            assert "Actual gross P&amp;L" in html
            assert "disabled" in html  # no management for a saved closed trade
        finally:
            reopened.close()
    finally:
        original.close()


def test_mixed_currency_summary_is_not_silently_summed(tmp_path):
    import pytest

    path = tmp_path / "history.sqlite3"
    seed_demo_history(path)
    store = TradeHistoryStore(path)
    try:
        row = load_journal(store, DEMO_ACCOUNT)[0]
        with pytest.raises(ValueError, match="currency"):
            summarize(
                (row, replace(row, contract=replace(row.contract, currency="GBP")))
            )
    finally:
        store.close()


def test_price_chart_preserves_elapsed_time_and_breaks_missing_minutes(tmp_path):
    import re

    from starhtml import to_xml

    from ibkr_options_manager.app.web.journal_view import _price_chart

    path = tmp_path / "history.sqlite3"
    seed_demo_history(path)
    store = TradeHistoryStore(path)
    try:
        row = next(
            r for r in load_journal(store, DEMO_ACCOUNT) if r.analysis is not None
        )
        bars = row.analysis.bars[:4]
        gap = bars[:2] + tuple(
            replace(b, timestamp=b.timestamp + 600) for b in bars[2:]
        )
        row = replace(row, analysis=replace(row.analysis, bars=gap))
        html = to_xml(_price_chart(row))
        assert html.count('class="journal-price-range"') == 2
        lines = re.findall(r'<polyline points="([^"]+)"', html)
        assert len(lines) == 2
        first_end = float(lines[0].split()[-1].split(",")[0])
        next_start = float(lines[1].split()[0].split(",")[0])
        assert next_start - first_end > 500
        assert "Elapsed time (UTC)" in html
    finally:
        store.close()


def test_live_insights_reject_stale_responses_and_invalid_inputs():
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    try:
        client.post(workbench.path + "action", data={"action": "show-dashboard"})
        data = dict(
            journal_stop="10",
            journal_target="40",
            generation=str(workbench._insight_generation),
            revision="2",
        )
        response = client.post(workbench.path + "journal-insights", data=data)
        assert response.status_code == 200
        assert "trades reached +40%" in response.text
        assert "Median dip before target" in response.text
        assert "Trade history" not in response.text
        assert workbench._journal_stop == 10
        stale = client.post(
            workbench.path + "journal-insights",
            data={**data, "revision": "1", "journal_stop": "40"},
        )
        assert stale.status_code == 409
        assert workbench._journal_stop == 10
        invalid = client.post(
            workbench.path + "journal-insights",
            data={**data, "revision": "3", "journal_target": "NaN"},
        )
        assert invalid.status_code == 422
        assert workbench._journal_target == 40
        client.post(workbench.path + "action", data={"action": "show-positions"})
        assert (
            client.post(
                workbench.path + "journal-insights", data={**data, "revision": "3"}
            ).status_code
            == 409
        )
    finally:
        workbench.close()


def test_entry_insights_recoveries_givebacks_and_ambiguity():
    from ibkr_options_manager.domain.trade_analysis import (
        PriceMinute,
        TradeFill,
        analyse_prices,
        group_trades,
    )
    from ibkr_options_manager.journal import JournalRow, entry_insights

    (trade,) = group_trades((TradeFill("insight.01", NOW, "BOT", D(1), D(1)),))

    def row(path):
        bars = tuple(
            PriceMinute(NOW + i * 60, D(hi), D(lo), D(close))
            for i, (hi, lo, close) in enumerate(path, 1)
        )
        return JournalRow(
            "insight",
            CONTRACT,
            trade,
            analyse_prices(trade, bars),
            "Open",
            D(1),
            D(1),
            NOW,
            None,
            None,
            "Observed minutes",
            "",
        )

    rows = (
        row((("1", ".8", ".9"), ("1.3", "1.1", "1.2"), ("1.4", ".9", "1.1"))),
        row((("1", ".6", ".8"), ("1.3", "1", "1.2"), ("1.3", "1.1", "1.2"))),
        row((("1.3", ".6", "1"),)),
        row((("1.3", "1.1", "1.2"), ("1", ".5", ".8"))),
        row((("1", ".8", ".9"), ("1", ".6", ".8"))),
    )
    insight = entry_insights(rows, stop=D(25), target=D(20))
    assert insight.eligible == 5 and insight.reached_target == 4
    assert insight.median_dip == -20 and insight.dip_count == 3
    assert insight.gave_back == 2 and insight.giveback_ambiguous == 1
    assert insight.followup_missing == 1 and insight.retained == 0
    level = next(r for r in insight.recovery if r.stop_percent == 25)
    assert (level.touched, level.recovered, level.ambiguous) == (3, 1, 1)
    gapped = replace(rows[0], coverage="Gaps in history")
    assert entry_insights((gapped,), stop=D(25), target=D(20)).eligible == 0


def test_dashboard_keeps_currency_totals_separate_without_a_filter(tmp_path):
    from starhtml import to_xml

    from ibkr_options_manager.app.web.journal_view import dashboard

    path = tmp_path / "history.sqlite3"
    seed_demo_history(path)
    store = TradeHistoryStore(path)
    try:
        row = next(
            r for r in load_journal(store, DEMO_ACCOUNT) if r.realized_pnl is not None
        )
        other = replace(
            row,
            row_id="gbp",
            contract=replace(row.contract, currency="GBP"),
            realized_pnl=D(123),
        )
        html = to_xml(
            dashboard((row, other), "/journal/", stop=D(25), target=D(20), generation=1)
        )
        assert "GBP 123.00" in html
        assert "Separate totals by currency" in html
        assert 'name="journal_currency"' not in html
    finally:
        store.close()
