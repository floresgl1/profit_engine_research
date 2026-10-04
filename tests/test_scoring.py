"""Scoring tests. Expected values are hand-computed in the comments."""

from datetime import timedelta
from decimal import Decimal as D

import pytest
from factories import T0

from profit_engine.core import Prediction
from profit_engine.scoring import (
    Selection,
    brier,
    calibration_table,
    compare,
    full_report,
)
from profit_engine.storage import ScoredRow


def row(market, model_p, market_p, outcome, minutes=0, model="m"):
    p = Prediction(
        venue="kalshi",
        market_id=market,
        model=model,
        predicted_at=T0 + timedelta(minutes=minutes),
        probability=D(model_p),
        market_probability=None if market_p is None else D(market_p),
        best_bid=None,
        best_ask=None,
        book_received_at=T0,
    )
    return ScoredRow(p, D(outcome))


class TestBrier:
    def test_hand_computed(self):
        # (0.7-1)^2 + (0.2-0)^2 + (0.5-0.5)^2 = 0.09 + 0.04 + 0 = 0.13 -> / 3
        assert brier([(D("0.7"), D("1")), (D("0.2"), D("0")), (D("0.5"), D("0.5"))]) == D("0.13") / 3

    def test_perfect_and_worst(self):
        assert brier([(D("1"), D("1")), (D("0"), D("0"))]) == 0
        assert brier([(D("0"), D("1"))]) == 1

    def test_coin_flip_is_quarter(self):
        assert brier([(D("0.5"), D("1")), (D("0.5"), D("0"))]) == D("0.25")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            brier([])


# Market A resolves YES, B resolves NO.
#   A t0: model 0.8 market 0.6   -> model 0.04, market 0.16
#   A t1: model 0.9 market 0.7   -> model 0.01, market 0.09
#   B t0: model 0.3 market 0.2   -> model 0.09, market 0.04
ROWS = [row("A", "0.8", "0.6", "1", 0), row("A", "0.9", "0.7", "1", 1), row("B", "0.3", "0.2", "0", 0)]


class TestCompare:
    def test_all_predictions(self):
        c = compare(ROWS, "m", Selection.ALL)
        assert c.model_brier == D("0.14") / 3
        assert c.market_brier == D("0.29") / 3
        assert (c.n_predictions, c.n_markets, c.n_excluded) == (3, 2, 0)

    def test_last_per_market(self):
        # A last: model 0.01, market 0.09; B: model 0.09, market 0.04
        c = compare(ROWS, "m", Selection.LAST)
        assert c.model_brier == D("0.05")
        assert c.market_brier == D("0.065")
        # skill = 1 - 0.05 / 0.065 = 3/13
        assert c.skill == 1 - D("0.05") / D("0.065")
        assert c.skill > 0

    def test_same_events_only(self):
        # C had no two-sided book: excluded from BOTH scores, not just the market's.
        rows = ROWS + [row("C", "0.99", None, "0", 0)]
        c = compare(rows, "m", Selection.ALL)
        assert c.n_excluded == 1
        assert c.model_brier == D("0.14") / 3

    def test_other_models_ignored(self):
        rows = ROWS + [row("A", "0.1", "0.6", "1", 0, model="other")]
        assert compare(rows, "m").n_predictions == 3

    def test_midpoint_model_ties_market(self):
        rows = [row("A", "0.6", "0.6", "1"), row("B", "0.2", "0.2", "0")]
        c = compare(rows, "m")
        assert c.model_brier == c.market_brier
        assert c.skill == 0

    def test_fifty_fifty_outcome(self):
        # (0.8 - 0.5)^2 = 0.09; (0.5 - 0.5)^2 = 0
        c = compare([row("A", "0.8", "0.5", "0.5")], "m")
        assert (c.model_brier, c.market_brier) == (D("0.09"), D("0"))
        assert c.skill is None  # market was perfect; skill undefined

    def test_no_rows_raises(self):
        with pytest.raises(ValueError):
            compare([], "m")


class TestCalibration:
    def test_hand_computed(self):
        # bucket 0: 0.05 (0), 0.08 (1)  -> n 2, mean 0.065, hit 0.5
        # bucket 1: 0.15 (0)            -> n 1, mean 0.15,  hit 0
        # bucket 9: 0.95 (1), 1.0 (1)   -> n 2, mean 0.975, hit 1
        pairs = [(D(p), D(o)) for p, o in [("0.05", 0), ("0.08", 1), ("0.15", 0), ("0.95", 1), ("1.0", 1)]]
        table = calibration_table(pairs)
        assert len(table) == 10
        assert (table[0].count, table[0].mean_predicted, table[0].hit_rate) == (2, D("0.065"), D("0.5"))
        assert (table[1].count, table[1].mean_predicted, table[1].hit_rate) == (1, D("0.15"), D("0"))
        assert (table[9].count, table[9].mean_predicted, table[9].hit_rate) == (2, D("0.975"), D("1"))
        assert table[5].count == 0 and table[5].hit_rate is None

    def test_bucket_edges(self):
        # 0.1 opens bucket 1 (lower bound inclusive); 1 lands in the last bucket.
        table = calibration_table([(D("0.1"), D(0)), (D("1"), D(1))])
        assert table[1].count == 1 and table[9].count == 1

    def test_fractional_outcomes_average(self):
        table = calibration_table([(D("0.5"), D("0.5")), (D("0.55"), D("1"))], n_buckets=2)
        assert table[1].hit_rate == D("0.75")

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            calibration_table([(D("1.1"), D(1))])


def test_full_report_renders():
    text = full_report(ROWS, ["m"])
    assert "m [all]" in text and "m [last]" in text and "calibration" in text


def test_full_report_empty():
    assert "No resolved predictions" in full_report([], ["m"])


def test_report_calibration_uses_same_rows_for_model_and_market():
    # C has no market price: it must not appear in the model's calibration table either.
    text = full_report(ROWS + [row("C", "0.55", None, "0", 5)], ["m"])
    model_table = text.split("m calibration")[1].split("market calibration")[0]
    assert "[0.5, 0.6)      0" in model_table
