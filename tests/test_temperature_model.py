"""Distribution math. Expected values computed by hand (normal CDF table values in comments)."""

from decimal import Decimal as D

import pytest

from profit_engine.models.temperature import (
    HighDistribution,
    RemainingMaxModel,
    SpreadModel,
    bucket_probabilities,
    floor_from_observations,
    max_of,
    observed_part,
    predict_high,
)
from profit_engine.weather.kalshi_temps import Bucket

# The Oct 4 KXHIGHNY event: six buckets that cover every whole-degree high exactly once.
EVENT = [
    Bucket("T63", "less", None, D(63)),
    Bucket("B63.5", "between", D(63), D(64)),
    Bucket("B65.5", "between", D(65), D(66)),
    Bucket("B67.5", "between", D(67), D(68)),
    Bucket("B69.5", "between", D(69), D(70)),
    Bucket("T70", "greater", D(70), None),
]


class TestRoundedNormal:
    def test_hand_computed(self):
        # mu 67, sigma 1:
        #   P(67)      = Phi(0.5) - Phi(-0.5) = 0.691462 - 0.308538 = 0.382925
        #   P(66)=P(68) = Phi(1.5) - Phi(0.5) = 0.933193 - 0.691462 = 0.241730
        #   P(65)=P(69) = Phi(2.5) - Phi(1.5) = 0.993790 - 0.933193 = 0.060598
        d = HighDistribution.rounded_normal(67, 1)
        assert d.probs[67] == pytest.approx(0.382925, abs=1e-5)
        assert d.probs[66] == pytest.approx(0.241730, abs=1e-5)
        assert d.probs[66] == pytest.approx(d.probs[68], abs=1e-12)
        assert d.probs[69] == pytest.approx(0.060598, abs=1e-5)
        assert d.mean() == pytest.approx(67)

    def test_half_degree_mean_splits_evenly(self):
        d = HighDistribution.rounded_normal(67.5, 2)
        assert d.probs[67] == pytest.approx(d.probs[68])

    def test_invalid_sigma(self):
        with pytest.raises(ValueError):
            HighDistribution.rounded_normal(67, 0)


class TestConditioning:
    # The worked example: forecast 67/68/69/70 = .30/.30/.25/.15, then observe H >= 68.
    MORNING = HighDistribution({67: 0.30, 68: 0.30, 69: 0.25, 70: 0.15})

    def test_cut_and_rescale(self):
        d = self.MORNING.at_least(68)
        assert d.probs == pytest.approx({68: 0.30 / 0.70, 69: 0.25 / 0.70, 70: 0.15 / 0.70})

    def test_bucket_moves_from_040_to_0571(self):
        b = EVENT[4]  # 69-70
        assert self.MORNING.probability(b) == pytest.approx(0.40)
        assert self.MORNING.at_least(68).probability(b) == pytest.approx(0.4 / 0.7)

    def test_ratios_unchanged(self):
        d = self.MORNING.at_least(68)
        assert d.probs[68] / d.probs[69] == pytest.approx(0.30 / 0.25)

    def test_observation_beyond_forecast(self):
        # Forecast never imagined 75; the observation wins.
        assert self.MORNING.at_least(75).probs == {75: 1.0}

    def test_floor_below_support_changes_nothing(self):
        assert self.MORNING.at_least(50).probs == pytest.approx(self.MORNING.probs)


class TestFloor:
    @pytest.mark.parametrize(
        "observed, band, floor",
        [
            (69.0, -0.8, 68),  # 68.2 -> 68: rounded down on purpose (see docstring)
            (69.0, 0.0, 69),
            (68.9, -0.8, 68),
            (70.4, -0.8, 69),
        ],
    )
    def test_floor(self, observed, band, floor):
        assert floor_from_observations(observed, band) == floor


class TestBuckets:
    @pytest.mark.parametrize("mu, sigma", [(66.4, 2.3), (61.0, 1.0), (75.0, 4.0), (67.5, 0.3)])
    def test_event_buckets_sum_to_one(self, mu, sigma):
        probs = bucket_probabilities(HighDistribution.rounded_normal(mu, sigma), EVENT)
        assert sum(probs) == pytest.approx(1.0, abs=1e-12)

    def test_conditioned_buckets_still_sum_to_one(self):
        d = HighDistribution.rounded_normal(66.4, 2.3).at_least(67)
        probs = bucket_probabilities(d, EVENT)
        assert sum(probs) == pytest.approx(1.0, abs=1e-12)
        assert probs[:3] == [0.0, 0.0, 0.0]


def test_rejects_non_distribution():
    with pytest.raises(ValueError):
        HighDistribution({67: 0.5, 68: 0.4})


class TestSpreadModel:
    def test_fit_fixed(self):
        # residuals 1, 3, -1, 1: mean 1; deviations 0, 2, -2, 0 -> var 8/3 -> sigma 1.633
        m = SpreadModel.fit_fixed([1.0, 3.0, -1.0, 1.0])
        assert m.bias == pytest.approx(1.0) and m.sigma == pytest.approx((8 / 3) ** 0.5)

    def test_fit_scaled(self):
        # residuals 2, 0 (bias 1): deviations 1, -1; xnd 1 and 2 -> k^2 = (1 + 0.25) / 2 -> k = 0.7906
        m = SpreadModel.fit_scaled([2.0, 0.0], [1.0, 2.0])
        assert m.k == pytest.approx((1.25 / 2) ** 0.5)
        assert m.spread(2.0) == pytest.approx(2 * m.k)
        assert m.spread(0.0) == pytest.approx(m.k)  # xnd floored at 1

    def test_predict_high_applies_bias_and_floor(self):
        m = SpreadModel(bias=1.0, sigma=1.0)
        d = predict_high(m, txn=66.0, xnd=None, observed_max=69.0, low_band=-0.8)
        # centered at 67, floor floor(68.2) = 68
        assert min(d.probs) == 68
        assert sum(d.probs.values()) == pytest.approx(1.0)

    def test_round_trip(self):
        m = SpreadModel(bias=0.5, k=1.7)
        assert SpreadModel.from_dict(m.to_dict()) == m

    def test_exactly_one_spread(self):
        with pytest.raises(ValueError):
            SpreadModel(bias=0.0)
        with pytest.raises(ValueError):
            SpreadModel(bias=0.0, sigma=1.0, k=1.0)


class TestRemainingMax:
    def test_observed_part_uses_undercount(self):
        # reading 68.6 rounds to 69; DST undercount counts -1:3, 0:324, 1:489, 2:100, 3:13 (929 days)
        p = observed_part(68.6, {-1: 3, 0: 324, 1: 489, 2: 100, 3: 13})
        assert p[70] == pytest.approx(489 / 929) and p[68] == pytest.approx(3 / 929)

    def test_max_of_hand_computed(self):
        # M: 70 or 71 (0.5 each); F: 69, 70, 71, 72 (0.25 each)
        # P(H<=69)=0, P(H<=70)=0.5*0.5=0.25, P(H<=71)=1*0.75=0.75, P(H<=72)=1
        d = max_of([{70: 0.5, 71: 0.5}, {69: 0.25, 70: 0.25, 71: 0.25, 72: 0.25}])
        assert d.probs == pytest.approx({70: 0.25, 71: 0.5, 72: 0.25})

    def test_no_hours_left_means_observed_only(self):
        m = RemainingMaxModel(bias=1.0, sigma=1.0)
        d = m.distribution(observed_max=75.0, lamp_max=None, dst=True)
        assert min(d.probs) == 74 and max(d.probs) == 78  # 75 + undercount -1..+3

    def test_no_readings_means_forecast_only(self):
        m = RemainingMaxModel(bias=1.0, sigma=1.0)
        assert m.distribution(None, 70.0, dst=True).mean() == pytest.approx(71.0, abs=1e-6)

    def test_warm_forecast_dominates_cool_morning(self):
        # 10:00 reading 65 but afternoon forecast 80: the observed part barely matters.
        m = RemainingMaxModel(bias=0.0, sigma=1.0)
        assert m.distribution(65.0, 80.0, dst=True).mean() == pytest.approx(80.0, abs=0.01)

    def test_fit_recovers_bias(self):
        # Forecast-only cases where the high is always forecast + 2 (spread around it by +/-1).
        cases = [(None, 70.0, 72 + (i % 3) - 1, True) for i in range(60)]
        m = RemainingMaxModel.fit(cases, biases=[0.0, 1.0, 2.0, 3.0], sigmas=[0.5, 1.0, 2.0])
        assert m.bias == pytest.approx(2.0, abs=0.25)

    def test_round_trip(self):
        m = RemainingMaxModel(bias=1.25, sigma=2.0)
        assert RemainingMaxModel.from_dict(m.to_dict()) == m


def test_alpha_shifts_forecast_by_current_error():
    m = RemainingMaxModel(bias=0.0, sigma=1.0, alpha=0.5)
    # forecast-only, LAMP running 4 degrees cold now -> mean 70 + 0.5 * 4 = 72
    assert m.distribution(None, 70.0, dst=True, error_now=4.0).mean() == pytest.approx(72.0, abs=1e-6)
    assert m.distribution(None, 70.0, dst=True).mean() == pytest.approx(70.0, abs=1e-6)
    assert RemainingMaxModel.from_dict({"bias": 0.0, "sigma": 1.0}).alpha == 0.0


def test_station_undercount_is_used_and_round_trips():
    # A station whose official high always equals the rounded max reading.
    m = RemainingMaxModel(bias=0.0, sigma=1.0, undercount_dst={0: 1}, undercount_standard={0: 1})
    assert m.distribution(75.0, None, dst=True).probs == {75: 1.0}
    assert RemainingMaxModel.from_dict(m.to_dict()) == m
    # Old parameter files without undercount fields fall back to Central Park's.
    old = RemainingMaxModel.from_dict({"bias": 0.0, "sigma": 1.0})
    assert old.undercount_dst == {-1: 3, 0: 324, 1: 489, 2: 100, 3: 13}
