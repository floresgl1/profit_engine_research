"""Daily climate data from ACIS (Applied Climate Information System, NOAA Regional Climate Centers).

`StnData` returns the NWS daily climate values for a station, e.g. Central
Park's daily maximum as published in the NWS climate report (sid `NYCthr`,
a "threaded" record that stitches the station's history together).
"""

from __future__ import annotations

import re
from datetime import date

from profit_engine.venues.http import ReadOnlyHttp

BASE_URL = "https://data.rcc-acis.org"
CENTRAL_PARK = "NYCthr"

_INT = re.compile(r"^-?\d+$")


def parse_value(raw: str) -> int | None:
    """ACIS daily value: an integer, or 'M' for missing. Anything else is unexpected."""
    if raw == "M":
        return None
    if _INT.match(raw):
        return int(raw)
    raise ValueError(f"unexpected ACIS value {raw!r}")


class AcisClient:
    def __init__(self, http: ReadOnlyHttp) -> None:
        self.http = http

    def daily_max(self, sid: str, start: date, end: date) -> dict[date, int | None]:
        """Daily maximum temperature (whole °F) for each day in [start, end], inclusive."""
        data = self.http.get_json(
            "/StnData",
            {"sid": sid, "sdate": start.isoformat(), "edate": end.isoformat(), "elems": "maxt", "output": "json"},
        )
        if "error" in data:
            raise ValueError(f"ACIS error: {data['error']}")
        return {date.fromisoformat(day): parse_value(value) for day, value in data["data"]}
