from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from profit_engine.maker.queue import PublicTrade, RestingQuote, match, refresh_queue

T0 = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)


def at(s):
    return T0 + timedelta(seconds=s)


def trade(s, price, count, taker_yes, tid=None):
    return PublicTrade(tid or f"t{s}", at(s), D(price), D(count), taker_yes)


BID = RestingQuote("bid", D("0.40"), D(10), queue_ahead=D(25), posted_at=T0)
ASK = RestingQuote("ask", D("0.44"), D(10), queue_ahead=D(5), posted_at=T0)


def test_queue_ahead_trades_first():
    # 20 sold at 0.40 (hits bids): all to the 25 ahead of us. Then 8 more: 5 ahead, 3 to us.
    filled, q = match(BID, [trade(1, "0.40", 20, False), trade(2, "0.40", 8, False)])
    assert filled == D(3)
    assert (q.size, q.queue_ahead) == (D(7), D(0))


def test_wrong_side_and_better_prices_do_nothing():
    trades = [trade(1, "0.40", 100, True),   # taker bought YES at 0.40: not our bid's side
              trade(2, "0.41", 100, False)]  # sold at 0.41: a better bid filled, not us
    filled, q = match(BID, trades)
    assert filled == 0 and q == BID


def test_trade_through_our_price_fills_us_completely():
    filled, q = match(BID, [trade(1, "0.38", 1, False)])
    assert filled == D(10) and q.size == 0
    filled, q = match(ASK, [trade(1, "0.46", 1, True)])
    assert filled == D(10) and q.size == 0


def test_trades_at_or_before_posting_are_ignored():
    filled, _ = match(BID, [trade(0, "0.38", 50, False), trade(-5, "0.38", 50, False)])
    assert filled == 0


def test_ask_side_queue():
    filled, q = match(ASK, [trade(1, "0.44", 12, True)])  # 5 ahead, 7 to us
    assert filled == D(7) and q.size == D(3)


def test_refresh_queue_only_shrinks():
    assert refresh_queue(BID, D(10)).queue_ahead == D(10)  # cancels ahead of us
    assert refresh_queue(BID, D(100)).queue_ahead == D(25)  # new orders join behind us


def test_validation():
    with pytest.raises(ValueError):
        RestingQuote("buy", D("0.4"), D(1), D(0), T0)
    with pytest.raises(ValueError):
        RestingQuote("bid", D("0.4"), D(-1), D(0), T0)
