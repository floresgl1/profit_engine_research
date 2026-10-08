# Design decisions

Every choice that shapes the system, with the alternatives and why. Entries
marked **(yours)** were decided by you; the rest were made during the
autonomous build and are open for you to overturn.

## Settled with you

| Decision | Choice | Why |
|---|---|---|
| Language **(yours)** | Python 3.11+ | `decimal` in stdlib, pandas for scoring, official SDKs exist (but see "SDK vs raw HTTP"). |
| Env/deps **(yours)** | uv with lockfile | Reproducible environments. |
| Core types **(yours)** | Frozen dataclasses, validation in `__post_init__` | No dependency in `core/`, every check is readable code. |
| Book orientation | Always the YES book, both sides best first | One book covers buying and selling both outcomes; walking is a loop from index 0. |
| Venue conversion **(yours)** | In the adapter | The fill engine never learns venue quirks (e.g. Kalshi NO bids -> YES asks). |
| Bad snapshots **(yours)** | `InvalidOrderBook` (a `ValueError`) for venue-data problems; ingest logs and skips only those; alert on high skip rate | Bugs in our own code raise `TypeError`/plain `ValueError` and stop the loop. |

## Fees

**Formula.** Both venues charge takers `rate * C * p * (1 - p)` per fill.
`FeeSchedule` holds `rate` plus the venue's rounding rules.

- **Kalshi**: `rate = 0.07 * fee_multiplier` for every `quadratic*` series
  fee type (the `_with_maker_fees` variants only add maker fees, and the paper
  engine only takes). Confirmed against Kalshi's own fee-rounding worked
  example (0.07 * 1 * 0.055 * 0.945 = 0.00363825). Per-fill fee is ceil'd to
  $0.000001; the order's cash change is then rounded against the trader to
  $0.0001 (direct member balance precision). Event-level fee overrides are
  applied when present.
- **Kalshi `flat` fee type**: not modeled. The official fee schedule PDF
  (`kalshi.com/docs/kalshi-fee-schedule.pdf`) returned HTTP 429 to every
  fetch, so the flat table is unknown. Those markets get `fee_schedule=None`
  and the paper engine refuses to trade them.
- **Polymarket**: per-market `feeSchedule.rate` from Gamma, fee rounded to 5
  dp, makers free. Only `exponent == 1` is modeled; the docs say the exponent
  applies to "the price component" without giving the exact formula, so any
  other exponent gets `fee_schedule=None` instead of a guess.
- **Unknown fees never mean zero fees.** A market without a schedule is not
  traded.

**Alternative considered:** per-venue fee classes. Rejected because the two
formulas differ only in rounding; one parameterized type is easier to test
and to store.

## Paper trading engine

| Decision | Choice | Alternatives | Why |
|---|---|---|---|
| Order type | Immediate-or-cancel taker orders only | Resting maker orders | Honest maker fills need queue-position modeling; out of Phase 1 scope. |
| Order larger than allowed size | Fill what the depth cap allows, cancel the rest (`PARTIAL`) | Reject the whole order | Matches how IOC orders behave on both venues; the `reason` field records why. |
| Depth cap | Fraction of **total** visible size on the side taken, default 0.25, floored to the market's contract step | Depth within the limit price; top-of-book only | Simple, conservative, configurable per venue. |
| Latency | Fill against the first book received **at or after** `decided_at + latency`; a book from before arrival is a hard error | Use the last book before arrival | Latency can then only hurt a fill, never help it. Default 250 ms; Polymarket crypto markets add a 150 ms taker delay, so configure per venue. |
| Stale books | Reject when the first book after arrival is more than `max_book_age` (60 s) late | Fill anyway | Polling gaps should not produce fills against a book from minutes later. |
| NO orders | Priced off the YES book at `1 - p` | Separate NO book | Both venues' NO books are exact mirrors (verified live on Polymarket: token 1 levels = 1 - token 0 levels). |
| Holding YES and NO | Both kept; settlement pays `yes * v + no * (1 - v)` | Kalshi-style netting | Same final PnL, simpler bookkeeping. |
| PnL | Realized at settlement (cost basis tracks fees) | Mark-to-market | Phase 1 scores predictions, not intraday PnL. |
| Selling | Only what the paper portfolio holds | Shorting | Keeps cash accounting obvious. |

## Venue access

| Decision | Choice | Alternatives | Why |
|---|---|---|---|
| SDK vs raw HTTP | Raw HTTP through one GET-only client (`venues/http.py`, httpx) | Official Kalshi/Polymarket SDKs | Both SDKs ship order placement (Polymarket's also wallet signing). Not installing them keeps any trading code out of the dependency tree. `tests/test_read_only.py` fails the build if a write method, order endpoint, credential, signing library or venue SDK appears in `src/` or `uv.lock`. |
| Polling vs WebSockets | REST polling | WebSockets | Kalshi's WebSocket requires an API key even for public channels, and keys default to read+write. Polling both venues needs **zero credentials**, which makes the no-orders rule trivially true. A WebSocket adapter can be added later behind the same interface (with a `scopes: ["read"]` Kalshi key). |
| Retries | 429/5xx/transport errors retried with exponential backoff (2, 4, 8, 16 s), `Retry-After` honored; 0.1 s minimum spacing between requests | No retries | The unauthenticated Kalshi API returned 429 on the very first call during testing. |
| Kalshi host | `external-api.kalshi.com/trade-api/v2` | `api.elections.kalshi.com` | Docs now recommend the dedicated external host. |
| Kalshi market selection | By series ticker (e.g. `KXHIGHNY`), status open | All open markets | Kalshi lists thousands of markets; research targets specific series. |
| Kalshi contract step | 0.01 contracts | Whole contracts | Docs: fixed-point counts with 0.01 granularity. |
| Kalshi resolution | Only `finalized` counts, payout = `settlement_value_dollars` | `determined` | A determined result can still be disputed and amended. |
| Kalshi status mapping | initialized->upcoming, active->open, inactive->paused, closed/determined/disputed/amended->closed, finalized->resolved; unknown->closed with a warning | Crash on unknown | A new venue status should not stop ingestion. |

## Storage

| Decision | Choice | Alternatives | Why |
|---|---|---|---|
| Database | SQLite (stdlib `sqlite3`, WAL mode), one file under `data/` | Postgres; Parquet files | Zero setup, single writer is all Phase 1 needs, easy to copy and inspect. Postgres adds a server for no Phase 1 benefit; Parquet is better for analytics but awkward for "first snapshot after time t" lookups. Revisit if multiple processes need to write. |
| Number storage | Decimals as TEXT | REAL | Exact round-trip; no float error in prices, sizes or fees. |
| Timestamps | Fixed-width ISO-8601 UTC TEXT with microseconds | Unix epoch | Human-readable and sorts correctly as text. |
| Book snapshots | One row per poll, levels deduplicated by content hash in `book_states` | Full copy per poll; only store changes | Replay needs to know a book was *observed* at time t even if unchanged; storing the levels once per distinct state keeps that cheap. |
| Skipped snapshots | Logged to `skipped_snapshots` with the reason | Log file only | Lets you audit what was skipped and why after the fact. |
| Migrations | `schema_version` table; mismatch is a hard error | Migration tool | One schema so far; add a tool when a second version exists. |

## Models and strategy

| Decision | Choice | Alternatives | Why |
|---|---|---|---|
| Model interface | `predict(market, book) -> Decimal \| None` (None = abstain) | Market only | A model needs the book to know the current price; abstaining is allowed so a model is never forced to guess. |
| Baseline | `MidpointBaseline`: returns the book midpoint | Last trade price | Its Brier score must equal the market's, which checks the pipeline end to end. Midpoint is also what "market probability" means in scoring. |
| Strategy | `EdgeStrategy` (placeholder): buy YES or NO when `p - ask - fee` beats `min_edge` (0.03); limit price stops the walk where pre-fee edge drops below `min_edge`; at most 50 contracts per outcome per market; hold to resolution | No strategy | The spec has no strategy, but the paper engine needs orders to run end to end. The midpoint baseline never trades by construction. |

## Scoring

| Decision | Choice | Alternatives | Why |
|---|---|---|---|
| Market probability | Book midpoint at prediction time (logged with best bid/ask) | Last trade; ask for YES | Mid is the standard implied probability; bid/ask are stored so you can re-score with another definition later. |
| Same events | Predictions without a two-sided book are dropped from **both** scores and counted as excluded | Score the model on everything | Your spec: model and market on the same events. |
| Multiple predictions per market | Reported two ways: `all` (every prediction) and `last` (latest per market) | One fixed choice | `all` over-weights markets polled longer; `last` weighs markets equally but uses the most-informed price. Seeing both shows whether the gap comes from a few heavily-polled markets. |
| Skill | `1 - model_brier / market_brier`; positive = model better | Raw difference | Scale-free; undefined (shown as `-`) when the market scored a perfect 0. |
| Fractional outcomes | Brier uses the payout value directly (0.5 for a Polymarket 50/50); calibration hit rate = mean outcome | Drop non-binary outcomes | Keeps every resolved market in the score. |
| Calibration buckets | 10 equal-width buckets, `[lower, upper)`, last one includes 1 | Quantile buckets | Readable fixed edges; empty buckets shown as `-`. |

## Polymarket specifics

| Decision | Choice | Alternatives | Why |
|---|---|---|---|
| Market id | `conditionId` | Gamma numeric id; token id | It's what `/v2/resolutions` keys on; token ids live in `venue_meta`. |
| YES side | The market's first outcome (`outcomes[0]`, e.g. "Florida" in "Florida vs. Missouri") | Only Yes/No markets | Every two-outcome market fits the binary model; `yes_label` records which outcome YES means. Markets with more than two outcomes are skipped. |
| Book | Only the first outcome token's book | Both tokens | The two books are exact mirrors (verified live). Halves the requests. |
| Market selection | Top N open markets by 24h volume (default 50), optional `tag_id` | All markets | Thousands of markets; volume keeps the sample liquid. |
| Missing book (404) | Counted as an invalid snapshot and skipped | Crash | Gamma can still say "open" after the CLOB has removed the book (seen live on a resolved market). Other HTTP errors propagate to the ingest loop. |
| Resolution | `/v2/resolutions` with `status == "resolved"`; YES value = `payouts[0] / sum(payouts)` | Gamma `outcomePrices` | Payout vectors are the on-chain settlement; `[1, 1]` gives the 50/50 value 0.5. |
| Contract step / minimum | 0.01 shares; minimum from `orderMinSize` (5 on every market seen) | | |

## Ingest loop

| Decision | Choice | Alternatives | Why |
|---|---|---|---|
| Scheduling | One synchronous loop (`profit-engine ingest`), poll every 30 s; re-list markets and check resolutions every 10 min | cron; APScheduler; asyncio | Simplest thing that works for tens of markets; easy to read and test with a fake clock. |
| Predictions | Every model, every market, every poll | Only on book change | Storage is cheap and scoring's `last` view removes the weighting bias. |
| Live paper fills | After a decision, wait out the latency, then fetch a **fresh** book for that market | Fill on the next poll | The next poll is up to 30 s away, which would silently replace your configured latency with the poll interval. |
| Skip alert | ERROR log when more than 20% of a venue's last 200 snapshot attempts were skipped (needs 20+ attempts); fires once, logs recovery | Email/Slack | No outbound integrations in Phase 1; the hook (`SkipMonitor.on_alert`) is where one would go. |
| Venue outages | `VenueHttpError` skips that venue for one cycle; other exceptions stop the loop | Retry forever / crash on any error | Matches the rule you set: only known-bad data is skippable. |
| Restart | Paper portfolio is rebuilt from stored fills and resolutions, replayed in time order | Persist portfolio state | One source of truth (the fills table). |

## Still unverified (check before relying on them)

- **Kalshi `flat` fee table**: the fee schedule PDF was unreachable (HTTP 429). Those markets are not paper traded.
- **Kalshi balance precision**: assumed direct-member $0.0001. If your account is FCM-cleared (e.g. through a broker), use $0.01 (`KalshiSource(balance_quantum=...)`).
- **Kalshi fractional contracts on every market**: docs say counts have 0.01 granularity; no per-market flag was found.
- **Kalshi unauthenticated rate limits**: not documented (token budgets apply to authenticated requests). The client spaces requests 0.1 s apart and backs off on 429.
- **Polymarket fee exponent other than 1**: formula not documented; such markets get no fee schedule.
- **Polymarket fee collection on buys**: modeled as a USDC cost; if the venue takes it in shares, the economic cost is the same to within rounding.

## Weather research (day window)

| Decision | Choice | Alternatives | Why |
|---|---|---|---|
| Weather data | ACIS (`data.rcc-acis.org`, sid `NYCthr`) for NWS daily highs; IEM ASOS (`station=NYC`, routine + special reports) for observations **(yours)** | NCEI CDO (needs a token); NWS API (short history) | Both public, no credentials, years of history. |
| Calibration days | Standard-time days plus DST days where both midnight hours are 5°F+ below the peak | All days | On other days the error measured would depend on the window being tested (circular). |
| Error band | Worst case in **both** directions; a qualifying hour must beat the rest by more than the band's width | Upper bound only (as first agreed) | The data contains observations above the official high (down to -2.2°F), so the low side matters too. |
| Data quality | Skip days with an observation gap over 90 minutes; drop isolated 5°F+ spikes | Use raw data | First run: a 2-hour gap produced an 8°F "undercount", and one spurious 80°F reading sat between 70 and 71. |
| Calibration variants | Report both all-season and DST-only bands | Pick one | Which one is right is a judgment call; neither produced a qualifying date, so the conclusion doesn't depend on it. |
| Method check | Run the same test on ACIS (known LST) before trusting it on TWC | Skip | A method that can't recover a known answer can't be trusted on an unknown one. It couldn't (no qualifying dates), which is itself the finding. |
| Code placement | Reusable clients in `src/profit_engine/weather/`; one-off analysis in `research/` | Everything in `research/` | The temperature model will need the same clients and window definitions. |

## Temperature model (KXHIGHNY)

| Decision | Choice | Alternatives | Why |
|---|---|---|---|
| Model shape | One distribution over the integer high per event; every bucket priced from it | Six independent bucket models | Buckets always sum to 1; one model serves any strike layout. |
| Forecast | NBM ("NBS") daily max `txn` and spread `xnd` from IEM's as-issued archive | GFS MOS; NWS point forecast | NBM is the blend; `xnd` gives a per-day spread; same product live and in training. |
| Point-in-time | A run counts only from runtime + 2 h; same-day leads use the newest run that still forecasts the day (NBM drops the current day once its max period starts) | Use runtime as availability | No forecast used before it was published. |
| Spread | Bias + `k * max(xnd, 1)` per lead time, chosen over a fixed sigma on held-out log score | Fixed sigma | Better held-out log score (-2.28 vs -2.34). |
| Observations | Floor = floor(max spike-checked reading - 0.8°F in DST / 2.2°F otherwise), then cut and rescale | Ignore; hard floor at the reading | Rounding down keeps a still-possible bucket alive. |
| Abstain | When NBM's hourly forecast puts a midnight hour within 2°F of the high | Always predict | Day window unknown (research/day_window.md). |
| Trading | **Not** used for paper trading; predictions logged only | Trade it | Backtest: Brier skill vs market -0.18 to -0.97 (research/README.md). |

## Temperature model v2

| Decision | Choice | Alternatives | Why |
|---|---|---|---|
| Same-day forecast | GFS LAMP hourly (IEM archive `LAV`), newest run available (runtime + 1 h) | Keep NBM | NBM drops the current day once its max period starts; LAMP is re-issued hourly. |
| Model structure | H = max(observed part, remaining part); CDF of max = product of CDFs | Floor-and-renormalize (v1) | Uses where the day stands, not just a lower bound; backtest skill at 14:00 went from -0.97 to -0.34. |
| Observed part | Rounded max reading + empirical undercount distribution (day-window calibration) | Fixed floor | Measured, not assumed. |
| Fit | Grid-search maximum likelihood of bias and sigma per lead | Moment estimates | The max of two parts has no simple residual to take moments of. |
| Midnight abstain | Exact disputed hours (00:00-01:00 local at both ends) vs the expected high | +/- 2 h window | The wider window wrongly included 01:00, a normal hour (caught by a test). |
| Trading | Not traded; logged next to v1 and the market | Trade | Backtest skill still negative at every lead. |
| Nowcast correction (v3) | Shift LAMP's remaining max by alpha * (latest reading - LAMP's forecast for that hour), alpha fitted per lead | Ignore current error | Held-out skill improved at every same-day lead (all: -0.20 to -0.16); alpha = 0.5 at every lead. |
| Live models | Log midpoint, v1 (`kxhigh_nbm`) and v3 (`kxhigh_lamp_v3`); v2 retired from the live command | Log everything | v3 contains v2 (alpha = 0) and beat it on held-out data. |
| Other cities | Six more KXHIGH series (Chicago, Austin, Miami, LA, Denver, Philadelphia) with per-station undercount | NYC only | More data and possibly softer markets; result: no edge anywhere (-0.12 to -0.26 skill). |
| Structural checks | Event arbitrage (buy/sell every bucket vs payout after fees) and dead buckets (ruled out by readings, still bid) on hourly candles | Live order books only | Model-free, testable on 14 months of history now; result: no usable edge (0.13% / 1.2% of event-hours positive, $4.45 total at unknown size; dead buckets cleared within the hour). |
| Post-outcome trading test | Dropped; replaced by dead buckets during the day | Prices after the day ends | Markets close at the end of the local standard-time day, before the outcome is known for sure. |
| Crossed candle quotes | Skip the event-hour | Use as is | A close of bid 0.99 / ask 0.10 showed a fake 84c arbitrage; a real book can't be crossed. |
| Calibration screen scope | All Kalshi series (archive, newest page each) and Polymarket closed markets; price vs outcome by category and price band; blind buy-YES / buy-NO after fees | Pick one new market by hand | Finds where mispricing is, with data, before building any market-specific pipeline. |
| Screen decision time | 24 h after the market opened (Kalshi open_time, Polymarket createdAt); markets closed by then are skipped | 24 h before close; 24 h before scheduled end | Close times depend on the outcome (Polymarket: 83% of first-outcome winners closed over a day early vs 61% of losers), which made YES look underpriced everywhere. Open times can't. Scheduled ends would need a Kalshi re-list and drop most Polymarket sports. |
| Screen sampling | Kalshi: up to 3 per series (drawn as 6, first 3 kept); Polymarket: one per event, up to 5 per category per day | Uniform random | ~77,000 Kalshi markets settle per day and Kalshi allows ~1 candle request per second; recurring markets would otherwise swamp the sample. |
| Screen statistics | SE clustered by series (Polymarket: category) and decision date, floored at the binomial SE; discovery/holdout halves by event hash; spreads over 10c dropped | Per-market SE; date split | Buckets of an event and same-day ladders share outcomes; unanimous cells had SE 0; busy series cover only recent weeks, unbalancing a date split. |
| Polymarket YES bias | Recorded as an artifact, not an edge | Paper-trade it | Live day-old Yes/No books priced 10-65c have a median spread of 94c (empty), so history prices are placeholder midpoints; Kalshi's real quotes in the same band are calibrated to 0.2c. |
| Market-making test | Markout of every public trade from the maker's side (entry, 1 h, settlement), maker fee shown both ways | Build a paper market maker first | Measures spread minus adverse selection with no simulator and no fill assumptions; result +0.78c per contract (upper bound for a new maker). |
| Markout reference mid | Hourly mid, entry edge only within 10 min of it | Any earlier mid | Stale mids made fills after a bucket died look like -10c fills. |
| Paper market maker v1 | Join best bid and ask on every NYC bucket, 10 contracts, max position 50, conservative maker fee 0.0175 x P x (1-P); no model | Model-informed from the start | An honest baseline that a model-informed quoter must beat. |
| Maker fills | Back of the queue: fill only after the visible size ahead at our price has traded, or on a trade through our price; size ahead shrinks only with trades at our price or when the book shows less (cancels ahead); trades at the posting instant ignored | Fill on any trade at our price | The markout's +0.8c is an upper bound because real makers ahead of us get the good fills; only a queue-aware simulation measures what a newcomer keeps. |
| Maker polling and scope | 10 s, KXHIGHNY only, own thread inside the ingest process, own database file | Separate always-on task; 60 s with the logger; all cities | The plan allows one always-on task; queue position needs frequent books; one series keeps requests (~1/s) and CPU small; separate HTTP client and file mean neither loop can block or lock the other. |
| Maker v2 rule | Veto a quote the v3 model puts more than 20c against us; only 16:00 day before to 16:00 on the day; otherwise like v1 | Skew quotes toward the model; veto at all hours | Chosen on the discovery half of 628k historical maker fills by total PnL (+22%), confirmed on the holdout (+29%). A veto only removes quotes, so it can cost fills but never adds risk; outside the tested window the model was seen disagreeing with near-settled books. |
| v1 vs v2 comparison | Both strategies in the same maker thread, same books and trades every tick, fills stored by strategy name | Separate runs | Identical data makes the comparison paired; no second always-on task needed. |
| Maker report unit | Event day: daily PnL, mean with 95% t interval, per contract, worst day, max drawdown, max position; v2 - v1 paired on days both covered | Per-fill statistics | Fills in one event share an outcome; days are roughly independent. Pairing on identical data removes the day's luck from the comparison. |
| Maker recording | Tick times and all trades always; books, model prices and markets when they change | Nothing; full snapshots every tick | Lets any strategy be replayed on exactly the live data instead of waiting weeks per idea; change-only storage halves books and cuts model prices ~10x (~10 MB a day). |
| Notifications | `tools/discord_notify.py` outside the engine package, run as PythonAnywhere scheduled tasks: daily summary 12:30 UTC, hourly problem-only health check | Notifier inside the engine with a guard exception for discord.com | Posting is an HTTP write; keeping it out of `src/` leaves the read-only guard strict, and a separate process can't slow or crash the maker. Webhook URL kept in `~/.discord_webhook`, never in the repo. |
| Engine log file | `--log-file data/engine.log`, rotating 5 MB x 4 | Parse PythonAnywhere's task log | The health check needs a log it can find and read; rotation bounds disk use. |
| Stored book depth | Best 5 levels per side (`Store(book_levels=5)`), existing data compacted once with `compact-books` | Full depth; drop the logger to NYC; delete old snapshots | Full depth grew data ~226 MB a day; live books average ~54 levels (1,062 bytes stored) vs 155 bytes for the top 5. Scoring uses the touch, current strategies join it, and depth-capped paper fills only get more conservative. Keeps all history and all cities. |
| Maker resolutions | Fetch resolutions for every traded market once closed, not only those with a position | Positions only | A market traded back to flat never got a resolution, so its day stayed "unsettled" forever (Oct 7). |
| Maker stop rule (agreed 2026-10-08) | After 10 settled paired days: if the 95% interval of mean daily PnL is entirely below zero for v1 and v2, retire them; v3 (inventory skew) goes live only if replay shows it fixing the inventory losses on those recorded days | Keep tweaking live | Decided before the results, so a losing experiment ends cleanly instead of becoming "one more tweak". First 4 days: all negative for both (v1 -0.45c per contract settled). |
| Replay | Walk the recorded ticks: last stored book, trades by delivering tick (or since the previous tick for older data), last recorded model price; >2 min tick gap = restart (quotes dropped); compare strategies with replayed v1, not live | Compare replayed strategies with live v1 | Live/replay timing differs slightly on older data; comparing replay with replay keeps the method identical. Tests require exact reproduction of live v1 and v2 fills on new recordings. |
| Recording additions | Trades tagged with the delivering tick; model price changes recorded including "no price" ('' sentinel); resolutions fetched for every recorded market | Leave as is | Needed for exact replay of v1/v2 and to settle markets only a replayed strategy traded. |
| v3 inventory skew (replay-only) | v3a: adding side's size = 10 x (1 - |pos|/50); v3b: plus one tick behind the touch from half the limit; resting quotes shrink in place (keep queue) | Price skew from the start; parameter search | Two fixed variants, no tuning, because four recorded days can't support a parameter search without overfitting. |
| Polling-speed experiment (2026-10-08) | Live maker at 2 s ticks (`--maker-interval 2`); `replay --every 1,5` runs each strategy at 2 s and at 10 s on the same recording, paired per day | Live 10 s vs live 2 s on different days; stop now | Two thirds of maker volume was swept through stale quotes, so speed is the open question. Recording at the faster speed lets the slower one be replayed exactly on the same market activity (tested against a live run polling 5x slower). About 1,000 CPU-s a day more, inside the 5,000 shared with finance_bot. |
| Trade lookback | Re-read trades from 10 s before the previous tick, whatever the interval; ids de-duplicate | 2 s (as at 10 s ticks) | At 2 s ticks a 2 s overlap drops for good every trade published more than about 4 s late, and missing sweeps would make faster polling look better than it is. Kalshi's publication lag couldn't be measured from the development container (rate limited at once). |
| Stop rule after the switch | Live v1/v2 from the switch are 2 s makers: the stop rule applies to them on their own, from the first event day entirely at 2 s (2026-10-10; the switch was at about 17:00 UTC on Oct 8, after Oct 9's markets opened); the 10 s days before stand as the 10 s result | Pool 10 s and 2 s days | Pooling would mix two different makers. If the 2 s makers' intervals are entirely below zero after 10 settled days, the polling speed within reach doesn't fix making; with the imbalance filter as the last replay test, the market-making line closes. |
| Discord daily summary wording (2026-10-08) | Plain words: one health line (warnings only when something is wrong), last settled day in dollars, means with a verdict from the 95% interval (clearly losing / clearly winning / not clear yet) and the interval in small text, stop-rule progress, open days; counted from `--since` | The maker-report table in a code block | Read on a phone at a glance; the verdict is exactly the stop rule's test (interval entirely below zero), so nothing is lost by saying it in words. Full statistics stay in `maker-report`. |
| Compaction guard (2026-10-08) | `compact-books` refuses if the newest maker tick or stored book is under 2 minutes old (`--force` overrides) | Rely on SQLite's lock; docs only | A compaction run with the task still writing wasn't blocked (SQLite locks don't hold between PythonAnywhere's console and task machines); the task's writes failed with "file is not a database" for 20 s until it restarted. Both files checked ok afterwards. The recorded data says whether the engine is writing, whatever the filesystem does. |
