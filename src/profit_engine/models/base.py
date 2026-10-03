"""Model slot: anything that turns a market snapshot into P(YES)."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from profit_engine.core import Market, OrderBook, midpoint


@runtime_checkable
class Model(Protocol):
    name: str

    def predict(self, market: Market, book: OrderBook) -> Decimal | None:
        """Probability that the market resolves YES, in [0, 1]. None to abstain."""
        ...


class MidpointBaseline:
    """Returns the book midpoint, i.e. agrees with the market.

    Its Brier score should equal the market's, which makes it a check that
    the pipeline and scoring are wired correctly. It never finds an edge,
    so it never trades.
    """

    name = "midpoint"

    def predict(self, market: Market, book: OrderBook) -> Decimal | None:
        return midpoint(book)
