"""A model's probability for a market, logged next to the market's own price."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from profit_engine.core.book import OrderBook
from profit_engine.core.time import require_utc


def midpoint(book: OrderBook) -> Decimal | None:
    """Market-implied YES probability: mid of best bid and ask. None if a side is empty."""
    if book.best_bid is None or book.best_ask is None:
        return None
    return (book.best_bid.price + book.best_ask.price) / 2


@dataclass(frozen=True, slots=True)
class Prediction:
    venue: str
    market_id: str
    model: str
    predicted_at: datetime
    probability: Decimal  # model's P(YES)
    market_probability: Decimal | None  # book midpoint at the same moment
    best_bid: Decimal | None
    best_ask: Decimal | None
    book_received_at: datetime

    def __post_init__(self) -> None:
        for name in ("probability", "market_probability"):
            value = getattr(self, name)
            if value is None and name == "market_probability":
                continue
            if not isinstance(value, Decimal) or not Decimal(0) <= value <= Decimal(1):
                raise ValueError(f"Prediction.{name} must be a Decimal in [0, 1], got {value!r}")
        require_utc(self.predicted_at, "Prediction.predicted_at")
        require_utc(self.book_received_at, "Prediction.book_received_at")

    @classmethod
    def from_book(cls, model: str, probability: Decimal, book: OrderBook, predicted_at: datetime) -> Prediction:
        return cls(
            venue=book.venue,
            market_id=book.market_id,
            model=model,
            predicted_at=predicted_at,
            probability=probability,
            market_probability=midpoint(book),
            best_bid=book.best_bid.price if book.best_bid else None,
            best_ask=book.best_ask.price if book.best_ask else None,
            book_received_at=book.received_at,
        )
