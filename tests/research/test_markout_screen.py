from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import markout_screen as ms
import pytest
from calibration_screen import Settled

from profit_engine.maker.queue import PublicTrade

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)


def trade(price, count, taker_yes, s=0, tid="t"):
    return PublicTrade(tid, T0 + timedelta(seconds=s), D(price), D(count), taker_yes)


def market(volume=1000, result=1, ticker="KXA-26MAR01-X", event="KXA-26MAR01"):
    return Settled(ticker, event, T0, T0 + timedelta(seconds=100), result, D(volume))


def test_maker_takes_the_other_side_of_the_taker():
    assert ms.maker_pnl(trade("0.40", 1, True), 1) == D("-0.60")  # taker bought YES; YES won: maker paid out
    assert ms.maker_pnl(trade("0.40", 1, True), 0) == D("0.40")  # NO won: maker kept the price
    assert ms.maker_pnl(trade("0.40", 1, False), 1) == D("0.60")  # taker sold YES: maker bought at 0.40
    assert ms.maker_fee(D("0.40")) == D("0.0042")  # 0.0175 x 0.4 x 0.6


def test_summarize_hand_computed():
    trades = [trade("0.40", 10, True), trade("0.30", 5, False)]  # YES wins: -6.00 and +3.50
    r = ms.summarize(market(result=1), trades, "KXA", "Sports", sampled=False)
    assert (r.contracts, r.trades, r.sampled) == (15, 2, False)
    assert r.pnl == pytest.approx(-2.50)
    assert r.pnl_fee == pytest.approx(-2.50 - 0.042 - 0.018375)  # fees 10 x 0.0042, 5 x 0.0175 x 0.3 x 0.7
    assert r.per_contract == pytest.approx(-2.560375 / 15)


def test_windows_cover_the_life_contiguously():
    lo = int(T0.timestamp())
    assert ms.windows(T0, T0 + timedelta(seconds=100), 4) == [(lo, lo + 25), (lo + 25, lo + 50), (lo + 50, lo + 75), (lo + 75, lo + 100)]


class FakeHttp:
    """/historical/trades over a list of raw trades, newest first, with cursor paging and time windows."""

    def __init__(self, trades, page_size=2):
        self.trades, self.page_size, self.requests = trades, page_size, []

    def get_json(self, path, params):
        assert path == "/historical/trades"
        self.requests.append(dict(params))
        rows = self.trades
        if "min_ts" in params:  # inclusive at both ends, so a trade on a window edge comes back twice
            rows = [t for t in rows if params["min_ts"] <= t["_s"] <= params["max_ts"]]
        start = int(params.get("cursor") or 0)
        more = start + self.page_size < len(rows)
        return {"trades": rows[start : start + self.page_size], "cursor": str(start + self.page_size) if more else ""}


def raw(tid, s, price="0.40", count="1.00", side="yes"):
    at = T0 + timedelta(seconds=s)
    return {"trade_id": tid, "created_time": at.isoformat().replace("+00:00", "Z"), "yes_price_dollars": price,
            "count_fp": count, "taker_outcome_side": side, "is_block_trade": False, "_s": int(at.timestamp())}


def test_small_market_reads_every_trade(monkeypatch):
    monkeypatch.setattr(ms, "CAP", 3)
    http = FakeHttp([raw(str(i), 90 - i) for i in range(5)])  # 3 pages of 2
    trades, sampled = ms.fetch_market(http, market(volume=100))
    assert (len(trades), sampled, len(http.requests)) == (5, False, 3)


def test_market_over_the_cap_is_time_sampled_once_per_window(monkeypatch):
    monkeypatch.setattr(ms, "CAP", 2)
    monkeypatch.setattr(ms, "WINDOWS", 4)
    http = FakeHttp([raw(str(i), s) for i, s in enumerate([95, 94, 93, 60, 50, 10])])  # t50 sits on a window edge
    trades, sampled = ms.fetch_market(http, market(volume=100))
    windowed = [r for r in http.requests if "min_ts" in r]
    assert sampled and len(windowed) == 4 and all("cursor" not in r for r in windowed)  # one page per window
    # one page of 2 per window: [0,25] t10; [25,50] t50; [50,75] t60, t50 (again); [75,100] t95, t94
    assert sorted(t.trade_id for t in trades) == ["0", "1", "3", "4", "5"]  # t50 counted once


def test_big_market_skips_the_full_read(monkeypatch):
    monkeypatch.setattr(ms, "WINDOWS", 2)
    http = FakeHttp([raw("a", 10), raw("b", 90)])
    trades, sampled = ms.fetch_market(http, market(volume=3_000_000))
    assert sampled and all("min_ts" in r for r in http.requests) and len(http.requests) == 2


def mk(series, volume, per_contract, holdout=False, category="X"):
    return ms.MarketMarkout(f"{series}-{volume}", series, category, holdout, volume, volume,
                            per_contract * volume, per_contract * volume, 1, False)  # fmt: skip


def test_weighted_stat_hand_computed():
    rows = [mk("S1", 100, 0.02), mk("S1", 300, 0.0), mk("S2", 100, 0.05)]
    s = ms.weighted_stat(rows)
    # mean (2 + 0 + 5) / 500 = 0.014; series residuals -3.6 and +3.6;
    # se sqrt((3.6^2 + 3.6^2) x 2/1) / 500 = 0.0144; half t95(1) x se = 12.706 x 0.0144
    assert s.mean == pytest.approx(0.014)
    assert s.half == pytest.approx(12.706 * 0.0144)
    assert (s.series, s.markets, s.contracts) == (2, 3, 500)


def test_candidate_is_the_registered_bar():
    def stat(mean, half=0.0):
        return ms.Stat(mean, half, 10, 30, 1e6)

    assert ms.candidate(stat(0.03, 0.015), stat(0.012))  # discovery low +1.5c, holdout +1.2c
    assert not ms.candidate(stat(0.03, 0.015), stat(0.009))  # holdout under +1c
    assert not ms.candidate(stat(0.03, 0.021), stat(0.02))  # discovery range reaches +0.9c
    assert not ms.candidate(None, stat(0.02))


def test_report_lists_categories_and_candidates():
    rows = [mk(f"A{i}", 1000, 0.03, holdout=i % 2 == 1, category="Good") for i in range(6)]
    rows += [mk(f"B{i}", 1000, 0.002 * i, holdout=i % 2 == 1, category="Weak") for i in range(6)]
    text = ms.report(rows, 12)
    assert "| Good | 6 | 6 | 6,000 | +3.00c" in text
    assert "**Candidates: Good.**" in text


def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "CACHE", str(tmp_path))
    done = {"m": mk("S1", 100, 0.02)}
    ms._save(done)
    assert ms._load() == done


def test_fresh_markets_apply_the_screen_filters():
    info = {"KXW": {"category": "World"}, "KXS": {"category": "Sports"}}

    def m(ticker, event, volume=1000, hours=48):
        return Settled(ticker, event, T0, T0 + timedelta(hours=hours), 0, D(volume))

    listing = [m("KXW-1-A", "KXW-1"), m("KXW-1-B", "KXW-1"), m("KXW-2-A", "KXW-2", volume=499),
               m("KXW-3-A", "KXW-3", hours=24), m("KXS-1-A", "KXS-1")]  # fmt: skip
    got = ms.fresh_markets(listing, info, "World", used={"KXW-1-B"})
    assert [x.ticker for x in got] == ["KXW-1-A"]  # used, too small, too short and other categories left out


def test_confirmation_bar_and_report():
    assert ms.confirmed(ms.Stat(0.03, 0.019, 15, 85, 1e6))  # range low +1.1c
    assert not ms.confirmed(ms.Stat(0.03, 0.021, 15, 85, 1e6))  # low +0.9c
    assert not ms.confirmed(None)
    rows = [mk(f"W{i}", 1000, 0.03) for i in range(4)]
    text = ms.confirm_report(rows, "World")
    assert "Markets: 4 in 4 series, 4,000 contracts" in text and "Passes: yes" in text
