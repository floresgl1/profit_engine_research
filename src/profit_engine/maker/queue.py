"""Honest fills for a pretend resting quote, from public trades.

We never place anything. A quote is a pretend limit order at the back of the
queue: it fills only after the size that was ahead of it at that price has
traded, or when a trade prints through its price (the taker swept the whole
level, so it would have taken us too).

Prices are in YES terms. Our bid buys YES and is hit by takers selling YES
(taker bought NO); our ask sells YES and is lifted by takers buying YES.

Conservative choices:
- trades at the same instant we posted are ignored (we can't tell the order);
- size ahead of us only shrinks by trades at our price, or when the book shows
  less size there than we think is ahead (cancels ahead of us); it never
  shrinks for any other reason;
- our quote does not take liquidity from anyone, so fills can only be
  overstated by the fact that real makers would have shared the trade with us.
  We take nothing from a trade until everything ahead of us is filled.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class PublicTrade:
    trade_id: str
    at: datetime
    yes_price: Decimal
    count: Decimal
    taker_yes: bool  # taker bought YES (lifts asks); False: taker sold YES (hits bids)


@dataclass(frozen=True)
class RestingQuote:
    side: str  # "bid" (buy YES) or "ask" (sell YES)
    price: Decimal
    size: Decimal  # still unfilled
    queue_ahead: Decimal  # visible size at our price that was there before us
    posted_at: datetime

    def __post_init__(self) -> None:
        if self.side not in ("bid", "ask"):
            raise ValueError(f"side must be bid or ask, got {self.side!r}")
        if self.size < 0 or self.queue_ahead < 0:
            raise ValueError("size and queue_ahead must be non-negative")


def hits(quote: RestingQuote, trade: PublicTrade) -> bool:
    """Did this trade take liquidity on our side of the book?"""
    return trade.taker_yes if quote.side == "ask" else not trade.taker_yes


def through(quote: RestingQuote, trade: PublicTrade) -> bool:
    """Did the trade print at a price worse for the taker than ours (so it swept our level)?"""
    return trade.yes_price > quote.price if quote.side == "ask" else trade.yes_price < quote.price


def match(quote: RestingQuote, trades: list[PublicTrade]) -> tuple[Decimal, RestingQuote]:
    """Contracts filled by `trades` (time order) and the quote left afterwards."""
    filled = Decimal(0)
    q = quote
    for t in trades:
        if q.size == 0:
            break
        if t.at <= q.posted_at or not hits(q, t):
            continue
        if through(q, t):
            filled += q.size
            q = replace(q, size=Decimal(0), queue_ahead=Decimal(0))
        elif t.yes_price == q.price:
            reaches_us = t.count - q.queue_ahead
            if reaches_us > 0:
                take = min(reaches_us, q.size)
                filled += take
                q = replace(q, size=q.size - take, queue_ahead=Decimal(0))
            else:
                q = replace(q, queue_ahead=q.queue_ahead - t.count)
    return filled, q


def refresh_queue(quote: RestingQuote, visible_at_price: Decimal) -> RestingQuote:
    """Orders ahead of us that were cancelled leave the book: we can't be behind more than is visible."""
    return replace(quote, queue_ahead=min(quote.queue_ahead, visible_at_price))
