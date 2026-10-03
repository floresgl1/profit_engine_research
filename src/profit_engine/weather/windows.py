"""Which hours count toward a "day" for a daily temperature market.

Two candidate conventions:
- Local standard time (LST): midnight to midnight on the standard-time clock
  all year. NWS climate reports use this, so during daylight saving time
  the "day" runs 01:00 to 01:00 local clock time.
- Clock time: midnight to midnight on the local wall clock.

Outside daylight saving time the two are identical.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

Window = tuple[datetime, datetime]  # [start, end) in UTC


def standard_offset(tz: ZoneInfo, year: int) -> timedelta:
    """The zone's standard-time UTC offset (its smaller offset in `year`)."""
    jan = datetime(year, 1, 15, 12, tzinfo=tz).utcoffset()
    jul = datetime(year, 7, 15, 12, tzinfo=tz).utcoffset()
    return min(jan, jul)


def lst_window(day: date, tz: ZoneInfo) -> Window:
    lst = timezone(standard_offset(tz, day.year))
    start = datetime.combine(day, time(0), tzinfo=lst)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def clock_window(day: date, tz: ZoneInfo) -> Window:
    start = datetime.combine(day, time(0), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def is_dst(day: date, tz: ZoneInfo) -> bool:
    noon = datetime.combine(day, time(12), tzinfo=tz)
    return noon.utcoffset() != standard_offset(tz, day.year)


def is_transition(day: date, tz: ZoneInfo) -> bool:
    """True on the days clocks change (23- or 25-hour days)."""
    start, end = clock_window(day, tz)
    return end - start != timedelta(days=1)
