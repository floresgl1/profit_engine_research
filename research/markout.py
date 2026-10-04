"""Would a market maker make money in the Kalshi daily-high markets?

For every public trade, the maker was on the other side of the taker. If we
had been that maker, what would we have earned per contract?

- entry edge: how far inside the hourly midpoint just before the trade the
  maker was filled (the half-spread actually captured);
- 1 h markout: maker PnL marked to the first hourly midpoint at least an hour
  after the trade (mid, not next trade, so bid/ask bounce can't flatter it);
- settlement: maker PnL holding to the outcome.

settlement = entry edge + drift of the mid against the maker (adverse
selection). A maker earns the spread only if informed takers don't take it
all back.

Maker fees: Kalshi's docs don't state what makers pay on these series, so
every PnL is shown with no maker fee and with 0.0175 x P x (1-P) per contract.

Caveat: this is the average over all fills that happened. A new maker joins
the back of the queue and is more likely to be filled when wrong, so treat
a positive result as an upper bound, not a forecast.

Data: N_EVENTS test-period events per city (evenly spaced), every non-block
trade from GET /historical/trades or /markets/trades (public), hourly mids
from the backtest and structural caches. Cached in data/markout/.

Run: uv run python research/markout.py --out research/markout.md
"""

from __future__ import annotations

import argparse
import math
import os
import pickle
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from calibration_screen import clustered_mean  # noqa: E402
from structural_edges import SERIES, TEST_END, TEST_START, base_cache  # noqa: E402

from profit_engine.weather.kalshi_temps import Quote  # noqa: E402
from profit_engine.weather.stations import CITIES  # noqa: E402

N_EVENTS = 40
HISTORICAL_CUTOFF = date(2026, 8, 4)
MAKER_FEE = Decimal("0.0175")
CACHE = "data/markout"


@dataclass(frozen=True)
class Trade:
    at: datetime
    yes_price: Decimal
    count: Decimal
    taker_yes: bool  # taker bought YES (so the maker sold YES)


def parse_trade(raw: dict) -> Trade | None:
    if raw.get("is_block_trade"):
        return None
    side = raw.get("taker_outcome_side") or raw.get("taker_side")
    if side not in ("yes", "no"):
        return None
    try:
        return Trade(
            datetime.fromisoformat(raw["created_time"].replace("Z", "+00:00")),
            Decimal(raw["yes_price_dollars"]), Decimal(raw["count_fp"]), side == "yes",
        )  # fmt: skip
    except (KeyError, ValueError, ArithmeticError):
        return None


def fetch_trades(http, ticker: str, historical: bool) -> list[Trade]:
    from profit_engine.venues.http import VenueHttpError

    path = "/historical/trades" if historical else "/markets/trades"
    out, cursor = [], None
    while True:
        params = {"ticker": ticker, "limit": "1000", **({"cursor": cursor} if cursor else {})}
        try:
            data = http.get_json(path, params)
        except VenueHttpError as exc:
            if historical and exc.status == 404:
                return fetch_trades(http, ticker, historical=False)  # settled after the archive cutoff
            raise
        out += [t for t in (parse_trade(r) for r in data.get("trades") or []) if t]
        cursor = data.get("cursor") or None
        if not cursor or not data.get("trades"):
            break
    if historical and not out:
        return fetch_trades(http, ticker, historical=False)
    return sorted(out, key=lambda t: t.at)


# --- maker PnL ---------------------------------------------------------------------------------


def maker_sign(t: Trade) -> int:
    """+1 if the maker ended up long YES, -1 if short YES."""
    return -1 if t.taker_yes else 1


def maker_fee(price: Decimal) -> Decimal:
    return MAKER_FEE * price * (1 - price)


def mid_at_or_before(mids: list[Quote], at: datetime) -> Decimal | None:
    best = None
    for q in mids:
        if q.at > at:
            break
        if q.midpoint is not None:
            best = q.midpoint
    return best


def mid_after(mids: list[Quote], at: datetime) -> Decimal | None:
    for q in mids:
        if q.at >= at and q.midpoint is not None:
            return q.midpoint
    return None


@dataclass(frozen=True)
class Fill:
    series: str
    event: str
    local_hour: int
    day_offset: int  # trade day relative to the event day (-1 = day before)
    price: Decimal
    count: Decimal
    entry: float | None  # maker edge vs mid before
    markout_1h: float | None
    settle: float
    settle_fee: float  # with the conservative maker fee

    @property
    def session(self) -> str:
        if self.day_offset < 0:
            return "day before"
        if self.local_hour < 10:
            return "D 00-10"
        if self.local_hour < 14:
            return "D 10-14"
        if self.local_hour < 18:
            return "D 14-18"
        return "D 18-close"


def fills_for(series: str, event, trades: dict[str, list[Trade]], mids: dict[str, list[Quote]]) -> list[Fill]:
    from datetime import timedelta

    zone = CITIES[series].zone
    out = []
    for b in event.buckets:
        outcome = Decimal(1 if b.contains(event.value) else 0)
        qs = mids.get(b.ticker, [])
        for t in trades.get(b.ticker, []):
            s = maker_sign(t)
            before = mid_at_or_before(qs, t.at)
            later = mid_after(qs, t.at + timedelta(hours=1))
            local = t.at.astimezone(zone)
            settle = s * (outcome - t.yes_price)
            out.append(Fill(
                series, event.event_ticker, local.hour, (local.date() - event.day).days, t.yes_price, t.count,
                float(s * (before - t.yes_price)) if before is not None else None,
                float(s * (later - t.yes_price)) if later is not None else None,
                float(settle), float(settle - maker_fee(t.yes_price)),
            ))  # fmt: skip
    return out


# --- data --------------------------------------------------------------------------------------


def _load(path: str, default):
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return pickle.load(fh)
    return default


def _save(data, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(data, fh)
    os.replace(tmp, path)


def pick_events(settled: list, n: int = N_EVENTS) -> list:
    events = sorted((s for s in settled if TEST_START <= s.day <= TEST_END and s.buckets and s.value is not None),
                    key=lambda s: s.day)  # fmt: skip
    if len(events) <= n:
        return events
    step = len(events) / n
    return [events[int(i * step)] for i in range(n)]


def load(series: str) -> tuple[list, dict, dict]:
    from profit_engine.venues.http import ReadOnlyHttp
    from profit_engine.venues.kalshi import BASE_URL as KALSHI_URL

    with open(base_cache(series), "rb") as fh:
        base = pickle.load(fh)
    late = _load(f"data/structural/{series}.pkl", {})
    mids = {}
    for ticker in set(base["quotes"]) | set(late):
        merged = {q.at: q for q in base["quotes"].get(ticker, []) + late.get(ticker, [])}
        mids[ticker] = [merged[t] for t in sorted(merged)]
    events = pick_events(base["settled"])
    os.makedirs(CACHE, exist_ok=True)
    path = f"{CACHE}/{series}.pkl"
    trades: dict[str, list[Trade]] = _load(path, {})
    http = ReadOnlyHttp(KALSHI_URL, min_interval=0.3)
    for i, event in enumerate(events):
        for b in event.buckets:
            if b.ticker not in trades:
                trades[b.ticker] = fetch_trades(http, b.ticker, historical=event.day < HISTORICAL_CUTOFF)
        if i % 10 == 9:
            _save(trades, path)
            print(f"  {series}: trades for {i + 1} of {len(events)} events", flush=True)
    _save(trades, path)
    return events, trades, mids


# --- report ------------------------------------------------------------------------------------


def weighted(fills: list[Fill], attr: str) -> tuple[float, float, int]:
    """Volume-weighted mean per contract, SE clustered by event (weights folded into values)."""
    rows = [(getattr(f, attr), float(f.count), f.event) for f in fills if getattr(f, attr) is not None]
    if not rows:
        return float("nan"), float("nan"), 0
    w = sum(c for _, c, _ in rows)
    mean_w = len(rows) / w
    values = [v * c * mean_w for v, c, _ in rows]  # mean of these = weighted mean
    m, se = clustered_mean(values, [e for _, _, e in rows])
    return m, se, len(rows)


def fmt(m: float, se: float) -> str:
    return "-" if math.isnan(m) else f"{100 * m:+.2f} ± {100 * se:.2f}"


def table(fills: list[Fill], key, title: str, order=None) -> list[str]:
    groups: dict = defaultdict(list)
    for f in fills:
        groups[key(f)].append(f)
    out = [f"### {title}", "", "| Group | Trades | Contracts | Entry edge | 1 h markout | Settlement | Settlement, maker fee |",
           "|---|---|---|---|---|---|---|"]  # fmt: skip
    for g in order or sorted(groups):
        if g not in groups:
            continue
        sel = groups[g]
        cells = [fmt(*weighted(sel, a)[:2]) for a in ("entry", "markout_1h", "settle", "settle_fee")]
        out.append(f"| {g} | {len(sel)} | {sum(float(f.count) for f in sel):,.0f} | " + " | ".join(cells) + " |")
    return out + [""]


def band(f: Fill) -> str:
    p = float(f.price)
    for lo, hi in [(0, 0.05), (0.05, 0.2), (0.2, 0.5), (0.5, 0.8), (0.8, 0.95), (0.95, 1.01)]:
        if lo <= p < hi:
            return f"{int(lo * 100):02d}-{min(int(hi * 100), 100)}c"
    return "?"


def report(fills: list[Fill], n_events: int) -> str:
    out = ["# Market-maker markouts, Kalshi daily-high markets", ""]
    out.append(f"{n_events} events ({N_EVENTS} per city, evenly spaced {TEST_START} to {TEST_END}), {len(fills):,} non-block "
               f"trades. Maker PnL per contract in cents, volume-weighted, ± SE clustered by event. Entry edge = vs the "
               f"hourly mid before the trade; 1 h markout = vs the first hourly mid an hour or more later; settlement = "
               f"held to the outcome; last column subtracts a maker fee of {MAKER_FEE} x P x (1-P).")  # fmt: skip
    out.append("")
    out += table(fills, lambda f: "All", "All trades")
    out += table(fills, lambda f: f.session, "By session (local time)", ["day before", "D 00-10", "D 10-14", "D 14-18", "D 18-close"])
    out += table(fills, band, "By trade price")
    out += table(fills, lambda f: f.series, "By city")
    return "\n".join(out) + "\n"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--series", default=",".join(SERIES))
    p.add_argument("--out", default="research/markout.md")
    args = p.parse_args()
    fills, n_events = [], 0
    for series in args.series.split(","):
        events, trades, mids = load(series)
        n_events += len(events)
        for event in events:
            fills += fills_for(series, event, trades, mids)
        print(f"{series}: {len(events)} events, {len(fills)} fills so far", flush=True)
    Path(args.out).write_text(report(fills, n_events))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
