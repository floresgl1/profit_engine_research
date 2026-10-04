from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

from cities_backtest import Obs, decision_time, undercount

from profit_engine.weather.iem import Observation
from profit_engine.weather.stations import CITIES

CHI = CITIES["KXHIGHCHI"]  # Central time: CDT = UTC-5 in summer, CST = UTC-6 in winter


def test_decision_time_uses_city_zone():
    assert decision_time(date(2026, 7, 15), "D 14:00", CHI.zone) == datetime(2026, 7, 15, 19, tzinfo=timezone.utc)
    assert decision_time(date(2026, 1, 15), "D 14:00", CHI.zone) == datetime(2026, 1, 15, 20, tzinfo=timezone.utc)


def hourly(day, temps_by_local_hour, zone):
    out = []
    for h in range(0, 26):
        local_dt = datetime.combine(day, datetime.min.time(), tzinfo=zone) + timedelta(hours=h, minutes=51)
        out.append(Observation(local_dt.astimezone(timezone.utc), D(temps_by_local_hour(h))))
    return out


def test_undercount_counts_only_window_insensitive_days():
    calm = date(2026, 7, 15)  # DST: midnight hours 60, afternoon peak 80 -> calm
    warm_midnight = date(2026, 7, 20)  # midnight 79 vs peak 80 -> excluded (not adjacent to calm)
    winter = date(2026, 1, 15)  # standard time: always usable
    obs = hourly(calm, lambda h: 80 if h == 15 else 60, CHI.zone)
    obs += hourly(warm_midnight, lambda h: 80 if h == 15 else 79, CHI.zone)
    obs += hourly(winter, lambda h: 40 if h == 14 else 30, CHI.zone)
    highs = {calm: 81, warm_midnight: 80, winter: 40}
    dst_u, std_u = undercount(CHI, Obs(obs), highs, [calm, warm_midnight, winter])
    # calm: official 81 - reading 80 = +1; winter: 40 - 40 = 0
    assert dst_u == {1: 1} and std_u == {0: 1}
