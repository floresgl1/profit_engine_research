"""Walk-the-book fill simulation for immediate-or-cancel taker orders.

Honesty rules enforced here:
- Buys pay the ask, sells hit the bid. Nothing ever fills at mid.
- Fills walk the book level by level and never exceed a level's size.
- Order size is capped at a fraction of the visible depth on the side taken.
- The book used must have been received at or after the order's arrival
  time (decision time + latency), so latency can only hurt, never help.
- The market's fee schedule is applied; a market without one is not traded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from profit_engine.core import (
    Fill,
    FillStatus,
    Level,
    Market,
    MarketStatus,
    OrderBook,
    Outcome,
    PaperOrder,
    Side,
)

_ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class FillConfig:
    latency: timedelta = timedelta(milliseconds=250)
    max_depth_fraction: Decimal = Decimal("0.25")  # of visible depth on the side taken
    max_book_age: timedelta = timedelta(seconds=60)  # how late after arrival a book may be

    def __post_init__(self) -> None:
        if self.latency < timedelta(0):
            raise ValueError("latency must be non-negative")
        if not isinstance(self.max_depth_fraction, Decimal) or not (
            Decimal(0) < self.max_depth_fraction <= _ONE
        ):
            raise ValueError("max_depth_fraction must be a Decimal in (0, 1]")
        if self.max_book_age < timedelta(0):
            raise ValueError("max_book_age must be non-negative")


def arrival_time(order: PaperOrder, config: FillConfig) -> datetime:
    return order.decided_at + config.latency


def floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    return (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step


def contract_levels(book: OrderBook, outcome: Outcome, side: Side) -> tuple[Level, ...]:
    """The levels an order consumes, re-priced in the traded contract's terms, best first.

    YES buys take YES asks; YES sells hit YES bids. A NO contract at price q
    is the other side of a YES contract at 1 - q, so NO buys take YES bids
    and NO sells hit YES asks, each re-priced to 1 - p. Since YES bids are
    descending, 1 - bid is ascending, so the cheapest NO comes first.
    """
    if outcome is Outcome.YES:
        return book.asks if side is Side.BUY else book.bids
    levels = book.bids if side is Side.BUY else book.asks
    return tuple(Level(_ONE - lvl.price, lvl.size) for lvl in levels)


def simulate_fill(order: PaperOrder, market: Market, book: OrderBook, config: FillConfig) -> Fill:
    if (order.venue, order.market_id) != (market.venue, market.market_id) or (
        book.venue,
        book.market_id,
    ) != (market.venue, market.market_id):
        raise ValueError("order, market and book must refer to the same market")
    arrival = arrival_time(order, config)
    if book.received_at < arrival:
        # A book from before the order arrived would let latency help the fill.
        raise ValueError(
            f"book received at {book.received_at} is before order arrival at {arrival}"
        )

    def reject(reason: str) -> Fill:
        return Fill(order, FillStatus.REJECTED, (), Decimal(0), Decimal(0), Decimal(0), book.received_at, reason)

    if market.status is not MarketStatus.OPEN:
        return reject(f"market is {market.status.value}")
    if market.fee_schedule is None:
        return reject("fee schedule unknown")
    if book.received_at - arrival > config.max_book_age:
        return reject("no book close enough to arrival time")

    levels = contract_levels(book, order.outcome, order.side)
    visible_depth = sum((lvl.size for lvl in levels), Decimal(0))
    if visible_depth == 0:
        return reject("no visible depth")

    cap = floor_to_step(config.max_depth_fraction * visible_depth, market.contract_step)
    target = floor_to_step(min(order.quantity, cap), market.contract_step)
    if target < market.min_order_size:
        return reject(f"size {target} after depth cap is below venue minimum {market.min_order_size}")

    buying = order.side is Side.BUY
    legs: list[Level] = []
    remaining = target
    limit_hit = False
    for lvl in levels:
        if remaining == 0:
            break
        if order.limit_price is not None and (
            (buying and lvl.price > order.limit_price) or (not buying and lvl.price < order.limit_price)
        ):
            limit_hit = True
            break
        take = min(remaining, lvl.size)
        legs.append(Level(lvl.price, take))
        remaining -= take

    filled = target - remaining
    if filled == 0:
        return reject("limit price not reachable")
    if filled < market.min_order_size:
        return reject(f"fill {filled} at limit is below venue minimum {market.min_order_size}")

    gross = sum((leg.price * leg.size for leg in legs), Decimal(0))
    fee = market.fee_schedule.order_fee(legs, buying=buying)

    # target <= cap <= visible depth, so without a limit the walk always reaches target.
    reasons = []
    if target < order.quantity:
        reasons.append(f"sized to {target}: cap {config.max_depth_fraction} of visible depth {visible_depth}")
    if limit_hit:
        reasons.append("limit price reached")
    status = FillStatus.FILLED if filled == order.quantity else FillStatus.PARTIAL
    return Fill(order, status, tuple(legs), filled, gross, fee, book.received_at, "; ".join(reasons))
