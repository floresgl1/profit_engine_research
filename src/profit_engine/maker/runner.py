"""Live paper market maker: poll books and trades, quote, record pretend fills.

Read-only: it only GETs public books and trades through the venue adapter;
every quote and fill is in memory and in the local database.

Each tick (default every 10 s):
- every `market_refresh`, re-list the series' open markets; pull quotes on
  any market that is no longer open, and record resolutions for markets
  where we still hold a position;
- fetch all books in one request and, per market, the trades since the last
  tick (re-reading a couple of seconds of overlap, de-duplicated by id);
- step each market's MarketMaker and store its fills.

Positions and cash are rebuilt from stored fills on start, so a restart
loses only the queue places of the quotes that were resting.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from profit_engine.core import InvalidOrderBook, Market, utc_now
from profit_engine.maker.queue import PublicTrade
from profit_engine.maker.quoter import MakerFill, MarketMaker, QuoterConfig
from profit_engine.storage import Store

log = logging.getLogger(__name__)

OVERLAP = timedelta(seconds=2)  # re-read this much before the last tick; ids de-duplicate


@dataclass(frozen=True)
class RunnerConfig:
    strategy: str = "join_touch_v1"
    interval: timedelta = timedelta(seconds=10)
    market_refresh: timedelta = timedelta(minutes=10)


class MakerRunner:
    def __init__(
        self,
        source,  # KalshiSource (list_markets, fetch_books, fetch_trades, fetch_resolutions)
        store: Store,
        quoter: QuoterConfig,
        config: RunnerConfig = RunnerConfig(),
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.source = source
        self.store = store
        self.quoter = quoter
        self.config = config
        self._now = now
        self.makers: dict[str, MarketMaker] = {}
        self.open: dict[str, Market] = {}
        self._since: dict[str, datetime] = {}
        self._seen: dict[str, dict[str, datetime]] = {}
        self._refreshed: datetime | None = None
        self._restore()

    def _restore(self) -> None:
        for _, _, fill in self.store.maker_fills(self.config.strategy):
            m = self._maker(fill.market_id)
            m.cash += fill.cash
            m.position += fill.position

    def _maker(self, ticker: str) -> MarketMaker:
        if ticker not in self.makers:
            self.makers[ticker] = MarketMaker(ticker, self.quoter)
        return self.makers[ticker]

    # --- one tick ----------------------------------------------------------------------------

    def tick(self) -> list[MakerFill]:
        now = self._now()
        if self._refreshed is None or now - self._refreshed >= self.config.market_refresh:
            self._refresh_markets(now)
        books = self.source.fetch_books(list(self.open.values())) if self.open else {}
        fills: list[MakerFill] = []
        for ticker in self.open:
            maker = self._maker(ticker)
            book = books.get(ticker)
            if book is None or isinstance(book, InvalidOrderBook):
                maker.stop()
                continue
            if maker.quotes:
                trades = self._new_trades(ticker, now)
            else:  # nothing resting can fill: skip the request, start reading trades from now
                self._since[ticker] = now
                trades = []
            for fill in maker.step(book, trades, now):
                self.store.add_maker_fill(self.config.strategy, "kalshi", fill)
                fills.append(fill)
        return fills

    def _new_trades(self, ticker: str, now: datetime) -> list[PublicTrade]:
        since = self._since.get(ticker)
        self._since[ticker] = now
        if since is None:
            return []  # first look at this market: nothing before our quotes exist
        seen = self._seen.setdefault(ticker, {})
        fresh = [t for t in self.source.fetch_trades(ticker, since - OVERLAP) if t.trade_id not in seen]
        for t in fresh:
            seen[t.trade_id] = t.at
        horizon = since - 3 * OVERLAP
        for tid in [tid for tid, at in seen.items() if at < horizon]:
            del seen[tid]
        return fresh

    def _refresh_markets(self, now: datetime) -> None:
        listed = {m.market_id: m for m in self.source.list_markets()}
        for ticker in set(self.open) - set(listed):
            self._maker(ticker).stop()
            self._since.pop(ticker, None)
            self._seen.pop(ticker, None)
        self.open = listed
        self._refreshed = now
        self._record_resolutions()

    def _record_resolutions(self) -> None:
        held = [t for t, m in self.makers.items() if m.position != 0 and t not in self.open]
        unresolved = [t for t in held if self.store.resolution("kalshi", t) is None]
        if not unresolved:
            return
        markets = list(self.source.fetch_markets(unresolved).values())
        for res in self.source.fetch_resolutions(markets).values():
            self.store.upsert_resolution(res)

    # --- loop ----------------------------------------------------------------------------------

    def run_forever(self, cycles: int | None = None) -> None:
        n, fills_total = 0, 0
        while cycles is None or n < cycles:
            started, cpu = time.monotonic(), time.process_time()
            try:
                fills_total += len(self.tick())
            except Exception:  # one bad tick (network, parse) must not end a weeks-long run
                log.exception("maker tick failed")
            n += 1
            if n % 30 == 0 or n == 1:
                quoting = sum(1 for t in self.open if self._maker(t).quotes)
                exposure = sum(abs(m.position) for m in self.makers.values())
                log.info(
                    "maker tick %d: %d open markets, %d quoting, %d fills so far, |position| %s, cpu %.3fs",
                    n, len(self.open), quoting, fills_total, exposure, time.process_time() - cpu,
                )  # fmt: skip
            sleep = self.config.interval.total_seconds() - (time.monotonic() - started)
            if sleep > 0 and (cycles is None or n < cycles):
                time.sleep(sleep)


# --- results -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketResult:
    market_id: str
    fills: int
    contracts: Decimal
    cash: Decimal
    position: Decimal
    settled_value: Decimal | None  # YES payout if resolved

    @property
    def pnl(self) -> Decimal | None:
        if self.settled_value is None:
            return None
        return self.cash + self.position * self.settled_value


def results(fills: list[MakerFill], resolutions: dict[str, Decimal]) -> list[MarketResult]:
    by_market: dict[str, list[MakerFill]] = {}
    for f in fills:
        by_market.setdefault(f.market_id, []).append(f)
    out = []
    for ticker, fs in sorted(by_market.items()):
        out.append(MarketResult(
            ticker, len(fs), sum((f.quantity for f in fs), Decimal(0)),
            sum((f.cash for f in fs), Decimal(0)), sum((f.position for f in fs), Decimal(0)), resolutions.get(ticker),
        ))  # fmt: skip
    return out


def summary(rows: list[MarketResult]) -> str:
    settled = [r for r in rows if r.pnl is not None]
    open_ = [r for r in rows if r.pnl is None]
    contracts = sum((r.contracts for r in settled), Decimal(0))
    pnl = sum((r.pnl for r in settled), Decimal(0))
    lines = [
        f"settled markets: {len(settled)}  contracts filled: {contracts}  pnl: {pnl:.4f}"
        + (f"  per contract: {100 * pnl / contracts:+.2f}c" if contracts else ""),
        f"open markets with fills: {len(open_)}  open |position|: {sum((abs(r.position) for r in open_), Decimal(0))}",
    ]
    for r in sorted(settled, key=lambda r: r.pnl)[:5]:
        lines.append(f"  worst: {r.market_id}  pnl {r.pnl:+.4f}  position at close {r.position}")
    return "\n".join(lines)
