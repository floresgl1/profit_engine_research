"""Discord notifications for the paper engine: a daily summary and problem-only alerts.

Lives outside src/profit_engine on purpose: posting to a webhook is an HTTP
write, and the engine package must stay GET-only (tests/test_read_only.py).
It only reads the engine's database and log, and only ever posts to a
Discord webhook URL.

    python tools/discord_notify.py summary --since 2026-10-10   # daily, after Kalshi settles (~12:30 UTC)
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
    last = store.last_maker_tick()
    if last is None:
        found["maker_stale"] = "The maker has not recorded a tick yet. Is the always-on task running with --maker-series?"
    elif now - last > STALE:
        mins = int((now - last).total_seconds() // 60)
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


# --- daily summary -----------------------------------------------------------------------------

NAMES = {"join_touch_v1": "v1 join the touch", "model_veto_v2": "v2 model veto"}
BASELINE = "join_touch_v1"
STOP_RULE_DAYS = 10  # docs/decisions.md: retire v1/v2 after this many settled days if both are clearly losing
SLOW = 1.5  # ticks more than this times their target apart are a problem
TICK_PACE = re.compile(r"(\d+) ticks ([\d.]+)s apart(?: \(target ([\d.]+)s\))?")


def name_of(strategy: str) -> str:
    return NAMES.get(strategy, strategy)


def money(x) -> str:
    x = float(x)
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def day_name(d) -> str:
    return f"{d:%a %b} {d.day}"


def size_text(n: float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def verdict(lo: float, hi: float, days: int, below: str = "clearly losing", above: str = "clearly winning") -> str:
    """Plain words for a 95% interval: entirely below zero, entirely above, or straddling it."""
    if days < 2:
        return "needs 2+ days"
    return below if hi < 0 else above if lo > 0 else "not clear yet"


def tick_pace(log_path: str | None) -> tuple[float, float | None] | None:
    """(seconds between ticks, target seconds or None for older log lines) from the newest tick line."""
    line = last_tick_line(log_path)
    m = TICK_PACE.search(line) if line else None
    if not m:
        return None
    return float(m.group(2)), float(m.group(3)) if m.group(3) else None


def health_lines(store, log_path: str | None, data_dir: str, now: datetime, previous: dict) -> list[str]:
    """One check-mark line when all is well; otherwise a warning line per problem, then the facts."""
    warnings, facts = [], []
    last = store.last_maker_tick()
    if last is None:
        warnings.append("the maker has not recorded a tick yet")
    elif now - last > STALE:
        warnings.append(f"the maker's last tick was {int((now - last).total_seconds() // 60)} min ago: "
                        "the always-on task may be down")  # fmt: skip
    pace = tick_pace(log_path)
    if pace:
        apart, target = pace
        facts.append(f"ticks {apart:.1f} s apart")
        if target and apart > SLOW * target:
            warnings.append(f"maker ticks {apart:.1f} s apart, target {target:g} s: requests are slow or rate "
                            "limited (`grep -c ' -> 429' data/engine.log`)")  # fmt: skip
    if log_path:
        errors = recent_errors(log_path, now, timedelta(hours=24))
        if len(errors) >= ERROR_LIMIT:
            latest = errors[-1].split(": ", 1)[-1][:150]
            warnings.append(f"{len(errors)} errors in the last 24 h; latest: {latest}")
        else:
            facts.append(f"{plural(len(errors), 'error')} in 24 h")
    for strategy, positions in sorted(open_positions(store).items()):
        full = sum(1 for p in positions.values() if abs(p) >= MAX_POSITION)
        if full >= LIMIT_MARKETS:
            warnings.append(f"{name_of(strategy)} is at its position limit in {full} open markets")
    size = dir_size(data_dir)
    growth = ""
    if previous.get("size") is not None and previous.get("at"):
        hours = (now - datetime.fromisoformat(previous["at"])).total_seconds() / 3600
        if hours > 0:
            growth = f" ({(size - previous['size']) / 1e6 * 24 / hours:+.0f} MB/day)"
    facts.append(f"data {size_text(size)}{growth}")
    if size > DISK_LIMIT:
        files = sorted(((dir_size(str(f)) if f.is_dir() else f.stat().st_size, f.name)
                        for f in Path(data_dir).iterdir()), reverse=True)  # fmt: skip
        warnings.append(f"data is over {size_text(DISK_LIMIT)}: "
                        + ", ".join(f"{n} {size_text(b)}" for b, n in files[:3]))  # fmt: skip
    if not warnings:
        return [f":white_check_mark: running normally: {', '.join(facts)}"]
    return [f":warning: {w}" for w in warnings] + [f"-# {', '.join(facts)}"]


def results_lines(store, since=None) -> list[str]:
    """Last settled day, the running averages with plain-word verdicts, and days still open."""
    from profit_engine.core import midpoint
    from profit_engine.maker.report import covered_from, daily, paired, started, stats

    resolutions = {r.market_id: r.yes_value for r in store.resolutions() if r.venue == "kalshi"}
    by_strategy: dict[str, list] = {}
    for strategy, _, fill in store.maker_fills():
        by_strategy.setdefault(strategy, []).append(fill)

    def mark(ticker: str):
        book = store.latest_book("kalshi", ticker)
        return midpoint(book) if book else None

    days_of = {}
    for strategy in sorted(by_strategy, key=name_of):
        days = daily(by_strategy[strategy], resolutions, mark)[0]
        days_of[strategy] = [d for d in days if since is None or d.day >= since]
    days_of = {k: v for k, v in days_of.items() if v}
    if not days_of:
        return ["No paper market-maker fills yet" + (f" since {day_name(since)}." if since else ".")]

    lines = []
    settled = sorted({d.day for days in days_of.values() for d in days if d.settled})
    if settled:
        last = settled[-1]
        lines.append(f"**Last settled day: {day_name(last)}**")
        for strategy, days in days_of.items():
            d = next((d for d in days if d.day == last), None)
            lines.append(f"{name_of(strategy)}: " + (f"{money(d.pnl)} on {d.contracts:.0f} contracts" if d else "no fills"))

    comparisons = []
    runs = store.maker_runs()
    for strategy, days in days_of.items():
        if strategy == BASELINE or BASELINE not in days_of:
            continue
        starts = [started(x, runs, by_strategy) for x in (strategy, BASELINE)]
        start = since or (covered_from(max(t for t in starts if t)) if any(starts) else min(settled, default=None))
        if start is not None:
            comparisons.append((strategy, paired(days_of[BASELINE], days, start)))
    count = max([c.days for _, c in comparisons if c] or [sum(1 for d in days_of.get(BASELINE, []) if d.settled)])
    if lines:
        lines.append("")
    if since is None:
        lines.append(f"**All settled days** ({count})")
    elif count >= STOP_RULE_DAYS:
        lines.append(f"**Since {day_name(since)}** ({count} settled days: the stop rule is due; "
                     "retire v1 and v2 if both are clearly losing)")  # fmt: skip
    else:
        lines.append(f"**Since {day_name(since)}** ({count} of {STOP_RULE_DAYS} settled days for the stop rule)")
    for strategy, days in days_of.items():
        st = stats(days)
        if st is None:
            lines.append(f"{name_of(strategy)}: no settled days yet")
            continue
        lo, hi = st.interval
        lines.append(f"{name_of(strategy)}: {money(st.mean)} a day, {verdict(lo, hi, st.days)}")
        detail = f"95% range {money(lo)} to {money(hi)} · " if st.days > 1 else ""
        days_note = f" · {plural(st.days, 'day')}" if st.days != count else ""  # only when it differs from the heading
        lines.append(f"-# {detail}total {money(st.total)} · {st.per_contract:+.2f}c per contract{days_note}")
    for strategy, c in comparisons:
        if c is None:
            continue
        short = name_of(strategy).split(" ")[0]
        lo, hi = c.interval
        lines.append(f"{short} vs {name_of(BASELINE).split(' ')[0]}: {money(c.mean)} a day, "
                     f"{verdict(lo, hi, c.days, f'{short} clearly behind', f'{short} clearly ahead')}")  # fmt: skip
        if c.days > 1:
            lines.append(f"-# 95% range {money(lo)} to {money(hi)}" + (f" · {plural(c.days, 'day')}" if c.days != count else ""))

    open_days = sorted({d.day for days in days_of.values() for d in days if not d.settled})
    if open_days:
        lines += ["", "**Still open** (at current prices)"]
        for day in open_days:
            parts = []
            for strategy, days in days_of.items():
                d = next((d for d in days if d.day == day), None)
                if d:
                    parts.append(f"{name_of(strategy).split(' ')[0]} {money(d.pnl)}")
            lines.append(f"{day_name(day)}: {', '.join(parts)}")
    return lines


def summary_text(store, data_dir: str, now: datetime, since=None, log_path: str | None = None,
                 previous: dict | None = None) -> str:
    lines = [f"**Profit engine · {day_name(now.date())}**",
             *health_lines(store, log_path, data_dir, now, previous or {}),
             "", *results_lines(store, since)]  # fmt: skip
    text = "\n".join(lines)
    return text if len(text) <= CONTENT_LIMIT else text[: CONTENT_LIMIT - 12] + "\n(trimmed)"


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
    p.add_argument("--since", "--pair-from", dest="since",
                   help="first event day the summary counts (YYYY-MM-DD; default: all days)")
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
            since = date.fromisoformat(args.since) if args.since else None
            send(summary_text(store, args.data_dir, now, since, args.log, previous))
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
