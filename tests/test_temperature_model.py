"""Distribution math. Expected values computed by hand (normal CDF table values in comments)."""

from decimal import Decimal as D

import pytest

from profit_engine.models.temperature import HighDistribution, bucket_probabilities, floor_from_observations
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
