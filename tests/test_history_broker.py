from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ibkr_options_manager.broker import history
from ibkr_options_manager.trade_history import TradeHistoryCollector, TradeHistoryStore
from test_trade_history import ACCOUNT, CONTRACT, NOW, REQUEST


def wire_contract(**changes):
    fields = dict(
        conId=CONTRACT.con_id,
        secType="OPT",
        lastTradeDateOrContractMonth=CONTRACT.expiry,
        strike=100,
        right="C",
        multiplier="100",
        currency="USD",
        tradingClass="XYZ",
        exchange="SMART",
        localSymbol="XYZ C100",
    )
    fields.update(changes)
    return SimpleNamespace(**fields)


@pytest.fixture
def transport(monkeypatch):
    state = SimpleNamespace(
        instances=[],
        calls=[],
        accounts=ACCOUNT,
        fail=False,
        incomplete=False,
        ambiguous=False,
        mismatch=False,
        position_account=ACCOUNT,
    )

    class Wrapper:
        pass

    class Client:
        def __init__(self, wrapper):
            state.instances.append(self)
            self.connected = False

        def connect(self, host, port, client_id):
            self.connected = True
            state.calls.append(("connect", client_id))
            self.nextValidId(100)

        def run(self):
            pass

        def disconnect(self):
            self.connected = False
            state.calls.append(("disconnect",))

        def isConnected(self):
            return self.connected

        def reqManagedAccts(self):
            self.managedAccounts(state.accounts)

        def reqPositions(self):
            self.position(state.position_account, wire_contract(exchange=""), 2, 125)
            self.position("DU99999", wire_contract(conId=999), 1, 100)
            self.position(ACCOUNT, wire_contract(conId=888), -1, 100)
            self.positionEnd()

        def reqExecutions(self, req_id, filter):
            self.execDetails(
                req_id,
                wire_contract(),
                SimpleNamespace(
                    acctNumber=ACCOUNT,
                    execId="buy.01",
                    permId=10,
                    side="BOT",
                    shares=2,
                    price=1.25,
                    time="20261016 14:00:00 Europe/London",
                ),
            )
            self.execDetailsEnd(req_id)

        def reqContractDetails(self, req_id, contract):
            state.calls.append(("contract", contract.conId, contract.exchange))
            result = wire_contract(strike=101 if state.mismatch else 100)
            self.contractDetails(req_id, SimpleNamespace(contract=result))
            if state.ambiguous:
                self.contractDetails(req_id, SimpleNamespace(contract=result))
            self.contractDetailsEnd(req_id)

        def reqHistoricalData(
            self,
            req_id,
            contract,
            end,
            duration,
            size,
            basis,
            rth,
            format,
            keep,
            options,
        ):
            state.calls.append(
                ("history", end, duration, size, basis, rth, format, keep)
            )
            self.historicalData(
                req_id, SimpleNamespace(date=str(NOW - 60), high=2, low=0, close=1.5)
            )
            if state.fail:
                self.error(req_id, 162, "no market data permissions")
            elif not state.incomplete:
                self.historicalDataEnd(req_id, "", "")

        def cancelHistoricalData(self, req_id):
            state.calls.append(("cancel-history", req_id))

    monkeypatch.setattr(
        history,
        "_load_ibapi",
        lambda: SimpleNamespace(
            EWrapper=Wrapper,
            EClient=Client,
            Contract=SimpleNamespace,
        ),
    )
    # Discovery imports only this lightweight filter from the optional API package.
    import sys

    monkeypatch.setitem(
        sys.modules, "ibapi.execution", SimpleNamespace(ExecutionFilter=SimpleNamespace)
    )
    return state


def test_discovery_verifies_identity_account_and_retains_fill_evidence(transport):
    result = history.IbkrHistorySource().discover(REQUEST)
    assert [c.con_id for c in result.contracts] == [123]
    assert result.fills[0].exec_id == "buy.01"
    assert result.positions[0].quantity == Decimal("2")
    assert not result.errors
    assert all(not app.isConnected() for app in transport.instances)


def test_history_requests_bid_epoch_minute_bars_and_excludes_forming_minute(transport):
    result = history.IbkrHistorySource().history(REQUEST, CONTRACT, NOW - 120, NOW)
    assert len(result) == 1
    call = next(c for c in transport.calls if c[0] == "history")
    assert call[2:] == ("120 S", "1 min", "BID", 0, 2, False)
    assert call[1].endswith("UTC")
    assert ("cancel-history", 4) in transport.calls
    assert all(not app.isConnected() for app in transport.instances)


@pytest.mark.parametrize("flag", ["ambiguous", "mismatch"])
def test_identity_failure_prevents_price_request(transport, flag):
    setattr(transport, flag, True)
    with pytest.raises(ValueError):
        history.IbkrHistorySource().history(REQUEST, CONTRACT, NOW - 120, NOW)
    assert not any(c[0] == "history" for c in transport.calls)


def test_wrong_managed_account_prevents_discovery(transport):
    transport.accounts = "DU999999"
    with pytest.raises(ValueError, match="account not verified"):
        history.IbkrHistorySource().discover(REQUEST)
    assert not any(c[0] == "contract" for c in transport.calls)


@pytest.mark.parametrize("flag", ["fail", "incomplete"])
def test_error_or_missing_acknowledgement_cannot_publish_partial_history(
    transport, tmp_path, flag
):
    setattr(transport, flag, True)
    store = TradeHistoryStore(tmp_path / "history.sqlite3")
    try:
        result = TradeHistoryCollector(store, history.IbkrHistorySource()).collect(
            replace(REQUEST, timeout_seconds=0.01),
            NOW,
        )
        assert result["errors"] > 0
        assert store.db.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 0
        assert store.tracked(ACCOUNT)[0][1] == NOW - 86400
        assert all(not app.isConnected() for app in transport.instances)
    finally:
        store.close()


def test_unrelated_callback_ids_do_not_complete_history(transport):
    app = history._build_app(ACCOUNT)
    app.historicalDataEnd(99, "", "")
    app.historicalData(99, SimpleNamespace())
    assert not app.events["history"].is_set()
    assert not app.bars


def test_disconnect_wakes_waiters_and_invalid_bar_fails_closed(transport):
    app = history._build_app(ACCOUNT)
    app.historicalData(4, SimpleNamespace(date=str(NOW), high=-1, low=0, close=1))
    assert app.errors
    app.connectionClosed()
    assert all(e.is_set() for e in app.events.values())


def test_end_to_end_command_writes_database_without_order_methods(
    transport, tmp_path, capsys, monkeypatch
):
    from ibkr_options_manager import cli

    monkeypatch.setattr(cli, "wall_time", lambda: NOW)
    path = tmp_path / "history.sqlite3"
    assert (
        cli.main(["collect", "--account", ACCOUNT, "--once", "--database", str(path)])
        == 0
    )
    assert '"status": "COLLECTED"' in capsys.readouterr().out
    store = TradeHistoryStore(path)
    try:
        assert store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 1
    finally:
        store.close()
