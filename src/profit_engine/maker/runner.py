"""Live paper market maker: poll books and trades, quote, record pretend fills.

Read-only: it only GETs public books and trades through the venue adapter;
every quote and fill is in memory and in the local database.

Each tick (default every 10 s):
- every `market_refresh`, re-list the series' open markets; pull quotes on
  any market that is no longer open, and record resolutions for markets
  where we still hold a position;
- fetch all books in one request and, per market, the trades since the last
  tick (re-reading a couple of seconds of overlap, de-duplicated by id);
- step every strategy's MarketMaker for the market on the same book and
  trades (so strategies are compared on identical data) and store fills.

With `record` on (the default) it also stores what it sees, for offline
replay of other strategies on the same data: every tick time, every trade,
the markets, and each book and model price whenever it changes (replay
carries the last one forward to each tick).

A strategy with a model margin gets the model's probability for the market
from `fair` (computed once per market per tick, only if some strategy needs
it); None means no model price, and the strategy quotes like v1.

Positions and cash are rebuilt from stored fills on start, so a restart
loses only the queue places of the quotes that were resting.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from profit_engine.core import InvalidOrderBook, Market, OrderBook, utc_now
from profit_engine.maker.queue import PublicTrade
from profit_engine.maker.quoter import MakerFill, MarketMaker, QuoterConfig
from profit_engine.storage import Store

log = logging.getLogger(__name__)
LOG_EVERY = timedelta(minutes=5)  # one progress line this often, whatever the tick interval

_UNSET = object()  # no model price recorded yet for a market
# Re-read this much before the last tick (ids de-duplicate), so a trade the venue publishes late is
# still read. Fixed, not a share of the interval: at 2-second ticks a 2 s overlap would drop for good
# every trade published more than about 4 s late, and missing sweeps would flatter faster polling.
OVERLAP = timedelta(seconds=10)


@dataclass(frozen=True)
class RunnerConfig:
    interval: timedelta = timedelta(seconds=10)
    market_refresh: timedelta = timedelta(minutes=10)
    # Store every book, trade and model price seen, so other strategies can be replayed
    # on exactly this data. Costs a trades request per open market per tick.
    record: bool = True


@dataclass(frozen=True)
class Strategy:
    name: str  # stored with every fill
    quoter: QuoterConfig


FairPrice = Callable[[Market, OrderBook], Decimal | None]


class MakerRunner:
    def __init__(
        self,
        source,  # KalshiSource (list_markets, fetch_books, fetch_trades, fetch_markets, fetch_resolutions)
        store: Store,
        strategies: Sequence[Strategy],
        config: RunnerConfig = RunnerConfig(),
        now: Callable[[], datetime] = utc_now,
        fair: FairPrice | None = None,
    ) -> None:
        if not strategies or len({s.name for s in strategies}) != len(strategies):
            raise ValueError("need at least one strategy, with distinct names")
        self.source = source
        self.store = store
        self.strategies = list(strategies)
        self.config = config
        self._now = now
        self._fair = fair
        self._needs_fair = any(s.quoter.model_margin is not None for s in strategies)
        self.makers: dict[str, dict[str, MarketMaker]] = {s.name: {} for s in strategies}
        self._last_book: dict[str, tuple] = {}  # last recorded (bids, asks) per market
        self._last_fair: dict[str, Decimal | None] = {}
        self._traded: set[str] = set()  # markets any strategy has filled in: they need a resolution
        started = now()
        for s in strategies:
            store.add_maker_run(s.name, started)
        self.open: dict[str, Market] = {}
        self._since: dict[str, datetime] = {}
        self._seen: dict[str, dict[str, datetime]] = {}
        self._refreshed: datetime | None = None
        self._restore()

    def _restore(self) -> None:
        for s in self.strategies:
            for _, _, fill in self.store.maker_fills(s.name):
                self._traded.add(fill.market_id)
                m = self._maker(s, fill.market_id)
                m.cash += fill.cash
                m.position += fill.position

    def _maker(self, strategy: Strategy, ticker: str) -> MarketMaker:
        makers = self.makers[strategy.name]
        if ticker not in makers:
            makers[ticker] = MarketMaker(ticker, strategy.quoter)
        return makers[ticker]

    # --- one tick ----------------------------------------------------------------------------

    def tick(self) -> list[MakerFill]:
        now = self._now()
        if self._refreshed is None or now - self._refreshed >= self.config.market_refresh:
            self._refresh_markets(now)
        books = self.source.fetch_books(list(self.open.values())) if self.open else {}
        if self.config.record:
            self.store.add_maker_tick(now)
        fills: list[MakerFill] = []
        for ticker, market in self.open.items():
            makers = [(s, self._maker(s, ticker)) for s in self.strategies]
            book = books.get(ticker)
            if book is None or isinstance(book, InvalidOrderBook):
                for _, maker in makers:
                    maker.stop()
                continue
            kept = self.store.stored_levels(book)
            if self.config.record and self._last_book.get(ticker) != kept:
                self.store.add_snapshot(book)
                self._last_book[ticker] = kept
            if self.config.record or any(maker.quotes for _, maker in makers):
                trades = self._new_trades(ticker, now)
                if self.config.record:
                    self.store.add_maker_trades(ticker, trades, tick_at=now)
            else:  # nothing resting can fill: skip the request, start reading trades from now
                self._since[ticker] = now
                trades = []
            fair = self._fair_price(market, book)
            if self.config.record and self._needs_fair and self._last_fair.get(ticker, _UNSET) != fair:
                self.store.add_maker_fair(ticker, now, fair)  # None too: replay must know the price went away
                self._last_fair[ticker] = fair
            for strategy, maker in makers:
                for fill in maker.step(book, trades, now, fair):
                    self.store.add_maker_fill(strategy.name, "kalshi", fill)
                    self._traded.add(ticker)
                    fills.append(fill)
        return fills

    def _fair_price(self, market: Market, book: OrderBook) -> Decimal | None:
        if not self._needs_fair or self._fair is None:
            return None
        try:
            return self._fair(market, book)
        except Exception:  # a model failure must not stop quoting; v2 falls back to v1 behaviour
            log.exception("model price failed for %s", market.market_id)
            return None

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
            for s in self.strategies:
                self._maker(s, ticker).stop()
            self._since.pop(ticker, None)
            self._seen.pop(ticker, None)
        self.open = listed
        self._refreshed = now
        if self.config.record:
            for market in listed.values():
                self.store.upsert_market(market, now)
        self._record_resolutions()

    def _record_resolutions(self) -> None:
        # Every traded market, even one we ended flat in: the report only counts a day as
        # settled once all its traded markets have a resolution. When recording, every
        # market seen too, so replayed strategies trading other markets can be settled.
        seen = {m.market_id for m in self.store.markets("kalshi")} if self.config.record else set()
        closed = (self._traded | seen) - set(self.open)
        unresolved = sorted(t for t in closed if self.store.resolution("kalshi", t) is None)
        if not unresolved:
            return
        markets = list(self.source.fetch_markets(unresolved).values())
        for res in self.source.fetch_resolutions(markets).values():
            self.store.upsert_resolution(res)

    # --- loop ----------------------------------------------------------------------------------

    def run_forever(self, cycles: int | None = None) -> None:
        n, fills_total = 0, 0
        window, window_ticks, window_cpu = time.monotonic(), 0, 0.0
        while cycles is None or n < cycles:
            started, cpu = time.monotonic(), time.thread_time()  # this thread only: ingest runs beside it
            try:
                fills_total += len(self.tick())
            except Exception:  # one bad tick (network, parse) must not end a weeks-long run
                log.exception("maker tick failed")
            n += 1
            window_ticks += 1
            window_cpu += time.thread_time() - cpu
            elapsed = time.monotonic() - window
            if n == 1 or elapsed >= LOG_EVERY.total_seconds():
                parts = []
                for s in self.strategies:
                    makers = self.makers[s.name]
                    quoting = sum(1 for t in self.open if t in makers and makers[t].quotes)
                    exposure = sum(abs(makers[t].position) for t in self.open if t in makers)  # open markets only
                    parts.append(f"{s.name}: {quoting} quoting, open |position| {exposure}")
                # ticks run back to back when one takes longer than the interval: "s apart" is the real pace
                log.info(
                    "maker tick %d: %d open markets, %d fills so far; %s; %d ticks %.1fs apart (target %gs), "
                    "cpu %.3fs per tick",
                    n, len(self.open), fills_total, "; ".join(parts), window_ticks,
                    elapsed / window_ticks, self.config.interval.total_seconds(), window_cpu / window_ticks,
                )  # fmt: skip
                window, window_ticks, window_cpu = time.monotonic(), 0, 0.0
            sleep = self.config.interval.total_seconds() - (time.monotonic() - started)
            if sleep > 0 and (cycles is None or n < cycles):
                time.sleep(sleep)
