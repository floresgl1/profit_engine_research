"""Quoting policies: join the best bid and best ask (v1), optionally vetoed by a model (v2).

Per market, each tick:
1. match the resting quotes against the trades since the last tick;
2. account fills (cash, inventory, maker fee);
3. re-quote: stay put if our price is still the best price on our side
   (keeping our queue place), otherwise cancel and re-post at the new best
   price behind everything already showing there.

Limits: a fixed quote size, and no new quote on the side that would push
the position past max_position (long or short).

v2 adds one rule: with a model probability `fair` and margin m, don't
quote a side whose fill the model puts at more than m against us (no bid
above fair + m, no ask below fair - m). research/maker_rules.md chose
m = 20c on the discovery half of 628k historical maker fills; on the holdout
it raised total maker PnL by 29%. Without a model price (the model abstains,
or no model), v2 quotes like v1.

v3 (inventory skew, replay-only so far) changes only the side that would add
to the position: its size shrinks with the position (skew_size) and, from
half the limit on, it quotes one tick behind the best price (skew_back). A
resting quote whose wanted size drops shrinks in place and keeps its queue
place; no quote grows back without re-posting (as in v1).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from profit_engine.core import OrderBook
from profit_engine.maker.queue import PublicTrade, RestingQuote, match_detail, refresh_queue

# Kalshi doesn't document maker fees on these series; charge the higher
# rate it uses where makers pay, so results err against us.
DEFAULT_MAKER_FEE_RATE = Decimal("0.0175")
TICK = Decimal("0.01")


@dataclass(frozen=True)
class QuoterConfig:
    size: Decimal = Decimal(10)
    max_position: Decimal = Decimal(50)
    maker_fee_rate: Decimal = DEFAULT_MAKER_FEE_RATE
    model_margin: Decimal | None = None  # v2: veto quotes the model puts more than this against us
    # v3, inventory skew. On the side that would add to the position:
    skew_size: bool = False  # quote size x (1 - |position| / max_position), whole contracts (0 at the limit)
    skew_back: bool = False  # and from half the limit on, quote one tick behind the best price

    def __post_init__(self) -> None:
        if self.size <= 0 or self.max_position < self.size:
            raise ValueError("need size > 0 and max_position >= size")
        if self.model_margin is not None and self.model_margin < 0:
            raise ValueError("model_margin must be non-negative")


@dataclass(frozen=True)
class MakerFill:
    market_id: str
    side: str  # "bid" = we bought YES, "ask" = we sold YES
    price: Decimal
    quantity: Decimal
    fee: Decimal
    at: datetime  # when we observed it (the trade's time is within the last tick)
    # Contracts of this fill that came from a trade printing through our price (the level was
    # swept) rather than our turn in the queue. For analysis only: not stored, not compared.
    swept: Decimal = field(default=Decimal(0), compare=False)

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

    def step(
        self, book: OrderBook, trades: list[PublicTrade], now: datetime, fair: Decimal | None = None
    ) -> list[MakerFill]:
        fills = self._match(trades, now)
        self._requote(book, now, fair)
        return fills

    def stop(self) -> None:
        """Pull both quotes (market closing or book unusable)."""
        self.quotes.clear()

    def _match(self, trades: list[PublicTrade], now: datetime) -> list[MakerFill]:
        fills = []
        ordered = sorted(trades, key=lambda t: t.at)
        for side, quote in list(self.quotes.items()):
            filled, swept, left = match_detail(quote, ordered)
            if filled:
                fee = maker_fee(quote.price, filled, self.config.maker_fee_rate)
                fill = MakerFill(self.market_id, side, quote.price, filled, fee, now, swept)
                fills.append(fill)
                self.cash += fill.cash
                self.position += fill.position
            if left.size > 0:
                self.quotes[side] = left
            else:
                del self.quotes[side]
        return fills

    def _vetoed(self, side: str, price: Decimal, fair: Decimal | None) -> bool:
        m = self.config.model_margin
        if m is None or fair is None:
            return False
        return price > fair + m if side == "bid" else price < fair - m

    def _adds(self, side: str) -> bool:
        """Would a fill on this side grow |position|?"""
        return self.position > 0 if side == "bid" else self.position < 0

    def _skewed(self, side: str, size: Decimal, target: Decimal) -> tuple[Decimal, Decimal | None]:
        """v3: smaller and/or one tick worse on the side that adds to the position. None price = don't quote."""
        if not self._adds(side):
            return size, target
        filled = abs(self.position) / self.config.max_position
        if self.config.skew_size:
            size = min(size, (self.config.size * (1 - filled)).to_integral_value(rounding=ROUND_FLOOR))
        if self.config.skew_back and filled >= Decimal("0.5"):
            target = target - TICK if side == "bid" else target + TICK
            if not TICK <= target <= 1 - TICK:
                return size, None
        return size, target

    def _requote(self, book: OrderBook, now: datetime, fair: Decimal | None = None) -> None:
        best = {"bid": book.best_bid, "ask": book.best_ask}
        if best["bid"] is None or best["ask"] is None:
            self.stop()  # one-sided book: no honest place to join
            return
        for side in ("bid", "ask"):
            room = self.config.max_position - (self.position if side == "bid" else -self.position)
            size, target = self._skewed(side, min(self.config.size, room), best[side].price)
            if target is None:
                self.quotes.pop(side, None)
                continue
            current = self.quotes.get(side)
            if size <= 0 or self._vetoed(side, target, fair):
                self.quotes.pop(side, None)
            elif current is not None and current.price == target:
                # Same price: keep the queue place. A smaller wanted size shrinks the quote in
                # place (a size cut keeps priority); it never grows back without re-posting.
                kept = replace(current, size=min(current.size, size))
                self.quotes[side] = refresh_queue(kept, visible_size(book, side, target))
            else:
                # New quote, or the best price moved: re-post behind everything showing there.
                self.quotes[side] = RestingQuote(side, target, size, visible_size(book, side, target), now)
