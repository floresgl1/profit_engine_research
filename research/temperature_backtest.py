"""Backtest the KXHIGHNY temperature model against the market.

For each target day and each decision time T:
- forecast: the latest NBM run available at T (runtime + 2 h lag)
- observations: spike-checked Central Park readings from 01:00 local
  (inside both candidate day windows) up to T
- market: midpoint of the last hourly candle that closed at or before T
- outcome: Kalshi's settlement value where published, else the ACIS high

Spread parameters are fitted on the training period only, then both
variants (fixed sigma, sigma scaled by NBM's xnd) are scored on the test
period: bucket Brier vs the market on identical rows, log score of the
realized high, and calibration. The better variant's parameters are saved
for the live model.

Run: uv run python research/temperature_backtest.py --cache data/backtest.pkl
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pickle
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from profit_engine.models.temperature import (
    LOW_BAND_DST,
    LOW_BAND_STANDARD,
    SpreadModel,
    predict_high,
)
from profit_engine.scoring import calibration_table
from profit_engine.scoring.report import format_calibration
from profit_engine.weather import is_dst
from profit_engine.weather.iem import Observation, drop_spikes
from profit_engine.weather.kalshi_temps import Bucket, Quote, SettledTemperature, quote_at

NY = ZoneInfo("America/New_York")
HISTORICAL_CUTOFF = date(2026, 8, 4)  # GET /historical/cutoff, market_settled_ts

# name -> (days relative to target, local hour)
LEADS: dict[str, tuple[int, int]] = {"D-1 16:00": (-1, 16), "D 10:00": (0, 10), "D 14:00": (0, 14)}


def decision_time(day: date, lead: str) -> datetime:
    offset, hour = LEADS[lead]
    return datetime.combine(day + timedelta(days=offset), time(hour), tzinfo=NY).astimezone(timezone.utc)


@dataclass(frozen=True)
class Case:
    """Everything known at one decision time about one target day, plus the answer."""

    day: date
    lead: str
    at: datetime
    txn: float
    xnd: float | None
    observed_max: float | None
    outcome: int
    buckets: tuple[Bucket, ...]


def observed_max(obs: list[Observation], day: date, at: datetime) -> float | None:
    start = datetime.combine(day, time(1), tzinfo=NY).astimezone(timezone.utc)
    values = [float(o.tmpf) for o in obs if start <= o.at <= at]
    return max(values) if values else None


def build_cases(days, leads, nbm_latest: Callable, obs, outcomes, settled_by_day) -> list[Case]:
    cases = []
    for day in days:
        if day not in outcomes:
            continue
        for lead in leads:
            at = decision_time(day, lead)
            run = nbm_latest(at)
            forecast = run.maxima.get(day) if run else None
            if forecast is None:
                continue
            event = settled_by_day.get(day)
            cases.append(
                Case(
                    day, lead, at, forecast.txn, forecast.xnd,
                    observed_max(obs, day, at), outcomes[day], event.buckets if event else (),
                )  # fmt: skip
            )
    return cases


def low_band(day: date) -> float:
    return LOW_BAND_DST if is_dst(day, NY) else LOW_BAND_STANDARD


def fit(cases: list[Case]) -> dict[str, dict[str, SpreadModel]]:
    """Per lead: residuals of the raw forecast (before observations) -> both spread variants."""
    out: dict[str, dict[str, SpreadModel]] = {}
    for lead in LEADS:
        rows = [c for c in cases if c.lead == lead]
        residuals = [c.outcome - c.txn for c in rows]
        xnds = [c.xnd for c in rows]
        out[lead] = {"fixed": SpreadModel.fit_fixed(residuals), "scaled": SpreadModel.fit_scaled(residuals, xnds)}
    return out


@dataclass
class Score:
    n_cases: int = 0
    log_score: float = 0.0  # mean log P(realized high); higher is better
    n_rows: int = 0  # bucket rows with a two-sided market price
    model_brier: float = 0.0
    market_brier: float = 0.0
    model_pairs: list = None
    market_pairs: list = None


def evaluate(cases: list[Case], model_for: Callable[[str], SpreadModel], quotes: dict[str, list[Quote]]) -> Score:
    s = Score(model_pairs=[], market_pairs=[])
    logs, mb, kb = [], [], []
    for c in cases:
        dist = predict_high(model_for(c.lead), c.txn, c.xnd, c.observed_max, low_band(c.day))
        logs.append(math.log(max(dist.probs.get(c.outcome, 0.0), 1e-9)))
        for b in c.buckets:
            q = quote_at(quotes.get(b.ticker, []), c.at)
            mid = q.midpoint if q else None
            if mid is None:
                continue
            p = dist.probability(b)
            o = 1.0 if b.contains(Decimal(c.outcome)) else 0.0
            mb.append((p - o) ** 2)
            kb.append((float(mid) - o) ** 2)
            s.model_pairs.append((Decimal(str(round(p, 6))), Decimal(int(o))))
            s.market_pairs.append((mid, Decimal(int(o))))
    s.n_cases = len(cases)
    s.log_score = sum(logs) / len(logs) if logs else float("nan")
    s.n_rows = len(mb)
    s.model_brier = sum(mb) / len(mb) if mb else float("nan")
    s.market_brier = sum(kb) / len(kb) if kb else float("nan")
    return s


# --- data ------------------------------------------------------------------------------------


def load(start: date, end: date, test_start: date, cache: str | None):
    data = {}
    if cache and os.path.exists(cache):
        with open(cache, "rb") as fh:
            data = pickle.load(fh)

    def save():
        if cache:
            os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
            with open(cache, "wb") as fh:
                pickle.dump(data, fh)

    from profit_engine.venues.http import ReadOnlyHttp
    from profit_engine.venues.kalshi import BASE_URL as KALSHI_URL
    from profit_engine.weather import AcisClient, IemAsosClient, acis, iem, settled_temperatures
    from profit_engine.weather.kalshi_temps import price_history
    from profit_engine.weather.nbm import NbmClient

    kalshi = ReadOnlyHttp(KALSHI_URL, min_interval=0.3)
    if "settled" not in data:
        data["settled"] = settled_temperatures(kalshi, "KXHIGHNY")
        data["highs"] = AcisClient(ReadOnlyHttp(acis.BASE_URL)).daily_max(acis.CENTRAL_PARK, start, end)
        client = IemAsosClient(ReadOnlyHttp(iem.BASE_URL, timeout=120, min_interval=1.0))
        obs, y = [], start - timedelta(days=1)
        while y <= end:
            nxt = min(date(y.year + 1, 1, 1), end + timedelta(days=2))
            obs += client.temperatures(iem.CENTRAL_PARK, y, nxt)
            y = nxt
        data["obs"] = obs
        save()

    nbm = NbmClient(ReadOnlyHttp("https://mesonet.agron.iastate.edu", timeout=60, min_interval=0.1))
    nbm._cache.update(data.get("nbm", {}))
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    latest: dict[datetime, object] = {}
    for i, day in enumerate(days):
        for lead in LEADS:
            at = decision_time(day, lead)
            latest[at] = nbm.latest_run(at)
        if i % 30 == 29:
            data["nbm"] = dict(nbm._cache)
            save()
            print(f"  NBM runs through {day} ({len(nbm._cache)} cached)", flush=True)
    data["nbm"] = dict(nbm._cache)
    save()

    quotes = data.setdefault("quotes", {})
    test_events = [s for s in data["settled"] if test_start <= s.day <= end and s.buckets]
    for event in test_events:
        tickers = [b.ticker for b in event.buckets if b.ticker not in quotes]
        if not tickers:
            continue
        lo = decision_time(event.day, "D-1 16:00") - timedelta(hours=6)
        hi = decision_time(event.day, "D 14:00") + timedelta(hours=1)
        quotes.update(price_history(kalshi, tickers, lo, hi, historical=event.day < HISTORICAL_CUTOFF))
        save()
    return data, latest


def run(start: date, test_start: date, end: date, cache: str | None, params_out: str | None) -> str:
    data, latest = load(start, end, test_start, cache)
    settled: list[SettledTemperature] = data["settled"]
    by_day = {s.day: s for s in settled}
    outcomes = {d: int(by_day[d].value) if d in by_day and by_day[d].value is not None else h
                for d, h in data["highs"].items() if h is not None}  # fmt: skip
    obs = drop_spikes(data["obs"])
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    cases = build_cases(days, LEADS, lambda at: latest.get(at), obs, outcomes, by_day)
    train = [c for c in cases if c.day < test_start]
    test = [c for c in cases if c.day >= test_start]
    params = fit(train)

    lines = [
        "# Temperature model backtest: KXHIGHNY",
        "",
        f"Run {datetime.now().date()}. Train {start} to {test_start - timedelta(days=1)} "
        f"({len(train)} cases), test {test_start} to {end} ({len(test)} cases). "
        "A case is one target day at one decision time.",
        "",
        "## Fitted on the training period",
        "",
        "Residual = actual high - NBM forecast, before using observations.",
        "",
        "| Lead | n | Bias °F | Fixed sigma °F | Scaled k (sigma = k * xnd) |",
        "|---|---|---|---|---|",
    ]
    for lead, v in params.items():
        n = sum(c.lead == lead for c in train)
        lines.append(f"| {lead} | {n} | {v['fixed'].bias:+.2f} | {v['fixed'].sigma:.2f} | {v['scaled'].k:.2f} |")

    lines += [
        "",
        "## Test period",
        "",
        "Brier on bucket markets where the market had a two-sided price at the decision time; "
        "model and market scored on exactly the same rows. Log score: mean log probability of the realized high "
        "(higher is better).",
        "",
        "| Variant | Lead | Cases | Log score | Bucket rows | Model Brier | Market Brier | Skill vs market |",
        "|---|---|---|---|---|---|---|---|",
    ]
    results = {}
    for variant in ("fixed", "scaled"):
        for lead in [*LEADS, "all"]:
            rows = test if lead == "all" else [c for c in test if c.lead == lead]
            s = evaluate(rows, lambda ld, v=variant: params[ld][v], data["quotes"])
            results[(variant, lead)] = s
            skill = 1 - s.model_brier / s.market_brier if s.n_rows and s.market_brier else float("nan")
            lines.append(
                f"| {variant} | {lead} | {s.n_cases} | {s.log_score:.3f} | {s.n_rows} | "
                f"{s.model_brier:.4f} | {s.market_brier:.4f} | {skill:+.3f} |"
            )

    best = max(("fixed", "scaled"), key=lambda v: results[(v, "all")].log_score)
    s = results[(best, "all")]
    lines += [
        "",
        f"Better variant on held-out log score: **{best}**.",
        "",
        format_calibration(f"Model calibration ({best}, all leads, same rows as market)", calibration_table(s.model_pairs)),
        "",
        format_calibration("Market calibration (same rows)", calibration_table(s.market_pairs)),
        "",
    ]
    if params_out:
        with open(params_out, "w") as fh:
            json.dump(
                {
                    "variant": best,
                    "trained": f"{start} to {test_start - timedelta(days=1)}",
                    "leads": {lead: params[lead][best].to_dict() for lead in LEADS},
                    "lead_times": {lead: list(v) for lead, v in LEADS.items()},
                },
                fh,
                indent=2,
            )
        lines.append(f"Parameters for the live model written to `{params_out}`.")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 8, 1))
    parser.add_argument("--test-start", type=date.fromisoformat, default=date(2025, 8, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 1))
    parser.add_argument("--cache", default="data/backtest.pkl")
    parser.add_argument("--out", default="research/temperature_backtest.md")
    parser.add_argument("--params", default="research/temperature_params.json")
    args = parser.parse_args()
    text = run(args.start, args.test_start, args.end, args.cache, args.params)
    print(text)
    with open(args.out, "w") as fh:
        fh.write(text)


if __name__ == "__main__":
    main()
