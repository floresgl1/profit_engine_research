# Running the engine on PythonAnywhere

Needs a paid (Developer) account: free accounts have no always-on tasks,
100 CPU-seconds a day and an internet allowlist. The engine uses the one
always-on task; finance_bot's scheduled tasks are unaffected.

## One-time setup (Bash console on PythonAnywhere)

```bash
cd ~
git clone https://github.com/floresgl1/profit_engine_research.git
# Private repo: use a fine-grained GitHub token with read-only "Contents"
# access to this repository, entered as the password when prompted.
cd profit_engine_research
python3.11 -m venv .venv            # or any installed python3.x >= 3.11
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
.venv/bin/python -m pytest -q 2>/dev/null || .venv/bin/pip install pytest && .venv/bin/python -m pytest -q
mkdir -p data
```

Smoke test (3 cycles, then exits):

```bash
.venv/bin/profit-engine --db data/research.db ingest --kalshi-series KXHIGHNY \
  --temperature-params research/temperature_params.json --interval 60 --cycles 3
.venv/bin/profit-engine --db data/research.db status
```

## Always-on task

Tasks page -> Always-on tasks -> command:

```
cd /home/YOURUSER/profit_engine_research && .venv/bin/profit-engine --db data/research.db ingest --kalshi-series KXHIGHNY --temperature-params research/temperature_params.json --temperature-params research/temperature_params_lamp_v3.json --interval 60
```

This logs three models side by side: `midpoint` (the market), `kxhigh_nbm`
(v1) and `kxhigh_lamp_v3` (v3, which supersedes v2). No `--paper-trade`: none has an edge yet
(research/README.md); `profit-engine score` compares them on live data.

### Optional: log every city

Each city's v3 parameters are in `research/params/<SERIES>.json`. To log
predictions for all seven cities (CPU stays small, about 0.1 s per cycle):

```
cd /home/YOURUSER/profit_engine_research && .venv/bin/profit-engine --db data/research.db ingest --kalshi-series KXHIGHNY,KXHIGHCHI,KXHIGHAUS,KXHIGHMIA,KXHIGHLAX,KXHIGHDEN,KXHIGHPHIL --temperature-params research/temperature_params.json --temperature-params research/temperature_params_lamp_v3.json --temperature-params research/params/KXHIGHCHI.json --temperature-params research/params/KXHIGHAUS.json --temperature-params research/params/KXHIGHMIA.json --temperature-params research/params/KXHIGHLAX.json --temperature-params research/params/KXHIGHDEN.json --temperature-params research/params/KXHIGHPHIL.json --interval 60
```

### Paper market maker (same always-on task)

The plan allows one always-on task, so the paper market maker runs inside
the ingest process, on its own thread with its own HTTP client and database
file (`data/maker.db`). It keeps pretend quotes at the best bid and ask of
every NYC bucket, filled only from public trades after the size ahead of it
in the queue has traded (see `src/profit_engine/maker/`). It never places
an order. Replace the always-on task's command with:

```
cd /home/YOURUSER/profit_engine_research && .venv/bin/profit-engine --db data/research.db ingest --kalshi-series KXHIGHNY --temperature-params research/temperature_params.json --temperature-params research/temperature_params_lamp_v3.json --interval 60 --maker-series KXHIGHNY
```

(Add `--maker-series KXHIGHNY` the same way to the all-cities command if you
run that one.) It runs two strategies on the same data: `join_touch_v1` (quote at the best bid and ask) and `model_veto_v2` (the same, minus quotes the temperature model puts more than 20c against us; see research/maker_rules.md). The log gets a `maker tick N` line every 30 ticks (5 minutes) with both.
The ingest line's `cpu Ns total` is now for the whole process, maker
included; the maker added about 0.02-0.05 s per 10 s tick in testing.
Results: `.venv/bin/profit-engine --db data/maker.db maker-report`.

## Watching it

- Task log (Tasks page -> log link): one line per cycle ending
  `cpu Ns total`. The Developer plan allows 5,000 CPU-seconds per day,
  shared with finance_bot; if the task exceeds it, it is stopped until the
  next day. Check the per-day growth after the first day.
- Skip-rate alerts appear in the same log as `ALERT`.
- `profit-engine --db data/research.db status` and `... score` from a console.
- Every city's v3 model logs as `kxhigh_lamp_v3`, so plain `score` pools
  the cities; `score --series KXHIGHCHI` (or a comma-separated list)
  scores one city on its own.

## Updating

```bash
cd ~/profit_engine_research && git pull && .venv/bin/pip install -e .
```

then restart the task from the Tasks page.
