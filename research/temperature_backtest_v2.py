"""Backtest v2 (LAMP, max of observed and remaining) against v1 (NBM) and the market.

Same leads, training and test periods as temperature_backtest.py, and the
same cached data, plus LAMP runs. v2's inputs at decision time T:
- observed_max: spike-checked readings from 01:00 local on the target day to T
- lamp_max: highest hourly forecast from the newest LAMP run available at T
  (runtime + 1 h), over the rest of the day: max(T, 01:00) to midnight local
v1, v2 and the market are scored on exactly the same bucket rows.

Run: uv run python research/temperature_backtest_v2.py --cache data/backtest.pkl
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pickle
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import temperature_backtest as v1
from temperature_backtest import LEADS, NY, decision_time, low_band, observed_max

from profit_engine.models.temperature import HighDistribution, RemainingMaxModel, SpreadModel, predict_high
from profit_engine.scoring import calibration_table
from profit_engine.scoring.report import format_calibration
from profit_engine.weather import is_dst
from profit_engine.weather.iem import drop_spikes
from profit_engine.weather.kalshi_temps import quote_at
from profit_engine.weather.lamp import LampClient, LampRun


def window(day: date, at: datetime) -> tuple[datetime, datetime]:
    """Rest of the target day still to come at `at`: [max(at, 01:00 local), next midnight local)."""
    start = datetime.combine(day, time(1), tzinfo=NY).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=NY).astimezone(timezone.utc)
    return max(start, at), end


def lamp_max(run: LampRun | None, day: date, at: datetime) -> float | None:
    if run is None:
        return None
    start, end = window(day, at)
    if start >= end:
        return None
    return run.max_between(start, end)


def save(data: dict, cache: str) -> None:
    tmp = cache + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(data, fh)
    os.replace(tmp, cache)


def fetch_lamp(days: list[date], cache: str, data: dict) -> dict[tuple[datetime, date], LampRun | None]:
    from profit_engine.venues.http import ReadOnlyHttp
    from profit_engine.weather import lamp

    client = LampClient(ReadOnlyHttp(lamp.BASE_URL, timeout=60, min_interval=0.1))
    client._cache.update(data.get("lamp_runs", {}))
    sparse_before = datetime.now(timezone.utc) - timedelta(days=6)
    chosen = {}
    for i, day in enumerate(days):
        until = datetime.combine(day + timedelta(days=1), time(1), tzinfo=NY).astimezone(timezone.utc)
        for lead in LEADS:
            at = decision_time(day, lead)
            chosen[(at, day)] = client.latest_run(at, until, sparse_before=sparse_before)
        if i % 60 == 59:
            data["lamp_runs"] = dict(client._cache)
            save(data, cache)
            print(f"  LAMP runs through {day} ({len(client._cache)} cached)", flush=True)
    data["lamp_runs"] = dict(client._cache)
    save(data, cache)
    return chosen


def brier_rows(case, dist: HighDistribution, quotes) -> list[tuple[float, float, float]]:
    """(model p, market mid, outcome) per bucket with a two-sided market price at decision time."""
    rows = []
    for b in case.buckets:
        q = quote_at(quotes.get(b.ticker, []), case.at)
        mid = q.midpoint if q else None
        if mid is None:
            continue
        rows.append((dist.probability(b), float(mid), 1.0 if b.contains(Decimal(case.outcome)) else 0.0))
    return rows


def run(start: date, test_start: date, end: date, cache: str, v1_params: str, out_params: str) -> str:
    data, nbm_latest = v1.load(start, end, test_start, cache)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    lamp_runs = fetch_lamp(days, cache, data)

    by_day = {s.day: s for s in data["settled"]}
    outcomes = {d: int(by_day[d].value) if d in by_day and by_day[d].value is not None else h
                for d, h in data["highs"].items() if h is not None}  # fmt: skip
    obs = drop_spikes(data["obs"])
    cases = v1.build_cases(days, LEADS, lambda at, day: nbm_latest.get((at, day)), obs, outcomes, by_day)

    # v2 inputs per case
    inputs = {}
    for c in cases:
        inputs[(c.day, c.lead)] = (observed_max(obs, c.day, c.at), lamp_max(lamp_runs.get((c.at, c.day)), c.day, c.at))
    train = [c for c in cases if c.day < test_start and inputs[(c.day, c.lead)][1] is not None]
    test = [c for c in cases if c.day >= test_start and inputs[(c.day, c.lead)][1] is not None]

    v2 = {}
    for lead in LEADS:
        rows = [(*inputs[(c.day, c.lead)], c.outcome, is_dst(c.day, NY)) for c in train if c.lead == lead]
        v2[lead] = RemainingMaxModel.fit(rows)
        print(f"  fitted {lead}: {v2[lead]}", flush=True)

    with open(v1_params) as fh:
        v1_raw = json.load(fh)
    v1_models = {k: SpreadModel.from_dict(v) for k, v in v1_raw["leads"].items()}

    lines = [
        "# Temperature model backtest v2: LAMP, max of observed and remaining",
        "",
        f"Run {datetime.now().date()}. Train {start} to {test_start - timedelta(days=1)} ({len(train)} cases), "
        f"test {test_start} to {end} ({len(test)} cases with a LAMP forecast).",
        "",
        "## Fitted v2 parameters (training period)",
        "",
        "| Lead | n | Bias °F (added to LAMP's remaining max) | Sigma °F |",
        "|---|---|---|---|",
    ]
    for lead, m in v2.items():
        n = sum(c.lead == lead for c in train)
        lines.append(f"| {lead} | {n} | {m.bias:+.2f} | {m.sigma:.2f} |")

    lines += [
        "",
        "## Test period: v1 vs v2 vs market on identical bucket rows",
        "",
        "Log score: mean log probability of the realized high (higher is better). "
        "Skill = 1 - model Brier / market Brier (positive beats the market).",
        "",
        "| Lead | Cases | v1 log | v2 log | Rows | v1 Brier | v2 Brier | Market Brier | v1 skill | v2 skill |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    v2_pairs, mkt_pairs = [], []
    for lead in [*LEADS, "all"]:
        rows_cases = test if lead == "all" else [c for c in test if c.lead == lead]
        l1, l2, b1, b2, bm = [], [], [], [], []
        for c in rows_cases:
            d1 = predict_high(v1_models[c.lead], c.txn, c.xnd, c.observed_max, low_band(c.day))
            o, f = inputs[(c.day, c.lead)]
            d2 = v2[c.lead].distribution(o, f, is_dst(c.day, NY))
            l1.append(math.log(max(d1.probs.get(c.outcome, 0.0), 1e-9)))
            l2.append(math.log(max(d2.probs.get(c.outcome, 0.0), 1e-9)))
            for (p1, mid, out), (p2, _, _) in zip(brier_rows(c, d1, data["quotes"]), brier_rows(c, d2, data["quotes"]), strict=True):
                b1.append((p1 - out) ** 2)
                b2.append((p2 - out) ** 2)
                bm.append((mid - out) ** 2)
                if lead == "all":
                    v2_pairs.append((Decimal(str(round(p2, 6))), Decimal(int(out))))
                    mkt_pairs.append((Decimal(str(mid)), Decimal(int(out))))
        def mean(xs):
            return sum(xs) / len(xs) if xs else float("nan")
        m1, m2, mm = mean(b1), mean(b2), mean(bm)
        lines.append(
            f"| {lead} | {len(rows_cases)} | {mean(l1):.3f} | {mean(l2):.3f} | {len(bm)} | {m1:.4f} | {m2:.4f} | "
            f"{mm:.4f} | {1 - m1 / mm:+.3f} | {1 - m2 / mm:+.3f} |"
        )

    lines += [
        "",
        format_calibration("v2 calibration (all leads, same rows as market)", calibration_table(v2_pairs)),
        "",
        format_calibration("Market calibration (same rows)", calibration_table(mkt_pairs)),
        "",
    ]
    with open(out_params, "w") as fh:
        json.dump(
            {
                "model": "lamp",
                "trained": f"{start} to {test_start - timedelta(days=1)}",
                "leads": {lead: v2[lead].to_dict() for lead in LEADS},
                "lead_times": {lead: list(v) for lead, v in LEADS.items()},
            },
            fh,
            indent=2,
        )
    lines.append(f"v2 parameters written to `{out_params}`.")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 8, 1))
    parser.add_argument("--test-start", type=date.fromisoformat, default=date(2025, 8, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 1))
    parser.add_argument("--cache", default="data/backtest.pkl")
    parser.add_argument("--v1-params", default="research/temperature_params.json")
    parser.add_argument("--out", default="research/temperature_backtest_v2.md")
    parser.add_argument("--params", default="research/temperature_params_lamp.json")
    args = parser.parse_args()
    text = run(args.start, args.test_start, args.end, args.cache, args.v1_params, args.params)
    print(text)
    with open(args.out, "w") as fh:
        fh.write(text)


if __name__ == "__main__":
    main()
