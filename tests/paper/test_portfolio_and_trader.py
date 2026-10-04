from datetime import timedelta
from decimal import Decimal as D

import pytest
from factories import KALSHI_DIRECT_FEES, T0, make_book, make_market, make_order

from profit_engine.core import FillStatus, InvalidOrderBook, Outcome, Resolution, Side
from profit_engine.paper import (
    FillConfig,
    LiveBookProvider,
    PaperTrader,
    Portfolio,
    simulate_fill,
)

FULL = FillConfig(latency=timedelta(milliseconds=250), max_depth_fraction=D("1"))


def resolve(value: str) -> Resolution:
    return Resolution("kalshi", "KXTEST", D(value), T0 + timedelta(days=1))


class TestPortfolio:
    def buy_40_yes_with_fees(self, portfolio):
        market = make_market(fee_schedule=KALSHI_DIRECT_FEES)
        portfolio.apply(simulate_fill(make_order("40"), market, make_book(), FULL))

    def test_buy_then_settle_yes(self):
        # cost 20.16 + fee 0.6915 = 20.8515; YES pays 40 -> pnl 19.1485
        p = Portfolio(cash=D("100"))
        self.buy_40_yes_with_fees(p)
        assert p.cash == D("79.1485")
        assert p.held("kalshi", "KXTEST", Outcome.YES) == D("40")
        assert p.settle(resolve("1")) == D("19.1485")
        assert p.cash == D("119.1485")
        assert p.positions == {}

    def test_settle_no(self):
        p = Portfolio(cash=D("100"))
        self.buy_40_yes_with_fees(p)
        assert p.settle(resolve("0")) == D("-20.8515")
        assert p.cash == D("79.1485")

    def test_settle_fifty_fifty(self):
        # Polymarket-style 50/50: 40 * 0.5 = 20 -> pnl -0.8515
        p = Portfolio(cash=D("100"))
        self.buy_40_yes_with_fees(p)
        assert p.settle(resolve("0.5")) == D("-0.8515")

    def test_partial_sell_then_settle(self):
        # buy 40 YES for 20.16, sell 20 YES into bids for 8.33 (13 @ 0.42 + 7 @ 0.41)
        # cash 100 - 20.16 + 8.33 = 88.17; remaining cost 11.83
        # YES settles: 20 * 1 = 20 -> pnl 8.17, cash 108.17
        p = Portfolio(cash=D("100"))
        market = make_market()
        p.apply(simulate_fill(make_order("40"), market, make_book(), FULL))
        p.apply(simulate_fill(make_order("20", side=Side.SELL), market, make_book(), FULL))
        assert p.cash == D("88.17")
        assert p.settle(resolve("1")) == D("8.17")
        assert p.cash == D("108.17")
        assert p.realized_pnl == D("8.17")

    def test_yes_and_no_both_pay(self):
        # 10 YES (10 @ 0.44 = 4.40) + 10 NO (10 @ 0.58 = 5.80) = 10.20 spent; v = 0.3 pays 3 + 7 = 10
        p = Portfolio(cash=D("100"))
        market = make_market()
        p.apply(simulate_fill(make_order("10"), market, make_book(), FULL))
        p.apply(simulate_fill(make_order("10", outcome=Outcome.NO), market, make_book(), FULL))
        assert p.settle(resolve("0.3")) == D("-0.20")

    def test_unknown_market_settles_to_zero(self):
        assert Portfolio(cash=D("1")).settle(resolve("1")) == 0

    def test_cannot_oversell(self):
        p = Portfolio(cash=D("100"))
        with pytest.raises(ValueError):
            p.apply(simulate_fill(make_order("5", side=Side.SELL), make_market(), make_book(), FULL))

    def test_cannot_overspend(self):
        p = Portfolio(cash=D("1"))
        with pytest.raises(ValueError):
            p.apply(simulate_fill(make_order("40"), make_market(), make_book(), FULL))

    def test_rejected_fill_is_ignored(self):
        p = Portfolio(cash=D("100"))
        p.apply(simulate_fill(make_order("40"), make_market(fee_schedule=None), make_book(), FULL))
        assert p.cash == D("100") and p.positions == {}


class FakeProvider:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def book_at_or_after(self, market, when):
        self.calls.append(when)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def trader(result, cash="100"):
    return PaperTrader(FakeProvider(result), Portfolio(cash=D(cash)), {"kalshi": FULL})


class TestPaperTrader:
    def test_asks_for_book_at_arrival_time(self):
        t = trader(make_book())
        f = t.execute(make_order("40"), make_market())
        assert t.provider.calls == [T0 + timedelta(milliseconds=250)]
        assert f.status is FillStatus.FILLED
        assert t.portfolio.cash == D("100") - D("20.16")

    def test_no_book(self):
        f = trader(None).execute(make_order("40"), make_market())
        assert f.status is FillStatus.REJECTED

    def test_invalid_book_rejects_order(self):
        f = trader(InvalidOrderBook("crossed")).execute(make_order("40"), make_market())
        assert f.status is FillStatus.REJECTED
        assert "invalid book" in f.reason

    def test_sell_without_position_rejected_before_fetch(self):
        t = trader(make_book())
        f = t.execute(make_order("5", side=Side.SELL), make_market())
        assert f.status is FillStatus.REJECTED
        assert t.provider.calls == []

    def test_insufficient_cash_leaves_portfolio_untouched(self):
        t = trader(make_book(), cash="10")
        f = t.execute(make_order("40"), make_market())
        assert f.status is FillStatus.REJECTED
        assert t.portfolio.cash == D("10") and t.portfolio.positions == {}


class FakeSource:
    venue = "kalshi"

    def __init__(self, book):
        self.book = book

    def fetch_books(self, markets):
        return {m.market_id: self.book for m in markets}


class TestLiveBookProvider:
    def test_sleeps_until_arrival_then_fetches(self):
        slept = []
        provider = LiveBookProvider({"kalshi": FakeSource(make_book())}, now=lambda: T0, sleep=slept.append)
        book = provider.book_at_or_after(make_market(), T0 + timedelta(milliseconds=250))
        assert slept == [0.25]
        assert book.market_id == "KXTEST"

    def test_no_sleep_when_already_late(self):
        slept = []
        provider = LiveBookProvider({"kalshi": FakeSource(make_book())}, now=lambda: T0, sleep=slept.append)
        provider.book_at_or_after(make_market(), T0 - timedelta(seconds=1))
        assert slept == []

    def test_invalid_book_raises(self):
        provider = LiveBookProvider(
            {"kalshi": FakeSource(InvalidOrderBook("bad"))}, now=lambda: T0, sleep=lambda s: None
        )
        with pytest.raises(InvalidOrderBook):
            provider.book_at_or_after(make_market(), T0)
