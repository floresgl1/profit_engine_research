"""Which "day" does The Weather Company use for KXHIGHNY settlement?

Plan (agreed in review):
1. Data: settled KXHIGHNY events (expiration_value, rules text), ACIS daily
   highs for Central Park (NWS climate report, local standard time), IEM
   ASOS observations for station NYC.
2. Calibrate: measure how far the max observation undercounts the official
   high, on days where the day window cannot matter (standard-time days, and
   DST days where both midnight hours are far below the day's peak). The
   margin U is the worst undercount seen.
3. Select: DST dates where one of the two midnight hours that the windows
   disagree on beats every other observation by more than U.
4. Decide: on those dates the LST and clock-time windows predict disjoint
   ranges for the official high. NWS match -> LST; clock match -> clock
   time; neither, conflicting, or no qualifying dates -> inconclusive.

The method is first run on ACIS itself, where the answer (LST) is known.

Run: uv run python research/day_window.py --out research/day_window.md
"""

from __future__ import annotations

import argparse
import bisect
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from profit_engine.weather import Observation, is_dst, is_transition

NY = ZoneInfo("America/New_York")
CALM_GAP = Decimal(5)  # DST day is "calm" when both midnight hours are this far below the core peak


# --- step 2/3 building block: split a day's observations by the hours the windows disagree on ---


@dataclass(frozen=True, slots=True)
class DaySplit:
    """Max observation in each part of a DST day, local clock time.

    start: [D 00:00, D 01:00)   counted by clock time only
    core:  [D 01:00, D+1 00:00) counted by both
    end:   [D+1 00:00, D+1 01:00)  counted by LST only
    """

    day: date
    start: Decimal | None
    core: Decimal | None
    end: Decimal | None

    @property
    def complete(self) -> bool:
        return None not in (self.start, self.core, self.end)

    @property
    def lst_max(self) -> Decimal:
        return max(self.core, self.end)

    @property
    def clock_max(self) -> Decimal:
        return max(self.start, self.core)


class ObservationIndex:
    def __init__(self, observations: Iterable[Observation]) -> None:
        self._obs = sorted(observations, key=lambda o: o.at)
        self._times = [o.at for o in self._obs]

    def max_between(self, start: datetime, end: datetime) -> Decimal | None:
        lo = bisect.bisect_left(self._times, start)
        hi = bisect.bisect_left(self._times, end)
        values = [o.tmpf for o in self._obs[lo:hi]]
        return max(values) if values else None


def local(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=NY)


def split_day(day: date, index: ObservationIndex) -> DaySplit:
    nxt = day + timedelta(days=1)
    return DaySplit(
        day,
        start=index.max_between(local(day, 0), local(day, 1)),
        core=index.max_between(local(day, 1), local(nxt, 0)),
        end=index.max_between(local(nxt, 0), local(nxt, 1)),
    )


# --- step 2: calibrate the undercount ---


def window_insensitive(day: date, split: DaySplit) -> bool:
    """True when LST and clock time must give the same high for `day`."""
    if is_transition(day, NY) or not split.complete:
        return False
    if not is_dst(day, NY):
        return True  # windows are identical in standard time
    return max(split.start, split.end) <= split.core - CALM_GAP


def undercounts(days: Iterable[tuple[date, int | None, DaySplit]]) -> list[Decimal]:
    """official high - max observation, over window-insensitive days."""
    return [
        Decimal(official) - split.lst_max
        for day, official, split in days
        if official is not None and window_insensitive(day, split)
    ]


# --- step 3: select qualifying dates and predict each convention's range ---


@dataclass(frozen=True, slots=True)
class Prediction:
    day: date
    case: str  # "start" (warm hour at the start of the day) or "end"
    lst: tuple[Decimal, Decimal]  # inclusive range the official high must fall in under LST
    clock: tuple[Decimal, Decimal]  # ... under clock time


def predict(split: DaySplit, margin: Decimal) -> Prediction | None:
    """Ranges for the official high under each window, or None if the day can't discriminate.

    Each window's official high is at least its max observation and at most
    that plus the margin (the worst undercount). A day qualifies when those
    two ranges cannot overlap.
    """
    if not split.complete or is_transition(split.day, NY) or not is_dst(split.day, NY):
        return None
    if split.start > max(split.core, split.end) + margin:
        case = "start"
    elif split.end > max(split.start, split.core) + margin:
        case = "end"
    else:
        return None
    lst, clock = split.lst_max, split.clock_max
    return Prediction(split.day, case, (lst, lst + margin), (clock, clock + margin))


# --- step 4: decide ---


def verdict(value: Decimal, prediction: Prediction, nws_high: int | None = None) -> str:
    """'lst', 'clock' or 'neither' for one qualifying date.

    With `nws_high` given (the TWC test), an exact NWS match counts as LST,
    per the agreed rule; otherwise the value is placed in a predicted range.
    """
    if nws_high is not None and value == nws_high:
        return "lst"
    if prediction.lst[0] <= value <= prediction.lst[1]:
        return "lst"
    if prediction.clock[0] <= value <= prediction.clock[1]:
        return "clock"
    return "neither"


def conclude(verdicts: Sequence[str]) -> tuple[str, str]:
    """Overall conclusion and the reason for it."""
    if not verdicts:
        return "inconclusive", "no qualifying dates"
    kinds = set(verdicts)
    if kinds == {"lst"}:
        return "lst", f"all {len(verdicts)} qualifying dates match the LST window"
    if kinds == {"clock"}:
        return "clock", f"all {len(verdicts)} qualifying dates match the clock-time window"
    counts = ", ".join(f"{k}: {verdicts.count(k)}" for k in sorted(kinds))
    return "inconclusive", f"qualifying dates disagree or match neither ({counts})"


# --- run against live data -------------------------------------------------------------


def fetch(start: date, end: date):
    from profit_engine.venues.http import ReadOnlyHttp
    from profit_engine.venues.kalshi import BASE_URL as KALSHI_URL
    from profit_engine.weather import AcisClient, IemAsosClient, settled_temperatures, acis, iem

    settled = settled_temperatures(ReadOnlyHttp(KALSHI_URL, min_interval=0.3), "KXHIGHNY")
    highs = AcisClient(ReadOnlyHttp(acis.BASE_URL)).daily_max(acis.CENTRAL_PARK, start, end)
    iem_client = IemAsosClient(ReadOnlyHttp(iem.BASE_URL, timeout=120, min_interval=1.0))
    observations: list[Observation] = []
    year_start = start
    while year_start <= end:  # one request per year keeps responses small
        year_end = min(date(year_start.year + 1, 1, 1), end + timedelta(days=2))
        observations += iem_client.temperatures(iem.CENTRAL_PARK, year_start, year_end)
        year_start = year_end
    return settled, highs, ObservationIndex(observations), len(observations)


def _fmt_dist(values: list[Decimal]) -> str:
    if not values:
        return "no data"
    ordered = sorted(values)

    def pct(p: float) -> Decimal:
        return ordered[min(len(ordered) - 1, int(p * len(ordered)))]

    counts = {}
    for v in ordered:
        counts[v] = counts.get(v, 0) + 1
    histogram = ", ".join(f"{v:+.0f}: {n}" for v, n in sorted(counts.items()))
    return (
        f"n={len(values)}, min {ordered[0]:+.0f}, median {pct(0.5):+.0f}, 95th {pct(0.95):+.0f}, "
        f"99th {pct(0.99):+.0f}, max {ordered[-1]:+.0f}\n\n  Histogram (official - max obs: days): {histogram}"
    )


def report(start: date, end: date) -> str:
    settled, highs, index, n_obs = fetch(start, end)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    splits = {d: split_day(d, index) for d in days}

    lines = [
        "# Day-window analysis: KXHIGHNY",
        "",
        f"Run {datetime.now().date().isoformat()} over {start} to {end}: {n_obs} IEM observations, "
        f"{sum(v is not None for v in highs.values())} ACIS highs, {len(settled)} settled Kalshi events.",
    ]

    # Step 2: calibration
    calib = [(d, highs.get(d), splits[d]) for d in days]
    std = undercounts((d, h, s) for d, h, s in calib if not is_dst(d, NY))
    dst = undercounts((d, h, s) for d, h, s in calib if is_dst(d, NY))
    allu = std + dst
    margin = max(max(allu), Decimal(0))
    lines += [
        "",
        "## 1. Calibration: how far observations undercount the official high",
        "",
        "Measured only on days where the window cannot matter.",
        "",
        f"- Standard-time days: {_fmt_dist(std)}",
        f"- Calm DST days (both midnight hours {CALM_GAP}°+ below the peak): {_fmt_dist(dst)}",
        "",
        f"**Margin U = {margin}°F** (worst undercount seen). Negative values mean an observation "
        "exceeded the official high.",
    ]

    # Method check on ACIS (known LST)
    acis_preds = [p for d in days if (p := predict(splits[d], margin)) and highs.get(d) is not None]
    acis_verdicts = [verdict(Decimal(highs[p.day]), p) for p in acis_preds]
    acis_conclusion = conclude(acis_verdicts)
    lines += [
        "",
        "## 2. Method check: ACIS (NWS climate report, known to use LST)",
        "",
        f"{len(acis_preds)} qualifying DST dates. Conclusion: **{acis_conclusion[0]}** ({acis_conclusion[1]}).",
        "",
        "| Date | Case | ACIS high | LST range | Clock range | Verdict |",
        "|---|---|---|---|---|---|",
    ]
    for p, v in zip(acis_preds, acis_verdicts, strict=True):
        lines.append(
            f"| {p.day} | {p.case} | {highs[p.day]} | {p.lst[0]:.0f}-{p.lst[1]:.0f} | "
            f"{p.clock[0]:.0f}-{p.clock[1]:.0f} | {v} |"
        )

    # Agreement between Kalshi settlement and NWS, by era
    lines += ["", "## 3. Kalshi settlement vs NWS high, by source named in the rules", ""]
    for source in ("nws", "twc", "unspecified"):
        rows = [s for s in settled if s.source == source and s.value is not None and highs.get(s.day) is not None]
        if not rows:
            continue
        diffs = [r for r in rows if r.value != highs[r.day]]
        lines.append(
            f"- **{source}**: {len(rows)} days, {len(rows) - len(diffs)} equal to ACIS, {len(diffs)} different"
            + (f" ({', '.join(f'{r.day}: Kalshi {r.value:.0f} vs NWS {highs[r.day]}' for r in diffs[:10])})" if diffs else "")
        )
    non_integer = [s for s in settled if s.value is not None and s.value != s.value.to_integral_value()]
    lines.append(f"- Non-integer settlement values: {len(non_integer)}")

    # Step 3/4 on TWC
    twc = [s for s in settled if s.source == "twc" and s.value is not None]
    twc_preds = [(s, p) for s in twc if (p := predict(splits.get(s.day) or split_day(s.day, index), margin))]
    twc_verdicts = [verdict(s.value, p, highs.get(s.day)) for s, p in twc_preds]
    decision = conclude(twc_verdicts)
    lines += [
        "",
        "## 4. The Weather Company era",
        "",
        f"{len(twc)} settled TWC days ({twc[0].day if twc else '-'} to {twc[-1].day if twc else '-'}), "
        f"{len(twc_preds)} qualifying.",
        "",
    ]
    if twc_preds:
        lines += ["| Date | Case | Kalshi value | NWS high | LST range | Clock range | Verdict |", "|---|---|---|---|---|---|---|"]
        for (s, p), v in zip(twc_preds, twc_verdicts, strict=True):
            lines.append(
                f"| {s.day} | {p.case} | {s.value:.0f} | {highs.get(s.day)} | {p.lst[0]:.0f}-{p.lst[1]:.0f} | "
                f"{p.clock[0]:.0f}-{p.clock[1]:.0f} | {v} |"
            )
        lines.append("")
    lines += [f"**Conclusion: {decision[0]}** ({decision[1]}).", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--start", type=date.fromisoformat, default=date(2021, 8, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="default: 2 days ago")
    parser.add_argument("--out", help="write the markdown report here as well as printing it")
    args = parser.parse_args()
    end = args.end or (datetime.now(NY).date() - timedelta(days=2))
    text = report(args.start, end)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)


if __name__ == "__main__":
    main()
