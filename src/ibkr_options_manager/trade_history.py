"""Durable, observation-only inputs for future trade analysis.

Contract price paths are shared across entries. Execution IDs retain the separate
trade evidence; a position's average cost is never treated as an entry timestamp.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .broker.read_only import (
    CapturedContract,
    CapturedExecution,
    CapturedPosition,
    PortfolioRequest,
)


@dataclass(frozen=True)
class MinuteBar:
    timestamp: int  # UTC epoch seconds at the start of a completed minute
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        if self.timestamp <= 0 or self.timestamp % 60:
            raise ValueError("bar timestamp must be a UTC minute boundary")
        if not all(p.is_finite() and p >= 0 for p in (self.high, self.low, self.close)):
            raise ValueError("bar prices must be finite and nonnegative")
        if not self.low <= self.close <= self.high:
            raise ValueError("bar must satisfy low <= close <= high")


@dataclass(frozen=True)
class Discovery:
    contracts: tuple[CapturedContract, ...]
    fills: tuple[CapturedExecution, ...]
    errors: tuple[str, ...] = ()
    positions: tuple[CapturedPosition, ...] = ()


class HistorySource(Protocol):
    def discover(self, request: PortfolioRequest) -> Discovery: ...

    def history(
        self,
        request: PortfolioRequest,
        contract: CapturedContract,
        start: int,
        end: int,
    ) -> tuple[MinuteBar, ...]: ...


def validate_contract(contract: CapturedContract) -> None:
    if (
        contract.con_id <= 0
        or contract.sec_type != "OPT"
        or contract.right not in ("C", "P")
        or not contract.strike.is_finite()
        or contract.strike <= 0
        or not contract.multiplier.is_finite()
        or contract.multiplier <= 0
        or not contract.currency
        or not contract.trading_class
        or not contract.exchange
        or len(contract.expiry) != 8
    ):
        raise ValueError("incomplete option identity")
    datetime.strptime(contract.expiry, "%Y%m%d")


def contract_identity(contract: CapturedContract) -> tuple[object, ...]:
    return (
        contract.con_id,
        contract.sec_type,
        contract.expiry,
        contract.strike,
        contract.right,
        contract.multiplier,
        contract.currency,
        contract.trading_class,
    )


def _contract_json(contract: CapturedContract) -> str:
    return json.dumps(asdict(contract), default=str, sort_keys=True)


def _decode_contract(payload: str) -> CapturedContract:
    data = json.loads(payload)
    data["strike"] = Decimal(data["strike"])
    data["multiplier"] = Decimal(data["multiplier"])
    return CapturedContract(**data)


def execution_epoch(value: str, fallback_zone: str | None = None) -> int | None:
    """Normalize only times with an explicit zone; reject DST ambiguity."""
    parts = value.split()
    try:
        if len(parts) not in (2, 3):
            return None
        zone = parts[2] if len(parts) == 3 else fallback_zone
        if zone is None:
            return None
        local = datetime.strptime(" ".join(parts[:2]), "%Y%m%d %H:%M:%S")
        aware = local.replace(tzinfo=ZoneInfo(zone))
        if aware.utcoffset() != aware.replace(fold=1).utcoffset():
            return None
        if aware.astimezone(UTC).astimezone(aware.tzinfo).replace(tzinfo=None) != local:
            return None
        return int(aware.timestamp())
    except (ValueError, ZoneInfoNotFoundError):
        return None


class TradeHistoryStore:
    """SQLite transactions keep bars and acknowledged request cursors together."""

    def __init__(self, path: Path, execution_timezone: str | None = None) -> None:
        if execution_timezone is not None:
            try:
                ZoneInfo(execution_timezone)
            except ZoneInfoNotFoundError as error:
                raise ValueError("unknown TWS execution timezone") from error
        self.execution_timezone = execution_timezone
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("unsupported trade history schema")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS contracts (
                account TEXT NOT NULL, con_id INTEGER NOT NULL,
                identity TEXT NOT NULL, first_seen INTEGER NOT NULL,
                cursor INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '', PRIMARY KEY(account, con_id)
            );
            CREATE TABLE IF NOT EXISTS fills (
                account TEXT NOT NULL, exec_id TEXT NOT NULL, con_id INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(account, exec_id)
            );
            CREATE TABLE IF NOT EXISTS bars (
                con_id INTEGER NOT NULL, timestamp INTEGER NOT NULL,
                high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
                basis TEXT NOT NULL CHECK(basis = 'BID'),
                PRIMARY KEY(con_id, timestamp, basis)
            );
            CREATE TABLE IF NOT EXISTS requests (
                account TEXT NOT NULL, con_id INTEGER NOT NULL,
                start INTEGER NOT NULL, end INTEGER NOT NULL,
                status TEXT NOT NULL, bar_count INTEGER NOT NULL,
                error TEXT NOT NULL, PRIMARY KEY(account, con_id, start, end)
            );
            CREATE TABLE IF NOT EXISTS position_observations (
                account TEXT NOT NULL, con_id INTEGER NOT NULL,
                observed_at INTEGER NOT NULL, quantity TEXT NOT NULL,
                average_cost TEXT NOT NULL,
                PRIMARY KEY(account, con_id, observed_at)
            );
            CREATE TABLE IF NOT EXISTS discovery_errors (
                account TEXT NOT NULL, observed_at INTEGER NOT NULL,
                errors TEXT NOT NULL, PRIMARY KEY(account, observed_at)
            );
            CREATE TABLE IF NOT EXISTS execution_times (
                account TEXT NOT NULL, exec_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL, source_timezone TEXT NOT NULL,
                PRIMARY KEY(account, exec_id)
            );
            PRAGMA user_version=1;
        """)

    def close(self) -> None:
        self.db.close()

    def register(self, account: str, discovery: Discovery, now: int) -> None:
        contracts = {c.con_id: c for c in discovery.contracts}
        if len(contracts) != len(discovery.contracts):
            raise ValueError("ambiguous discovery contracts")
        for contract in contracts.values():
            validate_contract(contract)
        for fill in discovery.fills:
            if (
                fill.account != account
                or fill.con_id not in contracts
                or not fill.exec_id
                or not fill.time
                or fill.side not in ("BOT", "SLD")
                or not fill.quantity.is_finite()
                or fill.quantity <= 0
                or not fill.price.is_finite()
                or fill.price < 0
            ):
                raise ValueError("invalid execution evidence")
        for position in discovery.positions:
            if (
                position.account != account
                or position.contract.con_id not in contracts
                or not position.quantity.is_finite()
                or position.quantity <= 0
                or not position.average_cost.is_finite()
                or position.average_cost < 0
            ):
                raise ValueError("invalid position evidence")
        with self.db:
            if discovery.errors:
                self.db.execute(
                    "INSERT OR REPLACE INTO discovery_errors VALUES(?,?,?)",
                    (account, now, json.dumps(discovery.errors)),
                )
            for contract in contracts.values():
                old = self.db.execute(
                    "SELECT identity FROM contracts WHERE account=? AND con_id=?",
                    (account, contract.con_id),
                ).fetchone()
                if old and contract_identity(
                    _decode_contract(old[0])
                ) != contract_identity(contract):
                    raise ValueError("saved contract identity conflict")
                self.db.execute(
                    "INSERT OR IGNORE INTO contracts"
                    "(account,con_id,identity,first_seen,cursor) VALUES(?,?,?,?,?)",
                    (
                        account,
                        contract.con_id,
                        _contract_json(contract),
                        now,
                        now // 60 * 60 - 86400,
                    ),
                )
            for fill in discovery.fills:
                payload = json.dumps(asdict(fill), default=str, sort_keys=True)
                old = self.db.execute(
                    "SELECT payload FROM fills WHERE account=? AND exec_id=?",
                    (account, fill.exec_id),
                ).fetchone()
                if old and old[0] != payload:
                    raise ValueError("execution ID reused with conflicting evidence")
                self.db.execute(
                    "INSERT OR IGNORE INTO fills VALUES(?,?,?,?)",
                    (account, fill.exec_id, fill.con_id, payload),
                )
                timestamp = execution_epoch(fill.time, self.execution_timezone)
                if timestamp is not None:
                    previous_time = self.db.execute(
                        "SELECT timestamp FROM execution_times "
                        "WHERE account=? AND exec_id=?",
                        (account, fill.exec_id),
                    ).fetchone()
                    if previous_time and previous_time[0] != timestamp:
                        raise ValueError("execution timezone conflicts with saved time")
                    self.db.execute(
                        "INSERT OR IGNORE INTO execution_times VALUES(?,?,?,?)",
                        (
                            account,
                            fill.exec_id,
                            timestamp,
                            fill.time.split()[2]
                            if len(fill.time.split()) == 3
                            else self.execution_timezone,
                        ),
                    )
                    if fill.side == "BOT" and (not old or not previous_time):
                        self.db.execute(
                            "UPDATE contracts SET cursor=MIN(cursor,?) "
                            "WHERE account=? AND con_id=?",
                            (timestamp // 60 * 60, account, fill.con_id),
                        )
            for position in discovery.positions:
                con_id = position.contract.con_id
                values = (str(position.quantity), str(position.average_cost))
                previous = self.db.execute(
                    "SELECT quantity,average_cost FROM position_observations "
                    "WHERE account=? AND con_id=? ORDER BY observed_at DESC LIMIT 1",
                    (account, con_id),
                ).fetchone()
                if previous != values:
                    self.db.execute(
                        "INSERT OR REPLACE INTO position_observations "
                        "VALUES(?,?,?,?,?)",
                        (account, con_id, now, *values),
                    )

    def tracked(self, account: str) -> list[tuple[CapturedContract, int]]:
        return [
            (_decode_contract(row[0]), row[1])
            for row in self.db.execute(
                "SELECT identity,cursor FROM contracts WHERE account=? ORDER BY con_id",
                (account,),
            )
        ]

    def record(
        self,
        account: str,
        contract: CapturedContract,
        start: int,
        end: int,
        bars: tuple[MinuteBar, ...],
        error: str = "",
        *,
        advance: bool = True,
    ) -> None:
        if not 0 < start < end or start % 60 or end % 60:
            raise ValueError("invalid history interval")
        if any(not start <= bar.timestamp < end for bar in bars):
            raise ValueError("bar outside requested completed-minute interval")
        if len({b.timestamp for b in bars}) != len(bars):
            raise ValueError("duplicate bars in history response")
        if error and bars:
            raise ValueError("failed responses cannot publish bars")
        # Empty acknowledged responses are recorded, but never certified as coverage.
        status = "error" if error else "received" if bars else "empty"
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?, 'BID')",
                [
                    (
                        contract.con_id,
                        b.timestamp,
                        str(b.high),
                        str(b.low),
                        str(b.close),
                    )
                    for b in bars
                ],
            )
            self.db.execute(
                "INSERT OR REPLACE INTO requests VALUES(?,?,?,?,?,?,?)",
                (account, contract.con_id, start, end, status, len(bars), error),
            )
            self.db.execute(
                "UPDATE contracts SET status=?,error=? WHERE account=? AND con_id=?",
                (status, error, account, contract.con_id),
            )
            if not error and advance:
                self.db.execute(
                    "UPDATE contracts SET cursor=MAX(cursor,?) "
                    "WHERE account=? AND con_id=?",
                    (end, account, contract.con_id),
                )

    def expired(self, account: str, con_id: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE contracts SET status='expired_unverified',error=? "
                "WHERE account=? AND con_id=?",
                (
                    "Expired option history/settlement cannot be guaranteed",
                    account,
                    con_id,
                ),
            )


class TradeHistoryCollector:
    def __init__(self, store: TradeHistoryStore, source: HistorySource) -> None:
        self.store, self.source = store, source

    def collect(self, request: PortfolioRequest, now: int) -> dict[str, int]:
        discovery = self.source.discover(request)
        self.store.register(request.expected_account, discovery, now)
        counts = {
            "contracts": 0,
            "bars": 0,
            "errors": len(discovery.errors),
            "expired": 0,
        }
        end = now // 60 * 60  # never persist the still-forming minute
        for contract, cursor in self.store.tracked(request.expected_account):
            counts["contracts"] += 1
            # Allow the expiration date through the latest possible exchange day.
            # This is a request cutoff only, never an inferred settlement value.
            cutoff = (
                int(
                    datetime.strptime(contract.expiry, "%Y%m%d")
                    .replace(tzinfo=UTC)
                    .timestamp()
                )
                + 2 * 86400
            )
            if now >= cutoff:
                self.store.expired(request.expected_account, contract.con_id)
                counts["expired"] += 1
                continue
            if cursor > end:
                raise ValueError("collector clock precedes saved history cursor")
            # Retry a minute for vendor corrections; fetch a day at a time after gaps.
            start = cursor - 60
            windows = [(start, min(end, start + 86400), True)]
            if end - start > 86400:
                # A stubborn old gap must not prevent saving the final session now.
                windows.append((end - 86400, end, False))
            for start, stop, advance in windows:
                if start >= stop:
                    continue
                try:
                    bars = self.source.history(request, contract, start, stop)
                    self.store.record(
                        request.expected_account,
                        contract,
                        start,
                        stop,
                        bars,
                        advance=advance,
                    )
                except (RuntimeError, ValueError, OSError) as error:
                    self.store.record(
                        request.expected_account,
                        contract,
                        start,
                        stop,
                        (),
                        str(error),
                        advance=False,
                    )
                    counts["errors"] += 1
                else:
                    counts["bars"] += len(bars)
        return counts
