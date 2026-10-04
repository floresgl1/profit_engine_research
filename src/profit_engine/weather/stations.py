"""Kalshi daily-high series and the station each settles on.

Station per series comes from the market rules ("maximum temperature
recorded at Chicago (CLIMDW)"): the NWS climate report site, which is an
airport ASOS except NYC's Central Park. Identifiers verified 2026-10-04
against ACIS, IEM ASOS and IEM's LAMP archive.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class City:
    series: str  # Kalshi series ticker
    name: str
    tz: str  # IANA time zone of the station
    climate_report: str  # NWS climate report id named in the rules
    icao: str  # LAMP / MOS station
    iem: str  # IEM ASOS station
    acis: str  # ACIS station id

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


CITIES: dict[str, City] = {
    c.series: c
    for c in (
        City("KXHIGHNY", "New York (Central Park)", "America/New_York", "CLINYC", "KNYC", "NYC", "NYCthr"),
        City("KXHIGHCHI", "Chicago (Midway)", "America/Chicago", "CLIMDW", "KMDW", "MDW", "KMDW"),
        City("KXHIGHAUS", "Austin (Bergstrom)", "America/Chicago", "CLIAUS", "KAUS", "AUS", "KAUS"),
        City("KXHIGHMIA", "Miami", "America/New_York", "CLIMIA", "KMIA", "MIA", "KMIA"),
        City("KXHIGHLAX", "Los Angeles (LAX)", "America/Los_Angeles", "CLILAX", "KLAX", "LAX", "KLAX"),
        City("KXHIGHDEN", "Denver (DIA)", "America/Denver", "CLIDEN", "KDEN", "DEN", "KDEN"),
        City("KXHIGHPHIL", "Philadelphia", "America/New_York", "CLIPHL", "KPHL", "PHL", "KPHL"),
    )
}
