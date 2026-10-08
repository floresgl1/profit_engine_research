"""Replay paper market-making strategies on what the live maker recorded.

The live maker stores every tick time, every trade (and, from 2026-10-08, the
tick that delivered it), each market's book whenever its top levels change,
and the model price whenever it changes (including "no price"). Replay walks
the ticks in order and, for every market open at that tick, feeds each
strategy's MarketMaker exactly what a live tick would have given it:

- the last recorded book at or before the tick (books are stored on change);
- the trades that tick delivered: by recorded tick when known, otherwise
  those printed since the previous tick (older recordings didn't keep the
  delivering tick, so live fills can differ slightly there);
- the last recorded model price (older recordings didn't mark "no price",
  so v2 replays before 2026-10-08 are approximate).

A market is open from its first recorded book until its close time. A gap
of more than RESTART_GAP between ticks means the live engine was down:
resting quotes are dropped (live quotes didn't survive a restart either),
positions are kept.

Compare strategies with each other *under replay*, not with live fills;
`fidelity` reports how closely replayed v1 matches live v1 as a check on the
simulator.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from profit_engine.maker.queue import PublicTrade
from profit_engine.maker.quoter import MakerFill, MarketMaker
from profit_engine.maker.report import event_day_of
from profit_engine.maker.runner import Strategy
from profit_engine.storage import Store

RESTART_GAP = timedelta(minutes=2)


@dataclass
class _Market:
    ticker: str
    close: datetime | None
    book_times: list[datetime]
    books: list
    trades_by_tick: dict[datetime, list[PublicTrade]]
    untagged: list[PublicTrade]  # trades without a recorded delivering tick, by time
    untagged_times: list[datetime]
    fair_times: list[datetime]
    fairs: list[Decimal | None]

    def book_at(self, t: datetime):
        i = bisect.bisect_right(self.book_times, t) - 1
        return self.books[i] if i >= 0 else None

    def trades_for(self, prev: datetime | None, t: datetime) -> list[PublicTrade]:
        out = list(self.trades_by_tick.get(t, []))
        if prev is not None:
            lo = bisect.bisect_right(self.untagged_times, prev)
            hi = bisect.bisect_right(self.untagged_times, t)
            out += self.untagged[lo:hi]
        return out

    def fair_at(self, t: datetime) -> Decimal | None:
        i = bisect.bisect_right(self.fair_times, t) - 1
        return self.fairs[i] if i >= 0 else None


def _load(store: Store) -> list[_Market]:
    out = []
    for m in store.markets("kalshi"):
        books = list(store.books("kalshi", m.market_id))
        if not books:
            continue
        by_tick: dict[datetime, list[PublicTrade]] = {}
        untagged = []
        for trade, tick in store.maker_trade_ticks(m.market_id):
            if tick is None:
                untagged.append(trade)
            else:
                by_tick.setdefault(tick, []).append(trade)
        fair = store.maker_fair(m.market_id)
        out.append(_Market(
            m.market_id, m.close_time, [b.received_at for b in books], books, by_tick,
            untagged, [t.at for t in untagged], [f[0] for f in fair], [f[1] for f in fair],
        ))  # fmt: skip
    return out


def replay(store: Store, strategies: Sequence[Strategy], start: datetime | None = None,
           end: datetime | None = None) -> dict[str, list[MakerFill]]:  # fmt: skip
    """Fills per strategy from replaying the recorded ticks in [start, end)."""
    markets = _load(store)
    ticks = [t for t in store.maker_ticks() if (start is None or t >= start) and (end is None or t < end)]
    makers = {s.name: {m.ticker: MarketMaker(m.ticker, s.quoter) for m in markets} for s in strategies}
    fills: dict[str, list[MakerFill]] = {s.name: [] for s in strategies}
    markets.sort(key=lambda m: m.book_times[0])
    opens = [m.book_times[0] for m in markets]
    active: list[_Market] = []
    added = 0
    prev = None
    for t in ticks:
        if prev is not None and t - prev > RESTART_GAP:
            for per in makers.values():
                for mm in per.values():
                    mm.stop()
            prev = None  # like a fresh start: nothing read before this tick
        while added < len(markets) and opens[added] <= t:  # markets open from their first recorded book
            active.append(markets[added])
            added += 1
        still = []
        for m in active:
            if m.close is not None and t >= m.close:
                for s in strategies:
                    makers[s.name][m.ticker].stop()
                continue
            still.append(m)
        active = still
        for m in active:
            book = m.book_at(t)
            trades = sorted(m.trades_for(prev, t), key=lambda x: x.at)
            fair = m.fair_at(t)
            for s in strategies:
                fills[s.name] += makers[s.name][m.ticker].step(book, trades, t, fair)
        prev = t
    return fills


@dataclass(frozen=True)
class DayFidelity:
    day: date
    live_contracts: Decimal
    replay_contracts: Decimal


def fidelity(live: list[MakerFill], replayed: list[MakerFill], start: datetime, end: datetime) -> list[DayFidelity]:
    """Contracts per event day, live vs replayed, over the replayed window."""

    def per_day(fs):
        out: dict[date, Decimal] = {}
        for f in fs:
            day = event_day_of(f.market_id)
            if day is not None and start <= f.at < end:
                out[day] = out.get(day, Decimal(0)) + f.quantity
        return out

    lv, rp = per_day(live), per_day(replayed)
    return [DayFidelity(d, lv.get(d, Decimal(0)), rp.get(d, Decimal(0))) for d in sorted(set(lv) | set(rp))]
