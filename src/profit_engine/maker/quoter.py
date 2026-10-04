"""v1 quoting policy: join the best bid and best ask, nothing smarter.

Per market, each tick:
1. match the resting quotes against the trades since the last tick;
2. account fills (cash, inventory, maker fee);
3. re-quote: stay put if our price is still the best price on our side
   (keeping our queue place), otherwise cancel and re-post at the new best
   price behind everything already showing there.

Limits: a fixed quote size, and no new quote on the side that would push
the position past max_position (long or short).

No model in v1; it's the baseline a model-informed quoter must beat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from profit_engine.core import OrderBook
from profit_engine.maker.queue import PublicTrade, RestingQuote, match, refresh_queue

# Kalshi doesn't document maker fees on these series; charge the higher
# rate it uses where makers pay, so results err against us.
DEFAULT_MAKER_FEE_RATE = Decimal("0.0175")


@dataclass(frozen=True)
class QuoterConfig:
    size: Decimal = Decimal(10)
    max_position: Decimal = Decimal(50)
    maker_fee_rate: Decimal = DEFAULT_MAKER_FEE_RATE

    def __post_init__(self) -> None:
        if self.size <= 0 or self.max_position < self.size:
            raise ValueError("need size > 0 and max_position >= size")


@dataclass(frozen=True)
class MakerFill:
    market_id: str
    side: str  # "bid" = we bought YES, "ask" = we sold YES
    price: Decimal
    quantity: Decimal
    fee: Decimal
    at: datetime  # when we observed it (the trade's time is within the last tick)

    @property
    def cash(self) -> Decimal:
        gross = self.price * self.quantity
        return (-gross if self.side == "bid" else gross) - self.fee

    @property
    def position(self) -> Decimal:
        return self.quantity if self.side == "bid" else -self.quantity


def maker_fee(price: Decimal, quantity: Decimal, rate: Decimal) -> Decimal:
    return rate * quantity * price * (1 - price)


def visible_size(book: OrderBook, side: str, price: Decimal) -> Decimal:
    levels = book.bids if side == "bid" else book.asks
    return sum((lv.size for lv in levels if lv.price == price), Decimal(0))


@dataclass
class MarketMaker:
    market_id: str
    config: QuoterConfig
    position: Decimal = Decimal(0)
    cash: Decimal = Decimal(0)
    quotes: dict[str, RestingQuote] = field(default_factory=dict)

    def step(self, book: OrderBook, trades: list[PublicTrade], now: datetime) -> list[MakerFill]:
        fills = self._match(trades, now)
        self._requote(book, now)
        return fills

    def stop(self) -> None:
        """Pull both quotes (market closing or book unusable)."""
        self.quotes.clear()

    def _match(self, trades: list[PublicTrade], now: datetime) -> list[MakerFill]:
        fills = []
        ordered = sorted(trades, key=lambda t: t.at)
        for side, quote in list(self.quotes.items()):
            filled, left = match(quote, ordered)
            if filled:
                fee = maker_fee(quote.price, filled, self.config.maker_fee_rate)
                fill = MakerFill(self.market_id, side, quote.price, filled, fee, now)
                fills.append(fill)
                self.cash += fill.cash
                self.position += fill.position
            if left.size > 0:
                self.quotes[side] = left
            else:
                del self.quotes[side]
        return fills

    def _requote(self, book: OrderBook, now: datetime) -> None:
        best = {"bid": book.best_bid, "ask": book.best_ask}
        if best["bid"] is None or best["ask"] is None:
            self.stop()  # one-sided book: no honest place to join
            return
        for side in ("bid", "ask"):
            room = self.config.max_position - (self.position if side == "bid" else -self.position)
            size = min(self.config.size, room)
            target = best[side].price
            current = self.quotes.get(side)
            if size <= 0:
                self.quotes.pop(side, None)
            elif current is not None and current.price == target:
                self.quotes[side] = refresh_queue(current, visible_size(book, side, target))
            else:
                # New quote, or the best price moved: re-post behind everything showing there.
                self.quotes[side] = RestingQuote(side, target, size, visible_size(book, side, target), now)
