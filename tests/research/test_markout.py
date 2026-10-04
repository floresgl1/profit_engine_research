from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest
from markout import Trade, fills_for, maker_fee, maker_sign, mid_after, mid_at_or_before, parse_trade, pick_events, weighted

from profit_engine.weather.kalshi_temps import Bucket, Quote, SettledTemperature

DAY = date(2026, 9, 28)  # EDT


def utc(h, m=0, d=28):
    return datetime(2026, 9, d, h, m, tzinfo=timezone.utc)


def test_parse_trade():
    raw = {"created_time": "2026-09-28T18:00:00.5Z", "yes_price_dollars": "0.4200", "count_fp": "10.00",
           "taker_outcome_side": "yes", "is_block_trade": False}
    t = parse_trade(raw)
    assert (t.yes_price, t.count, t.taker_yes) == (D("0.42"), D(10), True)
    assert parse_trade(dict(raw, is_block_trade=True)) is None
    assert parse_trade(dict(raw, taker_outcome_side="")) is None


def test_mid_lookups_never_peek():
    qs = [Quote(utc(17), D("0.40"), D("0.44")), Quote(utc(18), D("0"), D("0.5")), Quote(utc(19), D("0.50"), D("0.54"))]
    assert mid_at_or_before(qs, utc(17, 5)) == D("0.42")
    assert mid_at_or_before(qs, utc(18, 5)) is None  # 18:00 book one-sided, 17:00 mid too old
    assert mid_at_or_before(qs, utc(16)) is None
    assert mid_after(qs, utc(18, 1)) == D("0.52")


def test_fills_hand_computed():
    # Bucket 70-71 wins (high 71). Mid 0.42 at 17Z (trades 5 min later), first mid >= 1 h later is 0.52 (19Z).
    event = SettledTemperature("KXHIGHNY-26SEP28", DAY, D(71), "twc", (Bucket("B70.5", "between", D(70), D(71)),))
    trades = {"B70.5": [Trade(utc(17, 5), D("0.44"), D(10), True),    # taker bought YES at 0.44: maker short
                        Trade(utc(17, 5), D("0.40"), D(30), False)]}  # taker sold YES at 0.40: maker long
    mids = {"B70.5": [Quote(utc(17), D("0.40"), D("0.44")), Quote(utc(19), D("0.50"), D("0.54"))]}
    short, long_ = fills_for("KXHIGHNY", event, trades, mids)
    assert (short.entry, short.markout_1h, short.settle) == pytest.approx((0.02, -0.08, -0.56))
    assert (long_.entry, long_.markout_1h, long_.settle) == pytest.approx((0.02, 0.12, 0.60))
    assert short.settle_fee == pytest.approx(-0.56 - 0.0175 * 0.44 * 0.56)
    assert (short.day_offset, short.local_hour, short.session) == (0, 13, "D 10-14")
    stale = fills_for("KXHIGHNY", event, {"B70.5": [Trade(utc(17, 30), D("0.44"), D(1), True)]}, mids)[0]
    assert stale.entry is None and stale.settle == pytest.approx(-0.56)
    # volume-weighted settlement: (10 * -0.56 + 30 * 0.60) / 40 = 0.31
    assert weighted([short, long_], "settle")[0] == pytest.approx(0.31)


def test_sign_and_fee():
    assert maker_sign(Trade(utc(1), D("0.5"), D(1), True)) == -1
    assert maker_fee(D("0.5")) == D("0.004375")


def test_pick_events_spreads_over_test_period():
    events = [SettledTemperature(f"E{i}", date(2025, 8, 1).fromordinal(date(2025, 8, 1).toordinal() + i), D(70), "nws",
                                 (Bucket("b", "between", D(70), D(71)),)) for i in range(400)]
    picked = pick_events(events, n=40)
    assert len(picked) == 40 and picked[0].day == date(2025, 8, 1) and picked[-1].day >= date(2026, 8, 1)
