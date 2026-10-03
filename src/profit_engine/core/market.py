"""Venue-agnostic market metadata and resolutions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from profit_engine.core.fees import FeeSchedule
from profit_engine.core.time import require_utc


class MarketStatus(str, Enum):
    UPCOMING = "upcoming"  # created, not yet open
    OPEN = "open"  # accepting orders
    PAUSED = "paused"  # temporarily halted by the venue
    CLOSED = "closed"  # no longer trading, outcome not final
    RESOLVED = "resolved"  # outcome final, payouts determined


@dataclass(frozen=True, slots=True)
class Market:
    """One binary market. YES is the venue's first outcome (`yes_label`).

    `venue_meta` holds venue-specific details (e.g. Polymarket token ids,
    Kalshi strike fields). Generic strategy code must not read it; models
    built for one venue's product (like the KXHIGH temperature model) may.
    """

    venue: str
    market_id: str
    title: str
    yes_label: str
    status: MarketStatus
    close_time: datetime | None
    fee_schedule: FeeSchedule | None  # None: fees unknown, paper engine will not trade
    contract_step: Decimal  # smallest contract increment
    min_order_size: Decimal  # smallest order the venue accepts, in contracts
    venue_meta: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("venue", "market_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"Market.{name} must be a non-empty string")
        if not isinstance(self.status, MarketStatus):
            raise TypeError("Market.status must be a MarketStatus")
        if self.close_time is not None:
            require_utc(self.close_time, "Market.close_time")
        if self.fee_schedule is not None and not isinstance(self.fee_schedule, FeeSchedule):
            raise TypeError("Market.fee_schedule must be a FeeSchedule or None")
        for name in ("contract_step", "min_order_size"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or value <= 0:
                raise ValueError(f"Market.{name} must be a positive Decimal")
        # Freeze the mapping so a "frozen" Market cannot be mutated through it.
        object.__setattr__(self, "venue_meta", MappingProxyType(dict(self.venue_meta)))


@dataclass(frozen=True, slots=True)
class Resolution:
    """Final outcome of a market, as the payout per YES contract.

    `yes_value` is 1 for YES, 0 for NO, and can sit in between: Polymarket
    50/50 resolutions pay 0.5, Kalshi scalar markets pay a fraction.
    """

    venue: str
    market_id: str
    yes_value: Decimal
    resolved_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.yes_value, Decimal):
            raise TypeError("Resolution.yes_value must be Decimal")
        if not Decimal(0) <= self.yes_value <= Decimal(1):
            raise ValueError(f"Resolution.yes_value must be in [0, 1], got {self.yes_value}")
        require_utc(self.resolved_at, "Resolution.resolved_at")
