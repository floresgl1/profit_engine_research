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

## v3 across cities (`cities_backtest.py`)

**Question.** Does the v3 model find an edge in any other Kalshi daily-high
city, where markets may be less efficient than NYC's?

**Method.** Same model, leads and train/test split as NYC v3, run per city
from weather/stations.py, each with its own station undercount calibration
and its own market as the benchmark (identical rows).

**Result** (`cities_backtest.md`): **no edge in any city.**

| City | Overall skill | Best lead skill | 14:00 skill |
|---|---|---|---|
| Philadelphia | -0.12 | -0.09 (10:00) | -0.24 |
| New York (v3) | -0.16 | -0.13 (day before) | -0.27 |
| Chicago | -0.17 | -0.14 (day before, 10:00) | -0.28 |
| Los Angeles | -0.23 | -0.13 (day before) | -1.17 |
| Denver | -0.24 | -0.21 (12:00) | -0.30 |
| Austin | -0.25 | -0.17 (day before) | -0.30 |
| Miami | -0.26 | -0.15 (day before) | -0.68 |

**Reading it.**
- The gap is consistent: every city's market beats the model by 10-26%,
  and every afternoon gap is the largest. The smaller markets are not
  softer: their market Brier scores (0.11-0.13) match NYC's.
- Where the afternoon is most predictable to a local (LA's sea breeze,
  Miami's steady tropical days), the market is near-certain by 14:00 and
  the model, which knows nothing about local regimes, falls furthest
  behind.
- Fitted parameters look physically sensible per city: correction weight
  0.25-0.75 on the same day, day-before spread from 1.75°F (Miami) to
  3.25°F (Denver).

**What it means.** A public-forecast model, however carefully calibrated,
doesn't beat these markets: the market already prices LAMP/NBM-level
information and then some. An edge, if any, would need information or
speed the crowd lacks, not a better treatment of the same forecasts.
Per-city parameters are saved in research/params/ for live logging only.

## Structural edges: event arbitrage and dead buckets (`structural_edges.py`)

**Question.** Without any model, do these markets leave money on the table?
(1) Does buying, or selling, YES on every bucket of an event ever cost less
than it pays, after fees? (2) Once the readings so far rule a bucket out,
is its YES still bid?

**Method.** All seven cities, test period Aug 2025 to Oct 2026, hourly
candle closes from the evening before through close. Markets close at the
end of the local standard-time day, so there is no post-outcome trading
window; test 2 checks buckets ruled out *during* the day, using the rounded
max reading plus the station's smallest training undercount as a lower bound
on the high. Hours where any bucket's close is crossed (bid > ask) are
skipped as artifacts (15 hours, all Philadelphia).

**Result** (`structural_edges.md`): **no usable edge in either.**

1. **Event arbitrage.** Buying every bucket costs a median $1.07-1.09 per
   $1 payout; selling every bucket raises a median $0.95-0.99 of bids against $1
   owed. After fees, 97 of 72,834 event-hours (0.13%) were positive to buy
   and 322 of 26,688 (1.2%) to sell, 25 and 39 of them by 2c or more. Summed
   over fourteen months, one contract set per positive hour, that is $4.45
   at top-of-book sizes we can't see, from quotes that may have lasted
   seconds. Many hits are 00:00-06:00, when books are thin and candle closes
   are least reliable.
2. **Dead buckets.** A bucket the readings have ruled out is quoted bid 0 /
   ask 1c, almost always within the hour: 17 bid quotes in 89,343
   event-hours, worth $1.91 per contract in total. The bound never failed. The one real
   case, Chicago 2025-08-12 (a 17:51 UTC reading of 87.8°F, the high settled at 88,
   while the 86-87 bucket was bid up to 55c for three hours), depends on
   Chicago's smallest undercount being 0; with NYC's -1 it would not count.
   Nothing "certain" (a 'greater' bucket already exceeded) was ever offered
   below $1.

**What it means.** These markets are efficient structurally as well as on
forecasts: the books are internally consistent and someone clears ruled-out
buckets as soon as readings arrive. Hourly data can't rule out edges that
last seconds, but those are a latency race, not research. Next candidates
are different markets (thinner crowds), not these.

Rerun (cached in `data/structural/`): `uv run python research/structural_edges.py`

## Calibration screens: all of Kalshi and Polymarket (`calibration_screen.py`, `polymarket_screen.py`)

**Question.** Is any category or price range of either venue systematically
mispriced, so that a blind rule (buy YES, or buy NO, in a price band) beats
fees? That would be an edge without a model, and would say where to dig.

**Method.** Settled/closed binary markets, Jan 2025 to Aug 2026 (Kalshi) and
Jan 2025 to Oct 2026 (Polymarket). Kalshi: newest archive page of all 14,585
series (557,015 markets), up to 3 per series with volume >= 500, best bid/ask
24 h after open, spreads over 10c dropped: 8,329 scored. Polymarket: 976,455
closed markets, one per event and at most 5 per category per day, CLOB price
24 h after creation: 16,090 scored. Discovery and holdout are disjoint halves
of events; errors clustered by series/category and decision date.

**Two method errors, caught before any result was used.**
1. *Look-ahead through the decision time.* The first version priced markets
   24 h before *close*. Markets that resolve early mostly resolve YES
   (Polymarket: 83% of first-outcome winners closed over a day early vs 61% of
   losers), so the decision time leaked the outcome and YES looked underpriced
   everywhere. Fixed by deciding 24 h after *open*, which can't depend on the
   outcome.
2. *Prices nobody offers.* An empty book's midpoint is about 50c. The Kalshi
   screen drops spreads over 10c; Polymarket's history has no bid/ask, so it
   can't.

**Results.**

- **Kalshi** (`calibration_screen.md`): **calibrated; no flags in either half.**
  YES priced 10-65c won within 0.2c of its price (4,744 markets: mid - won
  +0.002, discovery -0.002, holdout +0.006). Every blind rule loses about what
  it pays: median spread 5-6c plus fee, so buying NO at the bid lost 3.8c per
  contract. No category stood out.
- **Polymarket** (`polymarket_screen.md`, flagged untradeable): Yes/No markets
  look YES-overpriced, +4.5c per contract buying NO after fees, in both
  halves and eight of eleven categories, while markets whose first outcome is
  arbitrary (team vs team) are calibrated. But live day-old Yes/No books priced
  10-65c have a **median spread of 94c**: the "price" is mostly the midpoint
  of an empty book, and most "Will X happen?" questions resolve NO. Kalshi's
  real quotes show no such bias. Treat it as an artifact of untradeable
  prices, not an edge.

**What it means.** Across every category on both venues, prices that can
actually be traded are as good as the outcomes; the only "edges" found were
in prices no one was offering. Together with the temperature and structural
results, these markets are not beatable by public information or simple
rules at the scale of this project. The remaining options are strategic, not
another screen: provide liquidity instead of taking it, or treat the engine
as the learning project it already is.

Rerun (cached in `data/screen/`, `data/polymarket_screen/`):
`uv run python research/calibration_screen.py` and `uv run python research/polymarket_screen.py`

## Market-maker markouts (`markout.py`)

**Question.** Every taker strategy lost the spread plus fees. Does the other
side, the maker who collects the spread, keep it after informed takers take
their share?

**Method.** 40 evenly spaced test-period events per daily-high city (280
events), every public non-block trade (1.43 million trades, 51 million
contracts). For each trade, take the maker's side: entry edge vs the hourly
mid (only for trades within 10 minutes of it; staler mids made the first
preview show a fake -3.8c), 1 h markout vs the first hourly mid an hour
later, and PnL holding to settlement. Volume-weighted, SE clustered by
event; also shown with a conservative maker fee of 0.0175 x P x (1-P), since
the docs don't state what makers pay on these series.

**Result** (`markout.md`): **makers keep a small positive amount.**

| | Entry edge | 1 h markout | Settlement | With maker fee |
|---|---|---|---|---|
| All trades | +2.62c | +0.19c | +0.78 ± 0.28c | +0.64 ± 0.28c |
| Same day 14-18 | +3.19c | -0.73c | +1.02c | +0.93c |
| Price 20-50c | +3.16c | +0.22c | +0.73 ± 0.53c | +0.36 ± 0.53c |

Makers capture about 2.6c at entry and informed takers take back about
1.8c by settlement. Positive in six of seven cities (NYC +1.5c, LA +1.2c);
Philadelphia -1.4 ± 3.0c is noise. The afternoon 1 h markout is the
only negative one: the informed flow arrives when readings are coming in.

**Reading it.** This is the average over fills that happened, i.e. over
the makers who are already there. A new maker joins the back of the queue,
and fills that reach the back of the queue are disproportionately the ones
where the price is about to move against it, so +0.8c is an upper bound. The
next test has to model our queue position and fills honestly on live data;
public history can't.
