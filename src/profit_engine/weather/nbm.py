"""National Blend of Models (NBM) station forecasts, as issued, from the IEM MOS archive.

`GET /api/1/mos.json?station=KNYC&model=NBS&runtime=...` returns one run's
text-product forecast. Rows with `txn` set carry the max/min forecast:
the row valid at 00Z holds the daytime max for the previous local day,
and `xnd` is NBM's own standard deviation for it.

Archive retention (observed 2026-10, docs say only the 1/7/13/19Z runs):
older days keep runs at 01, 07, 13 and 19Z; recent days at 00, 06, 12 and
18Z. `latest_run` tries both.

A run is not usable the moment it starts: AVAILABILITY_LAG is added to its
runtime before it may inform a decision, so backtests can't use a forecast
before it was published.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from profit_engine.venues.http import ReadOnlyHttp, VenueHttpError

BASE_URL = "https://mesonet.agron.iastate.edu"
CENTRAL_PARK = "KNYC"
MODEL = "NBS"
AVAILABILITY_LAG = timedelta(hours=2)
_RUN_HOURS = (0, 1, 6, 7, 12, 13, 18, 19)


@dataclass(frozen=True, slots=True)
class MaxForecast:
    target: date  # local day the max is for
    runtime: datetime  # UTC
    txn: float  # forecast max, °F
    xnd: float | None  # NBM standard deviation, °F


@dataclass(frozen=True, slots=True)
class NbmRun:
    runtime: datetime
    maxima: dict[date, MaxForecast]
    temps: dict[datetime, float] = field(default_factory=dict)  # 3-hourly forecast temperature, UTC

    @property
    def available_at(self) -> datetime:
        return self.runtime + AVAILABILITY_LAG


def parse_run(data: dict, runtime: datetime) -> NbmRun:
    maxima = {}
    temps = {}
    for row in data.get("data") or []:
        valid = datetime.strptime(row["ftime"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        if row.get("tmp") is not None:
            temps[valid] = float(row["tmp"])
        if row.get("txn") is None:
            continue
        if valid.hour != 0:
            continue  # 12Z rows are overnight minima
        target = (valid - timedelta(days=1)).date()
        xnd = row.get("xnd")
        maxima[target] = MaxForecast(target, runtime, float(row["txn"]), None if xnd is None else float(xnd))
    return NbmRun(runtime, maxima, temps)


class NbmClient:
    def __init__(self, http: ReadOnlyHttp, station: str = CENTRAL_PARK) -> None:
        self.http = http
        self.station = station
        self._cache: dict[datetime, NbmRun | None] = {}

    def run(self, runtime: datetime) -> NbmRun | None:
        """One archived run, or None if the archive doesn't have it."""
        if runtime not in self._cache:
            try:
                data = self.http.get_json(
                    "/api/1/mos.json",
                    {"station": self.station, "model": MODEL, "runtime": runtime.strftime("%Y-%m-%dT%H:%MZ")},
                )
                run = parse_run(data, runtime)
                self._cache[runtime] = run if run.maxima else None
            except VenueHttpError as exc:
                if exc.status != 404:
                    raise
                self._cache[runtime] = None
        return self._cache[runtime]

    def latest_run(self, at: datetime, lookback: timedelta = timedelta(hours=30)) -> NbmRun | None:
        """Most recent archived run that was available (runtime + lag) at `at`."""
        newest = at - AVAILABILITY_LAG
        day = newest.date()
        candidates = []
        while datetime.combine(day, time(23), tzinfo=timezone.utc) >= newest - lookback:
            for hour in _RUN_HOURS:
                runtime = datetime.combine(day, time(hour), tzinfo=timezone.utc)
                if newest - lookback <= runtime <= newest:
                    candidates.append(runtime)
            day -= timedelta(days=1)
        for runtime in sorted(candidates, reverse=True):
            run = self.run(runtime)
            if run is not None:
                return run
        return None
