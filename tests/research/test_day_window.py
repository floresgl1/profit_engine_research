"""Day-window analysis tests on hand-built days.

DST day 2026-08-20 (EDT = UTC-4):
    start hour [00:00, 01:00) EDT = [04:00, 05:00) UTC Aug 20   counted by clock time only
    core       [01:00 EDT Aug 20, 00:00 EDT Aug 21)              counted by both
    end hour   [00:00, 01:00) EDT Aug 21 = [04:00, 05:00) UTC    counted by LST only
"""

from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest
from day_window import (
    Bounds,
    DaySplit,
    ObservationIndex,
    conclude,
    drop_spikes,
    predict,
    split_day,
    undercounts,
    verdict,
    window_insensitive,
)

from profit_engine.weather import Observation

DAY = date(2026, 8, 20)
U = Bounds(D(0), D(2))  # official high is 0 to 2 degrees above the max observation


def obs(day, hour, minute, tmpf):
    return Observation(datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc), D(tmpf))


def split(start, core, end, day=DAY):
    return DaySplit(day, None if start is None else D(start), None if core is None else D(core), None if end is None else D(end))


class TestSplitDay:
    def test_hours_land_in_the_right_part(self):
        index = ObservationIndex([
            obs(20, 4, 51, "82"),   # 00:51 EDT Aug 20 -> start
            obs(20, 5, 0, "79"),    # 01:00 EDT exactly -> core (start is [00:00, 01:00))
            obs(20, 18, 51, "75"),  # 14:51 EDT -> core
            obs(21, 3, 51, "71"),   # 23:51 EDT -> core
            obs(21, 4, 51, "70"),   # 00:51 EDT Aug 21 -> end
            obs(21, 5, 51, "99"),   # 01:51 EDT Aug 21 -> outside the day
        ])  # fmt: skip
        s = split_day(DAY, index)
        assert (s.start, s.core, s.end) == (D(82), D(79), D(70))
        assert s.gap  # 01:00 to 14:51 EDT has no readings, so the day is not usable

    def test_gap_makes_day_incomplete(self):
        # every part has a reading, but nothing between 01:00 and 23:00 EDT
        index = ObservationIndex([obs(20, 4, 51, "82"), obs(20, 5, 10, "79"), obs(21, 3, 51, "71"), obs(21, 4, 51, "70")])
        s = split_day(DAY, index)
        assert s.gap and not s.complete

    def test_missing_hour_is_none(self):
        index = ObservationIndex([obs(20, 18, 51, "75")])
        s = split_day(DAY, index)
        assert s.start is None and s.end is None and not s.complete


class TestPredict:
    def test_warm_start_hour(self):
        # start 82 > max(core 75, end 70) + 2 = 77 -> qualifies.
        # LST window = core + end -> max 75, official in [75, 77]; clock = start + core -> [82, 84].
        p = predict(split("82", "75", "70"), U)
        assert (p.case, p.lst, p.clock) == ("start", (D(75), D(77)), (D(82), D(84)))

    def test_warm_end_hour(self):
        # end 82 > max(start 70, core 75) + 2 -> LST [82, 84], clock [75, 77]
        p = predict(split("70", "75", "82"), U)
        assert (p.case, p.lst, p.clock) == ("end", (D(82), D(84)), (D(75), D(77)))

    def test_gap_not_above_margin(self):
        # start 77 is not > 75 + 2
        assert predict(split("77", "75", "70"), U) is None

    def test_standard_time_never_qualifies(self):
        assert predict(split("82", "75", "70", day=date(2026, 1, 20)), U) is None

    def test_transition_day_never_qualifies(self):
        assert predict(split("82", "75", "70", day=date(2026, 3, 8)), U) is None

    def test_incomplete_day(self):
        assert predict(split("82", "75", None), U) is None

    def test_two_sided_band(self):
        # band -1 to +2 (width 3): start 79 > 75 + 3 -> LST [74, 77], clock [78, 81]; 78 would not qualify.
        band = Bounds(D(-1), D(2))
        p = predict(split("79", "75", "70"), band)
        assert (p.lst, p.clock) == ((D(74), D(77)), (D(78), D(81)))
        assert predict(split("78", "75", "70"), band) is None


class TestBounds:
    def test_from_errors_always_includes_zero(self):
        assert Bounds.from_errors([D(1), D(3)]) == Bounds(D(0), D(3))
        assert Bounds.from_errors([D(-2), D(4)]) == Bounds(D(-2), D(4))
        assert Bounds.from_errors([D(-2), D(4)]).width == D(6)


class TestDropSpikes:
    def test_isolated_spike_removed(self):
        # Real case, 2026-08-27 (UTC times): 70 -> 80 -> 71 within an hour
        series = [obs(27, 19, 42, "70"), obs(27, 19, 51, "80"), obs(27, 19, 59, "71")]
        assert [o.tmpf for o in drop_spikes(series)] == [D(70), D(71)]

    def test_sustained_rise_kept(self):
        series = [obs(27, 19, 0, "70"), obs(27, 19, 30, "76"), obs(27, 19, 50, "77")]
        assert len(drop_spikes(series)) == 3

    def test_far_neighbours_do_not_count(self):
        # neighbours more than an hour away: can't tell a spike from real change
        series = [obs(27, 10, 0, "70"), obs(27, 12, 0, "80"), obs(27, 14, 0, "70")]
        assert len(drop_spikes(series)) == 3


class TestVerdict:
    p = predict(split("82", "75", "70"), U)  # LST [75, 77], clock [82, 84]

    @pytest.mark.parametrize("value, expected", [("75", "lst"), ("77", "lst"), ("82", "clock"), ("84", "clock"), ("79", "neither"), ("85", "neither")])
    def test_ranges(self, value, expected):
        assert verdict(D(value), self.p) == expected

    def test_nws_match_counts_as_lst(self):
        # The agreed rule: matching the NWS high means LST, even outside the predicted range.
        assert verdict(D("78"), self.p, nws_high=78) == "lst"

    def test_nws_mismatch_falls_back_to_ranges(self):
        assert verdict(D("83"), self.p, nws_high=76) == "clock"


class TestCalibration:
    def test_standard_day_always_usable(self):
        assert window_insensitive(date(2026, 1, 20), split("70", "60", "70", day=date(2026, 1, 20)))

    def test_calm_dst_day(self):
        # both midnight hours (70, 74) at least 5 below the core peak 80
        assert window_insensitive(DAY, split("70", "80", "74"))
        assert not window_insensitive(DAY, split("70", "80", "76"))

    def test_undercount(self):
        # official 81 - max obs 80 = 1; second day is not calm, so it's excluded
        days = [(DAY, 81, split("70", "80", "74")), (date(2026, 8, 21), 90, split("88", "80", "70", day=date(2026, 8, 21)))]
        assert undercounts(days) == [D(1)]


class TestConclude:
    @pytest.mark.parametrize(
        "verdicts, expected",
        [
            ([], "inconclusive"),
            (["lst", "lst"], "lst"),
            (["clock"], "clock"),
            (["lst", "clock"], "inconclusive"),
            (["lst", "neither"], "inconclusive"),
        ],
    )
    def test_rule(self, verdicts, expected):
        assert conclude(verdicts)[0] == expected

    def test_no_dates_reason(self):
        assert conclude([])[1] == "no qualifying dates"
