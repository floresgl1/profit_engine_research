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
