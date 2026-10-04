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


# --- v2 (LAMP) ---------------------------------------------------------------------------------

import json  # noqa: E402

from profit_engine.models.kalshi_temperature import (
    KalshiHighTemperatureLamp,  # noqa: E402
)
from profit_engine.models.temperature import RemainingMaxModel  # noqa: E402
from profit_engine.weather.lamp import LampRun  # noqa: E402

V2_LEADS = {name: RemainingMaxModel(bias=1.0, sigma=1.0) for name in PARAMS.lead_times}


class FakeLamp:
    def __init__(self, temps):
        self.temps = temps
        self.calls = 0

    def latest_run(self, at, until):
        self.calls += 1
        return LampRun(utc(2026, 9, 28, 12), self.temps)


def lamp_temps(afternoon=70.0, midnight=55.0):
    # hourly 05Z Sep 28 (01:00 EDT) .. 05Z Sep 29; afternoon peak at 19Z (15:00 EDT)
    temps = {utc(2026, 9, 28, h): 60.0 for h in range(5, 24)} | {utc(2026, 9, 29, h): midnight for h in range(0, 6)}
    temps[utc(2026, 9, 28, 19)] = afternoon
    temps[utc(2026, 9, 28, 4)] = midnight
    return temps


def v2(now, temps=None, obs=()):
    return KalshiHighTemperatureLamp(V2_LEADS, PARAMS.lead_times, FakeLamp(temps or lamp_temps()), FakeIem(list(obs)), now=lambda: now)


class TestLampModel:
    def test_buckets_sum_to_one(self):
        m = v2(utc(2026, 9, 28, 14))
        probs = [m.predict(market(*s), make_book()) for s in STRIKES]
        assert sum(probs) == pytest.approx(D(1), abs=D("0.001"))

    def test_observed_heat_beats_cool_forecast(self):
        # 13:51 EDT reading 72 with forecast rest-of-day max 60 (+1 bias): high comes from observations.
        # 72 + DST undercount (-1..+3) -> 71..75: all mass in 70-71 and 71+.
        temps = lamp_temps(afternoon=60.0)
        m = v2(utc(2026, 9, 28, 18, 5), temps=temps, obs=[Observation(utc(2026, 9, 28, 17, 51), D(72))])
        probs = {s[0]: m.predict(market(*s), make_book()) for s in STRIKES}
        assert probs["T71"] > D("0.95")

    def test_abstains_when_midnight_is_warm(self):
        m = v2(utc(2026, 9, 28, 14), temps=lamp_temps(afternoon=70.0, midnight=69.0))
        assert m.predict(market(*STRIKES[0]), make_book()) is None

    def test_abstains_after_the_day(self):
        assert v2(utc(2026, 9, 29, 5)).predict(market(*STRIKES[0]), make_book()) is None

    def test_from_file_requires_lamp_params(self, tmp_path):
        path = tmp_path / "p.json"
        path.write_text(json.dumps({"model": "nbm", "leads": {}, "lead_times": {}}))
        with pytest.raises(ValueError):
            KalshiHighTemperatureLamp.from_file(path, FakeLamp({}), FakeIem([]))


def test_v3_params_get_their_own_name(tmp_path):
    temps = lamp_temps(afternoon=60.0)
    obs = [Observation(utc(2026, 9, 28, 17, 51), D(64))]
    leads = {name: RemainingMaxModel(bias=0.0, sigma=1.0, alpha=0.5) for name in PARAMS.lead_times}
    path = tmp_path / "v3.json"
    path.write_text(json.dumps({"model": "lamp", "version": 3, "leads": {k: v.to_dict() for k, v in leads.items()},
                                "lead_times": {k: list(v) for k, v in PARAMS.lead_times.items()}}))
    assert KalshiHighTemperatureLamp.from_file(path, FakeLamp(temps), FakeIem(obs)).name == "kxhigh_lamp_v3"


def test_current_error_shifts_mean_by_alpha():
    temps = lamp_temps(afternoon=60.0)
    obs = [Observation(utc(2026, 9, 28, 17, 51), D(64))]
    now = utc(2026, 9, 28, 18, 5)
    zero = {n: RemainingMaxModel(bias=0.0, sigma=1.0, alpha=0.0) for n in PARAMS.lead_times}
    half = {n: RemainingMaxModel(bias=0.0, sigma=1.0, alpha=0.5) for n in PARAMS.lead_times}
    # remaining LAMP max after 18:05Z is 60 (afternoon hour 19Z); error now = 64 - 60 (18Z) = 4
    d0 = KalshiHighTemperatureLamp(zero, PARAMS.lead_times, FakeLamp(temps), FakeIem(obs), now=lambda: now)._build(DAY, now)
    d5 = KalshiHighTemperatureLamp(half, PARAMS.lead_times, FakeLamp(temps), FakeIem(obs), now=lambda: now)._build(DAY, now)
    # forecast part centred at 60 vs 62; both maxed with the observed part (64 + undercount)
    assert d5.mean() > d0.mean()


def test_lamp_model_rejects_wrong_station(tmp_path):
    # Regression: the CLI once gave every city's model Central Park's LAMP client.
    from profit_engine.venues.http import ReadOnlyHttp
    from profit_engine.weather.lamp import LampClient
    from profit_engine.weather.stations import CITIES

    nyc_client = LampClient(ReadOnlyHttp("https://iem.test"), station="KNYC")
    with pytest.raises(ValueError, match="KMIA"):
        KalshiHighTemperatureLamp(V2_LEADS, PARAMS.lead_times, nyc_client, FakeIem([]), city=CITIES["KXHIGHMIA"])


def test_cli_builds_city_specific_clients(tmp_path, monkeypatch):
    from profit_engine import cli
    from profit_engine.models.kalshi_temperature import KalshiHighTemperatureLamp as Lamp

    built = []
    real_from_file = Lamp.from_file.__func__

    def spy(cls, path, lamp, iem, **kwargs):
        built.append(lamp.station)
        raise SystemExit(0)  # stop before the ingest loop starts

    monkeypatch.setattr(Lamp, "from_file", classmethod(spy))
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "x.db"), "ingest", "--kalshi-series", "KXHIGHMIA",
                  "--temperature-params", "research/params/KXHIGHMIA.json", "--cycles", "1"])
    assert built == ["KMIA"]
    assert real_from_file is not None
