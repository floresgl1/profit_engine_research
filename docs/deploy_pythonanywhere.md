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
Results: `.venv/bin/profit-engine --db data/maker.db maker-report`: per
strategy, PnL by event day (settled days only in the statistics, unsettled
ones marked at the last mid), mean per day with a 95% interval, per
contract, worst day, max drawdown, largest position; then v2 - v1 paired on
the same days. `--pair-from YYYY-MM-DD` sets the first paired day by hand.

The maker also records what it sees in `data/maker.db` (every tick time and
trade, books and model prices when they change) so other strategies can be
replayed on the same data. Expect roughly 10 MB a day; check disk use on the
Files page now and then (`du -sh data/`).

## Discord notifications (scheduled tasks)

`tools/discord_notify.py` sends a daily summary and problem-only alerts to a
Discord channel. It lives outside the engine package because posting is an
HTTP write and the engine must stay GET-only; it only reads `data/` and only
posts to the webhook.

1. In Discord: Server Settings -> Integrations -> Webhooks -> New Webhook,
   pick the channel, Copy Webhook URL. Treat the URL like a password.
2. On PythonAnywhere (Bash console), store it outside the repository:
   ```
   echo 'PASTE_THE_URL_HERE' > ~/.discord_webhook && chmod 600 ~/.discord_webhook
   ```
3. Make the engine also write its log to a file the health check can read:
   add `--log-file data/engine.log` right after `.venv/bin/profit-engine` in
   the always-on task's command (it is a global option, before `--db`), and
   restart the task.
4. Test: `cd ~/profit_engine_research && .venv/bin/python tools/discord_notify.py summary --dry-run`
   prints the message; without `--dry-run` it posts.
5. Tasks page -> Scheduled tasks (times are UTC):
   - daily at 12:30: `cd /home/YOURUSER/profit_engine_research && .venv/bin/python tools/discord_notify.py summary --pair-from 2026-10-06`
   - hourly: `cd /home/YOURUSER/profit_engine_research && .venv/bin/python tools/discord_notify.py health`

Health alerts: maker's last tick over 15 minutes old; 5+ ERROR log lines in
the last hour; a strategy at its position limit in 3+ open markets; `data/`
over 2 GB. Each repeats at most every 6 hours while it lasts and sends a
"resolved" message when it clears (state in `~/.profit_engine_alerts.json`).

## Current schedule (as deployed, 2026-10-05)

The PythonAnywhere account (`floresgl907`, Developer plan) allows one
always-on task and shares its daily CPU allowance with finance_bot.

**Always-on task** (logger for all seven cities, paper market makers v1 and
v2 on NYC, log file for the health check):

```
cd /home/floresgl907/profit_engine_research && .venv/bin/profit-engine --log-file data/engine.log --db data/research.db ingest --kalshi-series KXHIGHNY,KXHIGHCHI,KXHIGHAUS,KXHIGHMIA,KXHIGHLAX,KXHIGHDEN,KXHIGHPHIL --temperature-params research/temperature_params.json --temperature-params research/temperature_params_lamp_v3.json --temperature-params research/params/KXHIGHCHI.json --temperature-params research/params/KXHIGHAUS.json --temperature-params research/params/KXHIGHMIA.json --temperature-params research/params/KXHIGHLAX.json --temperature-params research/params/KXHIGHDEN.json --temperature-params research/params/KXHIGHPHIL.json --interval 60 --maker-series KXHIGHNY
```

**Scheduled tasks** (UTC), with finance_bot's around them for reference:

| Frequency | Time | Task |
|---|---|---|
| Daily | 12:00 | finance_bot: `dispatch_workflow.py update_marke...` |
| Daily | 12:30 | **profit engine: Discord daily summary** |
| Daily | 13:00 | finance_bot: `pre_run_validation.py` |
| Daily | 13:30 | finance_bot: `sentiment_collector.py` |
| Daily | 14:00 | finance_bot: `generate_signals.py` |
| Daily | 14:15 | finance_bot: `dispatch_workflow.py agent_pretrad...` |
| Daily | 15:00 | finance_bot: `run_bot.py` |
| Daily | 15:30 | finance_bot: `edge_monitor.py` |
| Daily | 15:45 | finance_bot: `pipeline_check.py` |
| Daily | 16:00 | finance_bot: `dispatch_workflow.py passive_alloc...` |
| Hourly | :30 | **profit engine: Discord health check** |

Profit engine commands:

```
cd /home/floresgl907/profit_engine_research && .venv/bin/python tools/discord_notify.py summary --pair-from 2026-10-06
cd /home/floresgl907/profit_engine_research && .venv/bin/python tools/discord_notify.py health
```

The webhook URL is in `~/.discord_webhook` (mode 600), not in the repo.
After changing the always-on command, restart the task and check that
`data/engine.log` appears; a `git pull` alone doesn't change a running task.

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
