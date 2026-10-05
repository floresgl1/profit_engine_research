from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from profit_engine.maker.quoter import MakerFill
from profit_engine.maker.report import covered_from, daily, event_day_of, paired, render, stats, t95

T = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)


def fill(market, side, price, qty, at=T):
    return MakerFill(market, side, D(price), D(qty), D(0), at)


def test_event_day_and_coverage():
    assert event_day_of("KXHIGHNY-26OCT05-B68.5") == date(2026, 10, 5)
    assert event_day_of("weird") is None
    # started 02:29Z Oct 5: Oct 5 markets opened Oct 4 14:00Z (before), Oct 6 ones Oct 5 14:00Z (after)
    assert covered_from(datetime(2026, 10, 5, 2, 29, tzinfo=timezone.utc)) == date(2026, 10, 6)
    assert covered_from(datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)) == date(2026, 10, 5)


def test_daily_hand_computed():
    fills = [
        fill("KXHIGHNY-26OCT05-B68.5", "bid", "0.40", 10),  # long 10 at 0.40, settles YES: +6.00
        fill("KXHIGHNY-26OCT05-B70.5", "ask", "0.30", 5),   # short 5 at 0.30, settles NO: +1.50
        fill("KXHIGHNY-26OCT06-B70.5", "bid", "0.50", 4),   # long 4, unsettled, last mid 0.55: +0.20 estimate
    ]
    res = {"KXHIGHNY-26OCT05-B68.5": D(1), "KXHIGHNY-26OCT05-B70.5": D(0)}
    days, max_pos = daily(fills, res, mark=lambda t: D("0.55"))
    assert [(d.day, d.pnl, d.settled) for d in days] == [(date(2026, 10, 5), D("7.50"), True),
                                                         (date(2026, 10, 6), D("0.20"), False)]
    assert max_pos == D(10)
    s = stats(days)
    assert (s.days, s.total, s.per_contract) == (1, 7.5, pytest.approx(50.0))  # 7.50 over 15 contracts


def test_stats_interval_and_drawdown():
    from profit_engine.maker.report import Day

    days = [Day(date(2026, 10, i), D(x), True, 1, D(10)) for i, x in enumerate([3, -1, -2, 4], start=1)]
    s = stats(days)
    # mean 1, sd sqrt(((2)^2+(-2)^2+(-3)^2+3^2)/3) = sqrt(26/3); half = 3.182 * sd / 2
    assert s.mean == 1 and s.half_width == pytest.approx(3.182 * (26 / 3) ** 0.5 / 2)
    assert (s.worst_day, s.max_drawdown) == (-2, 3)  # cum 3, 2, 0, 4: peak 3 -> trough 0
    assert t95(1000) == 1.96


def test_paired_counts_missing_days_as_zero_and_skips_unsettled():
    from profit_engine.maker.report import Day

    a = [Day(date(2026, 10, 5), D(1), True, 1, D(1)), Day(date(2026, 10, 6), D(2), True, 1, D(1)),
         Day(date(2026, 10, 7), D(5), False, 1, D(1))]
    b = [Day(date(2026, 10, 5), D(3), True, 1, D(1))]
    s = paired(a, b, date(2026, 10, 5))  # diffs: +2, -2 (b idle on the 6th), 7th unsettled
    assert (s.days, s.mean, s.total) == (2, 0, 0)
    assert paired(a, b, date(2026, 10, 6)).total == -2


def test_render_lists_strategies_without_fills():
    out = render({"join_touch_v1": [fill("KXHIGHNY-26OCT05-B68.5", "bid", "0.40", 10)]}, {"KXHIGHNY-26OCT05-B68.5": D(1)},
                 mark=lambda t: None, runs={"join_touch_v1": T, "model_veto_v2": datetime(2026, 10, 5, 2, 29, tzinfo=timezone.utc)})
    assert "[model_veto_v2] fills: 0" in out and "[model_veto_v2 - join_touch_v1, paired, days from 2026-10-07]" in out
    assert render({}, {}, lambda t: None, {}) == "No paper market-maker fills yet."


def test_pairing_starts_when_the_later_strategy_started():
    early = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    v2_start = datetime(2026, 10, 5, 2, 29, tzinfo=timezone.utc)
    # v1 has a run recorded late (restart) but fills from Oct 1: it started Oct 1.
    out = render({"join_touch_v1": [fill("KXHIGHNY-26OCT02-B68.5", "bid", "0.40", 1, at=early)]}, {},
                 mark=lambda t: None, runs={"join_touch_v1": datetime(2026, 10, 9, tzinfo=timezone.utc), "model_veto_v2": v2_start})
    assert "paired, days from 2026-10-06]" in out


def test_pair_from_overrides_start():
    out = render({"join_touch_v1": [fill("KXHIGHNY-26OCT05-B68.5", "bid", "0.40", 1)]}, {}, mark=lambda t: None,
                 runs={"model_veto_v2": T}, pair_from=date(2026, 10, 5))
    assert "paired, days from 2026-10-05]" in out
