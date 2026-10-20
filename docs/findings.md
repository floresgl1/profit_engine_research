# Findings (final, 2026-10-20)

**Question.** Can a read-only research engine, using public data only, find
a repeatable, honest edge in Kalshi (and Polymarket) prediction markets?

**Answer: no.** Every approach either matched the market or lost to it once
costs and fill realism were applied. Where a price could actually be traded,
the market was right. The one place money is made, providing liquidity, is
won on speed this setup can't reach: polling 5x faster made no difference,
and the last filter only lost less by trading less. A maker markout across
every Kalshi category found no place where makers clearly keep enough for a
newcomer to survive.

Paper only throughout: no orders, no credentials, public GET endpoints only
(`tests/test_read_only.py`). The engine was stopped on 2026-10-20.

## What was tried

| # | Approach | Result | Detail |
|---|---|---|---|
| 1 | Forecast model for NYC daily highs (NBM, then LAMP + observations, then a nowcast correction) | Market better at every lead; best version skill -0.16 vs the market's Brier score | `research/README.md` (temperature v1-v3) |
| 2 | Same model, six more cities | No edge anywhere (skill -0.12 to -0.26); smaller markets not softer | `research/cities_backtest.md` |
| 3 | Event arbitrage (buy or sell every bucket) | Positive after fees in 0.13% / 1.2% of event-hours, $4.45 total over 14 months at unknown size | `research/structural_edges.md` |
| 4 | Buckets already ruled out by readings | Cleared within the hour: 17 bid quotes in 89,343 event-hours | `research/structural_edges.md` |
| 5 | Calibration screen, all Kalshi categories | Calibrated: YES at 10-65c won within 0.2c of its price; blind rules lose the spread plus fee | `research/calibration_screen.md` |
| 6 | Calibration screen, Polymarket | Apparent YES bias sat on empty books (median spread 94c): not tradeable | `research/polymarket_screen.md` |
| 7 | Market-maker markout (incumbent makers' fills) | +0.78 +/- 0.28c per contract to settlement: an upper bound for a newcomer | `research/markout.md` |
| 8 | Model veto on maker fills (historical) | +29% total maker PnL on the holdout half | `research/maker_rules.md` |
| 9 | Live paper market maker, honest queue, 10 s ticks (v1 join the touch, v2 + model veto) | v1 -0.73c, v2 -1.02c per contract; every settled day negative | `research/README.md` (paper market maker) |
| 10 | Inventory skew (v3a, v3b) in replay | No improvement (-0.77c, -0.75c); positions capped, losses unchanged | same |
| 11 | Where the maker loses (replay breakdown) | Queue fills about break even; fills where a trade swept through our price lose, and are two thirds of volume | same |
| 12 | Same makers live at 2 s ticks, 10 settled days, pre-agreed stop rule | Both clearly losing: v1 -$25.14 a day [-38.92, -11.36], -0.36c; v2 -$24.94 [-37.00, -12.89], -0.45c. Retired | same |
| 13 | Polling speed: the 2 s recording replayed at 2 s and at 10 s, same days | 2 s not better, slightly worse: v1 -$10.98 a day [-21.89, -0.06]; swept share 67% vs 70% | same |
| 14 | Book-imbalance filter (v4), registered in advance, replay | Passed the registered bar in dollars, but -0.43c per contract vs v1's -0.46c on 43% of the volume: it lost less by trading less. Line closed | same |
| 15 | Maker markout across all Kalshi categories (16,435 markets, 6,751 series), registered bar +1c per contract after fee | All categories together +0.43c [-0.75, +1.62]. World and Exotics passed, each resting on one market or series; World not confirmed on 85 fresh markets (+1.95c [-1.15, +5.06]). Closed | `research/README.md` (cross-category markout) |

## Why

- **Taking prices** pays the spread (5-6c) plus fee. Public forecasts, the
  same ones the crowd watches, never beat the market by that much: the
  market prices LAMP/NBM-level information and more.
- **Making prices** earns the spread only if quotes move out of the way when
  information arrives. Incumbent makers do (+0.78c). A quote refreshed every
  2-10 seconds from the back of the queue gets run over: two thirds of our
  volume was swept through stale quotes, and polling 5x faster barely changed
  that (70% to 67%). The traders doing the sweeping react in well under two
  seconds. Faster polling only rejoined the touch sooner behind moves that
  were still going.
- **Filters that quote less** (the model veto, the imbalance filter) left the
  loss per contract where it was or made it worse. They cut dollar losses
  only by cutting volume. Not trading at all is the limit of that.

## What an edge would need

One of these, none available within this project's rules:
- **Information the crowd lacks** (not public forecasts or readings).
- **Sub-second speed**: Kalshi's real-time feed needs an API key, which the
  no-credentials rule forbids. Anonymous polling tops out around 2 s
  (Kalshi limits anonymous reads by the work they cost), and 2 s didn't help.
- **A market where liquidity providers are scarce but takers aren't**:
  Polymarket's new markets have empty books, but also few takers.

## Methods that mattered

The engine rejected every attractive illusion it met, which is why the
negative results can be trusted:
- a fake 84c arbitrage from crossed hourly candle quotes;
- look-ahead through a close-based decision time (early closes are mostly YES);
- Polymarket "prices" that were midpoints of empty books;
- stale hourly mids that made maker fills look like -10c fills;
- the markout's +0.8c turning negative once the queue was simulated honestly;
- a stop rule agreed before the results, which ended the live makers cleanly;
- replay of the live recording, tested to reproduce a live maker exactly
  (and a live maker polling 5x slower), so strategies and speeds were
  compared on identical market activity;
- comparing per contract, not only per day: v2 and v4 both "won" in dollars
  by trading less;
- testing a fragile pass on fresh data instead of acting on it: World passed
  the cross-category screen on one market and didn't hold on 85 new ones.

## Mistakes worth remembering

- **The imbalance test's pass bar was written only in dollars.** A filter
  that skips quotes at random would pass it too: it loses fewer dollars, and
  its smaller, noisier daily figure reaches zero more easily. The
  volume effect had been worked out a week earlier; the bar should have
  required a better loss per contract. v4 passed and the line was closed
  anyway (`docs/decisions.md`, 2026-10-20).
- **Replay was checked on contracts, not PnL.** Contracts matched live within
  0-3% a day, but over Oct 10-19 replayed v1 lost -0.46c per contract against
  -0.36c live: small timing differences change positions, and positions
  held to settlement swing the dollars. Every comparison was replay against
  replay, so the conclusions stand, but replay's absolute levels are about
  0.1c pessimistic.
- **SQLite in WAL mode on a disk shared between machines.** PythonAnywhere's
  consoles, scheduled tasks and always-on task run on different machines
  over a network mount, and SQLite's locks don't hold between them. A
  compaction and a long replay run from a console while the engine wrote
  gave "file is not a database" and "malformed" errors. Integrity checks
  on stopped copies were clean. Read a copy, never the live file.
- **Rate limits count work, not requests.** Per-market trade reads at 2 s
  ticks drew 429s; one exchange-wide read of two pages per tick still did;
  one page didn't.

## What's left

Nothing runs. The 2-second recording (Oct 9-20) is kept on PythonAnywhere
in `data/rescue/maker.db` (not in the repository), and `replay` can test any
further quoting rule on it. A new idea should first beat v1 per contract on
those days. One loose thread, not a lead: one-off event markets (World,
some Politics) showed positive maker point estimates, consistent with takers
backing longshots, but too noisy to separate from luck.
