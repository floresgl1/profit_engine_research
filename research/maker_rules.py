"""Should a market maker step back when the temperature model disagrees?

For the markout trades (markout.py), take the maker's side of every trade in
the two hours after each model decision time (16:00 the day before; 10:00,
12:00 and 14:00 on the day) and compute the model's probability for that
bucket from the v3 model at that time (parameters fitted on the training
period only, data the model had at that time only).

model edge = maker side x (model probability - trade price): positive when
the model agrees with the maker's trade (bought YES below the model, or sold
above it), negative when the model says the maker was picked off.

Candidate v2 rule: don't quote a side whose fills the model would put at a
model edge below -m. Each m is judged by the maker PnL it keeps; m is chosen
on the discovery half of events and checked on the holdout half.

Run: uv run python research/maker_rules.py --out research/maker_rules.md
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from calibration_screen import clustered_mean, is_holdout  # noqa: E402
from cities_backtest import LEADS, Obs, decision_time, local  # noqa: E402
from markout import MAKER_FEE, Trade, maker_sign, pick_events  # noqa: E402
from structural_edges import SERIES, base_cache  # noqa: E402

from profit_engine.models.temperature import RemainingMaxModel  # noqa: E402
from profit_engine.weather import is_dst  # noqa: E402
from profit_engine.weather.iem import drop_spikes  # noqa: E402
from profit_engine.weather.lamp import current_error  # noqa: E402
from profit_engine.weather.stations import CITIES  # noqa: E402

WINDOW = timedelta(hours=2)
MARGINS = [None, 0.20, 0.10, 0.05, 0.02, 0.0]  # None = v1 (quote regardless)
EDGE_BINS = [(-1.0, -0.10), (-0.10, -0.05), (-0.05, -0.02), (-0.02, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 1.0)]


def params_path(series: str) -> str:
    return "research/temperature_params_lamp_v3.json" if series == "KXHIGHNY" else f"research/params/{series}.json"


def load_models(series: str) -> dict[str, RemainingMaxModel]:
    raw = json.loads(Path(params_path(series)).read_text())
    return {lead: RemainingMaxModel.from_dict(v) for lead, v in raw["leads"].items()}


def chosen_runs(series: str, base: dict, days: list) -> dict:
    """LAMP run available at each decision time: from the city cache, or (NYC) re-chosen from cached runs."""
    if "chosen" in base:
        return base["chosen"]
    from profit_engine.venues.http import ReadOnlyHttp
    from profit_engine.weather import lamp
    from profit_engine.weather.lamp import LampClient

    city = CITIES[series]
    client = LampClient(ReadOnlyHttp(lamp.BASE_URL, timeout=60, min_interval=0.1), station=city.icao)
    client._cache.update(base.get("lamp_runs", {}))
    sparse_before = datetime.now(timezone.utc) - timedelta(days=6)
    out = {}
    for day in days:
        until = local(day + timedelta(days=1), 1, city.zone)
        for lead in LEADS:
            out[(day, lead)] = client.latest_run(decision_time(day, lead, city.zone), until, sparse_before=sparse_before)
    return out


@dataclass(frozen=True)
class ModelFill:
    series: str
    event: str
    lead: str
    price: float
    count: float
    model_edge: float  # maker side x (model prob - price)
    pnl: float  # maker settlement PnL per contract, conservative maker fee


def fills_with_model(series: str) -> list[ModelFill]:
    city = CITIES[series]
    with open(base_cache(series), "rb") as fh:
        base = pickle.load(fh)
    import __main__

    __main__.Trade = Trade  # markout.py ran as a script, so its cache refers to __main__.Trade
    with open(f"data/markout/{series}.pkl", "rb") as fh:
        trades: dict[str, list[Trade]] = pickle.load(fh)
    events = pick_events(base["settled"])
    runs = chosen_runs(series, base, [e.day for e in events])
    models = load_models(series)
    obs = Obs(drop_spikes(base["obs"]))
    out = []
    for e in events:
        day_start, day_end = local(e.day, 1, city.zone), local(e.day + timedelta(days=1), 0, city.zone)
        for lead in LEADS:
            at = decision_time(e.day, lead, city.zone)
            run = runs.get((e.day, lead))
            lamp_max = run.max_between(max(at, day_start), day_end) if run else None
            if lamp_max is None:
                continue
            todays = obs.between(day_start, at) if at > day_start else []
            observed = max((float(o.tmpf) for o in todays), default=None)
            error = current_error(run, todays, at) if todays else None
            dist = models[lead].distribution(observed, lamp_max, is_dst(e.day, city.zone), error)
            for b in e.buckets:
                fair = dist.probability(b)
                outcome = 1.0 if b.contains(e.value) else 0.0
                for t in trades.get(b.ticker, []):
                    if not at <= t.at < at + WINDOW:
                        continue
                    s, p = maker_sign(t), float(t.yes_price)
                    pnl = s * (outcome - p) - float(MAKER_FEE) * p * (1 - p)
                    out.append(ModelFill(series, e.event_ticker, lead, p, float(t.count), s * (fair - p), pnl))
    return out


def weighted(fills: list[ModelFill]) -> tuple[float, float, float]:
    """(mean PnL per contract, clustered SE, total PnL) volume-weighted."""
    if not fills:
        return float("nan"), float("nan"), 0.0
    w = sum(f.count for f in fills)
    scale = len(fills) / w
    m, se = clustered_mean([f.pnl * f.count * scale for f in fills], [f.event for f in fills])
    return m, se, sum(f.pnl * f.count for f in fills)


def kept(fills: list[ModelFill], margin: float | None) -> list[ModelFill]:
    return fills if margin is None else [f for f in fills if f.model_edge >= -margin]


def report(fills: list[ModelFill]) -> str:
    disc = [f for f in fills if not is_holdout(f.event)]
    hold = [f for f in fills if is_holdout(f.event)]
    out = ["# Maker fills vs the temperature model", ""]
    out.append(f"{len(fills):,} maker fills in the 2 h after each model decision time, {len({f.event for f in fills})} "
               f"events. PnL per contract in cents (held to settlement, maker fee {MAKER_FEE} x P x (1-P)), "
               "volume-weighted, ± SE clustered by event. Model edge = maker side x (model prob - price).")  # fmt: skip
    out += ["", "## Maker PnL by model edge", "", "| Model edge | Fills | Contracts | PnL per contract | Discovery | Holdout |",
            "|---|---|---|---|---|---|"]  # fmt: skip
    for lo, hi in EDGE_BINS:
        sel = [f for f in fills if lo <= f.model_edge < hi]
        if not sel:
            continue
        a = weighted(sel)
        d = weighted([f for f in sel if not is_holdout(f.event)])
        h = weighted([f for f in sel if is_holdout(f.event)])
        out.append(f"| {100 * lo:+.0f} to {100 * hi:+.0f}c | {len(sel)} | {sum(f.count for f in sel):,.0f} | "
                   f"{100 * a[0]:+.2f} ± {100 * a[1]:.2f} | {100 * d[0]:+.2f} | {100 * h[0]:+.2f} |")  # fmt: skip
    out += ["", "## Rule: skip fills the model puts below -m", "",
            "| m | Half | Contracts kept | PnL per contract | Total PnL ($ per contract-size 1) |", "|---|---|---|---|---|"]  # fmt: skip
    for margin in MARGINS:
        for name, half in (("discovery", disc), ("holdout", hold)):
            k = kept(half, margin)
            m, se, total = weighted(k)
            label = "v1 (none)" if margin is None else f"{100 * margin:.0f}c"
            out.append(f"| {label} | {name} | {sum(f.count for f in k):,.0f} | {100 * m:+.2f} ± {100 * se:.2f} | {total:+,.0f} |")
    out += ["", "## By lead (all fills)", "", "| Lead | Contracts | v1 PnL | PnL keeping model edge >= -5c |", "|---|---|---|---|"]
    for lead in LEADS:
        sel = [f for f in fills if f.lead == lead]
        out.append(f"| {lead} | {sum(f.count for f in sel):,.0f} | {100 * weighted(sel)[0]:+.2f} | "
                   f"{100 * weighted(kept(sel, 0.05))[0]:+.2f} |")  # fmt: skip
    return "\n".join(out) + "\n"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--series", default=",".join(SERIES))
    p.add_argument("--out", default="research/maker_rules.md")
    args = p.parse_args()
    fills = []
    for series in args.series.split(","):
        part = fills_with_model(series)
        print(f"{series}: {len(part)} fills with a model price", flush=True)
        fills += part
    Path(args.out).write_text(report(fills))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
