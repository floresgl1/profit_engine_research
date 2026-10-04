from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from temperature_backtest import Case, build_cases, decision_time, evaluate, observed_max

from profit_engine.models.temperature import SpreadModel
from profit_engine.weather.iem import Observation
from profit_engine.weather.kalshi_temps import Bucket, Quote, SettledTemperature
from profit_engine.weather.nbm import MaxForecast, NbmRun

DAY = date(2026, 9, 28)  # EDT, UTC-4


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_decision_times_are_local():
    assert decision_time(DAY, "D-1 16:00") == utc(2026, 9, 27, 20)
    assert decision_time(DAY, "D 10:00") == utc(2026, 9, 28, 14)
    assert decision_time(DAY, "D 14:00") == utc(2026, 9, 28, 18)


def test_observed_max_only_uses_the_past():
    obs = [
        Observation(utc(2026, 9, 28, 4, 51), D(80)),  # 00:51 EDT: before 01:00, excluded
        Observation(utc(2026, 9, 28, 13, 51), D(66)),
        Observation(utc(2026, 9, 28, 17, 51), D(70)),  # 13:51 EDT
        Observation(utc(2026, 9, 28, 18, 51), D(72)),  # 14:51 EDT: after a 14:00 decision
    ]
    assert observed_max(obs, DAY, decision_time(DAY, "D 14:00")) == 70.0
    assert observed_max(obs, DAY, decision_time(DAY, "D 10:00")) == 66.0
    assert observed_max(obs, DAY, decision_time(DAY, "D-1 16:00")) is None


def test_build_cases_uses_run_available_at_decision_time():
    run = NbmRun(utc(2026, 9, 27, 12), {DAY: MaxForecast(DAY, utc(2026, 9, 27, 12), 70.0, 2.0)})
    seen = []

    def latest(at, day):
        seen.append(at)
        return run

    event = SettledTemperature("KXHIGHNY-26SEP28", DAY, D(71), "twc", (Bucket("B70.5", "between", D(70), D(71)),))
    cases = build_cases([DAY], ["D-1 16:00"], latest, [], {DAY: 71}, {DAY: event})
    assert seen == [utc(2026, 9, 27, 20)]
    assert (cases[0].txn, cases[0].xnd, cases[0].outcome, len(cases[0].buckets)) == (70.0, 2.0, 71, 1)


def test_evaluate_scores_same_rows():
    bucket = Bucket("B70.5", "between", D(70), D(71))
    case = Case(DAY, "D 10:00", decision_time(DAY, "D 10:00"), 70.5, None, None, 71, (bucket,))
    model = SpreadModel(bias=0.0, sigma=1e-3)  # certain: H = 70 or 71 (70.5 rounds both ways)
    quotes = {
        "B70.5": [
            Quote(case.at - timedelta(hours=1), D("0.40"), D("0.50")),  # used: mid 0.45
            Quote(case.at + timedelta(hours=1), D("0.90"), D("0.95")),  # after T: must be ignored
        ]
    }
    s = evaluate([case], lambda lead: model, quotes)
    # outcome 71 is in the bucket: model p = 1 -> Brier 0; market 0.45 -> (0.45 - 1)^2 = 0.3025
    assert s.n_rows == 1
    assert s.model_brier == pytest.approx(0.0, abs=1e-9)
    assert s.market_brier == pytest.approx(0.3025)
