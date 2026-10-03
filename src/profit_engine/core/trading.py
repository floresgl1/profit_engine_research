"""Paper orders and their simulated fills. Nothing here ever reaches a venue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from profit_engine.core.book import Level
from profit_engine.core.time import require_utc


class Outcome(str, Enum):
    YES = "yes"
    NO = "no"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class FillStatus(str, Enum):
    FILLED = "filled"  # full requested quantity
    PARTIAL = "partial"  # some filled, rest cancelled (immediate-or-cancel)
    REJECTED = "rejected"  # nothing filled; see `reason`


@dataclass(frozen=True, slots=True)
class PaperOrder:
    """Immediate-or-cancel taker order.

    `limit_price` is in the traded contract's terms: for a NO order it is a
    NO price. None means no limit (walk as far as depth and caps allow).
    """

    venue: str
    market_id: str
    outcome: Outcome
    side: Side
    quantity: Decimal
    decided_at: datetime
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, Outcome) or not isinstance(self.side, Side):
            raise TypeError("PaperOrder.outcome/side must be Outcome/Side enums")
        if not isinstance(self.quantity, Decimal) or self.quantity <= 0:
            raise ValueError("PaperOrder.quantity must be a positive Decimal")
        if self.limit_price is not None and (
            not isinstance(self.limit_price, Decimal) or not Decimal(0) < self.limit_price < Decimal(1)
        ):
            raise ValueError("PaperOrder.limit_price must be a Decimal strictly between 0 and 1")
        require_utc(self.decided_at, "PaperOrder.decided_at")


@dataclass(frozen=True, slots=True)
class Fill:
    """Result of simulating one PaperOrder against one book snapshot.

    `legs` are the per-level fills in the traded contract's terms (a NO buy
    lists NO prices). `gross` is sum(price * size); `fee` comes from the
    market's FeeSchedule. Cash change: buy = -(gross + fee), sell = gross - fee.
    """

    order: PaperOrder
    status: FillStatus
    legs: tuple[Level, ...]
    filled_quantity: Decimal
    gross: Decimal
    fee: Decimal
    book_received_at: datetime | None  # snapshot the fill was computed against
    reason: str = ""

    @property
    def average_price(self) -> Decimal | None:
        if self.filled_quantity == 0:
            return None
        return self.gross / self.filled_quantity

    @property
    def cash_change(self) -> Decimal:
        if self.order.side is Side.BUY:
            return -(self.gross + self.fee)
        return self.gross - self.fee
