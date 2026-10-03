"""Placeholder strategy: buy when the model's probability beats the price plus fees.

This exists so the pipeline runs end to end. It is not a recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from profit_engine.core import Market, OrderBook, Outcome, PaperOrder, Side

_ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class EdgeStrategy:
    """Buy YES (or NO) when expected value per contract beats `min_edge`.

    For YES at ask a: edge = p - a - fee(a), with fee(a) = rate * a * (1 - a).
    For NO at ask 1 - b (b = best YES bid): edge = (1 - p) - (1 - b) - fee(1 - b).
    The order's limit price stops the walk where the edge (before fees) would
    drop below `min_edge`. Positions are held to resolution; no exits.
    """

    min_edge: Decimal = Decimal("0.03")
    order_size: Decimal = Decimal("10")
    max_position: Decimal = Decimal("50")  # contracts per outcome per market

    def decide(
        self,
        market: Market,
        book: OrderBook,
        probability: Decimal,
        now: datetime,
        held_yes: Decimal = Decimal(0),
        held_no: Decimal = Decimal(0),
    ) -> PaperOrder | None:
        fees = market.fee_schedule
        if fees is None:
            return None

        candidates = []
        if book.best_ask is not None and held_yes < self.max_position:
            ask = book.best_ask.price
            edge = probability - ask - fees.rate * ask * (_ONE - ask)
            candidates.append((edge, Outcome.YES, probability - self.min_edge, held_yes))
        if book.best_bid is not None and held_no < self.max_position:
            no_ask = _ONE - book.best_bid.price
            edge = (_ONE - probability) - no_ask - fees.rate * no_ask * (_ONE - no_ask)
            candidates.append((edge, Outcome.NO, (_ONE - probability) - self.min_edge, held_no))
        if not candidates:
            return None

        edge, outcome, limit, held = max(candidates, key=lambda c: c[0])
        if edge < self.min_edge or not Decimal(0) < limit < _ONE:
            return None
        quantity = min(self.order_size, self.max_position - held)
        return PaperOrder(market.venue, market.market_id, outcome, Side.BUY, quantity, now, limit)
