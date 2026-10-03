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
    DaySplit,
    ObservationIndex,
    conclude,
    predict,
    split_day,
    undercounts,
    verdict,
    window_insensitive,
)

from profit_engine.weather import Observation

DAY = date(2026, 8, 20)
U = D(2)


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
        assert split_day(DAY, index) == split("82", "79", "70")

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
