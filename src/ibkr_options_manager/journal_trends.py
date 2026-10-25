"""Weekly historical metric samples for the read-only Dashboard."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .journal import JournalRow

D = Decimal
WEEK = 7 * 86400


@dataclass(frozen=True)
class MetricTrend:
    currency: str
    values: tuple[Decimal | None, ...]


def weekly_metric_trends(
    rows: tuple[JournalRow, ...], *, as_of: int
) -> dict[str, tuple[MetricTrend, ...]]:
    """Twelve UTC weeks, including the current partial week; no future prices.

    Rates and excursions use four calendar weeks at each sample, with at least
    two eligible trades. Monetary totals and payoff ratios stay per currency.
    Excursions are reconstructed from prices available at each sample date.
    """
    day = datetime.fromtimestamp(as_of, UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    monday = int((day - timedelta(days=day.weekday())).timestamp())
    first = monday - 11 * WEEK
    closed = tuple(
        row
        for row in rows
        if row.trade is not None
        and row.trade.remaining == 0
        and row.exit_at is not None
        and row.realized_pnl is not None
    )
    currencies = sorted({row.contract.currency for row in closed})
    values: dict[str, list[MetricTrend]] = {
        key: [] for key in ("win", "pnl", "rr", "dip", "peak")
    }
    samples: dict[str, list[Decimal | None]] = {
        key: [] for key in ("win", "dip", "peak")
    }
    pnl: dict[str, list[Decimal | None]] = {c: [] for c in currencies}
    rr: dict[str, list[Decimal | None]] = {c: [] for c in currencies}
    for index in range(12):
        week = first + index * WEEK
        end = min(week + WEEK, as_of + 1)
        start = week - 3 * WEEK
        recent = tuple(
            r for r in closed if r.exit_at is not None and start <= r.exit_at < end
        )
        outcomes = [
            r.realized_pnl
            for r in recent
            if r.realized_pnl is not None and r.realized_pnl != 0
        ]
        samples["win"].append(
            D(sum(p > 0 for p in outcomes)) / len(outcomes) * 100
            if len(outcomes) >= 2
            else None
        )
        for currency in currencies:
            realized = [
                r.realized_pnl
                for r in closed
                if r.contract.currency == currency
                and r.exit_at is not None
                and first <= r.exit_at < end
                and r.realized_pnl is not None
            ]
            pnl[currency].append(sum(realized, D(0)) if realized else None)
            wins = [
                r.realized_pnl
                for r in recent
                if r.contract.currency == currency
                and r.realized_pnl is not None
                and r.realized_pnl > 0
            ]
            losses = [
                -r.realized_pnl
                for r in recent
                if r.contract.currency == currency
                and r.realized_pnl is not None
                and r.realized_pnl < 0
            ]
            rr[currency].append(
                (sum(wins, D(0)) / len(wins)) / (sum(losses, D(0)) / len(losses))
                if wins and losses
                else None
            )
        dips, peaks = [], []
        for row in rows:
            if not (
                row.entry_at is not None
                and start <= row.entry_at < end
                and row.entry_price
                and row.coverage == "Observed minutes"
                and row.analysis
            ):
                continue
            bars = tuple(
                b for b in row.analysis.bars if row.entry_at < b.timestamp < end
            )
            if not bars:
                continue
            dips.append(
                min(D(0), (min(b.low for b in bars) / row.entry_price - 1) * 100)
            )
            peaks.append(
                max(D(0), (max(b.high for b in bars) / row.entry_price - 1) * 100)
            )
        samples["dip"].append(sum(dips, D(0)) / len(dips) if len(dips) >= 2 else None)
        samples["peak"].append(
            sum(peaks, D(0)) / len(peaks) if len(peaks) >= 2 else None
        )
    for key, series in samples.items():
        values[key].append(MetricTrend("", tuple(series)))
    values["pnl"] = [MetricTrend(c, tuple(pnl[c])) for c in currencies]
    values["rr"] = [MetricTrend(c, tuple(rr[c])) for c in currencies]
    return {key: tuple(series) for key, series in values.items()}
