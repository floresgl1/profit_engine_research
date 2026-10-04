"""Model-free edges in Kalshi daily-high events: two checks on hourly prices.

1. Event arbitrage. An event's buckets cover every whole-degree high exactly
   once, so exactly one pays $1. Buying YES on every bucket costs the sum of
   the asks plus fees and pays $1; selling YES on every bucket (buying NO on
   each) costs the sum of (1 - bid) plus fees and pays n - 1. Either is free
   money if its cost is below its payout.
2. Dead buckets. The official high is at least the rounded max reading so
   far plus the station's smallest undercount ever seen in training. A
   bucket entirely below that can't win; if its YES still has a bid, selling
   it (buying NO) earns the bid minus the fee. Likewise a "greater" bucket
   whose floor is below the bound can't lose.

Prices are hourly candle closes (best bid/ask at the close of each hour,
size unknown), so a hit here is a lead to confirm on live order books, not a
tradeable amount. All buckets of an event are compared at the same candle
close. Markets close at the end of the local standard-time day (01:00 local
in daylight time), so every hour checked is a tradeable hour.

Data: test-period events (Aug 2025 to Oct 2026) of all seven cities. Hourly
quotes from the backtest caches (16:00 the day before to 15:00 on the day)
plus the evening up to close, cached in data/structural/<series>.pkl.

Run: uv run python research/structural_edges.py --out research/structural_edges.md
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import pickle
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from profit_engine.models.temperature import UNDERCOUNT_DST, UNDERCOUNT_STANDARD
from profit_engine.weather import is_dst
from profit_engine.weather.iem import drop_spikes
from profit_engine.weather.kalshi_temps import Bucket, Quote
from profit_engine.weather.stations import CITIES, City

FEE_RATE = Decimal("0.07")  # quadratic, multiplier 1 for every KXHIGH series (GET /series, 2026-10-04)
HISTORICAL_CUTOFF = date(2026, 8, 4)
TEST_START, TEST_END = date(2025, 8, 1), date(2026, 10, 1)
SERIES = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHAUS", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHDEN", "KXHIGHPHIL"]


def fee(price: Decimal, contracts: int = 100) -> Decimal:
    """Taker fee per contract for an order of `contracts` (each fill rounded up to 1e-6)."""
    total = (FEE_RATE * contracts * price * (1 - price)).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
    return total / contracts


# --- 1. event arbitrage ------------------------------------------------------------------------


def is_partition(buckets: list[Bucket], lo: int = -60, hi: int = 140) -> bool:
    """True if every whole degree in [lo, hi] falls in exactly one bucket."""
    return all(sum(b.contains(Decimal(h)) for b in buckets) == 1 for h in range(lo, hi + 1))


def buy_all_edge(asks: list[Decimal]) -> Decimal | None:
    """Payout 1 minus the cost of one YES on every bucket; None if any bucket has no ask."""
    if any(a >= 1 or a <= 0 for a in asks):
        return None
    return 1 - sum(a + fee(a) for a in asks)


def sell_all_edge(bids: list[Decimal]) -> Decimal | None:
    """Payout n - 1 minus the cost of one NO on every bucket (NO price = 1 - YES bid)."""
    if any(b <= 0 or b >= 1 for b in bids):
        return None
    return len(bids) - 1 - sum((1 - b) + fee(1 - b) for b in bids)


# --- 2. dead buckets ---------------------------------------------------------------------------


def round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


def lower_bound(max_reading: float, undercount: dict[int, int]) -> int:
    """Smallest official high consistent with the readings: rounded max + smallest undercount seen."""
    return round_half_up(max_reading) + min(undercount)


def is_dead(bucket: Bucket, bound: int) -> bool:
    """No whole degree >= bound is in the bucket."""
    if bucket.strike_type == "greater":
        return False
    return bucket.cap is not None and (bucket.cap <= bound if bucket.strike_type == "less" else bucket.cap < bound)


def is_certain(bucket: Bucket, bound: int) -> bool:
    """Every whole degree >= bound is in the bucket."""
    return bucket.strike_type == "greater" and bucket.floor is not None and bucket.floor < bound


# --- data ------------------------------------------------------------------------------------


def local(day: date, hour: int, city: City) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=city.zone).astimezone(timezone.utc)


def base_cache(series: str) -> str:
    return "data/backtest.pkl" if series == "KXHIGHNY" else f"data/cities/{series}.pkl"


def undercounts(series: str) -> tuple[dict[int, int], dict[int, int]]:
    """The station's undercount from its fitted parameter file (NYC: the model defaults)."""
    path = Path(f"research/params/{series}.json")
    if not path.exists():
        return UNDERCOUNT_DST, UNDERCOUNT_STANDARD
    lead = next(iter(json.loads(path.read_text())["leads"].values()))
    conv = lambda d: {int(k): v for k, v in d.items()}  # noqa: E731
    return conv(lead["undercount_dst"]), conv(lead["undercount_standard"])


def load(series: str) -> dict:
    """Base backtest cache plus evening quotes (15:00 on the day to close), downloaded once."""
    from profit_engine.venues.http import ReadOnlyHttp
    from profit_engine.venues.kalshi import BASE_URL as KALSHI_URL
    from profit_engine.weather.kalshi_temps import price_history

    city = CITIES[series]
    with open(base_cache(series), "rb") as fh:
        base = pickle.load(fh)
    path = f"data/structural/{series}.pkl"
    os.makedirs("data/structural", exist_ok=True)
    late: dict[str, list[Quote]] = {}
    if os.path.exists(path):
        with open(path, "rb") as fh:
            late = pickle.load(fh)
    kalshi = ReadOnlyHttp(KALSHI_URL, min_interval=0.3)
    events = [s for s in base["settled"] if TEST_START <= s.day <= TEST_END and s.buckets]
    for i, event in enumerate(events):
        tickers = [b.ticker for b in event.buckets if b.ticker not in late]
        if not tickers:
            continue
        lo = local(event.day, 14, city)
        hi = local(event.day + timedelta(days=1), 2, city)
        late.update(price_history(kalshi, tickers, lo, hi, historical=event.day < HISTORICAL_CUTOFF))
        if i % 25 == 24:
            _save(late, path)
            print(f"  {series}: evening quotes through {event.day}", flush=True)
    _save(late, path)
    quotes = {}
    for ticker in set(base["quotes"]) | set(late):
        merged = {q.at: q for q in base["quotes"].get(ticker, []) + late.get(ticker, [])}
        quotes[ticker] = [merged[t] for t in sorted(merged)]
    return {"events": events, "obs": drop_spikes(base["obs"]), "quotes": quotes}


def _save(data: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(data, fh)
    os.replace(tmp, path)


# --- analysis --------------------------------------------------------------------------------


@dataclass
class EventHour:
    series: str
    event: str
    at: datetime
    local_hour: int
    buy_edge: Decimal | None
    sell_edge: Decimal | None
    ask_sum: Decimal
    bid_sum: Decimal


@dataclass
class DeadQuote:
    series: str
    ticker: str
    at: datetime
    local_hour: int
    kind: str  # "dead" (sell YES at the bid) or "certain" (buy YES at the ask)
    price: Decimal  # bid for dead, ask for certain
    profit: Decimal  # per contract after fee, if the bound holds
    won: bool  # what actually happened to the YES side
    bound_ok: bool  # settled high >= the bound (False = the undercount bound was wrong)

    @property
    def pnl(self) -> Decimal:
        """Realized per contract: sell YES on a dead bucket / buy YES on a certain one."""
        if self.kind == "dead":
            return self.profit if not self.won else self.profit - 1
        return self.profit if self.won else self.profit - 1


def analyse(series: str, data: dict) -> tuple[list[EventHour], list[DeadQuote], dict]:
    city = CITIES[series]
    dst_u, std_u = undercounts(series)
    obs = sorted(data["obs"], key=lambda o: o.at)
    times_obs = [o.at for o in obs]
    hours: list[EventHour] = []
    dead: list[DeadQuote] = []
    skipped = defaultdict(int)
    for event in data["events"]:
        buckets = list(event.buckets)
        if not is_partition(buckets):
            skipped["not a partition"] += 1
            continue
        if event.value is None:
            skipped["no settlement value"] += 1
            continue
        by_ticker = {b.ticker: {q.at: q for q in data["quotes"].get(b.ticker, [])} for b in buckets}
        times = sorted(set.intersection(*(set(q) for q in by_ticker.values()))) if by_ticker else []
        if not times:
            skipped["no common quote hour"] += 1
            continue
        # Readings that count under both candidate day windows: 01:00 local to midnight local.
        start, end = local(event.day, 1, city), local(event.day + timedelta(days=1), 0, city)
        undercount = dst_u if is_dst(event.day, city.zone) else std_u
        todays = obs[bisect.bisect_left(times_obs, start) : bisect.bisect_right(times_obs, end)]
        for at in times:
            qs = [by_ticker[b.ticker][at] for b in buckets]
            hour = at.astimezone(city.zone).hour
            hours.append(EventHour(
                series, event.event_ticker, at, hour,
                buy_all_edge([q.ask for q in qs]), sell_all_edge([q.bid for q in qs]),
                sum(q.ask for q in qs), sum(q.bid for q in qs),
            ))  # fmt: skip
            readings = [float(o.tmpf) for o in todays if o.at <= at]
            if not readings:
                continue
            bound = lower_bound(max(readings), undercount)
            bound_ok = event.value >= bound
            for b, q in zip(buckets, qs, strict=True):
                won = b.contains(event.value)
                if is_dead(b, bound) and q.bid > 0:
                    no_price = 1 - q.bid
                    dead.append(DeadQuote(series, b.ticker, at, hour, "dead", q.bid, q.bid - fee(no_price), won, bound_ok))
                elif is_certain(b, bound) and q.ask < 1:
                    dead.append(DeadQuote(series, b.ticker, at, hour, "certain", q.ask, 1 - q.ask - fee(q.ask), won, bound_ok))
    return hours, dead, dict(skipped)


# --- report ----------------------------------------------------------------------------------


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "-"


def median(xs: list[Decimal]) -> Decimal:
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else Decimal(0)


def report(results: dict[str, tuple]) -> str:
    out = ["# Structural edges in Kalshi daily-high events", ""]
    out.append(f"Test period {TEST_START} to {TEST_END}, hourly candle closes, fee 0.07 x P x (1-P) per contract.")
    out += ["", "## 1. Event arbitrage", ""]
    out.append("Buy-all edge = 1 - sum(ask + fee); sell-all edge = (n-1) - sum((1-bid) + fee). Positive = free money.")
    out += ["", "| City | Event-hours | Median ask sum | Buy-all > 0 | Best buy-all | Median bid sum | Sell-all > 0 | Best sell-all |",
            "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for series, (hours, _, skipped) in results.items():
        buys = [h.buy_edge for h in hours if h.buy_edge is not None]
        sells = [h.sell_edge for h in hours if h.sell_edge is not None]
        out.append(
            f"| {series} | {len(hours)} | {median([h.ask_sum for h in hours]):.3f} | "
            f"{sum(e > 0 for e in buys)} of {len(buys)} | {max(buys, default=Decimal(0)):+.4f} | "
            f"{median([h.bid_sum for h in hours]):.3f} | {sum(e > 0 for e in sells)} of {len(sells)} | "
            f"{max(sells, default=Decimal(0)):+.4f} |"
        )
    positive = sorted(
        (h for hours, _, _ in results.values() for h in hours if (h.buy_edge or -1) > 0 or (h.sell_edge or -1) > 0),
        key=lambda h: -max(h.buy_edge or -1, h.sell_edge or -1),
    )
    if positive:
        out += ["", "Positive event-hours (largest first, up to 20):", "", "| Event | Local hour | Buy-all | Sell-all |", "|---|---|---|---|"]
        for h in positive[:20]:
            fmt = lambda e: "-" if e is None else f"{e:+.4f}"  # noqa: E731
            out.append(f"| {h.event} | {h.local_hour:02d}:00 | {fmt(h.buy_edge)} | {fmt(h.sell_edge)} |")
    out += ["", "## 2. Dead and certain buckets", ""]
    out.append("Bound = rounded max reading so far (01:00 to midnight local) + the station's smallest training undercount.")
    out.append("Dead: bucket entirely below the bound, still bid. Certain: 'greater' bucket whose floor is below the bound, still offered below $1.")
    out += ["", "| City | Kind | Quotes | Bound wrong | Profit >= 1c | Profit >= 3c | Median price | Realized, summed |", "|---|---|---|---|---|---|---|---|"]
    for series, (_, dead, _) in results.items():
        for kind in ("dead", "certain"):
            qs = [d for d in dead if d.kind == kind]
            out.append(
                f"| {series} | {kind} | {len(qs)} | {sum(not d.bound_ok for d in qs)} | "
                f"{sum(d.profit >= Decimal('0.01') for d in qs)} | {sum(d.profit >= Decimal('0.03') for d in qs)} | "
                f"{median([d.price for d in qs]):.3f} | {sum((d.pnl for d in qs), Decimal(0)):+.2f} |"
            )
    every = [d for _, dead, _ in results.values() for d in dead]
    out += ["", "By local hour, all cities (quotes with profit >= 1c; realized from the settled value):", ""]
    by_hour = defaultdict(lambda: [0, 0, Decimal(0)])
    for d in every:
        if d.profit >= Decimal("0.01"):
            row = by_hour[d.local_hour]
            row[0 if d.bound_ok else 1] += 1
            row[2] += d.pnl
    out += ["| Local hour | Bound held | Bound failed | Realized per contract, summed |", "|---|---|---|---|"]
    for hour in sorted(by_hour):
        held, failed, net = by_hour[hour]
        out.append(f"| {hour:02d}:00 | {held} | {failed} | {net:+.2f} |")
    failures = [d for d in every if not d.bound_ok]
    if failures:
        out += ["", "Bound failures (up to 20):", "", "| Ticker | Hour (UTC) | Kind | Price |", "|---|---|---|---|"]
        for d in failures[:20]:
            out.append(f"| {d.ticker} | {d.at:%Y-%m-%d %H:%M} | {d.kind} | {d.price} |")
    out += ["", "## Skipped events", ""]
    for series, (_, _, skipped) in results.items():
        out.append(f"- {series}: {skipped or 'none'}")
    return "\n".join(out) + "\n"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--series", default=",".join(SERIES))
    p.add_argument("--out", default="research/structural_edges.md")
    args = p.parse_args()
    results = {}
    for series in args.series.split(","):
        data = load(series)
        results[series] = analyse(series, data)
        print(f"{series}: {len(results[series][0])} event-hours, {len(results[series][1])} dead/certain quotes", flush=True)
    Path(args.out).write_text(report(results))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
