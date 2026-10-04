"""Which "day" does The Weather Company use for KXHIGHNY settlement?

Plan (agreed in review):
1. Data: settled KXHIGHNY events (expiration_value, rules text), ACIS daily
   highs for Central Park (NWS climate report, local standard time), IEM
   ASOS observations for station NYC.
2. Calibrate: measure official high - max observation on days where the day
   window cannot matter (standard-time days, and DST days where both
   midnight hours are far below the day's peak). The worst values in each
   direction bound the error: official in [max_obs + low, max_obs + high].
3. Select: DST dates where one of the two midnight hours that the windows
   disagree on beats every other observation by more than high - low, so
   the two windows' ranges cannot overlap.

Data quality (found in the first run): days with an observation gap over
90 minutes are skipped, and isolated spikes (a reading 5°F+ above both
neighbours within an hour) are dropped before anything else.
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
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

from profit_engine.weather import Observation, is_dst, is_transition
from profit_engine.weather.iem import drop_spikes

NY = ZoneInfo("America/New_York")
CALM_GAP = Decimal(5)  # DST day is "calm" when both midnight hours are this far below the core peak
MAX_GAP = timedelta(minutes=90)  # longest allowed gap between observations in a usable day


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
    gap: bool = False  # an observation gap over MAX_GAP somewhere in the three parts

    @property
    def complete(self) -> bool:
        return None not in (self.start, self.core, self.end) and not self.gap

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

    def has_gap(self, start: datetime, end: datetime, limit: timedelta = MAX_GAP) -> bool:
        """True if [start, end) has a stretch longer than `limit` without observations."""
        lo = bisect.bisect_left(self._times, start)
        hi = bisect.bisect_left(self._times, end)
        points = [start, *self._times[lo:hi], end]
        return any(b - a > limit for a, b in pairwise(points))

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
        gap=index.has_gap(local(day, 0), local(nxt, 1)),
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
class Bounds:
    """official high - max observation lies in [low, high] (low <= 0 <= high)."""

    low: Decimal
    high: Decimal

    @classmethod
    def from_errors(cls, errors: Sequence[Decimal]) -> Bounds:
        return cls(min(min(errors), Decimal(0)), max(max(errors), Decimal(0)))

    @property
    def width(self) -> Decimal:
        return self.high - self.low


@dataclass(frozen=True, slots=True)
class Prediction:
    day: date
    case: str  # "start" (warm hour at the start of the day) or "end"
    lst: tuple[Decimal, Decimal]  # inclusive range the official high must fall in under LST
    clock: tuple[Decimal, Decimal]  # ... under clock time


def predict(split: DaySplit, bounds: Bounds) -> Prediction | None:
    """Ranges for the official high under each window, or None if the day can't discriminate.

    Under either window the official high is that window's max observation
    plus an error in [bounds.low, bounds.high]. A day qualifies when the two
    resulting ranges cannot overlap: one midnight hour beats everything else
    by more than the width of the error band.
    """
    if not split.complete or is_transition(split.day, NY) or not is_dst(split.day, NY):
        return None
    if split.start > max(split.core, split.end) + bounds.width:
        case = "start"
    elif split.end > max(split.start, split.core) + bounds.width:
        case = "end"
    else:
        return None
    lst, clock = split.lst_max, split.clock_max
    return Prediction(
        split.day, case, (lst + bounds.low, lst + bounds.high), (clock + bounds.low, clock + bounds.high)
    )


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
    from profit_engine.weather import AcisClient, IemAsosClient, acis, iem, settled_temperatures

    settled = settled_temperatures(ReadOnlyHttp(KALSHI_URL, min_interval=0.3), "KXHIGHNY")
    highs = AcisClient(ReadOnlyHttp(acis.BASE_URL)).daily_max(acis.CENTRAL_PARK, start, end)
    iem_client = IemAsosClient(ReadOnlyHttp(iem.BASE_URL, timeout=120, min_interval=1.0))
    observations: list[Observation] = []
    year_start = start
    while year_start <= end:  # one request per year keeps responses small
        year_end = min(date(year_start.year + 1, 1, 1), end + timedelta(days=2))
        observations += iem_client.temperatures(iem.CENTRAL_PARK, year_start, year_end)
        year_start = year_end
    return settled, highs, observations


def _fmt_errors(values: list[Decimal]) -> str:
    if not values:
        return "no data"
    ordered = sorted(values)
    bins: dict[int, int] = {}
    for v in ordered:
        key = int(v.to_integral_value(rounding=ROUND_HALF_UP))
        bins[key] = bins.get(key, 0) + 1
    histogram = ", ".join(f"{k:+d}: {n}" for k, n in sorted(bins.items()))
    return f"n={len(values)}, min {ordered[0]:+.1f}, max {ordered[-1]:+.1f}; days per rounded error: {histogram}"


def _range(r: tuple[Decimal, Decimal]) -> str:
    return f"{r[0]:.1f} to {r[1]:.1f}"


def method_check(days, splits, highs, bounds: Bounds) -> tuple[list[Prediction], list[str]]:
    preds = [p for d in days if highs.get(d) is not None and (p := predict(splits[d], bounds))]
    return preds, [verdict(Decimal(highs[p.day]), p) for p in preds]


def report(start: date, end: date, cache: str | None = None) -> str:
    import os
    import pickle

    if cache and os.path.exists(cache):
        with open(cache, "rb") as fh:
            settled, highs, raw = pickle.load(fh)
    else:
        settled, highs, raw = fetch(start, end)
        if cache:
            with open(cache, "wb") as fh:
                pickle.dump((settled, highs, raw), fh)
    observations = drop_spikes(raw)
    index = ObservationIndex(observations)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    splits = {d: split_day(d, index) for d in days}

    lines = [
        "# Day-window analysis: KXHIGHNY",
        "",
        f"Run {datetime.now().date().isoformat()} over {start} to {end}: {len(raw)} IEM observations "
        f"({len(raw) - len(observations)} dropped as spikes), {sum(v is not None for v in highs.values())} ACIS "
        f"highs, {len(settled)} settled Kalshi events. "
        f"{sum(s.gap for s in splits.values())} days skipped for observation gaps over {MAX_GAP}.",
    ]

    # Step 2: calibration
    calib = [(d, highs.get(d), splits[d]) for d in days]
    std = undercounts((d, h, s) for d, h, s in calib if not is_dst(d, NY))
    dst = undercounts((d, h, s) for d, h, s in calib if is_dst(d, NY))
    variants = {"all seasons": Bounds.from_errors(std + dst), "DST days only": Bounds.from_errors(dst)}
    lines += [
        "",
        "## 1. Calibration: official high minus max observation",
        "",
        "Measured only on days where the window cannot matter. Negative means an observation exceeded the official high.",
        "",
        f"- Standard-time days: {_fmt_errors(std)}",
        f"- Calm DST days (both midnight hours {CALM_GAP}°F+ below the peak): {_fmt_errors(dst)}",
        "",
        "| Calibration | Error band | A qualifying midnight hour must beat the rest by more than |",
        "|---|---|---|",
    ]
    for name, b in variants.items():
        lines.append(f"| {name} | {b.low:+.1f} to {b.high:+.1f} | {b.width:.1f}°F |")

    # Method check on ACIS, where LST is known
    lines += ["", "## 2. Method check on ACIS (NWS climate report, known to use LST)", ""]
    for name, b in variants.items():
        preds, verdicts = method_check(days, splits, highs, b)
        result, reason = conclude(verdicts)
        lines.append(f"- **{name}**: {len(preds)} qualifying dates, conclusion **{result}** ({reason})")
    preds, verdicts = method_check(days, splits, highs, variants["DST days only"])
    if preds:
        lines += [
            "",
            "Qualifying dates with DST-only calibration:",
            "",
            "| Date | Case | ACIS high | LST range | Clock range | Verdict |",
            "|---|---|---|---|---|---|",
        ]
        for p, v in zip(preds, verdicts, strict=True):
            lines.append(f"| {p.day} | {p.case} | {highs[p.day]} | {_range(p.lst)} | {_range(p.clock)} | {v} |")

    # Agreement between Kalshi settlement and NWS, by era
    lines += ["", "## 3. Kalshi settlement value vs NWS high, by source named in the rules", ""]
    for source in ("unspecified", "nws", "twc"):
        rows = [s for s in settled if s.source == source and s.value is not None and highs.get(s.day) is not None]
        if not rows:
            continue
        diffs = [r for r in rows if r.value != highs[r.day]]
        detail = ", ".join(f"{r.day} Kalshi {r.value:.0f} vs NWS {highs[r.day]}" for r in diffs[:10])
        lines.append(
            f"- **{source}** ({rows[0].day} to {rows[-1].day}): {len(rows)} days, "
            f"{len(rows) - len(diffs)} equal to the NWS high, {len(diffs)} different" + (f" ({detail})" if diffs else "")
        )
    missing = sum(s.value is None for s in settled)
    non_integer = sum(s.value is not None and s.value != s.value.to_integral_value() for s in settled)
    lines.append(f"- Events with no settlement value: {missing}. Non-integer settlement values: {non_integer}.")

    # Step 3/4 on TWC
    twc = [s for s in settled if s.source == "twc" and s.value is not None]
    lines += [
        "",
        "## 4. The Weather Company era",
        "",
        f"{len(twc)} settled TWC days, {twc[0].day if twc else '-'} to {twc[-1].day if twc else '-'}.",
        "",
    ]
    decisions = {}
    for name, b in variants.items():
        preds = [(s, p) for s in twc if (p := predict(splits.get(s.day) or split_day(s.day, index), b))]
        verdicts = [verdict(s.value, p, highs.get(s.day)) for s, p in preds]
        decisions[name] = conclude(verdicts)
        lines.append(f"- **{name}**: {len(preds)} qualifying dates, conclusion **{decisions[name][0]}** ({decisions[name][1]})")
        for (s, p), v in zip(preds, verdicts, strict=True):
            lines.append(
                f"  - {s.day} ({p.case}): Kalshi {s.value:.0f}, NWS {highs.get(s.day)}, "
                f"LST {_range(p.lst)}, clock {_range(p.clock)} -> {v}"
            )

    # Weak evidence: TWC days where the windows' observations differ at all
    weak = []
    for s in twc:
        sp = splits.get(s.day) or split_day(s.day, index)
        if sp.complete and is_dst(s.day, NY) and sp.lst_max != sp.clock_max:
            weak.append((s, sp))
    lines += [
        "",
        "### Weak evidence (does not meet the decision rule)",
        "",
        f"TWC days where the two windows' max observations differ at all: {len(weak)}. "
        "On these the windows' error bands overlap, so a match is suggestive, not decisive.",
        "",
    ]
    if weak:
        lines += ["| Date | Kalshi | NWS | Max obs, LST window | Max obs, clock window |", "|---|---|---|---|---|"]
        for s, sp in weak:
            lines.append(f"| {s.day} | {s.value:.0f} | {highs.get(s.day)} | {sp.lst_max:.1f} | {sp.clock_max:.1f} |")

    # How often could the window matter at all?
    usable = [sp for d, sp in splits.items() if sp.complete and is_dst(d, NY) and not is_transition(d, NY)]
    diffs = [abs(sp.clock_max - sp.lst_max) for sp in usable]
    lines += [
        "",
        "## 5. How often the day window could matter",
        "",
        f"Usable DST days {start} to {end}: {len(usable)}. Share where the two windows' max observations differ by:",
        "",
        "| Difference | Days | Share |",
        "|---|---|---|",
    ]
    for threshold in (Decimal("0.1"), Decimal(1), Decimal(2), Decimal(3), Decimal(4)):
        n = sum(x >= threshold for x in diffs)
        share = f"{100 * n / len(usable):.1f}%" if usable else "-"
        label = "any" if threshold < 1 else f">= {threshold}°F"
        lines.append(f"| {label} | {n} | {share} |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--start", type=date.fromisoformat, default=date(2021, 8, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="default: 2 days ago")
    parser.add_argument("--out", help="write the markdown report here as well as printing it")
    parser.add_argument("--cache", help="pickle file to reuse downloaded data between runs")
    args = parser.parse_args()
    end = args.end or (datetime.now(NY).date() - timedelta(days=2))
    text = report(args.start, end, args.cache)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)


if __name__ == "__main__":
    main()
