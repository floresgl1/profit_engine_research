"""Where on Kalshi do makers keep the spread? A maker markout for every category.

The weather markout (markout.py) found incumbent makers keep +0.78c per
contract to settlement (+0.64c after the maker fee), while our newcomer at
the back of the queue lost -0.36c live: about 1c less. This screen asks
whether any category leaves makers enough that a newcomer could plausibly
survive there.

Sample: the calibration screen's markets (calibration_screen.sample: up to
3 settled binary markets per series with >= 500 contracts traded, Jan 2025
to the archive cutoff), read from its cache in data/screen/.

Trades: every public non-block trade from GET /historical/trades, up to CAP
pages of 1,000. Bigger markets are time-sampled instead: one page from each
of WINDOWS evenly spaced windows over the market's life, so the sample isn't
stuck at one end. A page holds the newest 1,000 trades of its window, so the
busiest stretches of a big market are under-sampled (an approximation).

Per trade, the maker took the other side of the taker: PnL per contract to
settlement = maker side x (outcome - price), less the conservative maker fee
0.0175 x P x (1 - P) (Kalshi doesn't state maker fees for most series).

Statistics: per category and half (discovery/holdout by event-ticker hash),
PnL per contract weighted by each market's traded contracts, standard error
clustered by series, 95% t interval.

Registered before any result (docs/decisions.md, 2026-10-20): a category is
a candidate for a paper test only if its discovery-half 95% range is
entirely above +1c per contract after fee AND its holdout-half estimate is
above +1c. Anything less closes the line. Series-level numbers are context.

Run: uv run python research/markout_screen.py --out research/markout_screen.md
"""

from __future__ import annotations

import argparse
import math
import os
import pickle
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import calibration_screen as cs  # noqa: E402
from calibration_screen import Settled  # noqa: E402,F401  (the cached listing was pickled from __main__)

from profit_engine.maker.queue import PublicTrade  # noqa: E402
from profit_engine.maker.report import t95  # noqa: E402
from profit_engine.venues.kalshi.adapter import parse_trade  # noqa: E402

CAP = 20  # pages of 1,000 trades read in full; beyond this a market is time-sampled
WINDOWS = 20
BIG_VOLUME = Decimal(2_000_000)  # contracts: time-sample these directly (they would hit CAP anyway)
MAKER_FEE = Decimal("0.0175")
BAR = 0.01  # dollars per contract after fee (registered)
CACHE = "data/markout_screen"


@dataclass(frozen=True)
class MarketMarkout:
    ticker: str
    series: str
    category: str
    holdout: bool
    volume: float  # contracts traded in the market (listing): its weight
    contracts: float  # contracts in the trades read
    pnl: float  # maker PnL to settlement on those, dollars, no fee
    pnl_fee: float  # the same after the maker fee
    trades: int
    sampled: bool  # time-sampled (big market) rather than every trade

    @property
    def per_contract(self) -> float:
        """Maker PnL per contract after fee, dollars."""
        return self.pnl_fee / self.contracts if self.contracts else 0.0


def maker_pnl(trade: PublicTrade, result: int) -> Decimal:
    """Per contract to settlement for the maker on the other side of `trade`, before fees."""
    # The taker bought YES: the maker sold YES at the price and pays the outcome. Otherwise it bought.
    return trade.yes_price - result if trade.taker_yes else result - trade.yes_price


def maker_fee(price: Decimal) -> Decimal:
    return MAKER_FEE * price * (1 - price)


def summarize(m: Settled, trades: list[PublicTrade], series: str, category: str, sampled: bool) -> MarketMarkout:
    contracts = pnl = pnl_fee = Decimal(0)
    for t in trades:
        p = maker_pnl(t, m.result) * t.count
        contracts += t.count
        pnl += p
        pnl_fee += p - maker_fee(t.yes_price) * t.count
    return MarketMarkout(m.ticker, series, category, cs.is_holdout(m.event), float(m.volume), float(contracts),
                         float(pnl), float(pnl_fee), len(trades), sampled)  # fmt: skip


def windows(open_time: datetime, close_time: datetime, n: int | None = None) -> list[tuple[int, int]]:
    """n (default WINDOWS) contiguous (min_ts, max_ts) windows, whole seconds, covering the market's life."""
    n = n or WINDOWS
    lo, hi = int(open_time.timestamp()), int(close_time.timestamp())
    step = (hi - lo) / n
    return [(lo + round(i * step), lo + round((i + 1) * step)) for i in range(n)]


def _pages(http, params: dict, max_pages: int) -> tuple[list[PublicTrade], bool]:
    """Trades from up to max_pages pages, and whether that was all of them."""
    out: list[PublicTrade] = []
    cursor = ""
    for _ in range(max_pages):
        page = dict(params, limit=1000, **({"cursor": cursor} if cursor else {}))
        data = http.get_json("/historical/trades", page)
        out += [t for raw in data.get("trades", []) if (t := parse_trade(raw))]
        cursor = data.get("cursor") or ""
        if not cursor:
            return out, True
    return out, False


def fetch_market(http, m: Settled) -> tuple[list[PublicTrade], bool]:
    """Every trade of the market, or for a big one a time sample: (trades, sampled)."""
    if m.volume <= BIG_VOLUME:
        trades, complete = _pages(http, {"ticker": m.ticker}, CAP)
        if complete:
            return trades, False
    seen: dict[str, PublicTrade] = {}  # a trade on a window edge can come back twice
    for lo, hi in windows(m.open_time, m.close_time):
        trades, _ = _pages(http, {"ticker": m.ticker, "min_ts": lo, "max_ts": hi}, 1)
        for t in trades:
            seen[t.trade_id] = t
    return list(seen.values()), True


@dataclass(frozen=True)
class Stat:
    mean: float  # dollars per contract after fee
    half: float  # 95% half-width
    series: int
    markets: int
    contracts: float

    @property
    def low(self) -> float:
        return self.mean - self.half


def weighted_stat(rows: list[MarketMarkout]) -> Stat | None:
    """PnL per contract after fee, each market weighted by its traded contracts; SE clustered by series."""
    use = [r for r in rows if r.contracts > 0 and r.volume > 0]
    total = sum(r.volume for r in use)
    if not use or total == 0:
        return None
    mean = sum(r.volume * r.per_contract for r in use) / total
    resid: dict[str, float] = defaultdict(float)
    for r in use:
        resid[r.series] += r.volume * (r.per_contract - mean)
    g = len(resid)
    if g < 2:
        return Stat(mean, float("nan"), g, len(use), total)
    se = math.sqrt(sum(e * e for e in resid.values()) * g / (g - 1)) / total
    return Stat(mean, t95(g - 1) * se, g, len(use), total)


def candidate(discovery: Stat | None, holdout: Stat | None) -> bool:
    """The registered bar: discovery 95% range entirely above +1c, holdout estimate above +1c."""
    return bool(discovery and holdout and discovery.low > BAR and holdout.mean > BAR)


# --- running and reporting -------------------------------------------------------------------------


def _load():
    path = f"{CACHE}/markets.pkl"
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return pickle.load(fh)
    return {}


def _save(done: dict) -> None:
    os.makedirs(CACHE, exist_ok=True)
    tmp = f"{CACHE}/markets.pkl.tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(done, fh)
    os.replace(tmp, f"{CACHE}/markets.pkl")


def run(http, picks: list[Settled], series_info: dict, limit: int | None = None) -> dict[str, MarketMarkout]:
    """Markouts for every sampled market, resuming from the cache. Random order: a partial run is a fair subset."""
    done = _load()
    order = sorted(picks, key=lambda m: m.ticker)
    random.Random(11).shuffle(order)
    todo = [m for m in order if m.ticker not in done][:limit]
    for i, m in enumerate(todo):
        name = cs.series_of(m.event)
        trades, sampled = fetch_market(http, m)
        done[m.ticker] = summarize(m, trades, name, series_info.get(name, {}).get("category") or "?", sampled)
        if i % 100 == 99:
            _save(done)
            print(f"  {len(done)} of {len(picks)} markets", flush=True)
    _save(done)
    return done


def cents(x: float) -> str:
    return f"{100 * x:+.2f}c"


def stat_cell(s: Stat | None) -> str:
    if s is None:
        return "-"
    rng = f" [{cents(s.low)}, {cents(s.mean + s.half)}]" if not math.isnan(s.half) else ""
    return f"{cents(s.mean)}{rng}"


def report(rows: list[MarketMarkout], n_sampled: int) -> str:
    sampled = [r for r in rows if r.sampled]
    lines = [
        "# Maker markout across Kalshi categories",
        "",
        "Generated by `research/markout_screen.py`; method and the registered bar in its docstring and",
        "in `docs/decisions.md` (2026-10-20). Maker PnL per contract to settlement after the",
        "0.0175 x P x (1-P) maker fee, each market weighted by its traded contracts, 95% t interval",
        "clustered by series. Candidate = discovery range entirely above +1c AND holdout estimate above +1c.",
        "",
        f"Markets: {len(rows)} of {n_sampled} sampled; {len(sampled)} big ones time-sampled "
        f"({sum(r.volume for r in sampled) / max(1.0, sum(r.volume for r in rows)):.0%} of contracts).",
        "",
        "| Category | Series | Markets | Contracts | All | Discovery | Holdout | Candidate |",
        "|---|---|---|---|---|---|---|---|",
    ]
    by_cat: dict[str, list[MarketMarkout]] = defaultdict(list)
    for r in rows:
        by_cat[r.category].append(r)
    found = []
    for cat in sorted(by_cat, key=lambda c: -sum(r.volume for r in by_cat[c])):
        group = by_cat[cat]
        a, d = weighted_stat(group), weighted_stat([r for r in group if not r.holdout])
        h = weighted_stat([r for r in group if r.holdout])
        ok = candidate(d, h)
        found += [cat] if ok else []
        lines.append(f"| {cat} | {len({r.series for r in group})} | {len(group)} | {sum(r.volume for r in group):,.0f} "
                     f"| {stat_cell(a)} | {stat_cell(d)} | {stat_cell(h)} | {'**yes**' if ok else 'no'} |")  # fmt: skip
    a = weighted_stat(rows)
    lines.append(f"| **All** | {len({r.series for r in rows})} | {len(rows)} | {sum(r.volume for r in rows):,.0f} "
                 f"| {stat_cell(a)} | {stat_cell(weighted_stat([r for r in rows if not r.holdout]))} "
                 f"| {stat_cell(weighted_stat([r for r in rows if r.holdout]))} | |")  # fmt: skip
    lines += ["", f"**Candidates: {', '.join(found) if found else 'none'}.**", ""]

    by_series: dict[str, list[MarketMarkout]] = defaultdict(list)
    for r in rows:
        by_series[r.series].append(r)
    big = [(name, g) for name, g in by_series.items() if sum(r.volume for r in g) >= 100_000]
    best = sorted(big, key=lambda x: -sum(r.volume * r.per_contract for r in x[1]) / sum(r.volume for r in x[1]))[:15]
    lines += ["## Context only: highest series (100,000+ contracts in the sample, both halves)", "",
              "Up to 3 markets per series: too few to decide anything; shown to see what the categories hold.", "",
              "| Series | Category | Markets | Contracts | Per contract after fee |", "|---|---|---|---|---|"]  # fmt: skip
    for name, g in best:
        vol = sum(r.volume for r in g)
        pc = sum(r.volume * r.per_contract for r in g) / vol
        lines.append(f"| {name} | {g[0].category} | {len(g)} | {vol:,.0f} | {cents(pc)} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    from profit_engine.venues.http import ReadOnlyHttp
    from profit_engine.venues.kalshi import BASE_URL as KALSHI_URL

    p = argparse.ArgumentParser()
    p.add_argument("--out", default="research/markout_screen.md")
    p.add_argument("--limit", type=int, help="fetch at most this many more markets (smoke test)")
    args = p.parse_args()
    listing = cs._load("listing", None)
    series_info = cs._load("series", None)
    if listing is None or series_info is None:
        raise SystemExit("run research/calibration_screen.py first: this screen reuses its sample")
    picks = cs.sample(list(listing["markets"].values()))
    print(f"{len(picks)} sampled markets", flush=True)
    http = ReadOnlyHttp(KALSHI_URL, min_interval=0.3)
    done = run(http, picks, series_info, args.limit)
    rows = [done[m.ticker] for m in picks if m.ticker in done]
    Path(args.out).write_text(report(rows, len(picks)))
    print(f"wrote {args.out} ({len(rows)} markets)")


if __name__ == "__main__":
    main()
