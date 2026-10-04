import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from calibration_screen import Row, Settled, band_of, clustered_mean, fee, parse_market, sample, series_of


def row(bid, ask, result, multiplier="1"):
    return Row("T", "E", "E", "Sports", False, D(bid), D(ask), result, D(multiplier))


def test_fee_and_strategy_pnl_hand_computed():
    # buy YES at 0.10: fee 0.07 * 0.10 * 0.90 = 0.0063; wins -> 1 - 0.10 - 0.0063
    assert row("0.08", "0.10", 1).buy_yes == D("0.8937")
    assert row("0.08", "0.10", 0).buy_yes == D("-0.1063")
    # buy NO at 1 - 0.08 = 0.92: fee 0.07 * 0.92 * 0.08 = 0.005152; YES loses -> 1 - 0.92 - 0.005152
    assert row("0.08", "0.10", 0).buy_no == D("0.074848")
    assert fee(D("0.5"), D("0.5")) == D("0.00875")  # half-fee series


def test_clustered_se_counts_events_not_markets():
    # two events, each two identical markets: clustering = two observations, not four
    mean, se = clustered_mean([1.0, 1.0, 0.0, 0.0], ["a", "a", "b", "b"])
    assert mean == 0.5
    # cluster sums of deviations: +1, -1 -> var = 2 / 16 * 2 / 1 = 0.25 -> se 0.5
    assert se == pytest.approx(0.5)
    assert math.isnan(clustered_mean([1.0, 0.0], ["a", "a"])[1])


def test_band_of():
    assert band_of(D("0.03")) == (0, 5)
    assert band_of(D("0.05")) == (5, 10)
    assert band_of(D("0.995")) == (95, 100)


def test_parse_market_and_series():
    raw = {"ticker": "KXA-1-B", "event_ticker": "KXA-1", "market_type": "binary", "result": "yes",
           "open_time": "2026-01-01T00:00:00Z", "close_time": "2026-01-03T00:00:00Z", "volume_fp": "900.5"}
    m = parse_market(raw)
    assert (m.result, m.volume, series_of(m.event)) == (1, D("900.5"), "KXA")
    assert parse_market(dict(raw, result="")) is None
    assert parse_market(dict(raw, market_type="scalar")) is None


def test_sample_caps_per_series_and_filters():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ms = [Settled(f"KXA-{i}-X", f"KXA-{i}", t0, t0 + timedelta(days=2), 0, D(1000)) for i in range(30)]
    ms.append(Settled("KXB-1-X", "KXB-1", t0, t0 + timedelta(hours=2), 0, D(1000)))  # opened too late
    ms.append(Settled("KXC-1-X", "KXC-1", t0, t0 + timedelta(days=2), 0, D(10)))  # too thin
    picks = sample(ms, per_series=5)
    assert len(picks) == 5 and all(series_of(m.event) == "KXA" for m in picks)
    assert picks == sample(ms, per_series=5)  # seeded


def test_holdout_split_is_stable_and_roughly_half():
    from calibration_screen import is_holdout

    events = [f"KXA-{i}" for i in range(2000)]
    share = sum(is_holdout(e) for e in events) / len(events)
    assert 0.45 < share < 0.55
    assert [is_holdout(e) for e in events] == [is_holdout(e) for e in events]


def test_smaller_samples_nest_in_larger():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ms = [Settled(f"KX{s}-{i}-X", f"KX{s}-{i}", t0, t0 + timedelta(days=2), 0, D(1000)) for s in "ABC" for i in range(20)]
    assert set(sample(ms, per_series=3)) <= set(sample(ms, per_series=6))
