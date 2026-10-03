# profit_engine_research

Phase 1 research infrastructure for prediction market trading: ingest market
data from Kalshi and Polymarket, log model predictions next to market prices,
paper trade with honest fills, and score the model against the market.

**Safety rule:** this project never places real orders. It holds no
credentials, uses only public GET endpoints through one client
(`venues/http.py`), and installs no venue SDK or signing library.
`tests/test_read_only.py` fails the build if any of that changes.

## Setup

```
uv sync
uv run pytest
```

## Usage

```
# Poll Kalshi's NYC high-temperature series and the 20 busiest Polymarket markets every 30 s
uv run profit-engine ingest --kalshi-series KXHIGHNY --polymarket-top 20

# Same, with the placeholder strategy paper trading (the midpoint baseline never trades)
uv run profit-engine ingest --kalshi-series KXHIGHNY --paper-trade --latency-ms 250 --depth-fraction 0.25

# What is stored
uv run profit-engine status

# Brier scores and calibration, model vs market, on resolved markets
uv run profit-engine score
```

Data goes to `data/research.db` (SQLite, git-ignored); `--db` changes it.

## How it fits together

```
venues/ (read-only adapters) --> storage/ (SQLite) --> models/ (P(YES)) --> paper/ (fills) --> scoring/
          \__________________ ingest/ (polling loop, skip-rate alert) _________________/
```

- `core/`: venue-agnostic types. `OrderBook` is always the YES book, both sides best first.
- `venues/`: `MarketDataSource` interface; `kalshi/` flips Kalshi's NO bids into YES asks; `polymarket/` reads the first outcome token's book.
- `paper/`: walk-the-book IOC fills (asks to buy, bids to sell, never past visible depth), depth cap, latency, venue fees.
- `models/`: `Model.predict(market, book)`; `MidpointBaseline` agrees with the market.
- `scoring/`: Brier model vs market on the same predictions, calibration tables.

## Plugging in a model

Implement `name` and `predict(market, book) -> Decimal | None`, add it to the
`models` list in `cli.py:cmd_ingest`, and set it as `trading_model` to paper
trade on it. Its predictions are logged and scored alongside the baseline.

## Design decisions

Every choice, its alternatives, and what is still unverified:
[`docs/decisions.md`](docs/decisions.md).
