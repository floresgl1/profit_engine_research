"""The polling loop: refresh markets, snapshot books, predict, paper trade, settle.

Error policy:
- `InvalidOrderBook` for one market: record the skip, feed the skip-rate
  monitor, carry on with the other markets.
- `VenueHttpError` (network down, venue 5xx after retries): log it and skip
  that venue for this cycle; the next cycle tries again.
- Anything else is a bug in our code and stops the loop.
"""

from __future__ import annotations

import logging
import resource
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from profit_engine.core import (
    InvalidOrderBook,
    Market,
    MarketStatus,
    Outcome,
    Prediction,
    utc_now,
)
from profit_engine.ingest.monitor import SkipMonitor
from profit_engine.models import Model
from profit_engine.paper import EdgeStrategy, PaperTrader
from profit_engine.storage import Store
from profit_engine.venues.base import MarketDataSource
from profit_engine.venues.http import VenueHttpError

log = logging.getLogger(__name__)


def cpu_seconds() -> float:
    """CPU time used by this process so far (user + system), for hosts with a CPU budget."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    poll_interval: timedelta = timedelta(seconds=30)
    market_refresh: timedelta = timedelta(minutes=10)
    resolution_check: timedelta = timedelta(minutes=10)


@dataclass
class CycleStats:
    snapshots: int = 0
    skipped: int = 0
    predictions: int = 0
    orders: int = 0
    resolutions: int = 0
    venue_errors: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(
        self,
        store: Store,
        sources: Mapping[str, MarketDataSource],
        models: Sequence[Model],
        *,
        monitor: SkipMonitor | None = None,
        strategy: EdgeStrategy | None = None,
        trader: PaperTrader | None = None,
        trading_model: str | None = None,
        config: PipelineConfig | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if (strategy is None) != (trader is None):
            raise ValueError("strategy and trader go together")
        if strategy is not None and trading_model not in {m.name for m in models}:
            raise ValueError("trading_model must name one of the models")
        self.store = store
        self.sources = sources
        self.models = list(models)
        self.monitor = monitor or SkipMonitor()
        self.strategy = strategy
        self.trader = trader
        self.trading_model = trading_model
        self.config = config or PipelineConfig()
        self._now = now
        self._last_refresh: dict[str, datetime] = {}
        self._last_resolution_check: dict[str, datetime] = {}

    def run_cycle(self) -> CycleStats:
        stats = CycleStats()
        for venue, source in self.sources.items():
            try:
                self._cycle_venue(venue, source, stats)
            except VenueHttpError as exc:
                log.error("%s unavailable this cycle: %s", venue, exc)
                stats.venue_errors.append(f"{venue}: {exc}")
        return stats

    def run_forever(self, cycles: int | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        done = 0
        while cycles is None or done < cycles:
            started = self._now()
            stats = self.run_cycle()
            done += 1
            log.info(
                "cycle %d: %d snapshots, %d skipped, %d predictions, %d orders, %d resolutions, "
                "cpu %.1fs total%s",
                done,
                stats.snapshots,
                stats.skipped,
                stats.predictions,
                stats.orders,
                stats.resolutions,
                cpu_seconds(),
                f", errors: {stats.venue_errors}" if stats.venue_errors else "",
            )
            if cycles is not None and done >= cycles:
                break
            wait = (started + self.config.poll_interval - self._now()).total_seconds()
            if wait > 0:
                sleep(wait)

    # --- one venue, one cycle -------------------------------------------------

    def _cycle_venue(self, venue: str, source: MarketDataSource, stats: CycleStats) -> None:
        if self._due(self._last_refresh, venue, self.config.market_refresh):
            self._refresh_markets(venue, source)
        open_markets = self.store.markets(venue=venue, statuses=[MarketStatus.OPEN])
        if open_markets:
            self._snapshot(venue, source, open_markets, stats)
        if self._due(self._last_resolution_check, venue, self.config.resolution_check):
            self._check_resolutions(venue, source, stats)

    def _refresh_markets(self, venue: str, source: MarketDataSource) -> None:
        now = self._now()
        listed = source.list_markets()
        listed_ids = {m.market_id for m in listed}
        # Markets we track that dropped off the open list: refresh their status.
        stale = [m.market_id for m in self.store.markets(venue=venue, statuses=[MarketStatus.OPEN]) if m.market_id not in listed_ids]
        refreshed = list(source.fetch_markets(stale).values()) if stale else []
        for market in listed + refreshed:
            self.store.upsert_market(market, now)
        self._last_refresh[venue] = now
        log.info("%s: %d open markets listed, %d stale refreshed", venue, len(listed), len(refreshed))

    def _snapshot(self, venue: str, source: MarketDataSource, markets: list[Market], stats: CycleStats) -> None:
        results = source.fetch_books(markets)
        by_id = {m.market_id: m for m in markets}
        for market_id, result in results.items():
            if isinstance(result, InvalidOrderBook):
                self.store.record_skip(venue, market_id, self._now(), str(result))
                self.monitor.record(venue, skipped=True)
                stats.skipped += 1
                log.warning("skipped %s %s: %s", venue, market_id, result)
                continue
            self.monitor.record(venue, skipped=False)
            self.store.add_snapshot(result)
            stats.snapshots += 1
            self._predict_and_trade(by_id[market_id], result, stats)

    def _predict_and_trade(self, market: Market, book, stats: CycleStats) -> None:
        for model in self.models:
            probability = model.predict(market, book)
            if probability is None:
                continue
            predicted_at = self._now()
            self.store.add_prediction(Prediction.from_book(model.name, probability, book, predicted_at))
            stats.predictions += 1
            if self.strategy is None or model.name != self.trading_model:
                continue
            portfolio = self.trader.portfolio
            order = self.strategy.decide(
                market,
                book,
                probability,
                predicted_at,
                held_yes=portfolio.held(market.venue, market.market_id, Outcome.YES),
                held_no=portfolio.held(market.venue, market.market_id, Outcome.NO),
            )
            if order is None:
                continue
            fill = self.trader.execute(order, market)
            self.store.add_fill(fill)
            stats.orders += 1
            log.info(
                "paper %s %s %s %s: %s %s @ avg %s fee %s %s",
                order.side.value,
                order.quantity,
                order.outcome.value,
                market.market_id,
                fill.status.value,
                fill.filled_quantity,
                fill.average_price,
                fill.fee,
                fill.reason,
            )

    def _check_resolutions(self, venue: str, source: MarketDataSource, stats: CycleStats) -> None:
        now = self._now()
        pending = self.store.unresolved_markets(venue)
        if pending:
            # Refresh status first so closed markets stop being polled.
            for market in source.fetch_markets([m.market_id for m in pending]).values():
                self.store.upsert_market(market, now)
            for resolution in source.fetch_resolutions(pending).values():
                self.store.upsert_resolution(resolution)
                stats.resolutions += 1
                if self.trader is not None:
                    pnl = self.trader.portfolio.settle(resolution)
                    if pnl:
                        log.info("settled %s %s at %s: pnl %s", venue, resolution.market_id, resolution.yes_value, pnl)
        self._last_resolution_check[venue] = now

    def _due(self, last: dict[str, datetime], venue: str, every: timedelta) -> bool:
        previous = last.get(venue)
        return previous is None or self._now() - previous >= every
