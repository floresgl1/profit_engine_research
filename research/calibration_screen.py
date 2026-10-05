"""Screen all of Kalshi for systematic mispricing, by category.

For a sample of settled binary markets, take the best YES bid/ask 24 hours
after the market opened and ask: when a contract was priced at p, did it win about p
of the time? And would blindly buying YES at the ask, or NO at (1 - bid),
in a price band have made money after fees? A category whose prices are
systematically off (e.g. the favourite-longshot bias: cheap contracts win
less often than their price says) is an edge without any model.

Data (read-only, cached in data/screen/):
1. The newest page (up to 1,000) of settled markets of every series from
   GET /historical/markets, closing Jan 2025 to the archive cutoff. Busy
   series therefore contribute only their latest weeks or hours.
2. Series categories and fee multipliers from GET /series.
3. Sample: markets with volume >= MIN_VOLUME that opened at least a day
   before close, up to PER_SERIES per series (seeded random).
4. Hourly candles up to open + 24h; the last candle that closed at or before
   that time (within LOOKBACK) gives bid/ask. Markets without both sides are dropped.

Statistics are clustered by series and decision date: buckets of one event
share an outcome, and a series' events on one day often share a driver
(e.g. hourly Bitcoin ladders), so markets are not independent. Events are
split into disjoint discovery and holdout halves by ticker hash.

Run: uv run python research/calibration_screen.py --out research/calibration_screen.md
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import pickle
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from profit_engine.weather.kalshi_temps import Quote, quote_at

START = datetime(2025, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 8, 4, tzinfo=timezone.utc)  # archive cutoff: the archive is listable per series
# Decision time = open + DECISION_DELAY. It must not depend on the outcome: a
# close-based time does (markets that resolve early tend to resolve YES), which
# biased the first version of this screen.
DECISION_DELAY = timedelta(hours=24)
LOOKBACK = timedelta(hours=6)
MIN_VOLUME = Decimal(500)
MAX_SPREAD = Decimal("0.10")  # wider and the midpoint is not a price anyone is offering
PER_SERIES = 3
SAMPLE_DRAW = 6  # draw this many per series, keep the first PER_SERIES (smaller samples nest in larger)
BASE_FEE = Decimal("0.07")
CACHE = "data/screen"
BANDS = [(0, 5), (5, 10), (10, 20), (20, 35), (35, 50), (50, 65), (65, 80), (80, 90), (90, 95), (95, 100)]


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def series_of(event_ticker: str) -> str:
    return event_ticker.split("-", 1)[0]


def is_holdout(event_ticker: str) -> bool:
    """Stable half of all events, by hash: discovery and holdout are disjoint sets of events."""
    return int(hashlib.sha256(event_ticker.encode()).hexdigest()[:8], 16) % 2 == 1


def fee(price: Decimal, rate: Decimal, contracts: int = 100) -> Decimal:
    """Taker fee per contract at `rate` x P x (1-P), each fill rounded up to 1e-6."""
    total = (rate * contracts * price * (1 - price)).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
    return total / contracts


# --- data ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Settled:
    ticker: str
    event: str
    open_time: datetime
    close_time: datetime
    result: int
    volume: Decimal


def parse_market(m: dict) -> Settled | None:
    if m.get("market_type") != "binary" or m.get("result") not in ("yes", "no"):
        return None
    try:
        return Settled(
            m["ticker"], m["event_ticker"], ts(m["open_time"]), ts(m["close_time"]),
            1 if m["result"] == "yes" else 0, Decimal(m.get("volume_fp") or "0"),
        )  # fmt: skip
    except (KeyError, ValueError):
        return None


def _load(name: str, default):
    path = f"{CACHE}/{name}.pkl"
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return pickle.load(fh)
    return default


def _save(name: str, data) -> None:
    os.makedirs(CACHE, exist_ok=True)
    tmp = f"{CACHE}/{name}.pkl.tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(data, fh)
    os.replace(tmp, f"{CACHE}/{name}.pkl")


def list_settled(http, series: dict[str, dict]) -> list[Settled]:
    """Settled binary markets closing in [START, END): the newest archive page of every series.

    Listing everything is not feasible (about 77,000 markets settle per day),
    so busy series contribute only their most recent weeks or hours.
    """
    state = _load("listing", {"markets": {}, "done": set()})
    markets: dict[str, Settled] = state["markets"]
    todo = [s for s in sorted(series) if s not in state["done"]]
    for i, name in enumerate(todo):
        raw = http.get_json("/historical/markets", {"series_ticker": name, "limit": "1000"}).get("markets") or []
        for m in raw:
            settled = parse_market(m)
            if settled and START <= settled.close_time < END:
                markets[settled.ticker] = settled
        state["done"].add(name)
        if i % 500 == 499:
            _save("listing", state)
            print(f"  listing: {i + 1} of {len(todo)} series, {len(markets)} markets", flush=True)
    _save("listing", state)
    return list(markets.values())


def load_series(http) -> dict[str, dict]:
    series = _load("series", None)
    if series is None:
        raw = http.get_json("/series", {}).get("series") or []
        series = {s["ticker"]: {"category": s.get("category") or "?", "fee_type": s.get("fee_type"),
                                "fee_multiplier": s.get("fee_multiplier")} for s in raw}  # fmt: skip
        _save("series", series)
    return series


def sample(markets: list[Settled], per_series: int = PER_SERIES, seed: int = 7) -> list[Settled]:
    by_series: dict[str, list[Settled]] = defaultdict(list)
    for m in markets:
        if m.volume >= MIN_VOLUME and m.close_time - m.open_time >= DECISION_DELAY + timedelta(hours=1):
            by_series[series_of(m.event)].append(m)
    rng = random.Random(seed)
    out = []
    for name in sorted(by_series):
        group = sorted(by_series[name], key=lambda m: m.ticker)
        out += rng.sample(group, min(SAMPLE_DRAW, len(group)))[:per_series]
    return out


def decision_time(m: Settled) -> datetime:
    return m.open_time + DECISION_DELAY


def fetch_quotes(http, picks: list[Settled]) -> dict[str, Quote | None]:
    """Quote at open + DECISION_DELAY per market (None if no candle within LOOKBACK)."""
    from profit_engine.weather.kalshi_temps import price_history

    quotes: dict[str, Quote | None] = _load("quotes_open", {})
    todo = [m for m in picks if m.ticker not in quotes]
    for i, m in enumerate(todo):
        at = decision_time(m)
        hist = price_history(http, [m.ticker], at - LOOKBACK, at + timedelta(minutes=1), historical=True)
        q = quote_at(hist.get(m.ticker, []), at)
        quotes[m.ticker] = q if q and q.at >= at - LOOKBACK else None
        if i % 200 == 199:
            _save("quotes_open", quotes)
            print(f"  quotes: {i + 1} of {len(todo)}", flush=True)
    _save("quotes_open", quotes)
    return quotes


# --- analysis --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    ticker: str
    event: str
    cluster: str  # series + close date: markets that share an outcome driver
    category: str
    holdout: bool
    bid: Decimal
    ask: Decimal
    result: int
    fee_rate: Decimal  # taker fee = fee_rate x P x (1-P) per contract

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def buy_yes(self) -> Decimal:
        """Per-contract PnL of buying YES at the ask, after fee."""
        return self.result - self.ask - fee(self.ask, self.fee_rate)

    @property
    def buy_no(self) -> Decimal:
        """Per-contract PnL of buying NO at 1 - bid, after fee."""
        price = 1 - self.bid
        return (1 - self.result) - price - fee(price, self.fee_rate)


def build_rows(picks: list[Settled], quotes: dict, series: dict) -> tuple[list[Row], dict]:
    rows, dropped = [], defaultdict(int)
    for m in picks:
        q = quotes.get(m.ticker)
        info = series.get(series_of(m.event))
        if q is None:
            dropped["no quote"] += 1
        elif not (0 < q.bid < q.ask < 1):
            dropped["one-sided or crossed"] += 1
        elif q.ask - q.bid > MAX_SPREAD:
            dropped["spread over 10c"] += 1
        elif info is None or info["fee_type"] not in ("quadratic", "quadratic_with_maker_fees") or info["fee_multiplier"] is None:
            dropped["fee not modelled"] += 1
        else:
            cluster = f"{series_of(m.event)}|{decision_time(m).date()}"
            rows.append(Row(m.ticker, m.event, cluster, info["category"], is_holdout(m.event), q.bid, q.ask, m.result,
                            BASE_FEE * Decimal(str(info["fee_multiplier"]))))  # fmt: skip
    return rows, dict(dropped)


def clustered_mean(values: list[float], clusters: list[str]) -> tuple[float, float]:
    """Mean and its standard error, clustered by `clusters` (CR0 sandwich)."""
    n = len(values)
    if n == 0:
        return float("nan"), float("nan")
    mean = sum(values) / n
    sums: dict[str, float] = defaultdict(float)
    for v, c in zip(values, clusters, strict=True):
        sums[c] += v - mean
    g = len(sums)
    if g < 2:
        return mean, float("nan")
    var = sum(s * s for s in sums.values()) / (n * n) * g / (g - 1)
    return mean, math.sqrt(var)


def band_of(mid: Decimal) -> tuple[int, int]:
    cents = mid * 100
    for lo, hi in BANDS:
        if lo <= cents < hi:
            return lo, hi
    return BANDS[-1]


@dataclass
class Cell:
    n: int
    events: int
    mid: float
    spread: float  # median ask - bid
    yes_rate: float
    yes_se: float
    buy_yes: float
    buy_yes_se: float
    buy_no: float
    buy_no_se: float


def cell(rows: list[Row]) -> Cell:
    ev = [r.cluster for r in rows]
    yes, yes_se = clustered_mean([float(r.result) for r in rows], ev)
    by, by_se = clustered_mean([float(r.buy_yes) for r in rows], ev)
    bn, bn_se = clustered_mean([float(r.buy_no) for r in rows], ev)
    mid = sum(float(r.mid) for r in rows) / len(rows) if rows else float("nan")
    # A cell where every outcome agreed has a clustered SE of 0, which is not certainty.
    # Floor every SE at the binomial SE expected if prices were right, over clusters.
    g = len(set(ev))
    floor = math.sqrt(max(mid * (1 - mid), 1e-6) / g) if g else float("nan")
    yes_se, by_se, bn_se = (max(x, floor) for x in (yes_se, by_se, bn_se))
    spreads = sorted(float(r.ask - r.bid) for r in rows)
    spread = spreads[len(spreads) // 2] if spreads else float("nan")
    return Cell(len(rows), g, mid, spread, yes, yes_se, by, by_se, bn, bn_se)


def flagged(c: Cell, z: float = 2.0) -> str:
    """Which blind strategy, if any, is profitable by more than z standard errors."""
    if c.n >= 30 and c.buy_yes - z * c.buy_yes_se > 0:
        return "buy YES"
    if c.n >= 30 and c.buy_no - z * c.buy_no_se > 0:
        return "buy NO"
    return ""


# --- report ----------------------------------------------------------------------------------


def table(rows: list[Row], title: str) -> list[str]:
    out = [f"### {title}", "", "| Price band | Markets | Clusters | Mean mid | Median spread | Won | Buy YES at ask | Buy NO at 1-bid | Flag |",
           "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for lo, hi in BANDS:
        sel = [r for r in rows if band_of(r.mid) == (lo, hi)]
        if not sel:
            continue
        c = cell(sel)
        out.append(
            f"| {lo}-{hi}c | {c.n} | {c.events} | {c.mid:.3f} | {c.spread:.3f} | {c.yes_rate:.3f} ± {c.yes_se:.3f} | "
            f"{c.buy_yes:+.4f} ± {c.buy_yes_se:.4f} | {c.buy_no:+.4f} ± {c.buy_no_se:.4f} | {flagged(c)} |"
        )
    return out + [""]


def report(rows: list[Row], dropped: dict, n_listed: int, n_sampled: int) -> str:
    disc = [r for r in rows if not r.holdout]
    hold = [r for r in rows if r.holdout]
    out = ["# Kalshi calibration screen", ""]
    out.append(f"Settled binary markets closing {START:%Y-%m-%d} to {END:%Y-%m-%d}: {n_listed} listed, "
               f"{n_sampled} sampled (volume >= {MIN_VOLUME}, up to {PER_SERIES} per series), {len(rows)} with a two-sided "
               f"quote {DECISION_DELAY.total_seconds() / 3600:.0f} h after open. Dropped: {dropped}.")  # fmt: skip
    out.append(f"Discovery and holdout: disjoint halves of events by ticker hash ({len(disc)} / {len(hold)} markets).")
    out.append("Won = realized YES rate. Strategy columns: mean PnL per contract after fees ± SE clustered by "
               "series and decision date, floored at the binomial SE if prices were right. Flag = profitable by more "
               "than 2 SE (n >= 30). Only markets quoted on both sides, at most 10c apart, 24 h after open are scored.")
    out.append("")
    out += ["## All categories", ""] + table(disc, "Discovery") + table(hold, "Holdout")
    out += ["## By category (discovery)", ""]
    counts = defaultdict(int)
    for r in disc:
        counts[r.category] += 1
    for cat in sorted(counts, key=lambda c: -counts[c]):
        if counts[cat] >= 100:
            out += table([r for r in disc if r.category == cat], f"{cat} ({counts[cat]} markets)")
    out += ["## Flagged cells, rechecked on the holdout", ""]
    out += ["| Category | Band | Strategy | Discovery PnL | Holdout PnL | Holdout markets |", "|---|---|---|---|---|---|"]
    for cat in ["All", *sorted(counts)]:
        d_rows = disc if cat == "All" else [r for r in disc if r.category == cat]
        h_rows = hold if cat == "All" else [r for r in hold if r.category == cat]
        for band in BANDS:
            d = [r for r in d_rows if band_of(r.mid) == band]
            if not d:
                continue
            c = cell(d)
            flag = flagged(c)
            if not flag:
                continue
            h = [r for r in h_rows if band_of(r.mid) == band]
            hc = cell(h) if h else None
            pick = (lambda x: (x.buy_yes, x.buy_yes_se)) if flag == "buy YES" else (lambda x: (x.buy_no, x.buy_no_se))
            dv, ds = pick(c)
            hv = f"{pick(hc)[0]:+.4f} ± {pick(hc)[1]:.4f}" if hc else "-"
            out.append(f"| {cat} | {band[0]}-{band[1]}c | {flag} | {dv:+.4f} ± {ds:.4f} | {hv} | {hc.n if hc else 0} |")
    return "\n".join(out) + "\n"


def main() -> None:
    from profit_engine.venues.http import ReadOnlyHttp
    from profit_engine.venues.kalshi import BASE_URL as KALSHI_URL

    p = argparse.ArgumentParser()
    p.add_argument("--out", default="research/calibration_screen.md")
    args = p.parse_args()
    http = ReadOnlyHttp(KALSHI_URL, min_interval=0.3)
    series = load_series(http)
    markets = list_settled(http, series)
    print(f"{len(markets)} settled binary markets listed", flush=True)
    picks = sample(markets)
    print(f"{len(picks)} sampled", flush=True)
    quotes = fetch_quotes(http, picks)
    rows, dropped = build_rows(picks, quotes, series)
    Path(args.out).write_text(report(rows, dropped, len(markets), len(picks)))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
