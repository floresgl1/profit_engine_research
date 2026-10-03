"""Brier scores and calibration tables.

Brier score = mean of (p - o)^2 over predictions, where p is a predicted
P(YES) and o the outcome (1, 0, or a fraction for 50/50 and scalar
resolutions). Lower is better; always predicting 0.5 scores 0.25 on 0/1
outcomes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from profit_engine.storage import ScoredRow

Pair = tuple[Decimal, Decimal]  # (predicted probability, outcome)


def brier(pairs: Iterable[Pair]) -> Decimal:
    pairs = list(pairs)
    if not pairs:
        raise ValueError("Brier score of zero predictions is undefined")
    return sum(((p - o) ** 2 for p, o in pairs), Decimal(0)) / len(pairs)


class Selection(str, Enum):
    ALL = "all"  # every logged prediction counts; markets polled more often weigh more
    LAST = "last"  # only each market's latest prediction


@dataclass(frozen=True, slots=True)
class Comparison:
    model: str
    selection: Selection
    n_predictions: int  # scored on both sides
    n_markets: int
    n_excluded: int  # predictions dropped because the market had no two-sided price
    model_brier: Decimal
    market_brier: Decimal

    @property
    def skill(self) -> Decimal | None:
        """Brier skill vs the market: 1 - model/market. Positive means the model beat the market."""
        if self.market_brier == 0:
            return None
        return 1 - self.model_brier / self.market_brier


def select(rows: Sequence[ScoredRow], selection: Selection) -> list[ScoredRow]:
    if selection is Selection.ALL:
        return list(rows)
    latest: dict[tuple[str, str], ScoredRow] = {}
    for row in rows:
        key = (row.prediction.venue, row.prediction.market_id)
        if key not in latest or row.prediction.predicted_at >= latest[key].prediction.predicted_at:
            latest[key] = row
    return list(latest.values())


def compare(rows: Sequence[ScoredRow], model: str, selection: Selection = Selection.ALL) -> Comparison:
    """Score `model` and the market on exactly the same predictions."""
    own = [r for r in rows if r.prediction.model == model]
    chosen = select(own, selection)
    both = [r for r in chosen if r.prediction.market_probability is not None]
    if not both:
        raise ValueError(f"no resolved predictions with a market price for model {model!r}")
    return Comparison(
        model=model,
        selection=selection,
        n_predictions=len(both),
        n_markets=len({(r.prediction.venue, r.prediction.market_id) for r in both}),
        n_excluded=len(chosen) - len(both),
        model_brier=brier((r.prediction.probability, r.outcome) for r in both),
        market_brier=brier((r.prediction.market_probability, r.outcome) for r in both),
    )


@dataclass(frozen=True, slots=True)
class CalibrationBucket:
    lower: Decimal
    upper: Decimal
    count: int
    mean_predicted: Decimal | None
    hit_rate: Decimal | None  # mean outcome in the bucket


def calibration_table(pairs: Iterable[Pair], n_buckets: int = 10) -> list[CalibrationBucket]:
    """Equal-width buckets on [0, 1]; each is [lower, upper) except the last, which includes 1."""
    if n_buckets < 1:
        raise ValueError("n_buckets must be at least 1")
    width = Decimal(1) / n_buckets
    grouped: list[list[Pair]] = [[] for _ in range(n_buckets)]
    for p, o in pairs:
        if not Decimal(0) <= p <= Decimal(1):
            raise ValueError(f"probability {p} outside [0, 1]")
        grouped[min(int(p * n_buckets), n_buckets - 1)].append((p, o))
    table = []
    for i, members in enumerate(grouped):
        n = len(members)
        table.append(
            CalibrationBucket(
                lower=width * i,
                upper=width * (i + 1),
                count=n,
                mean_predicted=sum((p for p, _ in members), Decimal(0)) / n if n else None,
                hit_rate=sum((o for _, o in members), Decimal(0)) / n if n else None,
            )
        )
    return table


def model_pairs(rows: Iterable[ScoredRow]) -> list[Pair]:
    return [(r.prediction.probability, r.outcome) for r in rows]


def market_pairs(rows: Iterable[ScoredRow]) -> list[Pair]:
    return [(r.prediction.market_probability, r.outcome) for r in rows if r.prediction.market_probability is not None]
