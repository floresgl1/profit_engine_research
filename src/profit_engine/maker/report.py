"""Paper market-maker results, by event day, with the statistics needed to decide anything.

The unit is the event day: all markets of one daily-high event share one
outcome, so fills within a day are not independent; days roughly are.

Per strategy:
- daily PnL (settled markets at their payout; unsettled ones marked at the
  last recorded mid, flagged as estimates and left out of the statistics);
- mean daily PnL with a 95% interval (Student t), PnL per contract, total;
- risk: worst day, maximum drawdown of cumulative daily PnL, largest
  |position| held in any market, fills and contracts.

Paired v2 - v1: on settled days both strategies covered (their markets
opened after both were running), the daily difference and its interval.
Both see the same books and trades, so pairing removes the day's luck.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from profit_engine.maker.quoter import MakerFill

# Two-sided 95% Student t quantiles; normal beyond.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        12: 2.179, 15: 2.131, 20: 2.086, 25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980}  # fmt: skip
# KXHIGH markets open at 14:00 UTC the day before the event (seen on every event checked).
MARKETS_OPEN = time(14, tzinfo=timezone.utc)


def t95(df: int) -> float:
    if df <= 0:
        return float("nan")
    keys = [k for k in _T95 if k <= df]
    return _T95[max(keys)] if df <= 120 else 1.96


def event_day_of(market_id: str) -> date | None:
    """`KXHIGHNY-26OCT05-B68.5` -> 2026-10-05; None if the ticker has no event date."""
    parts = market_id.split("-")
    if len(parts) < 2:
        return None
    try:
        return datetime.strptime(parts[1], "%y%b%d").date()
    except ValueError:
        return None


def covered_from(started: datetime) -> date:
    """First event day whose markets opened after `started`."""
    d = started.date() + timedelta(days=1)
    while datetime.combine(d - timedelta(days=1), MARKETS_OPEN) < started:
        d += timedelta(days=1)
    return d


@dataclass(frozen=True)
class Day:
    day: date
    pnl: Decimal
    settled: bool  # every market with fills that day has resolved
    fills: int
    contracts: Decimal


@dataclass(frozen=True)
class Stats:
    days: int
    mean: float
    half_width: float  # 95% interval half-width
    total: float
    per_contract: float  # cents
    worst_day: float
    max_drawdown: float

    @property
    def interval(self) -> tuple[float, float]:
        return self.mean - self.half_width, self.mean + self.half_width


def daily(
    fills: list[MakerFill], resolutions: dict[str, Decimal], mark: Callable[[str], Decimal | None]
) -> tuple[list[Day], Decimal]:
    """Days in order, and the largest |position| any market reached."""
    per_market: dict[str, list[MakerFill]] = {}
    for f in sorted(fills, key=lambda f: f.at):
        per_market.setdefault(f.market_id, []).append(f)
    by_day: dict[date, list[tuple[Decimal | None, bool, int, Decimal]]] = {}
    max_pos = Decimal(0)
    for ticker, fs in per_market.items():
        position, cash = Decimal(0), Decimal(0)
        for f in fs:
            position += f.position
            cash += f.cash
            max_pos = max(max_pos, abs(position))
        settled = ticker in resolutions
        value = resolutions.get(ticker, mark(ticker) if position else Decimal(0))
        pnl = None if value is None else cash + position * value
        day = event_day_of(ticker)
        if day is not None:
            by_day.setdefault(day, []).append((pnl, settled, len(fs), sum((f.quantity for f in fs), Decimal(0))))
    days = []
    for day in sorted(by_day):
        rows = by_day[day]
        known = [r[0] for r in rows if r[0] is not None]
        days.append(Day(day, sum(known, Decimal(0)), all(r[1] for r in rows) and len(known) == len(rows),
                        sum(r[2] for r in rows), sum((r[3] for r in rows), Decimal(0))))  # fmt: skip
    return days, max_pos


def stats(days: list[Day]) -> Stats | None:
    settled = [d for d in days if d.settled]
    if not settled:
        return None
    pnls = [float(d.pnl) for d in settled]
    n = len(pnls)
    mean = sum(pnls) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in pnls) / (n - 1)) if n > 1 else float("nan")
    half = t95(n - 1) * sd / math.sqrt(n) if n > 1 else float("nan")
    contracts = sum(float(d.contracts) for d in settled)
    peak = cum = drawdown = 0.0
    for x in pnls:
        cum += x
        peak = max(peak, cum)
        drawdown = max(drawdown, peak - cum)
    return Stats(n, mean, half, sum(pnls), 100 * sum(pnls) / contracts if contracts else float("nan"), min(pnls), drawdown)


def paired(a: list[Day], b: list[Day], start: date) -> Stats | None:
    """b - a on settled days from `start`; a day one strategy didn't trade counts as 0 for it."""
    pa = {d.day: d for d in a if d.day >= start}
    pb = {d.day: d for d in b if d.day >= start}
    diffs = []
    for day in sorted(set(pa) | set(pb)):
        da, db = pa.get(day), pb.get(day)
        if (da is not None and not da.settled) or (db is not None and not db.settled):
            continue
        diffs.append(Day(day, (db.pnl if db else Decimal(0)) - (da.pnl if da else Decimal(0)), True, 0,
                         (db.contracts if db else Decimal(0)) + (da.contracts if da else Decimal(0))))  # fmt: skip
    return stats(diffs)


def fmt_stats(s: Stats | None) -> str:
    if s is None:
        return "  no settled days yet"
    lo, hi = s.interval
    ci = f"[{lo:+.2f}, {hi:+.2f}]" if s.days > 1 else "[needs 2+ days]"
    return (f"  settled days: {s.days}  mean/day: {s.mean:+.2f} 95% {ci}  total: {s.total:+.2f}  "
            f"per contract: {s.per_contract:+.2f}c\n  worst day: {s.worst_day:+.2f}  max drawdown: {s.max_drawdown:.2f}")


def started(strategy: str, runs: dict[str, datetime], by_strategy: dict[str, list[MakerFill]]) -> datetime | None:
    """When a strategy started: its first recorded run or first fill, whichever is earlier.

    Runs are recorded from the version that added recording on, so a strategy
    that was already running is dated by its first fill.
    """
    times = [t for t in (runs.get(strategy), min((f.at for f in by_strategy.get(strategy, [])), default=None)) if t]
    return min(times) if times else None


def render(
    by_strategy: dict[str, list[MakerFill]],
    resolutions: dict[str, Decimal],
    mark: Callable[[str], Decimal | None],
    runs: dict[str, datetime],
    baseline: str = "join_touch_v1",
    pair_from: date | None = None,
    pairs: Sequence[tuple[str, str]] | None = None,
) -> str:
    """Per-strategy results, then paired comparisons: each (a, b) in `pairs` as a - b on the same
    days (default: every strategy against `baseline`)."""
    if not by_strategy:
        return "No paper market-maker fills yet."
    lines, days_of = [], {}
    for name in sorted(set(by_strategy) | set(runs)):
        fills = by_strategy.get(name, [])
        days, max_pos = daily(fills, resolutions, mark)
        days_of[name] = days
        lines.append(f"[{name}] fills: {len(fills)}  contracts: {sum((f.quantity for f in fills), Decimal(0))}  "
                     f"max |position| in a market: {max_pos}")  # fmt: skip
        lines.append(fmt_stats(stats(days)))
        for d in days[-7:]:
            tag = "" if d.settled else "  (unsettled: marked at last mid)"
            lines.append(f"    {d.day}  {d.pnl:+.2f}  fills {d.fills}{tag}")
    if pairs is None:
        pairs = [(name, baseline) for name in sorted(days_of) if name != baseline] if baseline in days_of else []
    for name, other in pairs:
        if name not in days_of or other not in days_of:
            continue
        starts = [started(s, runs, by_strategy) for s in (name, other)]
        start = pair_from or (covered_from(max(t for t in starts if t)) if any(starts) else date.min)
        lines.append(f"[{name} - {other}, paired, days from {start}]")
        lines.append(fmt_stats(paired(days_of[other], days_of[name], start)))
    return "\n".join(lines)
