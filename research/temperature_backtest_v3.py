"""Backtest v3: v2 plus a correction for how far off LAMP is running right now.

v3 adds alpha * error_now to LAMP's remaining-day max, where error_now is
the latest reading minus LAMP's forecast for that hour (at decision time,
from the same run the model uses). alpha = 0 is v2. Both are fitted on the
same training cases per lead and scored against the market on identical
rows. Adds a 12:00 lead (market prices are cached up to 15:00).

Run: uv run python research/temperature_backtest_v3.py --cache data/backtest.pkl
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from temperature_backtest import NY, observed_max
from temperature_backtest_v2 import brier_rows, lamp_max

from profit_engine.models.temperature import RemainingMaxModel
from profit_engine.scoring import calibration_table
from profit_engine.scoring.report import format_calibration
from profit_engine.weather import is_dst
from profit_engine.weather.iem import drop_spikes
from profit_engine.weather.lamp import LampClient, current_error

LEADS: dict[str, tuple[int, int]] = {
    "D-1 16:00": (-1, 16),
    "D 10:00": (0, 10),
    "D 12:00": (0, 12),
    "D 14:00": (0, 14),
}
BIASES = [b / 4 for b in range(-4, 13)]  # -1.0 .. 3.0
SIGMAS = [s / 4 for s in range(4, 15)]  # 1.0 .. 3.5
ALPHAS = [0.0, 0.25, 0.5, 0.75, 1.0]


def decision_time(day: date, lead: str) -> datetime:
    offset, hour = LEADS[lead]
    return datetime.combine(day + timedelta(days=offset), time(hour), tzinfo=NY).astimezone(timezone.utc)


def build(data: dict, days: list[date]):
    from profit_engine.venues.http import ReadOnlyHttp
    from profit_engine.weather import lamp

    client = LampClient(ReadOnlyHttp(lamp.BASE_URL, timeout=60, min_interval=0.1))
    client._cache.update(data.get("lamp_runs", {}))
    sparse_before = datetime.now(timezone.utc) - timedelta(days=6)
    by_day = {s.day: s for s in data["settled"]}
    outcomes = {d: int(by_day[d].value) if d in by_day and by_day[d].value is not None else h
                for d, h in data["highs"].items() if h is not None}  # fmt: skip
    obs = drop_spikes(data["obs"])
    cases = []
    for day in days:
        if day not in outcomes:
            continue
        until = datetime.combine(day + timedelta(days=1), time(1), tzinfo=NY).astimezone(timezone.utc)
        for lead in LEADS:
            at = decision_time(day, lead)
            run = client.latest_run(at, until, sparse_before=sparse_before)
            fmax = lamp_max(run, day, at)
            if fmax is None:
                continue
            day_start = datetime.combine(day, time(1), tzinfo=NY).astimezone(timezone.utc)
            err = current_error(run, [o for o in obs if o.at >= day_start - timedelta(hours=2)], at) if at > day_start else None
            event = by_day.get(day)
            cases.append(
                {
                    "day": day, "lead": lead, "at": at, "observed": observed_max(obs, day, at), "lamp": fmax,
                    "error": err, "outcome": outcomes[day], "dst": is_dst(day, NY),
                    "buckets": event.buckets if event else (),
                }  # fmt: skip
            )
    data["lamp_runs"] = dict(client._cache)
    return cases


def run(start: date, test_start: date, end: date, cache: str, out_params: str) -> str:
    with open(cache, "rb") as fh:
        data = pickle.load(fh)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    # Only days with a full observation index nearby; obs list is large, so pre-sort once.
    data["obs"] = sorted(data["obs"], key=lambda o: o.at)
    cases = build(data, days)
    train = [c for c in cases if c["day"] < test_start]
    test = [c for c in cases if c["day"] >= test_start]

    def rows(cs):
        return [(c["observed"], c["lamp"], c["outcome"], c["dst"], c["error"]) for c in cs]

    v2, v3 = {}, {}
    for lead in LEADS:
        tr = rows([c for c in train if c["lead"] == lead])
        v2[lead] = RemainingMaxModel.fit(tr, BIASES, SIGMAS, [0.0])
        v3[lead] = RemainingMaxModel.fit(tr, BIASES, SIGMAS, ALPHAS)
        print(f"  {lead}: v2 {v2[lead]}  v3 {v3[lead]}", flush=True)

    lines = [
        "# Temperature model backtest v3: correcting LAMP by its current error",
        "",
        f"Run {datetime.now().date()}. Train {start} to {test_start - timedelta(days=1)} ({len(train)} cases), "
        f"test {test_start} to {end} ({len(test)} cases).",
        "",
        "## Fitted parameters (training period)",
        "",
        "| Lead | n | v2 bias / sigma | v3 bias / sigma / alpha | Cases with an error_now |",
        "|---|---|---|---|---|",
    ]
    for lead in LEADS:
        tr = [c for c in train if c["lead"] == lead]
        n_err = sum(c["error"] is not None for c in tr)
        a, b = v2[lead], v3[lead]
        lines.append(
            f"| {lead} | {len(tr)} | {a.bias:+.2f} / {a.sigma:.2f} | {b.bias:+.2f} / {b.sigma:.2f} / {b.alpha:.2f} | {n_err} |"
        )

    lines += [
        "",
        "## Test period: v2 vs v3 vs market on identical bucket rows",
        "",
        "| Lead | Cases | v2 log | v3 log | Rows | v2 Brier | v3 Brier | Market Brier | v2 skill | v3 skill |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    pairs3, pairs_m = [], []
    for lead in [*LEADS, "all"]:
        cs = test if lead == "all" else [c for c in test if c["lead"] == lead]
        l2, l3, b2, b3, bm = [], [], [], [], []
        for c in cs:
            d2 = v2[c["lead"]].distribution(c["observed"], c["lamp"], c["dst"], c["error"])
            d3 = v3[c["lead"]].distribution(c["observed"], c["lamp"], c["dst"], c["error"])
            l2.append(math.log(max(d2.probs.get(c["outcome"], 0.0), 1e-9)))
            l3.append(math.log(max(d3.probs.get(c["outcome"], 0.0), 1e-9)))
            case = type("C", (), {"buckets": c["buckets"], "at": c["at"], "outcome": c["outcome"]})
            for (p2, mid, out), (p3, _, _) in zip(
                brier_rows(case, d2, data["quotes"]), brier_rows(case, d3, data["quotes"]), strict=True
            ):
                b2.append((p2 - out) ** 2)
                b3.append((p3 - out) ** 2)
                bm.append((mid - out) ** 2)
                if lead == "all":
                    pairs3.append((Decimal(str(round(p3, 6))), Decimal(int(out))))
                    pairs_m.append((Decimal(str(mid)), Decimal(int(out))))

        def mean(xs):
            return sum(xs) / len(xs) if xs else float("nan")

        m2, m3, mm = mean(b2), mean(b3), mean(bm)
        lines.append(
            f"| {lead} | {len(cs)} | {mean(l2):.3f} | {mean(l3):.3f} | {len(bm)} | {m2:.4f} | {m3:.4f} | {mm:.4f} | "
            f"{1 - m2 / mm:+.3f} | {1 - m3 / mm:+.3f} |"
        )

    lines += [
        "",
        format_calibration("v3 calibration (all leads, same rows as market)", calibration_table(pairs3)),
        "",
        format_calibration("Market calibration (same rows)", calibration_table(pairs_m)),
        "",
    ]
    with open(out_params, "w") as fh:
        json.dump(
            {
                "model": "lamp",
                "version": 3,
                "trained": f"{start} to {test_start - timedelta(days=1)}",
                "leads": {lead: v3[lead].to_dict() for lead in LEADS},
                "lead_times": {lead: list(v) for lead, v in LEADS.items()},
            },
            fh,
            indent=2,
        )
    lines.append(f"v3 parameters written to `{out_params}`.")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 8, 1))
    parser.add_argument("--test-start", type=date.fromisoformat, default=date(2025, 8, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 1))
    parser.add_argument("--cache", default="data/backtest.pkl")
    parser.add_argument("--out", default="research/temperature_backtest_v3.md")
    parser.add_argument("--params", default="research/temperature_params_lamp_v3.json")
    args = parser.parse_args()
    text = run(args.start, args.test_start, args.end, args.cache, args.params)
    print(text)
    with open(args.out, "w") as fh:
        fh.write(text)


if __name__ == "__main__":
    main()
