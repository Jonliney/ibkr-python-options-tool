"""Pure long-option trade grouping and descriptive price-path analysis."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal(0)


@dataclass(frozen=True)
class TradeFill:
    exec_id: str
    timestamp: int
    side: str
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class PriceMinute:
    timestamp: int
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class TradeEpisode:
    trade_id: str
    fills: tuple[TradeFill, ...]
    warning: str = ""

    @property
    def entry_at(self) -> int:
        return self.fills[0].timestamp

    @property
    def quantity(self) -> Decimal:
        return sum((f.quantity for f in self.fills if f.side == "BOT"), ZERO)

    @property
    def remaining(self) -> Decimal:
        return sum(
            (f.quantity * (1 if f.side == "BOT" else -1) for f in self.fills), ZERO
        )

    @property
    def entry_price(self) -> Decimal:
        return (
            sum((f.price * f.quantity for f in self.fills if f.side == "BOT"), ZERO)
            / self.quantity
        )

    @property
    def exit_at(self) -> int | None:
        return self.fills[-1].timestamp if self.remaining == 0 else None

    def realized(self, multiplier: Decimal) -> Decimal | None:
        if self.warning or self.remaining != 0:
            return None
        return (
            sum(
                (
                    f.price * f.quantity * (1 if f.side == "SLD" else -1)
                    for f in self.fills
                ),
                ZERO,
            )
            * multiplier
        )


def execution_revision(exec_id: str) -> tuple[str, int]:
    prefix, separator, revision = exec_id.rpartition(".")
    return (prefix, int(revision)) if separator and revision.isdigit() else (exec_id, 0)


def group_trades(fills: tuple[TradeFill, ...]) -> tuple[TradeEpisode, ...]:
    """Flat-to-flat episodes; latest execution corrections supersede older revisions.

    A leading sell is evidence of missing history or a short position. It cannot
    be silently assigned to the next buy. Simultaneous opposite fills also need
    broker ordering evidence, so the affected stream is withheld.
    """
    latest: dict[str, TradeFill] = {}
    for fill in fills:
        if (
            fill.side not in ("BOT", "SLD")
            or fill.timestamp <= 0
            or not fill.quantity.is_finite()
            or fill.quantity <= 0
            or not fill.price.is_finite()
            or fill.price < 0
        ):
            raise ValueError("invalid trade fill")
        identity, revision = execution_revision(fill.exec_id)
        old = latest.get(identity)
        if old is None or revision > execution_revision(old.exec_id)[1]:
            latest[identity] = fill
        elif revision == execution_revision(old.exec_id)[1] and old != fill:
            raise ValueError("conflicting execution revision")
    ordered = sorted(latest.values(), key=lambda f: (f.timestamp, f.exec_id))
    sides: dict[int, set[str]] = {}
    for fill in ordered:
        sides.setdefault(fill.timestamp, set()).add(fill.side)
    if any(len(value) > 1 for value in sides.values()):
        return ()
    trades = []
    current: list[TradeFill] = []
    held = ZERO
    for fill in ordered:
        held += fill.quantity * (1 if fill.side == "BOT" else -1)
        if held < 0:
            return ()
        current.append(fill)
        if held == 0:
            trades.append(
                TradeEpisode(execution_revision(current[0].exec_id)[0], tuple(current))
            )
            current = []
    if current:
        trades.append(
            TradeEpisode(execution_revision(current[0].exec_id)[0], tuple(current))
        )
    return tuple(trades)


@dataclass(frozen=True)
class PriceAnalysis:
    bars: tuple[PriceMinute, ...]
    low: Decimal | None
    high: Decimal | None
    average: Decimal | None
    low_at: int | None
    high_at: int | None
    sequence: str
    stop_then_target: bool | None
    threshold_sequence: str
    warning: str = ""


def analyse_prices(
    trade: TradeEpisode,
    bars: tuple[PriceMinute, ...],
    *,
    stop_percent: Decimal = Decimal(25),
    target_percent: Decimal = Decimal(20),
) -> PriceAnalysis:
    if not ZERO < stop_percent < 100 or target_percent <= 0:
        raise ValueError("invalid analysis thresholds")
    # Skip the entry minute: its extrema can precede the fill. For scale-ins,
    # start after the final buy; earlier prices do not apply to the full size.
    last_buy = max(f.timestamp for f in trade.fills if f.side == "BOT")
    first_minute = last_buy // 60 * 60 + 60
    path = tuple(
        sorted(
            (b for b in bars if b.timestamp >= first_minute), key=lambda b: b.timestamp
        )
    )
    warning = "Starts after the final entry fill." if last_buy != trade.entry_at else ""
    if not path:
        return PriceAnalysis(
            (),
            None,
            None,
            None,
            None,
            None,
            "Unavailable",
            None,
            "Unavailable",
            warning,
        )
    low_bar = min(path, key=lambda b: b.low)
    high_bar = max(path, key=lambda b: b.high)
    sequence = (
        "Low before high"
        if low_bar.timestamp < high_bar.timestamp
        else "High before low"
        if low_bar.timestamp > high_bar.timestamp
        else "Same minute · order unknown"
    )
    stop = trade.entry_price * (1 - stop_percent / 100)
    target = trade.entry_price * (1 + target_percent / 100)
    stop_at = next((b.timestamp for b in path if b.low <= stop), None)
    target_at = next((b.timestamp for b in path if b.high >= target), None)
    both = stop_at is not None and target_at is not None
    stop_then_target: bool | None = None
    if both and stop_at == target_at:
        threshold_sequence = "Same minute · order unknown"
    elif both and stop_at < target_at:  # type: ignore[operator]
        threshold_sequence = "Stop level, then target"
        stop_then_target = True
    elif both:
        threshold_sequence = "Target before stop level"
        stop_then_target = False
    elif target_at is not None:
        threshold_sequence = "Target reached · stop level not seen"
        stop_then_target = False
    elif stop_at is not None:
        threshold_sequence = "Stop level reached · target not seen"
        stop_then_target = False
    else:
        threshold_sequence = "Neither level reached"
        stop_then_target = False
    return PriceAnalysis(
        path,
        low_bar.low,
        high_bar.high,
        sum((b.close for b in path), ZERO) / len(path),
        low_bar.timestamp,
        high_bar.timestamp,
        sequence,
        stop_then_target,
        threshold_sequence,
        warning,
    )
