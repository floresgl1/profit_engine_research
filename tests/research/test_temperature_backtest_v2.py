from datetime import date, datetime, timezone

from temperature_backtest_v2 import lamp_max, window

from profit_engine.weather.lamp import LampRun

DAY = date(2026, 9, 28)  # EDT


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_window_is_rest_of_the_day():
    # Before the day: from 01:00 EDT (05Z); end: midnight EDT (04Z next day).
    assert window(DAY, utc(2026, 9, 27, 20)) == (utc(2026, 9, 28, 5), utc(2026, 9, 29, 4))
    # At 14:00 EDT the window starts now.
    assert window(DAY, utc(2026, 9, 28, 18)) == (utc(2026, 9, 28, 18), utc(2026, 9, 29, 4))


def test_lamp_max_ignores_past_and_next_day():
    temps = {utc(2026, 9, 28, 17): 90.0, utc(2026, 9, 28, 19): 80.0, utc(2026, 9, 29, 5): 95.0}
    run = LampRun(utc(2026, 9, 28, 12), temps)
    # At 14:00 EDT (18Z): 17Z is past and 05Z next day is after midnight EDT; only 19Z counts.
    assert lamp_max(run, DAY, utc(2026, 9, 28, 18)) == 80.0
    assert lamp_max(None, DAY, utc(2026, 9, 28, 18)) is None
