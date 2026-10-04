"""Weather data for temperature markets: NWS climate values (ACIS), hourly
observations (IEM ASOS), Kalshi settlement values, and day-window definitions."""

from profit_engine.weather.acis import AcisClient
from profit_engine.weather.iem import IemAsosClient, Observation
from profit_engine.weather.kalshi_temps import SettledTemperature, settled_temperatures
from profit_engine.weather.windows import (
    clock_window,
    is_dst,
    is_transition,
    lst_window,
)

__all__ = [
    "AcisClient",
    "IemAsosClient",
    "Observation",
    "SettledTemperature",
    "clock_window",
    "is_dst",
    "is_transition",
    "lst_window",
    "settled_temperatures",
]
