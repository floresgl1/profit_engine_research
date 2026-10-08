"""SQLite persistence.

Conventions:
- Decimals are stored as TEXT (exact, never float).
- Timestamps are stored as fixed-width ISO-8601 UTC TEXT with microseconds,
  so string order equals time order.
- Every poll writes one `book_snapshots` row; the levels themselves live in
  `book_states`, keyed by content hash, so an unchanged book costs one small
  row per poll instead of a full copy.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from profit_engine.core import (
    FeeSchedule,
    Fill,
    FillStatus,
    Level,
    Market,
    MarketStatus,
    OrderBook,
    Outcome,
    PaperOrder,
    Prediction,
    Resolution,
    Side,
    require_utc,
)
from profit_engine.maker.queue import PublicTrade
from profit_engine.maker.quoter import MakerFill

SCHEMA_VERSION = 1

# Price levels kept per side when a book is stored. Full depth made stored books
# grow ~200 MB a day on 90 markets; scoring needs the touch, today's quoting
# strategies join it, and paper fills capped at a fraction of visible depth only
# get more conservative with less depth shown.
BOOK_LEVELS = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS markets (
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    title TEXT NOT NULL,
    yes_label TEXT NOT NULL,
    status TEXT NOT NULL,
    close_time TEXT,
    fee_schedule TEXT,
    contract_step TEXT NOT NULL,
    min_order_size TEXT NOT NULL,
    venue_meta TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (venue, market_id)
);

CREATE TABLE IF NOT EXISTS book_states (
    hash TEXT PRIMARY KEY,
    bids TEXT NOT NULL,
    asks TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY,
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    state_hash TEXT NOT NULL REFERENCES book_states(hash)
);
CREATE INDEX IF NOT EXISTS book_snapshots_lookup ON book_snapshots (venue, market_id, received_at);

CREATE TABLE IF NOT EXISTS skipped_snapshots (
    id INTEGER PRIMARY KEY,
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    at TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resolutions (
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    yes_value TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    PRIMARY KEY (venue, market_id)
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    model TEXT NOT NULL,
    predicted_at TEXT NOT NULL,
    probability TEXT NOT NULL,
    market_probability TEXT,
    best_bid TEXT,
    best_ask TEXT,
    book_received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS predictions_market ON predictions (venue, market_id);

CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY,
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL,
    limit_price TEXT,
    decided_at TEXT NOT NULL,
    status TEXT NOT NULL,
    legs TEXT NOT NULL,
    filled_quantity TEXT NOT NULL,
    gross TEXT NOT NULL,
    fee TEXT NOT NULL,
    book_received_at TEXT,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maker_fills (
    id INTEGER PRIMARY KEY,
    strategy TEXT NOT NULL,
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    fee TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS maker_fills_strategy ON maker_fills (strategy, market_id);

-- What the paper market maker saw, for replaying other strategies on the same data.
-- (Its books go in book_snapshots / book_states like the logger's.)
CREATE TABLE IF NOT EXISTS maker_trades (
    trade_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    at TEXT NOT NULL,
    yes_price TEXT NOT NULL,
    count TEXT NOT NULL,
    taker_yes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS maker_trades_market ON maker_trades (market_id, at);

CREATE TABLE IF NOT EXISTS maker_fair (
    id INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    at TEXT NOT NULL,
    fair TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS maker_fair_market ON maker_fair (market_id, at);

-- One row per maker tick; books and model prices are stored only when they change,
-- so replay carries the last one forward to each tick.
CREATE TABLE IF NOT EXISTS maker_ticks (at TEXT PRIMARY KEY);

CREATE TABLE IF NOT EXISTS maker_runs (
    strategy TEXT NOT NULL,
    started_at TEXT NOT NULL
);
"""


def ts(value: datetime) -> str:
    require_utc(value, "timestamp")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def from_ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _undec(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def _levels_json(levels: Iterable[Level]) -> str:
    return json.dumps([[str(lvl.price), str(lvl.size)] for lvl in levels], separators=(",", ":"))


def _levels(text: str) -> tuple[Level, ...]:
    return tuple(Level(Decimal(p), Decimal(s)) for p, s in json.loads(text))


def fee_to_json(fee: FeeSchedule | None) -> str | None:
    if fee is None:
        return None
    return json.dumps(
        {
            "rate": str(fee.rate),
            "fill_quantum": str(fee.fill_quantum),
            "fill_rounding": fee.fill_rounding,
            "cash_quantum": _dec(fee.cash_quantum),
        }
    )


def fee_from_json(text: str | None) -> FeeSchedule | None:
    if text is None:
        return None
    d = json.loads(text)
    return FeeSchedule(Decimal(d["rate"]), Decimal(d["fill_quantum"]), d["fill_rounding"], _undec(d["cash_quantum"]))


@dataclass(frozen=True, slots=True)
class ScoredRow:
    """A prediction joined with its market's resolution."""

    prediction: Prediction
    outcome: Decimal


class Store:
    def __init__(self, path: str | Path, book_levels: int | None = BOOK_LEVELS) -> None:
        """`book_levels`: price levels kept per side in stored books (None = full depth)."""
        self.path = str(path)
        self.book_levels = book_levels
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        with self._conn:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                raise RuntimeError(f"database schema v{row[0]}, code expects v{SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    # --- markets ------------------------------------------------------------

    def upsert_market(self, market: Market, seen_at: datetime) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (venue, market_id) DO UPDATE SET
                    title = excluded.title, yes_label = excluded.yes_label, status = excluded.status,
                    close_time = excluded.close_time, fee_schedule = excluded.fee_schedule,
                    contract_step = excluded.contract_step, min_order_size = excluded.min_order_size,
                    venue_meta = excluded.venue_meta, updated_at = excluded.updated_at
                """,
                (
                    market.venue,
                    market.market_id,
                    market.title,
                    market.yes_label,
                    market.status.value,
                    ts(market.close_time) if market.close_time else None,
                    fee_to_json(market.fee_schedule),
                    str(market.contract_step),
                    str(market.min_order_size),
                    json.dumps(dict(market.venue_meta), sort_keys=True),
                    ts(seen_at),
                    ts(seen_at),
                ),
            )

    def market(self, venue: str, market_id: str) -> Market | None:
        row = self._conn.execute(
            "SELECT * FROM markets WHERE venue = ? AND market_id = ?", (venue, market_id)
        ).fetchone()
        return _market(row) if row else None

    def markets(self, venue: str | None = None, statuses: Iterable[MarketStatus] | None = None) -> list[Market]:
        sql, args = "SELECT * FROM markets WHERE 1=1", []
        if venue is not None:
            sql += " AND venue = ?"
            args.append(venue)
        if statuses is not None:
            values = [s.value for s in statuses]
            sql += f" AND status IN ({','.join('?' * len(values))})"
            args.extend(values)
        return [_market(row) for row in self._conn.execute(sql + " ORDER BY venue, market_id", args)]

    def unresolved_markets(self, venue: str) -> list[Market]:
        rows = self._conn.execute(
            """
            SELECT m.* FROM markets m
            LEFT JOIN resolutions r ON r.venue = m.venue AND r.market_id = m.market_id
            WHERE m.venue = ? AND r.market_id IS NULL ORDER BY m.market_id
            """,
            (venue,),
        )
        return [_market(row) for row in rows]

    # --- books ----------------------------------------------------------------

    def stored_levels(self, book: OrderBook) -> tuple[tuple[Level, ...], tuple[Level, ...]]:
        """The part of `book` this store keeps: the best `book_levels` levels per side."""
        n = self.book_levels
        return (book.bids, book.asks) if n is None else (book.bids[:n], book.asks[:n])

    def add_snapshot(self, book: OrderBook) -> None:
        kept_bids, kept_asks = self.stored_levels(book)
        bids, asks = _levels_json(kept_bids), _levels_json(kept_asks)
        digest = hashlib.sha256(f"{bids}|{asks}".encode()).hexdigest()
        with self._conn:
            self._conn.execute("INSERT OR IGNORE INTO book_states VALUES (?, ?, ?)", (digest, bids, asks))
            self._conn.execute(
                "INSERT INTO book_snapshots (venue, market_id, received_at, state_hash) VALUES (?, ?, ?, ?)",
                (book.venue, book.market_id, ts(book.received_at), digest),
            )

    def compact_books(self, levels: int, batch: int = 5000) -> int:
        """Trim every stored book to its best `levels` per side, in place; returns rows changed.

        Rows keep their key, so snapshots still point at them. Run `vacuum()`
        afterwards to give the space back to the disk.
        """
        changed = 0
        last = ""
        while True:
            rows = self._conn.execute(
                "SELECT hash, bids, asks FROM book_states WHERE hash > ? ORDER BY hash LIMIT ?", (last, batch)
            ).fetchall()
            if not rows:
                return changed
            updates = []
            for digest, bids, asks in rows:
                b, a = json.loads(bids), json.loads(asks)
                if len(b) > levels or len(a) > levels:
                    updates.append((json.dumps(b[:levels], separators=(",", ":")),
                                    json.dumps(a[:levels], separators=(",", ":")), digest))  # fmt: skip
            with self._conn:
                self._conn.executemany("UPDATE book_states SET bids = ?, asks = ? WHERE hash = ?", updates)
            changed += len(updates)
            last = rows[-1][0]

    def vacuum(self) -> None:
        """Rewrite the file to release freed pages (needs free disk about the file's size, and no other writer)."""
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._conn.execute("VACUUM")
        # In WAL mode VACUUM writes the compacted copy to the WAL; the main file only
        # shrinks once that is checkpointed back.
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def book_at_or_after(self, venue: str, market_id: str, when: datetime) -> OrderBook | None:
        row = self._conn.execute(
            """
            SELECT s.venue, s.market_id, s.received_at, b.bids, b.asks
            FROM book_snapshots s JOIN book_states b ON b.hash = s.state_hash
            WHERE s.venue = ? AND s.market_id = ? AND s.received_at >= ?
            ORDER BY s.received_at LIMIT 1
            """,
            (venue, market_id, ts(when)),
        ).fetchone()
        return _book(row) if row else None

    def latest_book(self, venue: str, market_id: str) -> OrderBook | None:
        row = self._conn.execute(
            """
            SELECT s.venue, s.market_id, s.received_at, b.bids, b.asks
            FROM book_snapshots s JOIN book_states b ON b.hash = s.state_hash
            WHERE s.venue = ? AND s.market_id = ?
            ORDER BY s.received_at DESC LIMIT 1
            """,
            (venue, market_id),
        ).fetchone()
        return _book(row) if row else None

    def books(self, venue: str, market_id: str) -> Iterator[OrderBook]:
        rows = self._conn.execute(
            """
            SELECT s.venue, s.market_id, s.received_at, b.bids, b.asks
            FROM book_snapshots s JOIN book_states b ON b.hash = s.state_hash
            WHERE s.venue = ? AND s.market_id = ? ORDER BY s.received_at
            """,
            (venue, market_id),
        )
        for row in rows:
            yield _book(row)

    def snapshot_count(self) -> tuple[int, int]:
        """(snapshot rows, distinct book states)."""
        snaps = self._conn.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
        states = self._conn.execute("SELECT COUNT(*) FROM book_states").fetchone()[0]
        return snaps, states

    def record_skip(self, venue: str, market_id: str, at: datetime, reason: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO skipped_snapshots (venue, market_id, at, reason) VALUES (?, ?, ?, ?)",
                (venue, market_id, ts(at), reason),
            )

    # --- resolutions ----------------------------------------------------------

    def upsert_resolution(self, resolution: Resolution) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO resolutions VALUES (?, ?, ?, ?)
                ON CONFLICT (venue, market_id) DO UPDATE SET
                    yes_value = excluded.yes_value, resolved_at = excluded.resolved_at
                """,
                (resolution.venue, resolution.market_id, str(resolution.yes_value), ts(resolution.resolved_at)),
            )

    def resolution(self, venue: str, market_id: str) -> Resolution | None:
        row = self._conn.execute(
            "SELECT * FROM resolutions WHERE venue = ? AND market_id = ?", (venue, market_id)
        ).fetchone()
        return Resolution(row[0], row[1], Decimal(row[2]), from_ts(row[3])) if row else None

    def resolutions(self) -> list[Resolution]:
        rows = self._conn.execute("SELECT * FROM resolutions ORDER BY resolved_at, venue, market_id")
        return [Resolution(r[0], r[1], Decimal(r[2]), from_ts(r[3])) for r in rows]

    # --- predictions ----------------------------------------------------------

    def add_prediction(self, p: Prediction) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO predictions (venue, market_id, model, predicted_at, probability,
                    market_probability, best_bid, best_ask, book_received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p.venue,
                    p.market_id,
                    p.model,
                    ts(p.predicted_at),
                    str(p.probability),
                    _dec(p.market_probability),
                    _dec(p.best_bid),
                    _dec(p.best_ask),
                    ts(p.book_received_at),
                ),
            )

    def scored_predictions(self, model: str | None = None, series: Sequence[str] = ()) -> list[ScoredRow]:
        """Predictions for resolved markets, oldest first, each with its outcome.

        `series` keeps only markets whose venue_meta series_ticker is one of
        them (Kalshi series such as KXHIGHCHI); empty means every market.
        """
        sql = """
            SELECT p.venue, p.market_id, p.model, p.predicted_at, p.probability, p.market_probability,
                   p.best_bid, p.best_ask, p.book_received_at, r.yes_value
            FROM predictions p JOIN resolutions r ON r.venue = p.venue AND r.market_id = p.market_id
        """
        where: list[str] = []
        args: list[str] = []
        if series:
            sql += " JOIN markets m ON m.venue = p.venue AND m.market_id = p.market_id"
            where.append(f"json_extract(m.venue_meta, '$.series_ticker') IN ({', '.join('?' * len(series))})")
            args.extend(series)
        if model is not None:
            where.append("p.model = ?")
            args.append(model)
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self._conn.execute(sql + " ORDER BY p.predicted_at, p.id", args)
        return [
            ScoredRow(
                Prediction(
                    venue=r[0],
                    market_id=r[1],
                    model=r[2],
                    predicted_at=from_ts(r[3]),
                    probability=Decimal(r[4]),
                    market_probability=_undec(r[5]),
                    best_bid=_undec(r[6]),
                    best_ask=_undec(r[7]),
                    book_received_at=from_ts(r[8]),
                ),
                Decimal(r[9]),
            )
            for r in rows
        ]

    def models(self) -> list[str]:
        return [r[0] for r in self._conn.execute("SELECT DISTINCT model FROM predictions ORDER BY model")]

    # --- paper fills ------------------------------------------------------------

    def add_fill(self, fill: Fill) -> None:
        o = fill.order
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO paper_fills (venue, market_id, outcome, side, quantity, limit_price, decided_at,
                    status, legs, filled_quantity, gross, fee, book_received_at, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    o.venue,
                    o.market_id,
                    o.outcome.value,
                    o.side.value,
                    str(o.quantity),
                    _dec(o.limit_price),
                    ts(o.decided_at),
                    fill.status.value,
                    _levels_json(fill.legs),
                    str(fill.filled_quantity),
                    str(fill.gross),
                    str(fill.fee),
                    ts(fill.book_received_at) if fill.book_received_at else None,
                    fill.reason,
                ),
            )

    def fills(self) -> list[Fill]:
        """All stored paper fills, oldest decision first."""
        fills = []
        for r in self.fill_rows():
            order = PaperOrder(
                venue=r["venue"],
                market_id=r["market_id"],
                outcome=Outcome(r["outcome"]),
                side=Side(r["side"]),
                quantity=Decimal(r["quantity"]),
                decided_at=from_ts(r["decided_at"]),
                limit_price=_undec(r["limit_price"]),
            )
            fills.append(
                Fill(
                    order=order,
                    status=FillStatus(r["status"]),
                    legs=_levels(r["legs"]),
                    filled_quantity=Decimal(r["filled_quantity"]),
                    gross=Decimal(r["gross"]),
                    fee=Decimal(r["fee"]),
                    book_received_at=from_ts(r["book_received_at"]) if r["book_received_at"] else None,
                    reason=r["reason"],
                )
            )
        return fills

    # --- paper market maker -----------------------------------------------------

    def add_maker_fill(self, strategy: str, venue: str, fill: MakerFill) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO maker_fills (strategy, venue, market_id, side, price, quantity, fee, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (strategy, venue, fill.market_id, fill.side, str(fill.price), str(fill.quantity), str(fill.fee), ts(fill.at)),
            )

    def maker_fills(self, strategy: str | None = None) -> list[tuple[str, str, MakerFill]]:
        """(strategy, venue, fill) rows, oldest first."""
        sql = "SELECT strategy, venue, market_id, side, price, quantity, fee, at FROM maker_fills"
        args: list[str] = []
        if strategy is not None:
            sql += " WHERE strategy = ?"
            args.append(strategy)
        rows = self._conn.execute(sql + " ORDER BY at, id", args)
        return [
            (r[0], r[1], MakerFill(r[2], r[3], Decimal(r[4]), Decimal(r[5]), Decimal(r[6]), from_ts(r[7])))
            for r in rows
        ]

    def add_maker_trades(self, market_id: str, trades: Iterable[PublicTrade]) -> None:
        rows = [(t.trade_id, market_id, ts(t.at), str(t.yes_price), str(t.count), int(t.taker_yes)) for t in trades]
        if rows:
            with self._conn:
                self._conn.executemany("INSERT OR IGNORE INTO maker_trades VALUES (?, ?, ?, ?, ?, ?)", rows)

    def maker_trades(self, market_id: str) -> list[PublicTrade]:
        rows = self._conn.execute(
            "SELECT trade_id, at, yes_price, count, taker_yes FROM maker_trades WHERE market_id = ? ORDER BY at, trade_id",
            (market_id,),
        )
        return [PublicTrade(r[0], from_ts(r[1]), Decimal(r[2]), Decimal(r[3]), bool(r[4])) for r in rows]

    def add_maker_fair(self, market_id: str, at: datetime, fair: Decimal) -> None:
        with self._conn:
            self._conn.execute("INSERT INTO maker_fair (market_id, at, fair) VALUES (?, ?, ?)", (market_id, ts(at), str(fair)))

    def maker_fair(self, market_id: str) -> list[tuple[datetime, Decimal]]:
        rows = self._conn.execute("SELECT at, fair FROM maker_fair WHERE market_id = ? ORDER BY at, id", (market_id,))
        return [(from_ts(r[0]), Decimal(r[1])) for r in rows]

    def add_maker_tick(self, at: datetime) -> None:
        with self._conn:
            self._conn.execute("INSERT OR IGNORE INTO maker_ticks VALUES (?)", (ts(at),))

    def maker_ticks(self) -> list[datetime]:
        return [from_ts(r[0]) for r in self._conn.execute("SELECT at FROM maker_ticks ORDER BY at")]

    def add_maker_run(self, strategy: str, started_at: datetime) -> None:
        with self._conn:
            self._conn.execute("INSERT INTO maker_runs VALUES (?, ?)", (strategy, ts(started_at)))

    def maker_runs(self) -> dict[str, datetime]:
        """First start per strategy."""
        rows = self._conn.execute("SELECT strategy, MIN(started_at) FROM maker_runs GROUP BY strategy")
        return {r[0]: from_ts(r[1]) for r in rows}

    def fill_rows(self) -> list[dict[str, str | None]]:
        cursor = self._conn.execute("SELECT * FROM paper_fills ORDER BY decided_at, id")
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor]


def _market(row: tuple) -> Market:
    return Market(
        venue=row[0],
        market_id=row[1],
        title=row[2],
        yes_label=row[3],
        status=MarketStatus(row[4]),
        close_time=from_ts(row[5]) if row[5] else None,
        fee_schedule=fee_from_json(row[6]),
        contract_step=Decimal(row[7]),
        min_order_size=Decimal(row[8]),
        venue_meta=json.loads(row[9]),
    )


def _book(row: tuple) -> OrderBook:
    return OrderBook(row[0], row[1], _levels(row[3]), _levels(row[4]), from_ts(row[2]))


class StoredBookProvider:
    """BookProvider for replay: the first stored snapshot at or after a time."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def book_at_or_after(self, market: Market, when: datetime) -> OrderBook | None:
        return self._store.book_at_or_after(market.venue, market.market_id, when)
