"""Screen all of Kalshi for systematic mispricing, by category.

For a sample of settled binary markets, take the best YES bid/ask 24 hours
before close and ask: when a contract was priced at p, did it win about p
of the time? And would blindly buying YES at the ask, or NO at (1 - bid),
in a price band have made money after fees? A category whose prices are
systematically off (e.g. the favourite-longshot bias: cheap contracts win
less often than their price says) is an edge without any model.

Data (read-only, cached in data/screen/):
1. Every settled market closing in the screen period, from GET /markets
   (status=settled) and GET /historical/markets (archived), combos excluded.
2. Series categories and fee multipliers from GET /series.
3. Sample: markets with volume >= MIN_VOLUME that opened at least a day
   before close, up to PER_SERIES per series (seeded random).
4. Hourly candles around close - 24h; the candle that closed at or before
   that time gives bid/ask. Markets without both sides are dropped.

Statistics are clustered by event: buckets of one event share an outcome
(only one wins), so markets are not independent. The first DISCOVERY_MONTHS
are for finding effects, the rest is a holdout to confirm them.

Run: uv run python research/calibration_screen.py --out research/calibration_screen.md
"""

from __future__ import annotations

import argparse
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

START = datetime(2025, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 1, tzinfo=timezone.utc)
SPLIT = datetime(2026, 6, 1, tzinfo=timezone.utc)  # discovery before, holdout after
HORIZON = timedelta(hours=24)
MIN_VOLUME = Decimal(500)
PER_SERIES = 15
BASE_FEE = Decimal("0.07")
CACHE = "data/screen"
BANDS = [(0, 5), (5, 10), (10, 20), (20, 35), (35, 50), (50, 65), (65, 80), (80, 90), (90, 95), (95, 100)]


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def series_of(event_ticker: str) -> str:
    return event_ticker.split("-", 1)[0]


def fee(price: Decimal, multiplier: Decimal, contracts: int = 100) -> Decimal:
    """Taker fee per contract, each fill rounded up to 1e-6 (as in the structural screen)."""
    total = (BASE_FEE * multiplier * contracts * price * (1 - price)).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
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


def list_settled(http) -> list[Settled]:
    """All settled binary markets closing in [START, END), live and archived listings."""
    state = _load("listing", {"markets": {}, "live_done": False, "hist_cursor": None, "hist_done": False})
    markets: dict[str, Settled] = state["markets"]

    def page(path: str, params: dict) -> tuple[list[dict], str | None]:
        data = http.get_json(path, params)
        return data.get("markets") or [], data.get("cursor") or None

    def keep(raw: list[dict]) -> None:
        for m in raw:
            s = parse_market(m)
            if s and START <= s.close_time < END:
                markets[s.ticker] = s

    if not state["live_done"]:
        params = {"status": "settled", "mve_filter": "exclude", "limit": "1000",
                  "min_close_ts": str(int(START.timestamp())), "max_close_ts": str(int(END.timestamp()))}  # fmt: skip
        cursor, n = None, 0
        while True:
            raw, cursor = page("/markets", dict(params, **({"cursor": cursor} if cursor else {})))
            keep(raw)
            n += 1
            if n % 50 == 0:
                print(f"  live listing: {n} pages, {len(markets)} markets", flush=True)
            if not cursor or not raw:
                break
        state["live_done"] = True
        _save("listing", state)
    if not state["hist_done"]:
        cursor, n = state["hist_cursor"], 0
        while True:
            params = {"mve_filter": "exclude", "limit": "1000", **({"cursor": cursor} if cursor else {})}
            raw, cursor = page("/historical/markets", params)
            keep(raw)
            n += 1
            closes = [ts(m["close_time"]) for m in raw if m.get("close_time")]
            if n % 25 == 0:
                state["hist_cursor"] = cursor
                _save("listing", state)
                print(f"  archive listing: {n} pages, {len(markets)} markets, back to {min(closes, default=None)}", flush=True)
            # The archive lists newest first; stop once a whole page closed before START.
            if not cursor or not raw or (closes and max(closes) < START):
                break
        state["hist_done"] = True
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
        if m.volume >= MIN_VOLUME and m.close_time - m.open_time >= HORIZON + timedelta(hours=1):
            by_series[series_of(m.event)].append(m)
    rng = random.Random(seed)
    out = []
    for name in sorted(by_series):
        group = sorted(by_series[name], key=lambda m: m.ticker)
        out += rng.sample(group, min(per_series, len(group)))
    return out


def fetch_quotes(http, picks: list[Settled]) -> dict[str, Quote | None]:
    """Quote at close - HORIZON per market (None if no candle or a one-sided book)."""
    from profit_engine.weather.kalshi_temps import price_history

    quotes: dict[str, Quote | None] = _load("quotes", {})
    todo = [m for m in picks if m.ticker not in quotes]
    for i, m in enumerate(todo):
        at = m.close_time - HORIZON
        hist = price_history(http, [m.ticker], at - timedelta(hours=3), at + timedelta(minutes=1), historical=True)
        q = quote_at(hist.get(m.ticker, []), at)
        quotes[m.ticker] = q if q and q.at >= at - timedelta(hours=3) else None
        if i % 200 == 199:
            _save("quotes", quotes)
            print(f"  quotes: {i + 1} of {len(todo)}", flush=True)
    _save("quotes", quotes)
    return quotes


# --- analysis --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    ticker: str
    event: str
    category: str
    holdout: bool
    bid: Decimal
    ask: Decimal
    result: int
    multiplier: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def buy_yes(self) -> Decimal:
        """Per-contract PnL of buying YES at the ask, after fee."""
        return self.result - self.ask - fee(self.ask, self.multiplier)

    @property
    def buy_no(self) -> Decimal:
        """Per-contract PnL of buying NO at 1 - bid, after fee."""
        price = 1 - self.bid
        return (1 - self.result) - price - fee(price, self.multiplier)


def build_rows(picks: list[Settled], quotes: dict, series: dict) -> tuple[list[Row], dict]:
    rows, dropped = [], defaultdict(int)
    for m in picks:
        q = quotes.get(m.ticker)
        info = series.get(series_of(m.event))
        if q is None:
            dropped["no quote"] += 1
        elif not (0 < q.bid < q.ask < 1):
            dropped["one-sided or crossed"] += 1
        elif info is None or info["fee_type"] not in ("quadratic", "quadratic_with_maker_fees") or info["fee_multiplier"] is None:
            dropped["fee not modelled"] += 1
        else:
            rows.append(Row(m.ticker, m.event, info["category"], m.close_time >= SPLIT, q.bid, q.ask, m.result,
                            Decimal(str(info["fee_multiplier"]))))  # fmt: skip
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
    yes_rate: float
    yes_se: float
    buy_yes: float
    buy_yes_se: float
    buy_no: float
    buy_no_se: float


def cell(rows: list[Row]) -> Cell:
    ev = [r.event for r in rows]
    yes, yes_se = clustered_mean([float(r.result) for r in rows], ev)
    by, by_se = clustered_mean([float(r.buy_yes) for r in rows], ev)
    bn, bn_se = clustered_mean([float(r.buy_no) for r in rows], ev)
    mid = sum(float(r.mid) for r in rows) / len(rows) if rows else float("nan")
    return Cell(len(rows), len(set(ev)), mid, yes, yes_se, by, by_se, bn, bn_se)


def flagged(c: Cell, z: float = 2.0) -> str:
    """Which blind strategy, if any, is profitable by more than z standard errors."""
    if c.n >= 30 and c.buy_yes - z * c.buy_yes_se > 0:
        return "buy YES"
    if c.n >= 30 and c.buy_no - z * c.buy_no_se > 0:
        return "buy NO"
    return ""


# --- report ----------------------------------------------------------------------------------


def table(rows: list[Row], title: str) -> list[str]:
    out = [f"### {title}", "", "| Price band | Markets | Events | Mean mid | Won | Buy YES at ask | Buy NO at 1-bid | Flag |",
           "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for lo, hi in BANDS:
        sel = [r for r in rows if band_of(r.mid) == (lo, hi)]
        if not sel:
            continue
        c = cell(sel)
        out.append(
            f"| {lo}-{hi}c | {c.n} | {c.events} | {c.mid:.3f} | {c.yes_rate:.3f} ± {c.yes_se:.3f} | "
            f"{c.buy_yes:+.4f} ± {c.buy_yes_se:.4f} | {c.buy_no:+.4f} ± {c.buy_no_se:.4f} | {flagged(c)} |"
        )
    return out + [""]


def report(rows: list[Row], dropped: dict, n_listed: int, n_sampled: int) -> str:
    disc = [r for r in rows if not r.holdout]
    hold = [r for r in rows if r.holdout]
    out = ["# Kalshi calibration screen", ""]
    out.append(f"Settled binary markets closing {START:%Y-%m-%d} to {END:%Y-%m-%d}: {n_listed} listed, "
               f"{n_sampled} sampled (volume >= {MIN_VOLUME}, up to {PER_SERIES} per series), {len(rows)} with a two-sided "
               f"quote {HORIZON.total_seconds() / 3600:.0f} h before close. Dropped: {dropped}.")  # fmt: skip
    out.append(f"Discovery: close before {SPLIT:%Y-%m-%d} ({len(disc)} markets); holdout: after ({len(hold)}).")
    out.append("Won = realized YES rate. Strategy columns: mean PnL per contract after fees ± event-clustered SE. "
               "Flag = profitable by more than 2 SE (n >= 30).")
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
    markets = list_settled(http)
    print(f"{len(markets)} settled binary markets listed", flush=True)
    picks = sample(markets)
    print(f"{len(picks)} sampled", flush=True)
    quotes = fetch_quotes(http, picks)
    rows, dropped = build_rows(picks, quotes, series)
    Path(args.out).write_text(report(rows, dropped, len(markets), len(picks)))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
