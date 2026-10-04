from decimal import Decimal as D

from structural_edges import buy_all_edge, fee, is_certain, is_dead, is_partition, lower_bound, sell_all_edge

from profit_engine.weather.kalshi_temps import Bucket

EVENT = [
    Bucket("T63", "less", None, D(63)),
    Bucket("B63.5", "between", D(63), D(64)),
    Bucket("B65.5", "between", D(65), D(66)),
    Bucket("B67.5", "between", D(67), D(68)),
    Bucket("B69.5", "between", D(69), D(70)),
    Bucket("T70", "greater", D(70), None),
]


def test_fee_per_contract():
    # 0.07 * 100 * 0.15 * 0.85 = 0.8925 for 100 contracts -> 0.008925 each
    assert fee(D("0.15")) == D("0.008925")
    assert fee(D("0.5")) == D("0.0175")


def test_partition():
    assert is_partition(EVENT)
    assert not is_partition(EVENT[:-1])  # 71+ uncovered
    assert not is_partition(EVENT + [Bucket("X", "between", D(69), D(70))])  # 69-70 twice


def test_buy_all_edge_hand_computed():
    # six asks of 0.15: cost 0.90 + 6 * 0.008925 = 0.95355 -> edge 0.04645
    assert buy_all_edge([D("0.15")] * 6) == D("0.04645")
    # asks summing to 1.02: no edge
    assert buy_all_edge([D("0.17")] * 6) < 0
    assert buy_all_edge([D("0.15")] * 5 + [D("1")]) is None  # empty ask side


def test_sell_all_edge_hand_computed():
    # six bids of 0.20: NO costs 0.80 each + fee 0.07*0.8*0.2 = 0.0112 -> 6 * 0.8112 = 4.8672; payout 5
    assert sell_all_edge([D("0.20")] * 6) == D("0.1328")
    assert sell_all_edge([D("0.20")] * 5 + [D("0")]) is None  # no bid


def test_lower_bound_and_dead_buckets():
    # max reading 68.6 rounds to 69; smallest undercount -1 -> high is at least 68
    bound = lower_bound(68.6, {-1: 3, 0: 324, 1: 489})
    assert bound == 68
    dead = [b.ticker for b in EVENT if is_dead(b, bound)]
    assert dead == ["T63", "B63.5", "B65.5"]  # 67-68 can still win (68)
    assert not any(is_certain(b, bound) for b in EVENT)
    assert is_certain(EVENT[-1], 71)  # high >= 71 > 70: "greater than 70" can't lose
    assert not is_certain(EVENT[-1], 70)
    assert is_dead(EVENT[0], 63) and not is_dead(EVENT[0], 62)  # "less than 63" dies once high >= 63


def test_realized_pnl():
    from datetime import datetime, timezone

    from structural_edges import DeadQuote

    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # sold YES at 0.30 on a dead bucket: keep 0.30 - fee(0.70) = 0.30 - 0.0147 if it loses
    held = DeadQuote("S", "T", at, 14, "dead", D("0.30"), D("0.30") - fee(D("0.70")), False, True)
    assert held.pnl == D("0.2853")
    failed = DeadQuote("S", "T", at, 14, "dead", D("0.30"), D("0.30") - fee(D("0.70")), True, False)
    assert failed.pnl == D("-0.7147")
