"""GFS LAMP hourly temperature forecasts, as issued, from the IEM MOS archive (`model=LAV`).

LAMP is re-issued every hour with hourly temperatures for about the next
day and a half, so unlike NBM it keeps updating on the target day itself.
IEM keeps every hourly run for about a week; older days keep only the
00, 06, 12 and 18Z runs (observed 2026-10). IEM normalises LAMP runtimes
from :30 to the top of the hour.

AVAILABILITY_LAG is added to a run's runtime before it may inform a
decision, so backtests never use a forecast before it was published.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from profit_engine.venues.http import ReadOnlyHttp, VenueHttpError

BASE_URL = "https://mesonet.agron.iastate.edu"
CENTRAL_PARK = "KNYC"
MODEL = "LAV"
AVAILABILITY_LAG = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class LampRun:
    runtime: datetime
    temps: dict[datetime, float]  # hourly forecast temperature, UTC

    @property
    def available_at(self) -> datetime:
        return self.runtime + AVAILABILITY_LAG

    def max_between(self, start: datetime, end: datetime) -> float | None:
        """Highest hourly forecast in [start, end), or None if the run doesn't cover it."""
        values = [t for at, t in self.temps.items() if start <= at < end]
        return max(values) if values else None

    def covers(self, end: datetime) -> bool:
        return bool(self.temps) and max(self.temps) >= end - timedelta(hours=1)


def parse_run(data: dict, runtime: datetime) -> LampRun:
    temps = {}
    for row in data.get("data") or []:
        if row.get("tmp") is None:
            continue
        valid = datetime.strptime(row["ftime"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        temps[valid] = float(row["tmp"])
    return LampRun(runtime, temps)


class LampClient:
    def __init__(self, http: ReadOnlyHttp, station: str = CENTRAL_PARK) -> None:
        self.http = http
        self.station = station
        self._cache: dict[datetime, LampRun | None] = {}

    def run(self, runtime: datetime) -> LampRun | None:
        if runtime not in self._cache:
            try:
                data = self.http.get_json(
                    "/api/1/mos.json",
                    {"station": self.station, "model": MODEL, "runtime": runtime.strftime("%Y-%m-%dT%H:%MZ")},
                )
                run = parse_run(data, runtime)
                self._cache[runtime] = run if run.temps else None
            except VenueHttpError as exc:
                if exc.status != 404:
                    raise
                self._cache[runtime] = None
        return self._cache[runtime]

    def latest_run(
        self, at: datetime, until: datetime, lookback_hours: int = 13, sparse_before: datetime | None = None
    ) -> LampRun | None:
        """Newest run available at `at` whose hourly forecasts reach `until`.

        Tries every hour back from the newest possible runtime. Runtimes before
        `sparse_before` are only tried at 00/06/12/18Z, the runs the archive
        keeps for older days, which saves requests in backtests.
        """
        newest = (at - AVAILABILITY_LAG).replace(minute=0, second=0, microsecond=0)
        for back in range(lookback_hours + 1):
            runtime = newest - timedelta(hours=back)
            if sparse_before is not None and runtime < sparse_before and runtime.hour % 6:
                continue
            run = self.run(runtime)
            if run is not None and run.covers(until):
                return run
        return None


def current_error(run: LampRun, observations: list, at: datetime, max_age: timedelta = timedelta(minutes=90)) -> float | None:
    """Latest reading minus LAMP's forecast for that hour: how far off LAMP is running right now.

    Uses the newest observation at or before `at` (no older than `max_age`)
    and the run's forecast for the nearest hour within 30 minutes of it.
    None if either is missing.
    """
    recent = [o for o in observations if at - max_age <= o.at <= at]
    if not recent:
        return None
    latest = max(recent, key=lambda o: o.at)
    nearest = min(run.temps, key=lambda t: abs(t - latest.at), default=None)
    if nearest is None or abs(nearest - latest.at) > timedelta(minutes=30):
        return None
    return float(latest.tmpf) - run.temps[nearest]
