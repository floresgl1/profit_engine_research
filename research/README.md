# Research

One-off analyses that inform the engine. Each script writes a generated
report next to it; this file holds the interpretation.

## Day window for KXHIGHNY settlement (`day_window.py`)

**Question.** Kalshi's NYC high-temperature markets settle on The Weather
Company's (TWC) number since 2026-08-14. Does TWC count the day in local
standard time (01:00 to 01:00 EDT in summer, like the NWS climate report)
or in local clock time (midnight to midnight)?

**Method** (plan agreed in review, refined after the first run):
measure how far observations miss the official high on days where the
window can't matter; keep only daylight-saving dates where one of the two
disputed midnight hours beats every other observation by more than that
error band, so the two windows predict non-overlapping highs; then check
which prediction the settlement value matches. The method runs on ACIS
(NWS, known to use LST) first as a check. Days with observation gaps over
90 minutes are skipped and isolated spikes (5°F+ above both neighbours)
are dropped; both problems showed up in the first run.

**Result** (`day_window.md`, data 2021-08-01 to 2026-10-01):

1. **Inconclusive.** No date in five years qualifies, not even for the ACIS
   check. Observations miss the official high by -0.8 to +3.0°F on
   daylight-saving days (-2.2 to +5.0°F all year), so a midnight hour would
   need to beat the rest of the day by 3.8°F or more; the largest such gap
   seen was 3°F, on 2 days. Public hourly data can't answer this question.
2. **It rarely matters.** The two windows' maximum observations differ on
   3.3% of daylight-saving days, by 2°F or more on 1.1%.
3. **TWC has matched NWS every time so far:** 49 of 49 TWC-era days settled
   on exactly the ACIS high. Small sample, summer and early autumn only.
4. **Kalshi and ACIS disagree occasionally in the NWS era:** 6 of 1,555
   days, including 2025-06-02 (Kalshi 66, ACIS 71). Likely causes are ACIS
   revisions after settlement or preliminary reports, which the contract
   says don't count. Not investigated further.
5. All settlement values are whole degrees.

**Implications for the temperature model.**

- **Training target:** Kalshi's `expiration_value` where it exists (it is
  literally what settled), the ACIS high otherwise. They agree on 99.6% of
  days.
- **Day window:** treat as unknown. Abstain, or widen uncertainty, on days
  when the expected high falls in a midnight hour (00:00 to 01:00 EDT at
  either end of the day). Expected cost: about 3% of daylight-saving days.
- **Revisit:** rerun as TWC days accumulate, especially in spring and
  autumn when fronts make midnight highs more likely. A single qualifying
  date decides it. Asking Kalshi support directly is the cheaper route.

Rerun: `uv run python research/day_window.py --out research/day_window.md`

## Temperature model backtest (`temperature_backtest.py`)

**Question.** Does the NBM-based model (`models/kalshi_temperature.py`) price
KXHIGHNY buckets better than the market?

**Method.** Fit bias and spread per lead time (16:00 the day before, 10:00
and 14:00 on the day) on Aug 2024 to Jul 2025. Score on Aug 2025 to Oct
2026 against the market midpoint from the last hourly candle that closed
before each decision time, on identical rows. Inputs at each decision time
are only the NBM run available by then (2-hour publication lag) and
observations before then.

**Result** (`temperature_backtest.md`): **no edge; the market is clearly
better at every lead.**

| Lead | Model Brier | Market Brier | Skill vs market |
|---|---|---|---|
| Day before, 16:00 | 0.141 | 0.120 | -0.18 |
| Same day, 10:00 | 0.176 | 0.134 | -0.31 |
| Same day, 14:00 | 0.235 | 0.119 | -0.97 |

(sigma scaled by NBM's `xnd`, which beat a fixed sigma on held-out log score.)

**Why, as far as the data shows:**

1. **No new forecast on the day.** NBM runs stop forecasting the current
   day once its daytime max period starts, so the 10:00 and 14:00 leads use
   the overnight run: the spread barely shrinks (2.63 to 2.51°F) while the
   market keeps updating.
2. **Observations are used only as a floor.** By 14:00 the market prices
   the trajectory (still rising, already peaked, front arriving); the model
   only knows "at least the max so far".
3. **Calibration.** The market is well calibrated; the model under-predicts
   most buckets (e.g. predicted 0.24, happened 0.29), so its spread or
   centre is off for the buckets that are actually contested.

**What it means.** Do not paper trade this model (`--trade-model
kxhigh_nbm`); log its predictions to keep measuring, that's all.
Promising next steps, each testable with the same backtest: hourly
forecast and observation trajectory for same-day leads, a sigma that
shrinks with the hours left in the day, and recalibrating the
distribution's tails against the training period.

Rerun (cached data in `data/`): `uv run python research/temperature_backtest.py`

## Temperature model v2: LAMP, max of observed and remaining (`temperature_backtest_v2.py`)

**Change from v1.** The high is modelled as max(M, F): M = the official max
of the hours already observed (rounded reading + the measured undercount
distribution), F = GFS LAMP's highest hourly forecast for the rest of the
day + fitted bias, with fitted spread. LAMP is re-issued hourly, so the
same-day leads get a fresh forecast (NBM stops forecasting the day once it
starts). Same training/test split, cached data and identical rows as v1.

**Result** (`temperature_backtest_v2.md`): **much better than v1, still worse
than the market at every lead.**

| Lead | v1 skill | v2 skill | v2 bias / sigma °F |
|---|---|---|---|
| Day before, 16:00 | -0.18 | -0.13 | +1.25 / 2.50 |
| Same day, 10:00 | -0.31 | -0.16 | +0.75 / 2.00 |
| Same day, 14:00 | -0.97 | -0.34 | -0.25 / 2.00 |
| All | -0.39 | -0.18 | |

Log score of the realized high improves at every lead (all: -2.28 to
-1.91). Calibration is now close (e.g. predicted 0.26, happened 0.26);
the remaining gap is sharpness, not bias: the market concentrates
probability on the right bucket more often.

**Reading it.**
- The backtest uses only the 00/06/12/18Z LAMP runs the archive keeps,
  so at 14:00 it uses the 12Z run; live runs are hourly and fresher, so
  live results should be somewhat better than this.
- Sigma is fixed per lead. It should depend on hours left in the day and
  on the weather regime (fronts, convection).
- Still no edge: keep logging, don't trade it.

**Next to try, same backtest:** a sigma that shrinks with hours left;
the latest observed temperature and its trend (not just the max); bucket
probabilities recalibrated on training data (isotonic); more cities for
more training data.

## Temperature model v3: correcting LAMP by its current error (`temperature_backtest_v3.py`)

**Change from v2.** LAMP's remaining-day max is shifted by alpha x
(latest reading - LAMP's forecast for that hour): if LAMP is running cold
or warm right now, part of that carries into the afternoon. A 12:00 lead
is added.

**Result** (`temperature_backtest_v3.md`): **better than v2 at every same-day
lead, still worse than the market.**

| Lead | v2 skill | v3 skill | v3 alpha |
|---|---|---|---|
| Day before, 16:00 | -0.13 | -0.13 | 0 (no readings yet) |
| Same day, 10:00 | -0.16 | -0.15 | 0.5 |
| Same day, 12:00 | -0.27 | -0.16 | 0.5 |
| Same day, 14:00 | -0.34 | -0.27 | 0.5 |
| All | -0.20 | -0.16 | |

The fitted alpha is 0.5 at all three same-day leads (fitted independently),
which suggests a real effect: about half of LAMP's current miss persists
into the afternoon. Spread tightened from 2.0 to 1.75°F.

**Still no edge.** The market remains sharper, most at 14:00. The live
model uses hourly LAMP runs (the backtest only has 6-hourly), so its live
skill may be a little better; the forward log will tell.
