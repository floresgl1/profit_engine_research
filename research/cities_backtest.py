"""Backtest the v3 temperature model (LAMP + current-error correction) for other Kalshi cities.

Per city (weather/stations.py), with the same leads and train/test split as
the NYC v3 backtest:
1. Data: settled Kalshi events (settlement value, bucket strikes), ACIS daily
   highs, IEM observations, as-issued LAMP runs, hourly market candles for
   the test period. Cached per city in data/cities/<series>.pkl.
2. Undercount: official high - max reading, measured on the city's own
   training days where the day window can't matter (standard-time days, and
   DST days whose midnight hours are 5°F+ below the peak), skipping days
   with observation gaps over 90 minutes.
3. Fit v3 per lead on the training period, score against the city's market
   midpoint on identical rows in the test period, and save parameters to
   research/params/<series>.json for the live model.

Run: uv run python research/cities_backtest.py [--cities KXHIGHCHI,KXHIGHAUS]
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import pickle
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from profit_engine.models.temperature import RemainingMaxModel
from profit_engine.weather import is_dst, is_transition
from profit_engine.weather.iem import drop_spikes
from profit_engine.weather.kalshi_temps import quote_at
from profit_engine.weather.lamp import LampClient, current_error
from profit_engine.weather.stations import CITIES, City

LEADS: dict[str, tuple[int, int]] = {
    "D-1 16:00": (-1, 16),
    "D 10:00": (0, 10),
    "D 12:00": (0, 12),
    "D 14:00": (0, 14),
}
BIASES = [b / 4 for b in range(-4, 13)]  # -1.0 .. 3.0
SIGMAS = [s / 4 for s in range(4, 15)]  # 1.0 .. 3.5
ALPHAS = [0.0, 0.25, 0.5, 0.75, 1.0]
HISTORICAL_CUTOFF = date(2026, 8, 4)
CALM_GAP = 5.0
MAX_GAP = timedelta(minutes=90)


def local(day: date, hour: int, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=zone).astimezone(timezone.utc)


def decision_time(day: date, lead: str, zone: ZoneInfo) -> datetime:
    offset, hour = LEADS[lead]
    return local(day + timedelta(days=offset), hour, zone)


class Obs:
    """Sorted observations with fast range queries."""

    def __init__(self, observations):
        self.obs = sorted(observations, key=lambda o: o.at)
        self.times = [o.at for o in self.obs]

    def between(self, start: datetime, end: datetime):
        return self.obs[bisect.bisect_left(self.times, start) : bisect.bisect_right(self.times, end)]

    def max_between(self, start: datetime, end: datetime) -> float | None:
        values = [float(o.tmpf) for o in self.between(start, end - timedelta(microseconds=1))]
        return max(values) if values else None

    def has_gap(self, start: datetime, end: datetime) -> bool:
        points = [start, *(o.at for o in self.between(start, end)), end]
        return any(b - a > MAX_GAP for a, b in zip(points, points[1:]))  # noqa: B905


def undercount(city: City, obs: Obs, highs: dict[date, int | None], days: list[date]) -> tuple[dict, dict]:
    """Official high - rounded max reading on window-insensitive days, split DST / standard time."""
    zone = city.zone
    dst_counts: dict[int, int] = {}
    std_counts: dict[int, int] = {}
    for day in days:
        official = highs.get(day)
        if official is None or is_transition(day, zone):
            continue
        nxt = day + timedelta(days=1)
        if obs.has_gap(local(day, 0, zone), local(nxt, 1, zone)):
            continue
        start_h = obs.max_between(local(day, 0, zone), local(day, 1, zone))
        core = obs.max_between(local(day, 1, zone), local(nxt, 0, zone))
        end_h = obs.max_between(local(nxt, 0, zone), local(nxt, 1, zone))
        if None in (start_h, core, end_h):
            continue
        dst = is_dst(day, zone)
        if dst and max(start_h, end_h) > core - CALM_GAP:
            continue
        reading = max(core, end_h)  # LST window; equals clock window on these days
        err = official - math.floor(reading + 0.5)
        counts = dst_counts if dst else std_counts
        counts[err] = counts.get(err, 0) + 1
    return dst_counts, std_counts


def save(data: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(data, fh)
    os.replace(tmp, path)


def load(city: City, start: date, test_start: date, end: date, cache: str) -> dict:
    from profit_engine.venues.http import ReadOnlyHttp
    from profit_engine.venues.kalshi import BASE_URL as KALSHI_URL
    from profit_engine.weather import AcisClient, IemAsosClient, acis, iem, lamp, settled_temperatures
    from profit_engine.weather.kalshi_temps import price_history

    data = {}
    if os.path.exists(cache):
        with open(cache, "rb") as fh:
            data = pickle.load(fh)
    kalshi = ReadOnlyHttp(KALSHI_URL, min_interval=0.3)
    if "settled" not in data:
        data["settled"] = settled_temperatures(kalshi, city.series)
        data["highs"] = AcisClient(ReadOnlyHttp(acis.BASE_URL)).daily_max(city.acis, start, end)
        client = IemAsosClient(ReadOnlyHttp(iem.BASE_URL, timeout=120, min_interval=1.0))
        obs, y = [], start - timedelta(days=1)
        while y <= end:
            nxt = min(date(y.year + 1, 1, 1), end + timedelta(days=2))
            obs += client.temperatures(city.iem, y, nxt)
            y = nxt
        data["obs"] = obs
        save(data, cache)
        print(f"  {city.series}: {len(data['settled'])} settled events, {len(obs)} observations", flush=True)

    lamp_client = LampClient(ReadOnlyHttp(lamp.BASE_URL, timeout=60, min_interval=0.1), station=city.icao)
    lamp_client._cache.update(data.get("lamp_runs", {}))
    sparse_before = datetime.now(timezone.utc) - timedelta(days=6)
    chosen = {}
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    for i, day in enumerate(days):
        until = local(day + timedelta(days=1), 1, city.zone)
        for lead in LEADS:
            at = decision_time(day, lead, city.zone)
            chosen[(day, lead)] = lamp_client.latest_run(at, until, sparse_before=sparse_before)
        if i % 90 == 89:
            data["lamp_runs"] = dict(lamp_client._cache)
            save(data, cache)
            print(f"  {city.series}: LAMP runs through {day}", flush=True)
    data["lamp_runs"] = dict(lamp_client._cache)
    data["chosen"] = chosen
    save(data, cache)

    quotes = data.setdefault("quotes", {})
    for event in [s for s in data["settled"] if test_start <= s.day <= end and s.buckets]:
        tickers = [b.ticker for b in event.buckets if b.ticker not in quotes]
        if not tickers:
            continue
        lo = decision_time(event.day, "D-1 16:00", city.zone) - timedelta(hours=6)
        hi = decision_time(event.day, "D 14:00", city.zone) + timedelta(hours=1)
        quotes.update(price_history(kalshi, tickers, lo, hi, historical=event.day < HISTORICAL_CUTOFF))
        save(data, cache)
    print(f"  {city.series}: {len(quotes)} market price histories", flush=True)
    return data


def cases_for(city: City, data: dict, days: list[date]) -> list[dict]:
    zone = city.zone
    by_day = {s.day: s for s in data["settled"]}
    outcomes = {d: int(by_day[d].value) if d in by_day and by_day[d].value is not None else h
                for d, h in data["highs"].items() if h is not None}  # fmt: skip
    obs = Obs(drop_spikes(data["obs"]))
    out = []
    for day in days:
        if day not in outcomes:
            continue
        day_start, day_end = local(day, 1, zone), local(day + timedelta(days=1), 0, zone)
        for lead in LEADS:
            at = decision_time(day, lead, zone)
            run = data["chosen"].get((day, lead))
            if run is None:
                continue
            fmax = run.max_between(max(at, day_start), day_end)
            if fmax is None:
                continue
            todays = obs.between(day_start, at) if at > day_start else []
            event = by_day.get(day)
            out.append(
                {
                    "day": day, "lead": lead, "at": at,
                    "observed": max((float(o.tmpf) for o in todays), default=None),
                    "lamp": fmax, "error": current_error(run, todays, at) if todays else None,
                    "outcome": outcomes[day], "dst": is_dst(day, zone),
                    "buckets": event.buckets if event else (),
                }  # fmt: skip
            )
    return out


def evaluate(city: City, start: date, test_start: date, end: date, cache: str, params_dir: str) -> list[str]:
    data = load(city, start, test_start, end, cache)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    train_days = [d for d in days if d < test_start]
    dst_u, std_u = undercount(city, Obs(drop_spikes(data["obs"])), data["highs"], train_days)
    cases = cases_for(city, data, days)
    train = [c for c in cases if c["day"] < test_start]
    test = [c for c in cases if c["day"] >= test_start]

    models = {}
    for lead in LEADS:
        rows = [(c["observed"], c["lamp"], c["outcome"], c["dst"], c["error"]) for c in train if c["lead"] == lead]
        models[lead] = RemainingMaxModel.fit(rows, BIASES, SIGMAS, ALPHAS, dst_u or None, std_u or None)

    lines = [
        f"## {city.name} ({city.series}, settles on {city.climate_report})",
        "",
        f"Train {len(train)} cases, test {len(test)} cases. Undercount days: {sum(dst_u.values())} DST, "
        f"{sum(std_u.values())} standard time.",
        "",
        "| Lead | Bias | Sigma | Alpha | Test cases | Rows | Model Brier | Market Brier | Skill | Log score |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for lead in [*LEADS, "all"]:
        cs = test if lead == "all" else [c for c in test if c["lead"] == lead]
        bm, bk, logs = [], [], []
        for c in cs:
            dist = models[c["lead"]].distribution(c["observed"], c["lamp"], c["dst"], c["error"])
            logs.append(math.log(max(dist.probs.get(c["outcome"], 0.0), 1e-9)))
            for b in c["buckets"]:
                q = quote_at(data["quotes"].get(b.ticker, []), c["at"])
                mid = q.midpoint if q else None
                if mid is None:
                    continue
                o = 1.0 if b.contains(Decimal(c["outcome"])) else 0.0
                bm.append((dist.probability(b) - o) ** 2)
                bk.append((float(mid) - o) ** 2)
        m = models.get(lead)
        mb = sum(bm) / len(bm) if bm else float("nan")
        kb = sum(bk) / len(bk) if bk else float("nan")
        skill = 1 - mb / kb if bm and kb else float("nan")
        lg = sum(logs) / len(logs) if logs else float("nan")
        cols = f"{m.bias:+.2f} | {m.sigma:.2f} | {m.alpha:.2f}" if m else " |  | "
        lines.append(f"| {lead} | {cols} | {len(cs)} | {len(bm)} | {mb:.4f} | {kb:.4f} | {skill:+.3f} | {lg:.3f} |")

    os.makedirs(params_dir, exist_ok=True)
    with open(os.path.join(params_dir, f"{city.series}.json"), "w") as fh:
        json.dump(
            {
                "model": "lamp",
                "version": 3,
                "series": city.series,
                "trained": f"{start} to {test_start - timedelta(days=1)}",
                "leads": {lead: models[lead].to_dict() for lead in LEADS},
                "lead_times": {lead: list(v) for lead, v in LEADS.items()},
            },
            fh,
            indent=2,
        )
    return [*lines, ""]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cities", default=",".join(s for s in CITIES if s != "KXHIGHNY"))
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 8, 1))
    parser.add_argument("--test-start", type=date.fromisoformat, default=date(2025, 8, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 1))
    parser.add_argument("--cache-dir", default="data/cities")
    parser.add_argument("--params-dir", default="research/params")
    parser.add_argument("--out", default="research/cities_backtest.md")
    args = parser.parse_args()
    os.makedirs(args.cache_dir, exist_ok=True)
    header = [
        "# v3 temperature model across Kalshi cities",
        "",
        f"Run {datetime.now().date()}. Train {args.start} to {args.test_start - timedelta(days=1)}, "
        f"test {args.test_start} to {args.end}. Skill = 1 - model Brier / market Brier on identical rows "
        "(positive beats the market). NYC results: research/temperature_backtest_v3.md.",
        "",
    ]
    body: list[str] = []
    for series in args.cities.split(","):
        city = CITIES[series]
        print(f"{city.series}: starting", flush=True)
        body += evaluate(city, args.start, args.test_start, args.end,
                         os.path.join(args.cache_dir, f"{series}.pkl"), args.params_dir)  # fmt: skip
        with open(args.out, "w") as fh:  # rewrite after every city so partial results survive
            fh.write("\n".join(header + body) + "\n")
        print(f"{city.series}: done", flush=True)
    print("\n".join(header + body))


if __name__ == "__main__":
    main()
