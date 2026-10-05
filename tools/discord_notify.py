"""Discord notifications for the paper engine: a daily summary and problem-only alerts.

Lives outside src/profit_engine on purpose: posting to a webhook is an HTTP
write, and the engine package must stay GET-only (tests/test_read_only.py).
It only reads the engine's database and log, and only ever posts to a
Discord webhook URL.

    python tools/discord_notify.py summary   # daily, after Kalshi settles (~12:30 UTC)
    python tools/discord_notify.py health    # hourly; silent unless something is wrong

Alerts (health):
- the maker's last tick is more than STALE old (the engine is down or stuck);
- ERROR_LIMIT or more ERROR lines in the log in the last hour;
- a strategy holds the maximum position in LIMIT_MARKETS or more open markets
  (resolved only once it is down to CLEAR_MARKETS or fewer);
- the data directory is larger than DISK_LIMIT bytes.
Each alert repeats at most every REPEAT while it lasts, and a short
"resolved" message is sent when it clears (state in ~/.profit_engine_alerts.json).

The webhook URL is a credential: it is read from $DISCORD_WEBHOOK_URL or a
file (default ~/.discord_webhook) and never stored in the repository.
Discord API: POST {webhook url} with JSON {"content": ...}, content up to
2000 characters (docs.discord.com/developers/resources/webhook, 2026-10-05).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

STALE = timedelta(minutes=15)
ERROR_LIMIT = 5
LIMIT_MARKETS = 3  # alert when a strategy is at its limit in this many open markets...
CLEAR_MARKETS = 1  # ...and only call it resolved at this many or fewer (no flapping around 3)
MAX_POSITION = Decimal(50)
DISK_LIMIT = 2_000_000_000
REPEAT = timedelta(hours=6)
CONTENT_LIMIT = 2000
WEBHOOK_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
LOG_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (\w+) ")


# --- inputs ------------------------------------------------------------------------------------


def webhook_url(path: str) -> str:
    url = os.environ.get("DISCORD_WEBHOOK_URL") or Path(path).expanduser().read_text().strip()
    if not url.startswith(WEBHOOK_PREFIXES):
        raise SystemExit("webhook URL must start with https://discord.com/api/webhooks/")
    return url


def dir_size(path: str) -> int:
    return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())


def recent_errors(log_path: str, now: datetime, window: timedelta = timedelta(hours=1)) -> list[str]:
    """ERROR lines from the last `window` of the engine log (its own format, times in UTC)."""
    path = Path(log_path)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines()[-20000:]:
        m = LOG_TIME.match(line)
        if m and m.group(2) == "ERROR":
            at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if now - at <= window:
                out.append(line)
    return out


def open_positions(store) -> dict[str, dict[str, Decimal]]:
    """strategy -> market -> position, for markets without a recorded resolution."""
    resolved = {r.market_id for r in store.resolutions()}
    out: dict[str, dict[str, Decimal]] = {}
    for strategy, _, fill in store.maker_fills():
        if fill.market_id in resolved:
            continue
        book = out.setdefault(strategy, {})
        book[fill.market_id] = book.get(fill.market_id, Decimal(0)) + fill.position
    return out


# --- checks ------------------------------------------------------------------------------------


def problems(
    store, log_path: str | None, data_dir: str, now: datetime, active: frozenset[str] | set[str] = frozenset()
) -> dict[str, str]:
    """Current problems, keyed so repeats of the same problem can be throttled.

    `active`: keys alerted earlier and not yet resolved (for hysteresis).
    """
    found = {}
    ticks = store.maker_ticks()
    if not ticks:
        found["maker_stale"] = "The maker has not recorded a tick yet. Is the always-on task running with --maker-series?"
    elif now - ticks[-1] > STALE:
        mins = int((now - ticks[-1]).total_seconds() // 60)
        found["maker_stale"] = f"The maker's last tick was {mins} min ago. The always-on task may be down or stuck."
    if log_path:
        errors = recent_errors(log_path, now)
        if len(errors) >= ERROR_LIMIT:
            found["errors"] = f"{len(errors)} ERROR lines in the last hour. Latest:\n{errors[-1][:300]}"
    for strategy, positions in open_positions(store).items():
        full = [m for m, p in positions.items() if abs(p) >= MAX_POSITION]
        key = f"limits:{strategy}"
        if len(full) >= LIMIT_MARKETS or (key in active and len(full) > CLEAR_MARKETS):
            found[f"limits:{strategy}"] = f"{strategy} is at its position limit in {len(full)} open markets: {', '.join(sorted(full)[:5])}"
    size = dir_size(data_dir)
    if size > DISK_LIMIT:
        found["disk"] = f"{data_dir} is {size / 1e9:.1f} GB."
    return found


def alerts_to_send(current: dict[str, str], state: dict[str, str], now: datetime) -> tuple[list[str], dict[str, str]]:
    """Messages to send now and the new state (key -> last sent, ISO)."""
    messages, new_state = [], {}
    for key, text in current.items():
        last = state.get(key)
        if last is None or now - datetime.fromisoformat(last) >= REPEAT:
            messages.append(f":warning: **profit engine**: {text}")
            new_state[key] = now.isoformat()
        else:
            new_state[key] = last
    for key in state:
        if key not in current:
            messages.append(f":white_check_mark: **profit engine**: resolved: {key}")
    return messages, new_state


def last_tick_line(log_path: str | None) -> str | None:
    """The newest 'maker tick' log line, from 'maker tick' on."""
    if not log_path or not Path(log_path).exists():
        return None
    for line in reversed(Path(log_path).read_text(errors="replace").splitlines()[-2000:]):
        if "maker tick" in line:
            return line[line.index("maker tick"):]
    return None


def status_lines(store, log_path: str | None, data_dir: str, now: datetime, previous: dict) -> list[str]:
    """Engine health for the daily summary: errors, quoting, positions, data size and growth."""
    lines = []
    if log_path:
        lines.append(f"errors in the last 24 h: {len(recent_errors(log_path, now, timedelta(hours=24)))}")
        tick = last_tick_line(log_path)
        if tick:
            lines.append(tick[:300])
    for strategy, positions in sorted(open_positions(store).items()):
        held = {m: p for m, p in positions.items() if p != 0}
        at_limit = sum(1 for p in held.values() if abs(p) >= MAX_POSITION)
        lines.append(f"{strategy}: {len(held)} open markets held, |position| {sum(abs(p) for p in held.values())}, "
                     f"{at_limit} at the limit")  # fmt: skip
    size = dir_size(data_dir)
    files = sorted(((dir_size(str(f)) if f.is_dir() else f.stat().st_size, f.name) for f in Path(data_dir).iterdir()), reverse=True)
    growth = ""
    if previous.get("size") is not None and previous.get("at"):
        hours = (now - datetime.fromisoformat(previous["at"])).total_seconds() / 3600
        if hours > 0:
            growth = f", +{(size - previous['size']) / 1e6 * 24 / hours:.0f} MB/day"
    lines.append(f"data {size / 1e6:.0f} MB{growth}: " + ", ".join(f"{n} {b / 1e6:.0f} MB" for b, n in files[:4]))
    return lines


def summary_text(store, data_dir: str, now: datetime, pair_from=None, log_path: str | None = None,
                 previous: dict | None = None) -> str:
    from profit_engine.core import midpoint
    from profit_engine.maker.report import render

    resolutions = {r.market_id: r.yes_value for r in store.resolutions() if r.venue == "kalshi"}
    by_strategy: dict[str, list] = {}
    for strategy, _, fill in store.maker_fills():
        by_strategy.setdefault(strategy, []).append(fill)

    def mark(ticker: str):
        book = store.latest_book("kalshi", ticker)
        return midpoint(book) if book else None

    report = render(by_strategy, resolutions, mark, store.maker_runs(), pair_from=pair_from)
    ticks = store.maker_ticks()
    age = f"{int((now - ticks[-1]).total_seconds() // 60)} min ago" if ticks else "never"
    head = "\n".join([f"**profit engine daily** ({now:%Y-%m-%d %H:%M} UTC), last maker tick {age}",
                      *status_lines(store, log_path, data_dir, now, previous or {})])
    return fit(head, report)


def fit(head: str, body: str) -> str:
    """Discord's 2000-character limit: keep the head, trim the body from the middle of its day lists."""
    room = CONTENT_LIMIT - len(head) - len("\n```\n\n```") - 20
    if len(body) > room:
        body = body[: room - 15] + "\n... (trimmed)"
    return f"{head}\n```\n{body}\n```"


# --- sending -----------------------------------------------------------------------------------


def post(url: str, content: str, sleep: Callable[[float], None] = time.sleep) -> None:
    import httpx

    payload = {"content": content[:CONTENT_LIMIT], "username": "profit-engine"}
    for attempt in range(2):
        r = httpx.post(url, json=payload, timeout=20)
        if r.status_code == 429 and attempt == 0:
            sleep(min(float(r.json().get("retry_after", 5)), 30))
            continue
        r.raise_for_status()
        return


def main(argv: list[str] | None = None, now: datetime | None = None, send: Callable[[str], None] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("command", choices=["summary", "health"])
    p.add_argument("--db", default="data/maker.db")
    p.add_argument("--log", default="data/engine.log", help="engine log written with --log-file")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--webhook-file", default="~/.discord_webhook")
    p.add_argument("--state", default="~/.profit_engine_alerts.json")
    p.add_argument("--summary-state", default="~/.profit_engine_summary.json", help="last data size, for growth per day")
    p.add_argument("--pair-from", help="first paired day for the summary (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true", help="print instead of posting")
    args = p.parse_args(argv)

    from datetime import date

    from profit_engine.storage import Store

    now = now or datetime.now(timezone.utc)
    if send is None:
        send = print if args.dry_run else (lambda text, url=webhook_url(args.webhook_file): post(url, text))
    store = Store(args.db)
    try:
        if args.command == "summary":
            summary_path = Path(args.summary_state).expanduser()
            previous = json.loads(summary_path.read_text()) if summary_path.exists() else {}
            pair_from = date.fromisoformat(args.pair_from) if args.pair_from else None
            send(summary_text(store, args.data_dir, now, pair_from, args.log, previous))
            summary_path.write_text(json.dumps({"size": dir_size(args.data_dir), "at": now.isoformat()}))
            return 0
        state_path = Path(args.state).expanduser()
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        messages, new_state = alerts_to_send(problems(store, args.log, args.data_dir, now, set(state)), state, now)
        for m in messages:
            send(m)
        state_path.write_text(json.dumps(new_state))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
