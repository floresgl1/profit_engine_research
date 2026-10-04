"""Live model for Kalshi daily-high markets (KXHIGHNY): NBM forecast + today's observations.

For each event it builds one distribution over the integer high and prices
every bucket from it, so an event's six predictions always sum to 1.
Parameters come from research/temperature_backtest.py (fitted on held-out
history). It abstains when it cannot model the day honestly:
- no NBM forecast for the day in the latest available run
- the forecast puts a midnight hour within MIDNIGHT_MARGIN of the high,
  because which day window Kalshi's source uses is unknown
  (research/day_window.md)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from profit_engine.core import Market, OrderBook, utc_now
from profit_engine.models.temperature import (
    LOW_BAND_DST,
    LOW_BAND_STANDARD,
    HighDistribution,
    SpreadModel,
    predict_high,
)
from profit_engine.weather.iem import CENTRAL_PARK, IemAsosClient, drop_spikes
from profit_engine.weather.kalshi_temps import Bucket, event_day
from profit_engine.weather.nbm import NbmClient, NbmRun
from profit_engine.weather.windows import is_dst

log = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
MIDNIGHT_MARGIN = 2.0  # °F


@dataclass(frozen=True)
class TemperatureParams:
    variant: str
    leads: dict[str, SpreadModel]
    lead_times: dict[str, tuple[int, int]]  # name -> (days relative to target, local hour)

    @classmethod
    def load(cls, path: str | Path) -> TemperatureParams:
        raw = json.loads(Path(path).read_text())
        return cls(
            variant=raw["variant"],
            leads={k: SpreadModel.from_dict(v) for k, v in raw["leads"].items()},
            lead_times={k: tuple(v) for k, v in raw["lead_times"].items()},
        )

    def lead_for(self, target: date, now: datetime) -> str:
        """The latest fitted lead time already reached at `now` (the earliest one before that)."""
        reached = []
        for name, (offset, hour) in self.lead_times.items():
            at = datetime.combine(target + timedelta(days=offset), time(hour), tzinfo=NY)
            if at <= now:
                reached.append((at, name))
        if reached:
            return max(reached)[1]
        return min(self.lead_times, key=lambda n: self.lead_times[n])


def bucket_of(market: Market) -> Bucket | None:
    meta = market.venue_meta

    def dec(key: str) -> Decimal | None:
        value = meta.get(key, "")
        return Decimal(value) if value else None

    if not meta.get("strike_type"):
        return None
    return Bucket(market.market_id, meta["strike_type"], dec("floor_strike"), dec("cap_strike"))


def midnight_risk(run: NbmRun, target: date, center: float, margin: float = MIDNIGHT_MARGIN) -> bool:
    """True if a forecast temperature near either midnight of `target` is within `margin` of the high."""
    for day in (target, target + timedelta(days=1)):
        midnight = datetime.combine(day, time(0), tzinfo=NY).astimezone(timezone.utc)
        for at, temp in run.temps.items():
            if abs((at - midnight).total_seconds()) <= 2 * 3600 and temp >= center - margin:
                return True
    return False


class KalshiHighTemperature:
    name = "kxhigh_nbm"

    def __init__(
        self,
        params: TemperatureParams,
        nbm: NbmClient,
        iem: IemAsosClient,
        series: str = "KXHIGHNY",
        now: Callable[[], datetime] = utc_now,
        refresh: timedelta = timedelta(minutes=10),
    ) -> None:
        self.params = params
        self.nbm = nbm
        self.iem = iem
        self.series = series
        self._now = now
        self._refresh = refresh
        self._dists: dict[date, tuple[datetime, HighDistribution | None]] = {}

    def predict(self, market: Market, book: OrderBook) -> Decimal | None:
        if market.venue != "kalshi" or market.venue_meta.get("series_ticker") != self.series:
            return None
        bucket = bucket_of(market)
        event = market.venue_meta.get("event_ticker", "")
        if bucket is None or not event:
            return None
        dist = self._distribution(event_day(event))
        if dist is None:
            return None
        return Decimal(str(round(dist.probability(bucket), 4)))

    def _distribution(self, target: date) -> HighDistribution | None:
        now = self._now()
        cached = self._dists.get(target)
        if cached and now - cached[0] < self._refresh:
            return cached[1]
        dist = self._build(target, now)
        self._dists[target] = (now, dist)
        return dist

    def _build(self, target: date, now: datetime) -> HighDistribution | None:
        run = self.nbm.latest_run(now, target=target)
        forecast = run.maxima.get(target) if run else None
        if forecast is None:
            log.info("%s: no NBM forecast for %s", self.name, target)
            return None
        model = self.params.leads[self.params.lead_for(target, now.astimezone(NY))]
        center = forecast.txn + model.bias
        if midnight_risk(run, target, center):
            log.info("%s: abstaining on %s, a midnight hour is forecast near the high", self.name, target)
            return None
        return predict_high(model, forecast.txn, forecast.xnd, self._observed_max(target, now), self._low_band(target))

    def _observed_max(self, target: date, now: datetime) -> float | None:
        start = datetime.combine(target, time(1), tzinfo=NY).astimezone(timezone.utc)
        if now <= start:
            return None
        obs = self.iem.temperatures(CENTRAL_PARK, start.date(), (now + timedelta(days=1)).date())
        values = [float(o.tmpf) for o in drop_spikes(obs) if start <= o.at <= now]
        return max(values) if values else None

    @staticmethod
    def _low_band(target: date) -> float:
        return LOW_BAND_DST if is_dst(target, NY) else LOW_BAND_STANDARD
