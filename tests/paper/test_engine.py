"""Fill engine tests. Every expected number is computed by hand in the comment above it.

Book (Kalshi docs example, YES terms):
    bids 0.42 x 13, 0.41 x 10, 0.35 x 11   (34 contracts)
    asks 0.44 x 17, 0.55 x 20, 0.56 x 29   (66 contracts)
"""

from datetime import timedelta
from decimal import Decimal as D

import pytest
from factories import DOC_ASKS, DOC_BIDS, KALSHI_DIRECT_FEES, T0, L, make_book, make_market, make_order

from profit_engine.core import FillStatus, MarketStatus, Outcome, Side
from profit_engine.paper import FillConfig, contract_levels, simulate_fill

FULL_DEPTH = FillConfig(latency=timedelta(milliseconds=250), max_depth_fraction=D("1"))


def fill(order, market=None, book=None, config=FULL_DEPTH):
    return simulate_fill(order, market or make_market(), book or make_book(), config)


class TestWalkTheBook:
    def test_buy_yes_walks_asks(self):
        # 17 @ 0.44 = 7.48, 20 @ 0.55 = 11.00, 3 @ 0.56 = 1.68 -> 20.16 / 40 = 0.504
        f = fill(make_order("40"))
        assert f.status is FillStatus.FILLED
        assert f.legs == (L("0.44", "17"), L("0.55", "20"), L("0.56", "3"))
        assert f.gross == D("20.16")
        assert f.average_price == D("0.504")

    def test_sell_yes_hits_bids(self):
        # 13 @ 0.42 = 5.46, 7 @ 0.41 = 2.87 -> 8.33
        f = fill(make_order("20", side=Side.SELL))
        assert f.legs == (L("0.42", "13"), L("0.41", "7"))
        assert f.gross == D("8.33")

    def test_buy_no_takes_yes_bids_at_complement(self):
        # NO asks = 1 - YES bids: 13 @ 0.58 = 7.54, 7 @ 0.59 = 4.13 -> 11.67
        f = fill(make_order("20", outcome=Outcome.NO))
        assert f.legs == (L("0.58", "13"), L("0.59", "7"))
        assert f.gross == D("11.67")

    def test_sell_no_hits_yes_asks_at_complement(self):
        # NO bids = 1 - YES asks: 17 @ 0.56 = 9.52, 3 @ 0.45 = 1.35 -> 10.87
        f = fill(make_order("20", outcome=Outcome.NO, side=Side.SELL))
        assert f.legs == (L("0.56", "17"), L("0.45", "3"))
        assert f.gross == D("10.87")

    def test_order_larger_than_book_is_partial(self):
        # 70 requested, 66 visible: 7.48 + 11.00 + 29 * 0.56 (16.24) = 34.72
        f = fill(make_order("70"))
        assert f.status is FillStatus.PARTIAL
        assert f.filled_quantity == D("66")
        assert f.gross == D("34.72")


class TestDepthCap:
    def test_half_depth(self):
        # cap = 0.5 * 66 = 33: 17 @ 0.44 = 7.48, 16 @ 0.55 = 8.80 -> 16.28
        f = fill(make_order("40"), config=FillConfig(max_depth_fraction=D("0.5")))
        assert f.status is FillStatus.PARTIAL
        assert f.filled_quantity == D("33")
        assert f.gross == D("16.28")
        assert "cap" in f.reason

    def test_fractional_contracts(self):
        # cap = 0.1 * 66 = 6.6, step 0.01 -> 6.6 @ 0.44 = 2.904
        f = fill(make_order("40"), config=FillConfig(max_depth_fraction=D("0.1")))
        assert f.filled_quantity == D("6.6")
        assert f.gross == D("2.904")

    def test_whole_contract_step_floors(self):
        # cap = 6.6, step 1 -> 6 @ 0.44 = 2.64
        market = make_market(contract_step=D("1"), min_order_size=D("1"))
        f = fill(make_order("40"), market=market, config=FillConfig(max_depth_fraction=D("0.1")))
        assert f.filled_quantity == D("6")
        assert f.gross == D("2.64")

    def test_cap_uses_the_side_being_taken(self):
        # Selling YES hits bids: cap = 0.5 * 34 = 17 -> 13 @ 0.42 + 4 @ 0.41 = 5.46 + 1.64 = 7.10
        f = fill(make_order("40", side=Side.SELL), config=FillConfig(max_depth_fraction=D("0.5")))
        assert f.filled_quantity == D("17")
        assert f.gross == D("7.10")

    def test_below_venue_minimum_rejected(self):
        # cap = 0.05 * 66 = 3.3 < Polymarket-style minimum of 5
        market = make_market(min_order_size=D("5"))
        f = fill(make_order("40"), market=market, config=FillConfig(max_depth_fraction=D("0.05")))
        assert f.status is FillStatus.REJECTED
        assert "minimum" in f.reason


class TestLimitPrice:
    def test_buy_stops_at_limit(self):
        # 0.56 > 0.55 limit: 17 @ 0.44 + 20 @ 0.55 = 7.48 + 11.00 = 18.48
        f = fill(make_order("40", limit="0.55"))
        assert f.status is FillStatus.PARTIAL
        assert f.filled_quantity == D("37")
        assert f.gross == D("18.48")
        assert "limit" in f.reason

    def test_sell_stops_at_limit(self):
        # 0.41 < 0.415 limit: only 13 @ 0.42
        f = fill(make_order("20", side=Side.SELL, limit="0.415"))
        assert f.legs == (L("0.42", "13"),)

    def test_unreachable_limit_rejected(self):
        f = fill(make_order("40", limit="0.43"))
        assert f.status is FillStatus.REJECTED

    def test_no_limit_in_no_terms(self):
        # Buying NO with NO limit 0.58: only the 0.58 level (from YES bid 0.42) qualifies.
        f = fill(make_order("20", outcome=Outcome.NO, limit="0.58"))
        assert f.legs == (L("0.58", "13"),)


class TestFees:
    def test_kalshi_fee_applied(self):
        # Same legs as test_buy_yes_walks_asks; fee 0.6915 computed in tests/core/test_fees.py.
        f = fill(make_order("40"), market=make_market(fee_schedule=KALSHI_DIRECT_FEES))
        assert f.fee == D("0.6915")
        assert f.cash_change == D("-20.8515")

    def test_unknown_fees_rejected(self):
        f = fill(make_order("40"), market=make_market(fee_schedule=None))
        assert f.status is FillStatus.REJECTED
        assert "fee" in f.reason


class TestRejections:
    def test_empty_side(self):
        f = fill(make_order("10"), book=make_book(asks=()))
        assert f.status is FillStatus.REJECTED
        assert "depth" in f.reason

    @pytest.mark.parametrize("status", [MarketStatus.CLOSED, MarketStatus.PAUSED, MarketStatus.RESOLVED])
    def test_market_not_open(self, status):
        f = fill(make_order("10"), market=make_market(status=status))
        assert f.status is FillStatus.REJECTED

    def test_stale_book(self):
        # arrival at T0 + 0.25s; book 61s later exceeds the 60s default
        book = make_book(received_at=T0 + timedelta(seconds=61.25, microseconds=1))
        f = fill(make_order("10"), book=book, config=FillConfig())
        assert f.status is FillStatus.REJECTED
        assert "arrival" in f.reason


class TestLatencyGuard:
    def test_book_before_arrival_is_an_error(self):
        # decided T0, latency 250ms -> arrival T0 + 0.25s; a book from T0 + 0.1s predates it.
        book = make_book(received_at=T0 + timedelta(milliseconds=100))
        with pytest.raises(ValueError, match="before order arrival"):
            fill(make_order("10"), book=book)

    def test_book_exactly_at_arrival_ok(self):
        book = make_book(received_at=T0 + timedelta(milliseconds=250))
        assert fill(make_order("10"), book=book).status is FillStatus.FILLED

    def test_mismatched_market_is_an_error(self):
        with pytest.raises(ValueError):
            fill(make_order("10", market_id="OTHER"))


class TestInvariants:
    @pytest.mark.parametrize("outcome", list(Outcome))
    @pytest.mark.parametrize("side", list(Side))
    @pytest.mark.parametrize("qty", ["0.01", "1", "13", "13.5", "33", "66", "100", "1000"])
    @pytest.mark.parametrize("fraction", ["0.01", "0.25", "1"])
    def test_never_beyond_depth_never_at_mid(self, outcome, side, qty, fraction):
        config = FillConfig(max_depth_fraction=D(fraction))
        f = fill(make_order(qty, outcome=outcome, side=side), config=config)
        available = contract_levels(make_book(), outcome, side)
        depth = sum(lvl.size for lvl in available)
        assert f.filled_quantity <= D(fraction) * depth
        assert f.filled_quantity <= D(qty)
        # Each leg sits exactly on a visible level and never takes more than it shows.
        for leg, lvl in zip(f.legs, available, strict=False):
            assert leg.price == lvl.price
            assert leg.size <= lvl.size
        # Every leg but the last consumes its whole level (no skipping ahead).
        for leg, lvl in zip(f.legs[:-1], available, strict=False):
            assert leg.size == lvl.size


def test_contract_levels_no_side_is_best_first():
    asks_for_no_buyer = contract_levels(make_book(DOC_BIDS, DOC_ASKS), Outcome.NO, Side.BUY)
    prices = [lvl.price for lvl in asks_for_no_buyer]
    assert prices == sorted(prices)
