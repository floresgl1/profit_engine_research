"""Surface observations from the Iowa Environmental Mesonet (IEM) ASOS archive.

These are METAR reports: routine ones near :51 past each hour plus specials
when conditions change. They are samples, so the true daily high usually
falls between them; see the undercount calibration in research/.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from profit_engine.venues.http import ReadOnlyHttp

BASE_URL = "https://mesonet.agron.iastate.edu"
CENTRAL_PARK = "NYC"
SPIKE = Decimal(5)  # a reading this far above both neighbours is treated as bad data


@dataclass(frozen=True, slots=True)
class Observation:
    at: datetime  # UTC
    tmpf: Decimal  # °F


def parse_csv(text: str) -> list[Observation]:
    """IEM `onlycomma` CSV with columns station,valid,tmpf (valid in UTC). Missing values ('M') are dropped."""
    observations = []
    for row in csv.DictReader(io.StringIO(text)):
        if row["tmpf"] in ("M", ""):
            continue
        at = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        observations.append(Observation(at, Decimal(row["tmpf"])))
    observations.sort(key=lambda o: o.at)
    return observations


class IemAsosClient:
    def __init__(self, http: ReadOnlyHttp) -> None:
        self.http = http

    def temperatures(self, station: str, start: date, end: date) -> list[Observation]:
        """Routine and special reports from `start` 00:00 UTC up to (not including) `end` 00:00 UTC."""
        text = self.http.get_text(
            "/cgi-bin/request/asos.py",
            {
                "station": station,
                "data": "tmpf",
                "year1": str(start.year),
                "month1": str(start.month),
                "day1": str(start.day),
                "year2": str(end.year),
                "month2": str(end.month),
                "day2": str(end.day),
                "tz": "Etc/UTC",
                "format": "onlycomma",
                "latlon": "no",
                "missing": "M",
                "report_type": ["3", "4"],  # routine + special
            },
        )
        return parse_csv(text)


def drop_spikes(observations: Sequence[Observation]) -> list[Observation]:
    """Remove isolated upward spikes: SPIKE above both neighbours, each within an hour."""
    obs = sorted(observations, key=lambda o: o.at)
    kept = []
    for i, o in enumerate(obs):
        if 0 < i < len(obs) - 1:
            before, after = obs[i - 1], obs[i + 1]
            close = o.at - before.at <= timedelta(hours=1) and after.at - o.at <= timedelta(hours=1)
            if close and o.tmpf - before.tmpf >= SPIKE and o.tmpf - after.tmpf >= SPIKE:
                continue
        kept.append(o)
    return kept
