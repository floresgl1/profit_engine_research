"""Ties the fill engine to a source of books and a portfolio."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from profit_engine.core import (
    Fill,
    FillStatus,
    InvalidOrderBook,
    Market,
    OrderBook,
    PaperOrder,
    Side,
    utc_now,
)
from profit_engine.paper.engine import FillConfig, arrival_time, simulate_fill
from profit_engine.paper.portfolio import Portfolio
from profit_engine.venues.base import MarketDataSource, fetch_book


class BookProvider(Protocol):
    def book_at_or_after(self, market: Market, when: datetime) -> OrderBook | None:
        """First known book for `market` received at or after `when`, or None."""
        ...


class LiveBookProvider:
    """Waits until the order's arrival time, then fetches a fresh book."""

    def __init__(
        self,
        sources: Mapping[str, MarketDataSource],
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sources = sources
        self._now = now
        self._sleep = sleep

    def book_at_or_after(self, market: Market, when: datetime) -> OrderBook | None:
        wait = (when - self._now()).total_seconds()
        if wait > 0:
            self._sleep(wait)
        return fetch_book(self._sources[market.venue], market)


def _rejected(order: PaperOrder, reason: str) -> Fill:
    zero = Decimal(0)
    return Fill(order, FillStatus.REJECTED, (), zero, zero, zero, None, reason)


class PaperTrader:
    def __init__(
        self,
        provider: BookProvider,
        portfolio: Portfolio,
        configs: Mapping[str, FillConfig],
    ) -> None:
        self.provider = provider
        self.portfolio = portfolio
        self.configs = configs  # per venue

    def execute(self, order: PaperOrder, market: Market) -> Fill:
        held = self.portfolio.held(order.venue, order.market_id, order.outcome)
        if order.side is Side.SELL and held < order.quantity:
            return _rejected(order, f"holding {held}, cannot sell {order.quantity}")

        config = self.configs[order.venue]
        try:
            book = self.provider.book_at_or_after(market, arrival_time(order, config))
        except InvalidOrderBook as exc:
            return _rejected(order, f"invalid book at arrival: {exc}")
        if book is None:
            return _rejected(order, "no book at or after arrival time")

        fill = simulate_fill(order, market, book, config)
        if fill.status is not FillStatus.REJECTED and not self.portfolio.can_afford(fill):
            return _rejected(order, f"insufficient cash for {-fill.cash_change}")
        self.portfolio.apply(fill)
        return fill
