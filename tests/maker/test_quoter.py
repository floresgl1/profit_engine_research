from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from profit_engine.core import Level, OrderBook
from profit_engine.maker.queue import PublicTrade
from profit_engine.maker.quoter import MakerFill, MarketMaker, QuoterConfig

T0 = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)


def at(s):
    return T0 + timedelta(seconds=s)


def book(bid=("0.40", 25), ask=("0.44", 5), s=0):
    bids = (Level(D(bid[0]), D(bid[1])),) if bid else ()
    asks = (Level(D(ask[0]), D(ask[1])),) if ask else ()
    return OrderBook("kalshi", "M", bids, asks, at(s))


def trade(s, price, count, taker_yes):
    return PublicTrade(f"t{s}", at(s), D(price), D(count), taker_yes)


def mm(**kw):
    return MarketMaker("M", QuoterConfig(maker_fee_rate=D("0.0175"), **kw))


def test_joins_both_sides_behind_visible_size():
    m = mm()
    assert m.step(book(), [], at(0)) == []
    assert (m.quotes["bid"].price, m.quotes["bid"].queue_ahead) == (D("0.40"), D(25))
    assert (m.quotes["ask"].price, m.quotes["ask"].queue_ahead) == (D("0.44"), D(5))


def test_fill_accounting_hand_computed():
    m = mm()
    m.step(book(), [], at(0))
    # taker buys 12 YES at 0.44: 5 ahead, 7 to us. fee 0.0175 * 7 * 0.44 * 0.56 = 0.030184
    fills = m.step(book(ask=("0.44", 3), s=10), [trade(5, "0.44", 12, True)], at(10))
    assert fills == [MakerFill("M", "ask", D("0.44"), D(7), D("0.030184"), at(10))]
    assert m.position == D(-7)
    assert m.cash == D("0.44") * 7 - D("0.030184")
    # 3 left on the ask, still at 0.44, now at the front of the queue
    assert (m.quotes["ask"].size, m.quotes["ask"].queue_ahead) == (D(3), D(0))


def test_keeps_queue_place_while_price_holds_and_loses_it_when_price_moves():
    m = mm()
    m.step(book(), [], at(0))
    m.step(book(bid=("0.40", 40), s=10), [trade(5, "0.40", 10, False)], at(10))
    assert m.quotes["bid"].queue_ahead == D(15)  # 25 - 10 traded; 40 visible includes orders behind us
    m.step(book(bid=("0.41", 8), s=20), [], at(20))
    q = m.quotes["bid"]
    assert (q.price, q.queue_ahead, q.posted_at) == (D("0.41"), D(8), at(20))  # back of the new level


def test_position_limit_stops_the_side_that_adds_risk():
    m = mm(size=D(10), max_position=D(10))
    m.step(book(), [], at(0))
    m.step(book(bid=("0.40", 1), s=10), [trade(5, "0.39", 1, False)], at(10))  # swept through: +10 long
    assert m.position == D(10)
    assert "bid" not in m.quotes and "ask" in m.quotes


def test_one_sided_book_pulls_quotes():
    m = mm()
    m.step(book(), [], at(0))
    m.step(book(ask=None, s=10), [], at(10))
    assert m.quotes == {}


def test_config_validation():
    with pytest.raises(ValueError):
        QuoterConfig(size=D(10), max_position=D(5))


def test_model_veto_hand_computed():
    # book 0.40 / 0.44, margin 0.20.
    m = MarketMaker("M", QuoterConfig(model_margin=D("0.20")))
    m.step(book(), [], at(0), fair=D("0.19"))  # bid 0.40 > 0.19 + 0.20 -> veto bid; ask 0.44 >= -0.01 -> keep
    assert set(m.quotes) == {"ask"}
    m.step(book(), [], at(1), fair=D("0.20"))  # bid 0.40 == 0.40: allowed again
    assert set(m.quotes) == {"bid", "ask"}
    m.step(book(), [], at(2), fair=D("0.65"))  # ask 0.44 < 0.65 - 0.20 = 0.45 -> veto ask
    assert set(m.quotes) == {"bid"}
    m.step(book(), [], at(3), fair=None)  # model abstains: quote like v1
    assert set(m.quotes) == {"bid", "ask"}
    with pytest.raises(ValueError):
        QuoterConfig(model_margin=D("-0.01"))
