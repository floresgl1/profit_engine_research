"""The calibration screen (calibration_screen.py) on Polymarket.

Same question: when a contract traded at p 24 hours after the market was
created, did it win
about p of the time, and does blindly buying either side in a price band
beat fees? Differences from the Kalshi screen, forced by the public data:

- Price history (CLOB GET /prices-history) is one price per step, no bid or
  ask, and the docs don't say whether it is a midpoint or a last trade. The
  strategy columns therefore pay that price plus the fee but NO spread: a
  flag here is necessary, not sufficient. Compare any edge to a spread of a
  cent or more before believing it.
- Fees: today's taker schedule, rate x P x (1-P) by category (docs, 2026-10):
  crypto 0.07, sports 0.05, finance/politics/tech/mentions 0.04,
  economics/culture/weather/other 0.05, geopolitics 0. When fees started is
  undocumented, so older markets may have traded fee-free; using today's
  rates is the conservative side.
- No series: recurring markets (e.g. 15-minute crypto up/down) would swamp a
  uniform sample, so the sample takes one market per event and at most
  PER_CATEGORY_DAY per category per decision date; errors are clustered by
  category and decision date.
- The "YES" side is the market's first outcome (for team-vs-team markets,
  the first listed team).

Data (cached in data/polymarket_screen/): closed markets with an end date in
the screen period and volume >= MIN_VOLUME from Gamma GET /markets/keyset,
resolved cleanly to 1/0; price of the first outcome's token 24 h after the
market was created (a time that can't depend on the outcome; close times do).
Markets already closed by then are skipped.

Run: uv run python research/polymarket_screen.py --out research/polymarket_screen.md
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import random
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from calibration_screen import BANDS, Row, band_of, cell, flagged, is_holdout, table

START = datetime(2025, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 1, tzinfo=timezone.utc)
DECISION_DELAY = timedelta(hours=24)  # after creation; see calibration_screen.py
LOOKBACK = timedelta(hours=6)
MIN_VOLUME = 1000  # USDC
PER_CATEGORY_DAY = 5
CACHE = "data/polymarket_screen"

# Fee category by tag label, first match wins (geopolitics first: it is fee-free).
FEE_RATES = [
    ("Geopolitics", Decimal("0")),
    ("Crypto", Decimal("0.07")),
    ("Sports", Decimal("0.05")),
    ("Mentions", Decimal("0.04")),
    ("Politics", Decimal("0.04")),
    ("Finance", Decimal("0.04")),
    ("Tech", Decimal("0.04")),
    ("Economy", Decimal("0.05")),
    ("Weather", Decimal("0.05")),
    ("Culture", Decimal("0.05")),
    ("Pop Culture", Decimal("0.05")),
]
OTHER_RATE = Decimal("0.05")


def category_of(tag_labels: list[str]) -> tuple[str, Decimal]:
    labels = set(tag_labels)
    for name, rate in FEE_RATES:
        if name in labels:
            return ("Culture" if name == "Pop Culture" else name), rate
    return "Other", OTHER_RATE


def parse_time(s: str) -> datetime:
    """Gamma closedTime looks like '2025-04-19 05:45:53+00'."""
    s = s.replace(" ", "T")
    if s.endswith("+00"):
        s += ":00"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Closed:
    market_id: str
    event_id: str
    token: str  # first outcome's CLOB token
    created_at: datetime
    closed_at: datetime
    result: int  # 1 if the first outcome won
    category: str
    fee_rate: Decimal


def parse_market(m: dict) -> Closed | None:
    try:
        prices = [Decimal(p) for p in json.loads(m.get("outcomePrices") or "[]")]
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        if len(prices) != 2 or len(tokens) != 2 or sorted(prices) != [0, 1] or not m.get("closedTime") or not m.get("createdAt"):
            return None  # not binary, or not cleanly resolved (e.g. 50-50, cancelled)
        events = m.get("events") or [{}]
        category, rate = category_of([t.get("label", "") for t in m.get("tags") or []])
        return Closed(str(m["id"]), str(events[0].get("id", m["id"])), tokens[0], parse_time(m["createdAt"]),
                      parse_time(m["closedTime"]),
                      int(prices[0]), category, rate)  # fmt: skip
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
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


def list_closed(gamma) -> list[Closed]:
    state = _load("listing_v2", {"markets": {}, "cursor": None, "done": False})
    params = {"closed": "true", "limit": "100", "include_tag": "true", "volume_num_min": str(MIN_VOLUME),
              "end_date_min": START.strftime("%Y-%m-%dT%H:%M:%SZ"), "end_date_max": END.strftime("%Y-%m-%dT%H:%M:%SZ")}  # fmt: skip
    n = 0
    while not state["done"]:
        page = dict(params, **({"after_cursor": state["cursor"]} if state["cursor"] else {}))
        data = gamma.get_json("/markets/keyset", page)
        for m in data.get("markets") or []:
            c = parse_market(m)
            if c and START <= c.closed_at < END:
                state["markets"][c.market_id] = c
        state["cursor"] = data.get("next_cursor") or None
        state["done"] = not state["cursor"] or not data.get("markets")
        n += 1
        if n % 100 == 0 or state["done"]:
            _save("listing_v2", state)
            print(f"  listing: {n} pages, {len(state['markets'])} markets", flush=True)
    return list(state["markets"].values())


def decision_time(m: Closed) -> datetime:
    return m.created_at + DECISION_DELAY


def sample(markets: list[Closed], per_category_day: int = PER_CATEGORY_DAY, seed: int = 7) -> list[Closed]:
    rng = random.Random(seed)
    by_event: dict[str, list[Closed]] = defaultdict(list)
    for m in markets:
        if m.closed_at - m.created_at >= DECISION_DELAY + timedelta(hours=1):
            by_event[m.event_id].append(m)
    one_each = [rng.choice(sorted(group, key=lambda m: m.market_id)) for _, group in sorted(by_event.items())]
    by_cell: dict[tuple[str, object], list[Closed]] = defaultdict(list)
    for m in one_each:
        by_cell[(m.category, decision_time(m).date())].append(m)
    out = []
    for key in sorted(by_cell, key=str):
        group = by_cell[key]
        out += rng.sample(group, min(per_category_day, len(group)))
    return out


def fetch_prices(picks: list[Closed], workers: int = 4) -> dict[str, Decimal | None]:
    """Price of each pick's first-outcome token at its decision time (None if no point within LOOKBACK)."""
    from profit_engine.venues.http import ReadOnlyHttp
    from profit_engine.venues.polymarket import CLOB_URL

    prices: dict[str, Decimal | None] = _load("prices_open", {})
    todo = [m for m in picks if m.market_id not in prices]
    local = threading.local()

    def one(m: Closed) -> tuple[str, Decimal | None]:
        if not hasattr(local, "http"):
            local.http = ReadOnlyHttp(CLOB_URL, min_interval=0.1)
        at = int(decision_time(m).timestamp())
        hist = local.http.get_json("/prices-history", {"market": m.token, "startTs": str(at - int(LOOKBACK.total_seconds())),
                                                       "endTs": str(at), "fidelity": "60"}).get("history") or []  # fmt: skip
        points = [h for h in hist if h["t"] <= at]
        return m.market_id, (Decimal(str(max(points, key=lambda h: h["t"])["p"])) if points else None)

    with ThreadPoolExecutor(workers) as pool:
        for i, (mid, price) in enumerate(pool.map(one, todo)):
            prices[mid] = price
            if i % 500 == 499:
                _save("prices_open", prices)
                print(f"  prices: {i + 1} of {len(todo)}", flush=True)
    _save("prices_open", prices)
    return prices


def build_rows(picks: list[Closed], prices: dict) -> tuple[list[Row], dict]:
    rows, dropped = [], defaultdict(int)
    for m in picks:
        p = prices.get(m.market_id)
        if p is None:
            dropped["no price"] += 1
        elif not (0 < p < 1):
            dropped["price at 0 or 1"] += 1
        else:
            cluster = f"{m.category}|{decision_time(m).date()}"
            rows.append(Row(m.market_id, m.event_id, cluster, m.category, is_holdout(m.event_id), p, p, m.result, m.fee_rate))
    return rows, dict(dropped)


def report(rows: list[Row], dropped: dict, n_listed: int, n_sampled: int) -> str:
    disc = [r for r in rows if not r.holdout]
    hold = [r for r in rows if r.holdout]
    out = ["# Polymarket calibration screen", ""]
    out.append(f"Closed binary markets, end date {START:%Y-%m-%d} to {END:%Y-%m-%d}, volume >= {MIN_VOLUME} USDC: "
               f"{n_listed} listed, {n_sampled} sampled (one per event, up to {PER_CATEGORY_DAY} per category per day), "
               f"{len(rows)} with a price 24 h after creation. Dropped: {dropped}.")  # fmt: skip
    out.append(f"Discovery and holdout: disjoint halves of events by id hash ({len(disc)} / {len(hold)} markets).")
    out.append("**No spread in the strategy columns** (the price history has no bid/ask): they pay the price plus "
               "today's taker fee only. Spread column is 0 by construction. SE clustered by category and decision date, "
               "floored at the binomial SE if prices were right. Flag = profitable by more than 2 SE (n >= 30).")
    out.append("")
    out += ["## All categories", ""] + table(disc, "Discovery") + table(hold, "Holdout")
    counts = defaultdict(int)
    for r in disc:
        counts[r.category] += 1
    out += ["## By category (discovery)", ""]
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
            if not d or not (flag := flagged(c := cell(d))):
                continue
            h = [r for r in h_rows if band_of(r.mid) == band]
            hc = cell(h) if h else None
            pick = (lambda x: (x.buy_yes, x.buy_yes_se)) if flag == "buy YES" else (lambda x: (x.buy_no, x.buy_no_se))
            hv = f"{pick(hc)[0]:+.4f} ± {pick(hc)[1]:.4f}" if hc else "-"
            out.append(f"| {cat} | {band[0]}-{band[1]}c | {flag} | {pick(c)[0]:+.4f} ± {pick(c)[1]:.4f} | {hv} | {hc.n if hc else 0} |")
    return "\n".join(out) + "\n"


def main() -> None:
    from profit_engine.venues.http import ReadOnlyHttp
    from profit_engine.venues.polymarket import GAMMA_URL

    p = argparse.ArgumentParser()
    p.add_argument("--out", default="research/polymarket_screen.md")
    args = p.parse_args()
    markets = list_closed(ReadOnlyHttp(GAMMA_URL, min_interval=0.2))
    print(f"{len(markets)} closed binary markets listed", flush=True)
    picks = sample(markets)
    print(f"{len(picks)} sampled", flush=True)
    prices = fetch_prices(picks)
    rows, dropped = build_rows(picks, prices)
    Path(args.out).write_text(report(rows, dropped, len(markets), len(picks)))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
