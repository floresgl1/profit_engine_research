# Findings (as of 2026-10-08)

**Question.** Can a read-only research engine, using public data only, find
a repeatable, honest edge in Kalshi (and Polymarket) prediction markets?

**Answer so far: no.** Every approach tried either matched the market or lost
to it once costs and fill realism were applied. Where a price could actually
be traded, the market was right. The one place money is made, providing
liquidity, is won on speed this setup does not have.

Paper only throughout: no orders, no credentials, public GET endpoints only
(`tests/test_read_only.py`).

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
| 9 | Live paper market maker, honest queue (v1 join the touch, v2 + model veto) | v1 -0.73c, v2 -1.02c per contract; every settled day negative | `research/README.md` (paper market maker) |
| 10 | Inventory skew (v3a, v3b) in replay | No improvement (-0.77c, -0.75c); positions capped, losses unchanged | same |
| 11 | Where the maker loses (replay breakdown) | Queue fills about break even (+0.26c v1, +0.99c v2); fills where a trade swept through our price lose (-1.23c, -2.03c) and are two thirds of volume | same |

## Why

- **Taking prices** pays the spread (5-6c) plus fee. Public forecasts, the
  same ones the crowd watches, never beat the market by that much: the
  market prices LAMP/NBM-level information and more.
- **Making prices** earns the spread only if quotes move out of the way when
  information arrives. Incumbent makers do (+0.78c); a quote refreshed every
  10 seconds, at the back of the queue, gets run over: our losses are almost
  entirely sweeps through stale quotes, concentrated when news arrives (the
  evening before and 10:00-14:00 on the day).

## What an edge would need

One of these, none available within this project's rules:
- **Information the crowd lacks** (not public forecasts or readings).
- **Speed**: Kalshi's real-time feed needs an API key, which the
  no-credentials rule forbids. Faster polling is possible (2-second ticks
  would cost roughly 1,000-1,500 CPU-seconds a day, inside the 5,000 a day
  shared with finance_bot) but untested: if the traders who sweep stale
  quotes react in under a second, 2 seconds is still too slow.
- **A market where liquidity providers are scarce but takers aren't**:
  Polymarket's new markets have empty books, but also few takers.

## Methods that mattered

The engine rejected every attractive illusion it met, which is why the
negative results can be trusted:
- a fake 84c arbitrage from crossed hourly candle quotes;
- look-ahead through a close-based decision time (early closes are mostly YES);
- Polymarket "prices" that were midpoints of empty books;
- stale hourly mids that made maker fills look like -10c fills;
- the markout's +0.8c turning negative once the queue was simulated honestly.

## Still running

- Logger: seven cities' books and model predictions every minute.
- Paper market makers v1 and v2 on NYC, to the agreed stop rule: after 10
  settled paired days, retire both if their 95% intervals of mean daily PnL
  are entirely below zero (`docs/decisions.md`).
- Recording for replay (exact from 2026-10-09).

## Open follow-up

**Book-imbalance quote filter** (deferred, not built): don't quote a side
whose best level is much thinner than the other side's, a common sign the
price is about to move through it. Test it in replay only on days recorded
exactly (2026-10-09 on), once there are at least 10 settled ones (around
2026-10-20), so it is not tuned on the days that suggested it. If it doesn't
turn swept losses around on those days, close the market-making line.
