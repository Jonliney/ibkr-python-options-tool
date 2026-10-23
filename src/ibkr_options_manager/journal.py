"""Database read model for trade history; never a source for order planning."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from .broker.read_only import CapturedContract
from .domain.trade_analysis import (
    PriceAnalysis,
    PriceMinute,
    TradeEpisode,
    TradeFill,
    analyse_prices,
    execution_revision,
    group_trades,
)
from .trade_history import TradeHistoryStore

ZERO = Decimal(0)


@dataclass(frozen=True)
class PositionObservation:
    timestamp: int
    quantity: Decimal
    average_cost: Decimal


@dataclass(frozen=True)
class JournalRow:
    row_id: str
    contract: CapturedContract
    trade: TradeEpisode | None
    analysis: PriceAnalysis | None
    status: str
    quantity: Decimal
    entry_price: Decimal | None
    entry_at: int | None
    exit_at: int | None
    realized_pnl: Decimal | None
    coverage: str
    warning: str
    final_price: Decimal | None = None
    final_kind: str = ""
    observations: tuple[PositionObservation, ...] = ()

    @property
    def actual_return(self) -> Decimal | None:
        cost = self.cost
        return (
            self.realized_pnl / cost * 100
            if self.realized_pnl is not None and cost
            else None
        )

    @property
    def cost(self) -> Decimal:
        return (self.entry_price or ZERO) * self.quantity * self.contract.multiplier

    def return_at(self, price: Decimal | None) -> Decimal | None:
        return (
            (price / self.entry_price - 1) * 100
            if price is not None and self.entry_price
            else None
        )

    def pnl_at(self, price: Decimal | None) -> Decimal | None:
        return (
            (price - self.entry_price) * self.quantity * self.contract.multiplier
            if price is not None and self.entry_price
            else None
        )


def load_journal(
    store: TradeHistoryStore,
    account: str,
    *,
    stop: Decimal = Decimal(25),
    target: Decimal = Decimal(20),
) -> tuple[JournalRow, ...]:
    rows: list[JournalRow] = []
    for contract, _cursor in store.tracked(account):
        observations = tuple(
            PositionObservation(int(ts), Decimal(qty), Decimal(cost))
            for ts, qty, cost in store.db.execute(
                "SELECT observed_at,quantity,average_cost FROM position_observations "
                "WHERE account=? AND con_id=? ORDER BY observed_at",
                (account, contract.con_id),
            )
        )
        raw = store.db.execute(
            "SELECT f.payload,t.timestamp FROM fills f LEFT JOIN execution_times t "
            "ON f.account=t.account AND f.exec_id=t.exec_id "
            "WHERE f.account=? AND f.con_id=?",
            (account, contract.con_id),
        ).fetchall()
        latest: dict[str, tuple[dict[str, str], int | None]] = {}
        for payload, timestamp in raw:
            data = json.loads(payload)
            key, revision = execution_revision(data["exec_id"])
            old = latest.get(key)
            if old is None or revision > execution_revision(old[0]["exec_id"])[1]:
                latest[key] = data, timestamp
        unknown = any(timestamp is None for _data, timestamp in latest.values())
        fills = tuple(
            TradeFill(
                d["exec_id"],
                int(timestamp),
                d["side"],
                Decimal(d["quantity"]),
                Decimal(d["price"]),
            )
            for d, timestamp in latest.values()
            if timestamp is not None
        )
        episodes = () if unknown else group_trades(fills)
        position = store.db.execute(
            "SELECT quantity,average_cost,closed_at FROM portfolio_state "
            "WHERE account=? AND con_id=?",
            (account, contract.con_id),
        ).fetchone()
        if not episodes:
            if not position and not raw:
                continue
            rows.append(
                JournalRow(
                    f"observed-{contract.con_id}",
                    contract,
                    None,
                    None,
                    "Closed · entry unknown"
                    if position and position[2] is not None
                    else "Entry unknown",
                    Decimal(position[0]) if position else ZERO,
                    Decimal(position[1]) / contract.multiplier if position else None,
                    None,
                    None,
                    None,
                    "Entry unknown",
                    "No complete, unambiguous long-trade fill history. "
                    "P&L and excursions are withheld.",
                    observations=observations,
                )
            )
            continue
        bars = tuple(
            PriceMinute(int(ts), Decimal(hi), Decimal(lo), Decimal(close))
            for ts, hi, lo, close in store.db.execute(
                "SELECT timestamp,high,low,close FROM bars "
                "WHERE con_id=? AND basis='BID' "
                "ORDER BY timestamp",
                (contract.con_id,),
            )
        )
        final = store.db.execute(
            "SELECT timestamp,price,kind FROM final_marks WHERE con_id=?",
            (contract.con_id,),
        ).fetchone()
        for episode in episodes:
            analysis = analyse_prices(
                episode, bars, stop_percent=stop, target_percent=target
            )
            gap = store.db.execute(
                "SELECT 1 FROM requests WHERE account=? AND con_id=? AND end>? "
                "AND status IN ('error','empty') LIMIT 1",
                (account, contract.con_id, episode.entry_at),
            ).fetchone()
            coverage = (
                "Gaps in history"
                if gap
                else "Observed minutes"
                if analysis.bars
                else "No price history"
            )
            warning = analysis.warning
            if gap:
                warning = (
                    "Some price history is missing; extrema may be understated. "
                    + warning
                )
            remaining = episode.remaining
            if position and Decimal(position[0]) != episodes[-1].remaining:
                warning += (
                    " Saved position quantity differs from fill history; "
                    "trade result withheld."
                )
                realized = None
            else:
                realized = episode.realized(contract.multiplier)
            rows.append(
                JournalRow(
                    f"{contract.con_id}:{episode.trade_id}",
                    contract,
                    episode,
                    analysis,
                    "Closed"
                    if remaining == 0
                    else "Open"
                    if remaining == episode.quantity
                    else "Partially closed",
                    episode.quantity,
                    episode.entry_price,
                    episode.entry_at,
                    episode.exit_at,
                    realized,
                    coverage,
                    warning.strip(),
                    Decimal(final[1])
                    if final and final[0] > episode.entry_at
                    else None,
                    str(final[2]) if final else "",
                    observations,
                )
            )
    return tuple(sorted(rows, key=lambda row: row.entry_at or 0, reverse=True))


@dataclass(frozen=True)
class JournalSummary:
    wins: int
    losses: int
    flat: int
    realized: Decimal
    average_adverse: Decimal | None
    average_favorable: Decimal | None
    priced_count: int
    stop_then_target: int
    sequence_count: int
    ambiguous_count: int
    average_loss: Decimal | None
    average_win: Decimal | None

    @property
    def reward_per_risk(self) -> Decimal | None:
        if self.average_loss is None or self.average_win is None:
            return None
        return self.average_win / self.average_loss

    @property
    def win_rate(self) -> Decimal | None:
        return (
            Decimal(self.wins) / (self.wins + self.losses) * 100
            if self.wins + self.losses
            else None
        )


def summarize(rows: tuple[JournalRow, ...]) -> JournalSummary:
    currencies = {r.contract.currency for r in rows}
    if len(currencies) > 1:
        raise ValueError("select one currency before combining trade results")
    closed = [r.realized_pnl for r in rows if r.realized_pnl is not None]
    priced = [
        r
        for r in rows
        if r.analysis
        and r.analysis.bars
        and r.coverage == "Observed minutes"
        and r.entry_price
    ]
    adverse: list[Decimal] = []
    favorable: list[Decimal] = []
    for row in priced:
        if row.analysis is None:
            continue
        low_return = row.return_at(row.analysis.low)
        high_return = row.return_at(row.analysis.high)
        if low_return is not None and high_return is not None:
            adverse.append(min(ZERO, low_return))
            favorable.append(max(ZERO, high_return))
    sequences = [r.analysis.stop_then_target for r in priced if r.analysis is not None]
    return JournalSummary(
        sum(p > 0 for p in closed),
        sum(p < 0 for p in closed),
        sum(p == 0 for p in closed),
        sum(closed, ZERO),
        sum(adverse, ZERO) / len(adverse) if adverse else None,
        sum(favorable, ZERO) / len(favorable) if favorable else None,
        len(priced),
        sequences.count(True),
        sum(s is not None for s in sequences),
        sequences.count(None),
        sum((-p for p in closed if p < 0), ZERO) / sum(p < 0 for p in closed)
        if any(p < 0 for p in closed)
        else None,
        sum((p for p in closed if p > 0), ZERO) / sum(p > 0 for p in closed)
        if any(p > 0 for p in closed)
        else None,
    )


@dataclass(frozen=True)
class RecoveryLevel:
    stop_percent: Decimal
    touched: int
    recovered: int
    ambiguous: int


@dataclass(frozen=True)
class EntryInsights:
    eligible: int
    reached_target: int
    median_dip: Decimal | None
    dip_count: int
    gave_back: int
    retained: int
    giveback_ambiguous: int
    followup_missing: int
    recovery: tuple[RecoveryLevel, ...]


def entry_insights(
    rows: tuple[JournalRow, ...],
    *,
    stop: Decimal,
    target: Decimal,
) -> EntryInsights:
    """Compare paths rather than simulate fills; respect intraminute ambiguity."""
    if (
        not stop.is_finite()
        or not target.is_finite()
        or not 0 < stop < 100
        or not 0 < target <= 10000
    ):
        raise ValueError("invalid entry insight thresholds")
    eligible = [
        r
        for r in rows
        if r.entry_price
        and r.analysis
        and r.analysis.bars
        and r.coverage == "Observed minutes"
    ]
    levels = sorted({Decimal(10), Decimal(20), Decimal(30), Decimal(40), stop})
    counts = {level: [0, 0, 0] for level in levels}
    dips: list[Decimal] = []
    reached = gave_back = retained = ambiguous_giveback = followup_missing = 0
    for row in eligible:
        assert row.analysis is not None and row.entry_price is not None
        bars = row.analysis.bars
        target_price = row.entry_price * (1 + target / 100)
        target_bar = next((b for b in bars if b.high >= target_price), None)
        if target_bar is not None:
            reached += 1
            earlier = [b for b in bars if b.timestamp < target_bar.timestamp]
            # The target minute's low may precede or follow its high. Omit a
            # dip statistic when that uncertainty could change its minimum.
            previous_low = min([row.entry_price, *(b.low for b in earlier)])
            if target_bar.low >= previous_low:
                dips.append(min(ZERO, (previous_low / row.entry_price - 1) * 100))
            later = [b for b in bars if b.timestamp > target_bar.timestamp]
            if not later:
                followup_missing += 1
            elif any(b.low <= row.entry_price for b in later):
                gave_back += 1
            elif target_bar.low <= row.entry_price:
                ambiguous_giveback += 1
            else:
                retained += 1
        for level in levels:
            stop_price = row.entry_price * (1 - level / 100)
            stop_bar = next((b for b in bars if b.low <= stop_price), None)
            if stop_bar is None:
                continue
            if target_bar is not None and target_bar.timestamp < stop_bar.timestamp:
                continue
            counts[level][0] += 1
            if target_bar is not None and stop_bar.timestamp == target_bar.timestamp:
                counts[level][2] += 1
            elif target_bar is not None and target_bar.timestamp > stop_bar.timestamp:
                counts[level][1] += 1
    dips.sort()
    middle = len(dips) // 2
    median = (
        (dips[middle] if len(dips) % 2 else (dips[middle - 1] + dips[middle]) / 2)
        if dips
        else None
    )
    return EntryInsights(
        len(eligible),
        reached,
        median,
        len(dips),
        gave_back,
        retained,
        ambiguous_giveback,
        followup_missing,
        tuple(RecoveryLevel(level, *counts[level]) for level in levels),
    )
