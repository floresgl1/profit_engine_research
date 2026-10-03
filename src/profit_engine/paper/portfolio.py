"""Paper cash and positions. Positions are held until sold or settled."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from profit_engine.core import Fill, FillStatus, Outcome, Resolution, Side

MarketKey = tuple[str, str]  # (venue, market_id)


@dataclass
class Position:
    yes: Decimal = Decimal(0)
    no: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)  # net cash spent on this market, fees included

    def held(self, outcome: Outcome) -> Decimal:
        return self.yes if outcome is Outcome.YES else self.no


@dataclass
class Portfolio:
    cash: Decimal
    positions: dict[MarketKey, Position] = field(default_factory=dict)
    realized_pnl: Decimal = Decimal(0)

    def held(self, venue: str, market_id: str, outcome: Outcome) -> Decimal:
        pos = self.positions.get((venue, market_id))
        return pos.held(outcome) if pos else Decimal(0)

    def can_afford(self, fill: Fill) -> bool:
        return self.cash + fill.cash_change >= 0

    def apply(self, fill: Fill) -> None:
        if fill.status is FillStatus.REJECTED:
            return
        order = fill.order
        key = (order.venue, order.market_id)
        if order.side is Side.SELL and self.held(*key, order.outcome) < fill.filled_quantity:
            raise ValueError(f"selling {fill.filled_quantity} {order.outcome.value} but holding less")
        if not self.can_afford(fill):
            raise ValueError("fill would make cash negative")

        pos = self.positions.setdefault(key, Position())
        signed = fill.filled_quantity if order.side is Side.BUY else -fill.filled_quantity
        if order.outcome is Outcome.YES:
            pos.yes += signed
        else:
            pos.no += signed
        pos.cost -= fill.cash_change
        self.cash += fill.cash_change

    def settle(self, resolution: Resolution) -> Decimal:
        """Pay out a resolved market; returns the market's realized PnL."""
        pos = self.positions.pop((resolution.venue, resolution.market_id), None)
        if pos is None:
            return Decimal(0)
        payout = pos.yes * resolution.yes_value + pos.no * (1 - resolution.yes_value)
        self.cash += payout
        pnl = payout - pos.cost
        self.realized_pnl += pnl
        return pnl


def rebuild_portfolio(cash: Decimal, fills: Iterable[Fill], resolutions: Iterable[Resolution]) -> Portfolio:
    """Replay stored fills and settlements in time order. Used on restart.

    Settlements replay at the venue's resolution time, which is no later than
    when the live loop noticed them, so replayed cash is never tighter than
    the original run's.
    """
    events: list[tuple[datetime, int, Fill | Resolution]] = [(f.order.decided_at, 0, f) for f in fills]
    events += [(r.resolved_at, 1, r) for r in resolutions]
    portfolio = Portfolio(cash=cash)
    for _, _, event in sorted(events, key=lambda e: (e[0], e[1])):
        if isinstance(event, Fill):
            portfolio.apply(event)
        else:
            portfolio.settle(event)
    return portfolio
