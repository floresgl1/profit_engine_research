"""Book: best bid 0.42, best ask 0.44 (Kalshi docs example). Kalshi fee rate 0.07."""

from decimal import Decimal as D

import pytest
from factories import KALSHI_DIRECT_FEES, T0, make_book, make_market

from profit_engine.core import Outcome, Side
from profit_engine.models import MidpointBaseline, Model
from profit_engine.paper import EdgeStrategy

KALSHI = make_market(fee_schedule=KALSHI_DIRECT_FEES)


class TestMidpointBaseline:
    def test_is_a_model(self):
        assert isinstance(MidpointBaseline(), Model)

    def test_midpoint(self):
        # (0.42 + 0.44) / 2
        assert MidpointBaseline().predict(KALSHI, make_book()) == D("0.43")

    def test_abstains_on_one_sided_book(self):
        assert MidpointBaseline().predict(KALSHI, make_book(asks=())) is None


class TestEdgeStrategy:
    strategy = EdgeStrategy(min_edge=D("0.03"), order_size=D("10"), max_position=D("50"))

    def decide(self, p, market=KALSHI, book=None, **held):
        return self.strategy.decide(market, book or make_book(), D(p), T0, **held)

    def test_buys_yes_when_edge_clears_fees(self):
        # edge = 0.50 - 0.44 - 0.07 * 0.44 * 0.56 = 0.06 - 0.017248 = 0.042752 >= 0.03
        order = self.decide("0.50")
        assert (order.outcome, order.side, order.quantity) == (Outcome.YES, Side.BUY, D("10"))
        assert order.limit_price == D("0.47")  # 0.50 - 0.03

    def test_fees_can_kill_the_edge(self):
        # edge = 0.47 - 0.44 - 0.017248 = 0.012752 < 0.03
        assert self.decide("0.47") is None

    def test_same_edge_without_fees_trades(self):
        # edge = 0.47 - 0.44 = 0.03 >= 0.03
        order = self.decide("0.47", market=make_market())
        assert order is not None and order.limit_price == D("0.44")

    def test_buys_no_when_model_is_low(self):
        # NO ask = 1 - 0.42 = 0.58; edge = 0.70 - 0.58 - 0.07 * 0.58 * 0.42 = 0.12 - 0.017052 = 0.102948
        order = self.decide("0.30")
        assert order.outcome is Outcome.NO
        assert order.limit_price == D("0.67")  # 0.70 - 0.03

    def test_midpoint_never_trades(self):
        assert self.decide("0.43") is None

    def test_unknown_fees_never_trade(self):
        assert self.decide("0.99", market=make_market(fee_schedule=None)) is None

    def test_respects_max_position(self):
        assert self.decide("0.50", held_yes=D("45")).quantity == D("5")
        assert self.decide("0.50", held_yes=D("50")) is None

    @pytest.mark.parametrize("p", ["0.02", "0.99"])
    def test_limit_must_be_a_valid_price(self, p):
        # p = 0.02 -> NO limit 0.95 is fine; p = 0.99 -> YES limit 0.96 is fine; both trade.
        assert self.decide(p) is not None

    def test_empty_ask_side_only_considers_no(self):
        assert self.decide("0.90", book=make_book(asks=())) is None
