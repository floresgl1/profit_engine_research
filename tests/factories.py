"""Shared builders for tests. Hand-computed examples live in the tests themselves."""

from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING
from decimal import Decimal as D

from profit_engine.core import (
    ZERO_FEES,
    FeeSchedule,
    Level,
    Market,
    MarketStatus,
    OrderBook,
    Outcome,
    PaperOrder,
    Side,
)

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)

KALSHI_DIRECT_FEES = FeeSchedule(D("0.07"), D("0.000001"), ROUND_CEILING, cash_quantum=D("0.0001"))


def L(price: str, size: str) -> Level:
    return Level(D(price), D(size))


# Kalshi docs orderbook example in YES terms (top three levels per side).
DOC_BIDS = (L("0.42", "13"), L("0.41", "10"), L("0.35", "11"))  # 34 contracts
DOC_ASKS = (L("0.44", "17"), L("0.55", "20"), L("0.56", "29"))  # 66 contracts


def make_market(**overrides) -> Market:
    kwargs = dict(
        venue="kalshi",
        market_id="KXTEST",
        title="Test market",
        yes_label="Yes",
        status=MarketStatus.OPEN,
        close_time=T0 + timedelta(days=1),
        fee_schedule=ZERO_FEES,
        contract_step=D("0.01"),
        min_order_size=D("0.01"),
    )
    kwargs.update(overrides)
    return Market(**kwargs)


def make_book(bids=DOC_BIDS, asks=DOC_ASKS, received_at=T0 + timedelta(seconds=1), **overrides) -> OrderBook:
    kwargs = dict(venue="kalshi", market_id="KXTEST")
    kwargs.update(overrides)
    return OrderBook(kwargs["venue"], kwargs["market_id"], tuple(bids), tuple(asks), received_at)


def make_order(qty="40", outcome=Outcome.YES, side=Side.BUY, limit=None, decided_at=T0, **overrides) -> PaperOrder:
    kwargs = dict(venue="kalshi", market_id="KXTEST")
    kwargs.update(overrides)
    return PaperOrder(
        kwargs["venue"],
        kwargs["market_id"],
        outcome,
        side,
        D(qty),
        decided_at,
        None if limit is None else D(limit),
    )
