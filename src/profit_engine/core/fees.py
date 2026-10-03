"""Taker fee schedules.

Both venues charge takers `rate * C * p * (1 - p)` per fill, where C is the
number of contracts and p the contract price. They differ only in rounding,
which this type parameterizes. Adapters build a FeeSchedule from venue data;
a market whose fee structure we cannot model gets no schedule at all, and the
paper engine refuses to trade it rather than assume zero fees.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from profit_engine.core.book import Level

_ROUNDING_MODES = {ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP}


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Taker fee model for one market.

    rate:           coefficient in `rate * C * p * (1 - p)`
    fill_quantum:   each fill's fee is rounded to this increment...
    fill_rounding:  ...using this decimal rounding mode
    cash_quantum:   if set, the order's net cash change is rounded to this
                    increment against the trader, and the difference counts
                    as fee (Kalshi's balance-precision rounding fee)
    """

    rate: Decimal
    fill_quantum: Decimal
    fill_rounding: str
    cash_quantum: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.rate, Decimal) or self.rate < 0:
            raise ValueError(f"FeeSchedule.rate must be a non-negative Decimal, got {self.rate!r}")
        if not isinstance(self.fill_quantum, Decimal) or self.fill_quantum <= 0:
            raise ValueError("FeeSchedule.fill_quantum must be a positive Decimal")
        if self.fill_rounding not in _ROUNDING_MODES:
            raise ValueError(f"unsupported rounding mode {self.fill_rounding!r}")
        if self.cash_quantum is not None and (
            not isinstance(self.cash_quantum, Decimal) or self.cash_quantum <= 0
        ):
            raise ValueError("FeeSchedule.cash_quantum must be a positive Decimal or None")

    def fill_fee(self, price: Decimal, contracts: Decimal) -> Decimal:
        raw = self.rate * contracts * price * (1 - price)
        return raw.quantize(self.fill_quantum, rounding=self.fill_rounding)

    def order_fee(self, fills: Iterable[Level], buying: bool) -> Decimal:
        """Total fee for one order made of `fills` (prices in the traded contract's terms)."""
        fills = list(fills)
        fee = sum((self.fill_fee(f.price, f.size) for f in fills), Decimal(0))
        if self.cash_quantum is None:
            return fee
        gross = sum((f.price * f.size for f in fills), Decimal(0))
        if buying:
            paid = (gross + fee).quantize(self.cash_quantum, rounding=ROUND_CEILING)
            return paid - gross
        received = (gross - fee).quantize(self.cash_quantum, rounding=ROUND_FLOOR)
        return gross - received


ZERO_FEES = FeeSchedule(rate=Decimal(0), fill_quantum=Decimal("0.000001"), fill_rounding=ROUND_CEILING)
