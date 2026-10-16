"""Deterministic journal fixtures stored through the production history database."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from ..broker.read_only import CapturedContract, CapturedExecution, CapturedPosition
from ..trade_history import Discovery, MinuteBar, TradeHistoryStore
from .demo import _POSITIONS, DEMO_ACCOUNT

D = Decimal


def seed_demo_history(path: Path) -> None:
    store = TradeHistoryStore(path)
    try:
        if store.db.execute(
            "SELECT 1 FROM contracts WHERE account<>?", (DEMO_ACCOUNT,)
        ).fetchone():
            raise ValueError("demo history cannot be mixed with brokerage records")
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS demo_seed(version INTEGER PRIMARY KEY)"
        )
        if store.db.execute("SELECT 1 FROM demo_seed WHERE version=1").fetchone():
            return
        profiles = (
            ("1", ".68", ".62", "1.1", "1.55", "1.28"),
            ("1", "1.25", "1.6", "1.2", ".8", ".55"),
            ("1", ".9", "1.15", "1.4", "1.7", "1.5"),
            ("1", ".95", "1.12", "1.3", "1.08", "1.18"),
            ("1", ".72", ".64", ".9", "1.3", "1.1"),
            ("1", "1.1", ".85", ".65", ".5", ".32"),
            ("1", ".9", "1.25", "1.4", "1.2", "1.3"),
            ("1", ".8", "1.22", "1.05", ".7", ".6"),
        )
        symbols = ("AAPL", "AMD", "META", "QQQ", "IWM", "TSLA", "NVDA", "SPY")
        for index in range(12):
            opened = index < 4
            if opened:
                original = _POSITIONS[index]
                contract, quantity, entry = (
                    original.contract,
                    original.quantity,
                    original.unit_basis,
                )
                date = ("20260923", "20261015", "20260928", "20261014")[index]
            else:
                symbol = symbols[index - 4]
                contract = CapturedContract(
                    2_000_000_000 + index,
                    "OPT",
                    "20261016",
                    D(150 + index * 5),
                    "P" if index % 3 == 0 else "C",
                    D(100),
                    "USD",
                    symbol,
                    "SMART",
                    f"{symbol}  261016{'P' if index % 3 == 0 else 'C'}"
                    f"{(150 + index * 5) * 1000:08d}",
                )
                quantity, entry = D(2 + index % 4), D("1.5") + D(index) / 5
                date = (
                    "20260921",
                    "20260922",
                    "20260924",
                    "20260929",
                    "20261001",
                    "20261005",
                    "20261008",
                    "20261012",
                )[index - 4]
            start = int(
                datetime.strptime(date + " 14:00", "%Y%m%d %H:%M")
                .replace(tzinfo=UTC)
                .timestamp()
            )
            anchors = tuple(D(value) for value in profiles[index % len(profiles)])
            bars = []
            for minute in range(120):
                progress = D(minute) / 24
                segment = min(int(progress), 4)
                fraction = progress - segment
                ratio = (
                    anchors[segment]
                    + (anchors[segment + 1] - anchors[segment]) * fraction
                )
                close = (entry * ratio).quantize(D(".0001"))
                bars.append(
                    MinuteBar(
                        start + (minute + 1) * 60,
                        close + D(".015"),
                        max(D(0), close - D(".015")),
                        close,
                    )
                )
            if index == 7:
                bars[10] = replace(
                    bars[10], high=entry * D("1.3"), low=entry * D(".65")
                )
            fills = [
                CapturedExecution(
                    f"demo-{index}-buy.01",
                    DEMO_ACCOUNT,
                    contract.con_id,
                    7000 + index,
                    "BOT",
                    quantity,
                    entry,
                    datetime.fromtimestamp(start, UTC).strftime("%Y%m%d %H:%M:%S UTC"),
                    currency="USD",
                )
            ]
            exit_time = start + 61 * 60
            if not opened:
                # Actual exits deliberately differ from the later path extremes.
                exit_price = (entry * (D("1.2") if index % 3 else D(".75"))).quantize(
                    D(".0001")
                )
                fills.append(
                    CapturedExecution(
                        f"demo-{index}-sell.01",
                        DEMO_ACCOUNT,
                        contract.con_id,
                        8000 + index,
                        "SLD",
                        quantity,
                        exit_price,
                        datetime.fromtimestamp(exit_time, UTC).strftime(
                            "%Y%m%d %H:%M:%S UTC"
                        ),
                        currency="USD",
                    )
                )
            position = CapturedPosition(
                DEMO_ACCOUNT, contract, quantity, entry * contract.multiplier
            )
            store.register(
                DEMO_ACCOUNT,
                Discovery((contract,), tuple(fills), positions=(position,)),
                start,
            )
            store.record(DEMO_ACCOUNT, contract, start, start + 121 * 60, tuple(bars))
            if index == 2:
                store.record(
                    DEMO_ACCOUNT,
                    contract,
                    start + 121 * 60,
                    start + 130 * 60,
                    (),
                    "Illustrative collection gap",
                    advance=False,
                )
            if not opened:
                with store.db:
                    store.db.execute(
                        "UPDATE portfolio_state SET quantity='0',"
                        "closed_at=?,last_seen=? "
                        "WHERE account=? AND con_id=?",
                        (exit_time, exit_time, DEMO_ACCOUNT, contract.con_id),
                    )
                    store.db.execute(
                        "INSERT INTO position_observations VALUES(?,?,?,'0',?)",
                        (
                            DEMO_ACCOUNT,
                            contract.con_id,
                            exit_time,
                            str(entry * contract.multiplier),
                        ),
                    )
                if index in (4, 6, 9, 11):
                    end = int(datetime(2026, 10, 16, 20, tzinfo=UTC).timestamp())
                    with store.db:
                        store.db.execute(
                            "INSERT INTO final_marks VALUES(?,?,?,'expiry_bid')",
                            (contract.con_id, end, str(entry * anchors[-1])),
                        )
        # An imported holding intentionally has no reconstructable entry fills.
        unknown = replace(
            _POSITIONS[0].contract,
            con_id=2_999_999_999,
            trading_class="MSFT",
            local_symbol="MSFT  261120C00450000",
            expiry="20261120",
            strike=D(450),
        )
        store.register(
            DEMO_ACCOUNT,
            Discovery(
                (unknown,),
                (),
                positions=(CapturedPosition(DEMO_ACCOUNT, unknown, D(1), D(210)),),
            ),
            start,
        )
        with store.db:
            store.db.execute("INSERT INTO demo_seed VALUES(1)")
    finally:
        store.close()
