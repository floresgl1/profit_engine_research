# profit_engine_research

Phase 1 research infrastructure for prediction market trading.

**Safety rule:** this project never places real orders. No module may contain
code that writes to a venue. Kalshi is read via public REST (and, if streaming,
a key created with `scopes: ["read"]` only). Polymarket is public market data
only, with no wallet and no signing.

## Layout

```
src/profit_engine/
  core/      venue-agnostic types
  venues/    read-only MarketDataSource + per-venue adapters
  storage/   snapshots, predictions, fills
  ingest/    what to fetch and how often
  models/    Model interface + baselines
  paper/     paper trading fill engine
  scoring/   Brier scores, calibration
tests/
```

## Setup

```
uv sync
uv run pytest
```
