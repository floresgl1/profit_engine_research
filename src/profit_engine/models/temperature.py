"""Probability distribution over a day's integer high temperature.

Every bucket market in an event is a sum over one distribution, so the
bucket probabilities always add up to 1.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class ContainsValue(Protocol):
    def contains(self, value: Decimal) -> bool: ...


def normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@dataclass(frozen=True)
class HighDistribution:
    """P(H = h) for integer highs h. Probabilities sum to 1."""

    probs: dict[int, float]

    def __post_init__(self) -> None:
        total = sum(self.probs.values())
        if not self.probs or any(p < 0 for p in self.probs.values()) or abs(total - 1) > 1e-9:
            raise ValueError(f"not a probability distribution (total {total})")

    @classmethod
    def rounded_normal(cls, mu: float, sigma: float, tails: float = 8.0) -> HighDistribution:
        """H = round(X) with X ~ Normal(mu, sigma): P(H = h) = P(h - 0.5 < X <= h + 0.5)."""
        if sigma <= 0:
            raise ValueError("sigma must be positive")
        lo, hi = math.floor(mu - tails * sigma), math.ceil(mu + tails * sigma)
        raw = {h: normal_cdf((h + 0.5 - mu) / sigma) - normal_cdf((h - 0.5 - mu) / sigma) for h in range(lo, hi + 1)}
        total = sum(raw.values())  # just under 1: the far tails beyond `tails` sigma are dropped
        return cls({h: p / total for h, p in raw.items() if p > 0})

    def at_least(self, floor: int) -> HighDistribution:
        """Condition on H >= floor: drop lower values and rescale the rest (Bayes with a hard cut).

        If the distribution had no mass at or above the floor, the observation
        wins: all probability goes to the floor itself.
        """
        kept = {h: p for h, p in self.probs.items() if h >= floor}
        total = sum(kept.values())
        if total <= 0:
            return HighDistribution({floor: 1.0})
        return HighDistribution({h: p / total for h, p in kept.items()})

    def probability(self, bucket: ContainsValue) -> float:
        return sum(p for h, p in self.probs.items() if bucket.contains(Decimal(h)))

    def mean(self) -> float:
        return sum(h * p for h, p in self.probs.items())


def floor_from_observations(max_observed: float, low_band: float) -> int:
    """Lowest official high still possible given the max (spike-checked) observation so far.

    The official high can sit up to |low_band| below the max observation
    (calibrated: -0.8°F on DST days). Rounded DOWN on purpose: a floor one
    degree too low costs a little probability, one too high zeroes a bucket
    that can still win.
    """
    return math.floor(max_observed + low_band)


def bucket_probabilities(dist: HighDistribution, buckets: Iterable[ContainsValue]) -> list[float]:
    return [dist.probability(b) for b in buckets]
