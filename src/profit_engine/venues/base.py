"""The read-only interface every venue adapter implements.

There is deliberately no method that writes to a venue. Adapters fetch
public data over GET only (see `venues.http.ReadOnlyHttp`) and hold no
credentials.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from profit_engine.core import InvalidOrderBook, Market, OrderBook, Resolution


@runtime_checkable
class MarketDataSource(Protocol):
    venue: str

    def list_markets(self) -> list[Market]:
        """Open markets matching this adapter's configured selection."""
        ...

    def fetch_markets(self, market_ids: Sequence[str]) -> dict[str, Market]:
        """Current metadata (status, fees) for known markets. Missing ids are omitted."""
        ...

    def fetch_books(self, markets: Sequence[Market]) -> dict[str, OrderBook | InvalidOrderBook]:
        """Current YES book per market id.

        A snapshot that fails validation is returned as its InvalidOrderBook
        error instead of raising, so one bad market cannot sink the batch and
        the caller can count skips per market.
        """
        ...

    def fetch_resolutions(self, markets: Sequence[Market]) -> dict[str, Resolution]:
        """Final outcomes for those of `markets` that have resolved."""
        ...


def fetch_book(source: MarketDataSource, market: Market) -> OrderBook:
    """Single-market convenience wrapper. Raises InvalidOrderBook on bad data."""
    result = source.fetch_books([market])[market.market_id]
    if isinstance(result, InvalidOrderBook):
        raise result
    return result
