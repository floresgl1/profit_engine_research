"""Venue-agnostic order book snapshot for a binary market, always quoted in YES terms."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

_ZERO = Decimal(0)
_ONE = Decimal(1)


class InvalidOrderBook(ValueError):
    """Venue data failed book validation (bad level, bad ordering, crossed book).

    Callers ingesting live data may catch this, log it, and skip the snapshot.
    Mistakes in our own code (wrong types, naive timestamps, empty ids) raise
    TypeError or plain ValueError instead, so they are never skipped silently.
    """


@dataclass(frozen=True, slots=True)
class Level:
    """One price level: `size` contracts resting at `price` dollars."""

    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        for name in ("price", "size"):
            value = getattr(self, name)
            # bool and float are rejected on purpose: Decimal(0.1) carries binary float error.
            if not isinstance(value, Decimal):
                raise TypeError(f"Level.{name} must be Decimal, got {type(value).__name__}")
            if not value.is_finite():
                raise InvalidOrderBook(f"Level.{name} must be finite, got {value}")
        if not _ZERO < self.price < _ONE:
            raise InvalidOrderBook(f"Level.price must be strictly between 0 and 1, got {self.price}")
        if self.size <= _ZERO:
            raise InvalidOrderBook(f"Level.size must be positive, got {self.size}")


@dataclass(frozen=True, slots=True)
class OrderBook:
    """Snapshot of one binary market's YES book.

    Both sides are ordered best first: bids by descending price, asks by
    ascending price. Each price appears at most once per side. Either side
    may be empty. Adapters are responsible for converting venue formats
    (e.g. Kalshi NO bids into YES asks) before constructing this.
    """

    venue: str
    market_id: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    received_at: datetime  # our clock when the snapshot arrived, UTC

    def __post_init__(self) -> None:
        for name in ("venue", "market_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"OrderBook.{name} must be a non-empty string")

        for name in ("bids", "asks"):
            side = getattr(self, name)
            # A list would let callers mutate a "frozen" snapshot after the fact.
            if not isinstance(side, tuple):
                raise TypeError(f"OrderBook.{name} must be a tuple, got {type(side).__name__}")
            for level in side:
                if not isinstance(level, Level):
                    raise TypeError(f"OrderBook.{name} must contain Level, got {type(level).__name__}")

        for better, worse in zip(self.bids, self.bids[1:]):
            if not better.price > worse.price:
                raise InvalidOrderBook(
                    f"bids must be strictly descending by price: {better.price} then {worse.price}"
                )
        for better, worse in zip(self.asks, self.asks[1:]):
            if not better.price < worse.price:
                raise InvalidOrderBook(
                    f"asks must be strictly ascending by price: {better.price} then {worse.price}"
                )

        # A bid at or above the ask would have matched on the venue, so the snapshot is inconsistent.
        if self.bids and self.asks and self.bids[0].price >= self.asks[0].price:
            raise InvalidOrderBook(
                f"book is crossed or locked: best bid {self.bids[0].price} >= best ask {self.asks[0].price}"
            )

        if not isinstance(self.received_at, datetime):
            raise TypeError("OrderBook.received_at must be a datetime")
        if self.received_at.utcoffset() != timedelta(0):
            raise ValueError("OrderBook.received_at must be timezone-aware UTC")

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None
