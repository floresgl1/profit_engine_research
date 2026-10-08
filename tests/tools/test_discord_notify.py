import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import discord_notify as dn  # noqa: E402

from profit_engine.core import Resolution  # noqa: E402
from profit_engine.maker.quoter import MakerFill  # noqa: E402
from profit_engine.storage import Store  # noqa: E402

NOW = datetime(2026, 10, 6, 12, 30, tzinfo=timezone.utc)


def store_with(tmp_path, last_tick=NOW - timedelta(minutes=2), fills=()):
    s = Store(tmp_path / "maker.db")
    if last_tick:
        s.add_maker_tick(last_tick)
    for strategy, f in fills:
        s.add_maker_fill(strategy, "kalshi", f)
    return s


def fill(market, side, qty, price="0.40"):
    return MakerFill(market, side, D(price), D(qty), D(0), NOW - timedelta(hours=1))


def test_healthy_engine_raises_nothing(tmp_path):
    (tmp_path / "data").mkdir()
    assert dn.problems(store_with(tmp_path), None, str(tmp_path / "data"), NOW) == {}


def test_stale_maker(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    p = dn.problems(store_with(tmp_path, NOW - timedelta(minutes=40)), None, str(data), NOW)
    assert "40 min ago" in p["maker_stale"]


def test_maker_never_ticked(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    p = dn.problems(store_with(tmp_path, last_tick=None), None, str(data), NOW)
    assert "not recorded a tick" in p["maker_stale"]


def test_errors_in_the_last_hour_only(tmp_path):
    log = tmp_path / "engine.log"
    old = "2026-10-06 10:00:00,000 ERROR x: old"
    new = [f"2026-10-06 12:{m:02d}:00,000 ERROR profit_engine.maker.runner: maker tick failed" for m in range(10, 16)]
    log.write_text("\n".join([old, "2026-10-06 12:20:00,000 INFO x: fine", *new]))
    errors = dn.recent_errors(str(log), NOW)
    assert len(errors) == 6
    (tmp_path / "data").mkdir()
    assert "6 ERROR lines" in dn.problems(store_with(tmp_path), str(log), str(tmp_path / "data"), NOW)["errors"]


def test_position_limits_on_open_markets(tmp_path):
    fills = [("v1", fill(f"KXHIGHNY-26OCT06-B{i}", "bid", 50)) for i in range(3)]
    fills.append(("v1", fill("KXHIGHNY-26OCT05-B1", "bid", 50)))  # settled: doesn't count
    s = store_with(tmp_path, fills=fills)
    s.upsert_resolution(Resolution("kalshi", "KXHIGHNY-26OCT05-B1", D(1), NOW))
    (tmp_path / "data").mkdir()
    p = dn.problems(s, None, str(tmp_path / "data"), NOW)
    assert "v1 is at its position limit in 3 open markets" in p["limits:v1"]


def test_alert_throttle_and_resolution():
    msgs, state = dn.alerts_to_send({"disk": "big"}, {}, NOW)
    assert len(msgs) == 1 and "big" in msgs[0]
    msgs, state = dn.alerts_to_send({"disk": "big"}, state, NOW + timedelta(hours=1))
    assert msgs == []  # still throttled
    msgs, state = dn.alerts_to_send({"disk": "big"}, state, NOW + timedelta(hours=7))
    assert len(msgs) == 1
    msgs, state = dn.alerts_to_send({}, state, NOW + timedelta(hours=8))
    assert msgs == [":white_check_mark: **profit engine**: resolved: disk"] and state == {}


def test_summary_fits_discord_limit(tmp_path):
    fills = [("join_touch_v1", fill(f"KXHIGHNY-26OCT0{d}-B{i}", "bid", 1)) for d in range(1, 10) for i in range(30)]
    (tmp_path / "data").mkdir()
    text = dn.summary_text(store_with(tmp_path, fills=fills), str(tmp_path / "data"), NOW)
    assert len(text) <= dn.CONTENT_LIMIT and text.startswith("**Profit engine · Tue Oct 6**")


def test_main_health_dry_run_writes_state(tmp_path):
    store_with(tmp_path, NOW - timedelta(hours=1)).close()
    sent = []
    state = tmp_path / "state.json"
    (tmp_path / "data").mkdir()
    rc = dn.main(["health", "--db", str(tmp_path / "maker.db"), "--log", str(tmp_path / "none.log"),
                  "--data-dir", str(tmp_path / "data"), "--state", str(state)], now=NOW, send=sent.append)
    assert rc == 0 and len(sent) == 1 and "maker_stale" in json.loads(state.read_text())


def test_webhook_url_must_be_discord(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    f = tmp_path / "hook"
    f.write_text("https://example.com/steal\n")
    with pytest.raises(SystemExit):
        dn.webhook_url(str(f))
    f.write_text("https://discord.com/api/webhooks/1/abc\n")
    assert dn.webhook_url(str(f)) == "https://discord.com/api/webhooks/1/abc"


def test_post_retries_once_on_rate_limit(monkeypatch):
    import httpx

    calls, slept = [], []

    class R:
        def __init__(self, code):
            self.status_code = code

        def json(self):
            return {"retry_after": 1.5}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("x", request=None, response=None)

    codes = iter([429, 204])
    monkeypatch.setattr(httpx, "post", lambda url, json, timeout: calls.append(json) or R(next(codes)))
    dn.post("https://discord.com/api/webhooks/1/abc", "hi", sleep=slept.append)
    assert len(calls) == 2 and slept == [1.5] and calls[0]["content"] == "hi"


def test_limit_alert_does_not_flap(tmp_path):
    (tmp_path / "data").mkdir()

    def at_limit(n):
        s = Store(tmp_path / f"m{n}.db")
        s.add_maker_tick(NOW)
        for i in range(n):
            s.add_maker_fill("v1", "kalshi", fill(f"KXHIGHNY-26OCT06-B{i}", "bid", 50))
        return s

    data = str(tmp_path / "data")
    assert "limits:v1" in dn.problems(at_limit(3), None, data, NOW)  # alert at 3
    assert "limits:v1" not in dn.problems(at_limit(2), None, data, NOW)  # not alerted yet: 2 is quiet
    assert "limits:v1" in dn.problems(at_limit(2), None, data, NOW, active={"limits:v1"})  # alerted: 2 still counts
    assert "limits:v1" not in dn.problems(at_limit(1), None, data, NOW, active={"limits:v1"})  # resolved at 1


TICK_LINE = ("2026-10-06 12:27:00,000 INFO profit_engine.maker.runner: maker tick 900: 12 open markets, 4 fills so far; "
             "v1: 6 quoting, open |position| 0; 150 ticks {apart}s apart{target}, cpu 0.024s per tick")


def health_setup(tmp_path, log_lines):
    data = tmp_path / "data"
    data.mkdir()
    (data / "maker.db").write_bytes(b"x" * 3_000_000)
    (data / "research.db").write_bytes(b"x" * 1_000_000)
    log = tmp_path / "engine.log"
    log.write_text("\n".join(log_lines))
    previous = {"size": 2_000_000, "at": (NOW - timedelta(hours=12)).isoformat()}
    return str(log), str(data), previous


def test_health_is_one_line_when_all_is_well(tmp_path):
    log, data, previous = health_setup(tmp_path, [
        "2026-10-05 13:00:00,000 ERROR x: old but within 24 h",
        TICK_LINE.format(apart="2.0", target=" (target 2s)"),
    ])
    s = store_with(tmp_path, fills=[("v1", fill("KXHIGHNY-26OCT06-B1", "bid", 50))])  # at the limit in 1 market: fine
    # 4 MB now vs 2 MB 12 h ago -> +4 MB/day
    assert dn.health_lines(s, log, data, NOW, previous) == [
        ":white_check_mark: running normally: ticks 2.0 s apart, 1 error in 24 h, data 4 MB (+4 MB/day)"]


def test_health_lists_each_problem_then_the_facts(tmp_path):
    errors = [f"2026-10-06 0{h}:00:00,000 ERROR profit_engine.maker.runner: maker tick failed" for h in range(6)]
    log, data, previous = health_setup(tmp_path, [*errors, TICK_LINE.format(apart="3.5", target=" (target 2s)")])
    full = [("join_touch_v1", fill(f"KXHIGHNY-26OCT06-B{i}", "bid", 50)) for i in range(3)]
    s = store_with(tmp_path, last_tick=NOW - timedelta(minutes=20), fills=full)
    assert dn.health_lines(s, log, data, NOW, previous) == [
        ":warning: the maker's last tick was 20 min ago: the always-on task may be down",
        ":warning: maker ticks 3.5 s apart, target 2 s: requests are slow or rate limited "
        "(`grep -c ' -> 429' data/engine.log`)",
        ":warning: 6 errors in the last 24 h; latest: maker tick failed",
        ":warning: v1 join the touch is at its position limit in 3 open markets",
        "-# ticks 3.5 s apart, data 4 MB (+4 MB/day)",
    ]


def test_health_tick_line_without_target_never_counts_as_slow(tmp_path):
    log, data, previous = health_setup(tmp_path, [TICK_LINE.format(apart="9.0", target="")])  # before the target was logged
    lines = dn.health_lines(store_with(tmp_path), log, data, NOW, previous)
    assert lines[0].startswith(":white_check_mark: running normally: ticks 9.0 s apart, 0 errors")


def test_results_hand_computed(tmp_path):
    from profit_engine.core import Level, OrderBook

    fills = [
        ("join_touch_v1", fill("KXHIGHNY-26OCT03-B0", "bid", 10)),  # before --since: not counted
        ("join_touch_v1", fill("KXHIGHNY-26OCT04-B1", "bid", 10)),  # YES: +6.00
        ("join_touch_v1", fill("KXHIGHNY-26OCT05-B2", "bid", 5)),  # NO: -2.00
        ("join_touch_v1", fill("KXHIGHNY-26OCT06-B3", "bid", 10)),  # open, mid 0.50: +1.00
        ("model_veto_v2", fill("KXHIGHNY-26OCT04-B1", "bid", 10)),  # YES: +6.00
    ]
    s = store_with(tmp_path, fills=fills)
    for market, value in (("B0", 0), ("B1", 1), ("B2", 0)):
        day = {"B0": 3, "B1": 4, "B2": 5}[market]
        s.upsert_resolution(Resolution("kalshi", f"KXHIGHNY-26OCT0{day}-{market}", D(value), NOW))
    s.add_snapshot(OrderBook("kalshi", "KXHIGHNY-26OCT06-B3", (Level(D("0.48"), D(5)),), (Level(D("0.52"), D(5)),), NOW))
    # v1: days +6, -2: mean 2, sd 5.657, half 12.706 x 5.657 / sqrt 2 = 50.82; 4.00 over 15 contracts.
    # v2 - v1: days 0, +2: mean 1, half 12.706 x 1.414 / sqrt 2 = 12.71.
    assert dn.results_lines(s, since=datetime(2026, 10, 4).date()) == [
        "**Last settled day: Mon Oct 5**",
        "v1 join the touch: -$2.00 on 5 contracts",
        "v2 model veto: no fills",
        "",
        "**Since Sun Oct 4** (2 of 10 settled days for the stop rule)",
        "v1 join the touch: +$2.00 a day, not clear yet",
        "-# 95% range -$48.82 to +$52.82 · total +$4.00 · +26.67c per contract",
        "v2 model veto: +$6.00 a day, needs 2+ days",
        "-# total +$6.00 · +60.00c per contract · 1 day",
        "v2 vs v1: +$1.00 a day, not clear yet",
        "-# 95% range -$11.71 to +$13.71",
        "",
        "**Still open** (at current prices)",
        "Tue Oct 6: v1 +$1.00",
    ]


def test_verdicts():
    assert dn.verdict(-3.0, -1.0, 5) == "clearly losing"
    assert dn.verdict(1.0, 3.0, 5) == "clearly winning"
    assert dn.verdict(-1.0, 3.0, 5) == "not clear yet"
    assert dn.verdict(float("nan"), float("nan"), 1) == "needs 2+ days"


def test_stop_rule_due_after_ten_days(tmp_path):
    fills = [("join_touch_v1", fill(f"KXHIGHNY-26OCT{d:02d}-B1", "ask", 10)) for d in range(1, 11)]
    s = store_with(tmp_path, fills=fills)
    for d in range(1, 11):
        s.upsert_resolution(Resolution("kalshi", f"KXHIGHNY-26OCT{d:02d}-B1", D(1), NOW))  # every day -6.00
    lines = dn.results_lines(s, since=datetime(2026, 10, 1).date())
    assert "**Since Thu Oct 1** (10 settled days: the stop rule is due; retire v1 and v2 if both are clearly losing)" in lines
    assert "v1 join the touch: -$6.00 a day, clearly losing" in lines


def test_summary_records_size_for_next_growth(tmp_path):
    store_with(tmp_path).close()
    (tmp_path / "data").mkdir()
    summary_state = tmp_path / "summary.json"
    sent = []
    dn.main(["summary", "--db", str(tmp_path / "maker.db"), "--log", str(tmp_path / "none.log"), "--data-dir",
             str(tmp_path / "data"), "--summary-state", str(summary_state)], now=NOW, send=sent.append)
    assert json.loads(summary_state.read_text())["at"] == NOW.isoformat() and len(sent) == 1
