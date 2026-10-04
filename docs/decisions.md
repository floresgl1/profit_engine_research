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
