from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from factories import make_book, make_market

from profit_engine.models.kalshi_temperature import (
    KalshiHighTemperature,
    TemperatureParams,
    bucket_of,
    midnight_risk,
)
from profit_engine.models.temperature import SpreadModel
from profit_engine.weather.iem import Observation
from profit_engine.weather.nbm import MaxForecast, NbmRun

DAY = date(2026, 9, 28)  # EDT


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


PARAMS = TemperatureParams(
    variant="fixed",
    leads={
        "D-1 16:00": SpreadModel(bias=0.0, sigma=2.0),
        "D 10:00": SpreadModel(bias=0.0, sigma=1.5),
        "D 14:00": SpreadModel(bias=0.0, sigma=1.0),
    },
    lead_times={"D-1 16:00": (-1, 16), "D 10:00": (0, 10), "D 14:00": (0, 14)},
)

STRIKES = [
    ("T64", "less", "", "64"),
    ("B64.5", "between", "64", "65"),
    ("B66.5", "between", "66", "67"),
    ("B68.5", "between", "68", "69"),
    ("B70.5", "between", "70", "71"),
    ("T71", "greater", "71", ""),
]


def market(suffix, strike_type, floor, cap, series="KXHIGHNY"):
    return make_market(
        market_id=f"KXHIGHNY-26SEP28-{suffix}",
        venue_meta={
            "event_ticker": "KXHIGHNY-26SEP28",
            "series_ticker": series,
            "strike_type": strike_type,
            "floor_strike": floor,
            "cap_strike": cap,
        },
    )


def run(txn=68.0, midnight_temp=55.0):
    temps = {utc(2026, 9, 28, 3): midnight_temp, utc(2026, 9, 28, 18): txn - 1, utc(2026, 9, 29, 3): midnight_temp}
    return NbmRun(utc(2026, 9, 28, 6), {DAY: MaxForecast(DAY, utc(2026, 9, 28, 6), txn, 2.0)}, temps)


class FakeNbm:
    def __init__(self, nbm_run):
        self.nbm_run = nbm_run
        self.calls = 0

    def latest_run(self, at, target=None):
        self.calls += 1
        return self.nbm_run


class FakeIem:
    def __init__(self, obs):
        self.obs = obs

    def temperatures(self, station, start, end):
        return self.obs


def model(now, nbm_run=None, obs=()):
    return KalshiHighTemperature(PARAMS, FakeNbm(nbm_run or run()), FakeIem(list(obs)), now=lambda: now)


class TestHelpers:
    def test_bucket_of(self):
        b = bucket_of(market("B70.5", "between", "70", "71"))
        assert (b.strike_type, b.floor, b.cap) == ("between", D(70), D(71))

    def test_lead_for(self):
        ny = timezone(timedelta(hours=-4))
        assert PARAMS.lead_for(DAY, datetime(2026, 9, 27, 9, tzinfo=ny)) == "D-1 16:00"  # before any lead
        assert PARAMS.lead_for(DAY, datetime(2026, 9, 27, 18, tzinfo=ny)) == "D-1 16:00"
        assert PARAMS.lead_for(DAY, datetime(2026, 9, 28, 11, tzinfo=ny)) == "D 10:00"
        assert PARAMS.lead_for(DAY, datetime(2026, 9, 28, 15, tzinfo=ny)) == "D 14:00"

    def test_midnight_risk(self):
        assert not midnight_risk(run(midnight_temp=60.0), DAY, center=68.0)
        assert midnight_risk(run(midnight_temp=66.5), DAY, center=68.0)  # within 2°F at 23:00 EDT


class TestPredict:
    def test_event_buckets_sum_to_one(self):
        m = model(utc(2026, 9, 28, 14))  # 10:00 EDT
        probs = [m.predict(market(*s), make_book()) for s in STRIKES]
        assert all(p is not None for p in probs)
        assert sum(probs) == pytest.approx(D(1), abs=D("0.001"))

    def test_observation_floor_kills_low_buckets(self):
        # 13:51 EDT reading of 71: floor(71 - 0.8) = 70 -> only 70-71 and 71+ survive.
        obs = [Observation(utc(2026, 9, 28, 17, 51), D(71))]
        m = model(utc(2026, 9, 28, 18, 5), obs=obs)  # 14:05 EDT
        probs = {s[0]: m.predict(market(*s), make_book()) for s in STRIKES}
        assert probs["T64"] == probs["B64.5"] == probs["B66.5"] == probs["B68.5"] == 0
        assert probs["B70.5"] + probs["T71"] == pytest.approx(D(1), abs=D("0.001"))

    def test_abstains_on_midnight_risk(self):
        m = model(utc(2026, 9, 28, 14), nbm_run=run(midnight_temp=67.0))
        assert m.predict(market(*STRIKES[0]), make_book()) is None

    def test_abstains_without_forecast(self):
        empty = NbmRun(utc(2026, 9, 28, 6), {}, {})
        assert model(utc(2026, 9, 28, 14), nbm_run=empty).predict(market(*STRIKES[0]), make_book()) is None

    def test_ignores_other_series_and_venues(self):
        m = model(utc(2026, 9, 28, 14))
        assert m.predict(market("T64", "less", "", "64", series="KXHIGHCHI"), make_book()) is None
        assert m.predict(make_market(venue="polymarket", market_id="0xa"), make_book(venue="polymarket", market_id="0xa")) is None

    def test_one_forecast_fetch_per_refresh(self):
        m = model(utc(2026, 9, 28, 14))
        for s in STRIKES:
            m.predict(market(*s), make_book())
        assert m.nbm.calls == 1
