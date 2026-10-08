import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from factories import make_market

from profit_engine.core import Level, OrderBook, Resolution
from profit_engine.maker.queue import PublicTrade
from profit_engine.maker.quoter import MakerFill, QuoterConfig
from profit_engine.maker.runner import OVERLAP, MakerRunner, RunnerConfig, Strategy
from profit_engine.storage import Store
from profit_engine.venues.kalshi.adapter import parse_trade

T0 = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)


def at(s):
    return T0 + timedelta(seconds=s)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class FakeSource:
    def __init__(self):
        self.open = ["M"]
        self.trades: list[PublicTrade] = []
        self.calls = []
        self.resolved = {}

    def list_markets(self):
        return [make_market(market_id=t) for t in self.open]

    def fetch_books(self, markets):
        return {m.market_id: OrderBook("kalshi", m.market_id, (Level(D("0.40"), D(5)),), (Level(D("0.44"), D(5)),), T0)
                for m in markets}

    def fetch_recent_trades(self, tickers, since):
        self.calls.append(since)
        return {ticker: [t for t in self.trades if t.at >= since] for ticker in tickers}

    def fetch_markets(self, tickers):
        return {t: make_market(market_id=t) for t in tickers}

    def fetch_resolutions(self, markets):
        return {m.market_id: self.resolved[m.market_id] for m in markets if m.market_id in self.resolved}


def runner(source, store, clock, strategies=None, fair=None, record=True):
    strategies = strategies or [Strategy("v1", QuoterConfig(maker_fee_rate=D(0)))]
    config = RunnerConfig(market_refresh=timedelta(seconds=30), record=record)
    return MakerRunner(source, store, strategies, config, now=clock, fair=fair)


def test_fills_once_per_trade_and_survive_restart(tmp_path):
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    r = runner(src, store, clock)
    r.tick()  # t=0: quotes posted, no trades read yet
    src.trades = [PublicTrade("a", at(5), D("0.44"), D(8), True)]  # 5 ahead on the ask, 3 to us
    clock.t = at(10)
    assert [f.quantity for f in r.tick()] == [D(3)]
    clock.t = at(20)
    assert r.tick() == []  # re-read with overlap, de-duplicated by id
    assert src.calls[-1] == at(10) - OVERLAP
    again = runner(src, store, clock)  # restart: position and cash rebuilt from the store
    assert again.makers["v1"]["M"].position == D(-3) and again.makers["v1"]["M"].cash == D("1.32")


def test_closed_market_stops_quoting_and_records_resolution(tmp_path):
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    r = runner(src, store, clock)
    r.tick()
    src.trades = [PublicTrade("a", at(5), D("0.39"), D(1), False)]  # swept through our 0.40 bid: +10
    clock.t = at(10)
    r.tick()
    assert r.makers["v1"]["M"].position == D(10)
    src.open, src.resolved = [], {"M": Resolution("kalshi", "M", D(1), at(40))}
    clock.t = at(40)
    r.tick()
    assert r.makers["v1"]["M"].quotes == {}
    assert store.resolution("kalshi", "M").yes_value == D(1)


def test_parse_trade():
    raw = {"trade_id": "x", "created_time": "2026-10-05T15:00:01Z", "yes_price_dollars": "0.4400", "count_fp": "3.00",
           "taker_outcome_side": "no", "is_block_trade": False}
    t = parse_trade(raw)
    assert (t.trade_id, t.yes_price, t.count, t.taker_yes) == ("x", D("0.44"), D(3), False)
    assert parse_trade(dict(raw, is_block_trade=True)) is None
    assert parse_trade(dict(raw, taker_outcome_side="maybe")) is None


def test_store_round_trip(tmp_path):
    store = Store(tmp_path / "m.db")
    f = MakerFill("M", "ask", D("0.44"), D(3), D("0.001"), T0)
    store.add_maker_fill("s1", "kalshi", f)
    store.add_maker_fill("s2", "kalshi", f)
    assert store.maker_fills("s1") == [("s1", "kalshi", f)]
    assert len(store.maker_fills()) == 2


def test_no_trade_requests_while_not_quoting(tmp_path):
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    src.fetch_books = lambda markets: {m.market_id: OrderBook("kalshi", m.market_id, (Level(D("0.40"), D(5)),), (), T0)
                                       for m in markets}  # one-sided: no quotes
    r = runner(src, store, clock, record=False)
    for s in (0, 10, 20):
        clock.t = at(s)
        r.tick()
    assert src.calls == [] and r.makers["v1"]["M"].quotes == {}


def test_strategies_share_data_and_v2_vetoes_with_the_model(tmp_path):
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    v1 = Strategy("v1", QuoterConfig(maker_fee_rate=D(0)))
    v2 = Strategy("v2", QuoterConfig(maker_fee_rate=D(0), model_margin=D("0.20")))
    # book 0.40 / 0.44. Model says 0.10: buying at 0.40 is 30c against us (> 20c) -> v2 pulls its bid.
    r = runner(src, store, clock, [v1, v2], fair=lambda market, book: D("0.10"))
    r.tick()
    assert set(r.makers["v1"]["M"].quotes) == {"bid", "ask"}
    assert set(r.makers["v2"]["M"].quotes) == {"ask"}
    src.trades = [PublicTrade("a", at(5), D("0.39"), D(1), False)]  # sweeps the bid: only v1 was there
    clock.t = at(10)
    r.tick()
    assert [s for s, _, _ in store.maker_fills()] == ["v1"]


def test_model_failure_falls_back_to_v1_behaviour(tmp_path):
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    v2 = Strategy("v2", QuoterConfig(maker_fee_rate=D(0), model_margin=D("0.20")))

    def broken(market, book):
        raise RuntimeError("LAMP down")

    r = runner(src, store, clock, [v2], fair=broken)
    r.tick()
    assert set(r.makers["v2"]["M"].quotes) == {"bid", "ask"}


def test_strategy_names_must_be_distinct(tmp_path):
    import pytest

    s = Strategy("same", QuoterConfig())
    with pytest.raises(ValueError):
        MakerRunner(FakeSource(), Store(tmp_path / "m.db"), [s, s])


def test_records_books_trades_fair_and_runs_for_replay(tmp_path):
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    v2 = Strategy("v2", QuoterConfig(maker_fee_rate=D(0), model_margin=D("0.20")))
    r = runner(src, store, clock, [v2], fair=lambda market, book: D("0.42"))
    r.tick()
    src.trades = [PublicTrade("a", at(5), D("0.44"), D(2), True), PublicTrade("b", at(6), D("0.40"), D(1), False)]
    clock.t = at(10)
    r.tick()
    clock.t = at(20)
    r.tick()  # overlap re-read: trades stored once
    assert [t.trade_id for t in store.maker_trades("M")] == ["a", "b"]
    assert store.maker_trades("M")[0] == src.trades[0]
    assert len(list(store.books("kalshi", "M"))) == 1  # same book every tick: stored once
    assert [f for _, f in store.maker_fair("M")] == [D("0.42")]  # same model price: stored once
    assert store.maker_ticks() == [at(0), at(10), at(20)]
    assert store.maker_runs() == {"v2": T0}
    assert store.market("kalshi", "M") is not None


def test_recording_reads_trades_even_when_not_quoting(tmp_path):
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    src.fetch_books = lambda markets: {m.market_id: OrderBook("kalshi", m.market_id, (Level(D("0.40"), D(5)),), (), T0)
                                       for m in markets}
    src.trades = [PublicTrade("a", at(5), D("0.40"), D(2), False)]
    r = runner(src, store, clock)
    r.tick()
    clock.t = at(10)
    r.tick()
    assert r.makers["v1"]["M"].quotes == {} and [t.trade_id for t in store.maker_trades("M")] == ["a"]


def test_flat_market_still_gets_its_resolution(tmp_path):
    # Bought 10 and sold 10 back: position 0. The day can only count as settled once
    # this market has a resolution, so the runner must fetch it anyway.
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    store.add_maker_fill("v1", "kalshi", MakerFill("M", "bid", D("0.40"), D(10), D(0), T0))
    store.add_maker_fill("v1", "kalshi", MakerFill("M", "ask", D("0.44"), D(10), D(0), T0))
    src.open, src.resolved = [], {"M": Resolution("kalshi", "M", D(0), at(5))}
    r = runner(src, store, clock)
    assert r.makers["v1"]["M"].position == 0
    r.tick()
    assert store.resolution("kalshi", "M").yes_value == D(0)


def test_deep_level_changes_are_not_recorded(tmp_path):
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    depth = {"n": 7}

    def books(markets):
        bids = tuple(Level(D("0.40") - D("0.01") * i, D(5)) for i in range(depth["n"]))
        return {m.market_id: OrderBook("kalshi", m.market_id, bids, (Level(D("0.44"), D(5)),), T0) for m in markets}

    src.fetch_books = books
    r = runner(src, store, clock)
    r.tick()
    depth["n"] = 9  # only levels 6+ change: the stored top 5 are identical
    clock.t = at(10)
    r.tick()
    assert len(list(store.books("kalshi", "M"))) == 1


def test_tick_log_counts_open_markets_only(tmp_path, caplog):
    import logging

    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    store.add_maker_fill("v1", "kalshi", MakerFill("OLD", "bid", D("0.40"), D(50), D(0), T0))  # settled market
    r = runner(src, store, clock)
    with caplog.at_level(logging.INFO):
        r.run_forever(cycles=1)
    assert "v1: 1 quoting, open |position| 0" in caplog.text
    assert re.search(r"1 ticks \d+\.\ds apart \(target 10s\), cpu \d+\.\d{3}s per tick", caplog.text)  # the real pace


def test_late_published_trade_is_read_at_two_second_ticks(tmp_path):
    # A trade printed at t=1 that the venue only shows from t=7 (6 s late) is still read, once.
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    src.fetch_recent_trades = lambda tickers, since: {
        ticker: [t for t in late if t.at >= since and clock.t >= at(7)] for ticker in tickers}
    late = [PublicTrade("a", at(1), D("0.44"), D(8), True)]  # 5 ahead on the ask, 3 to us
    r = runner(src, store, clock)
    fills = []
    for s in range(0, 13, 2):
        clock.t = at(s)
        fills += r.tick()
    assert [(f.quantity, f.at) for f in fills] == [(D(3), at(8))]
    assert [tick for _, tick in store.maker_trade_ticks("M")] == [at(8)]


def test_one_trades_request_per_tick_for_all_markets(tmp_path):
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    src.open = ["M", "N", "P"]
    r = runner(src, store, clock)
    for s in (0, 10, 20):
        clock.t = at(s)
        r.tick()
    assert src.calls == [at(0) - OVERLAP, at(10) - OVERLAP]  # none on the first tick, then one per tick


def test_failed_trades_read_loses_nothing(tmp_path):
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    r = runner(src, store, clock)
    r.tick()
    src.trades = [PublicTrade("a", at(5), D("0.44"), D(8), True)]  # 5 ahead on the ask, 3 to us
    real = src.fetch_recent_trades

    def down(tickers, since):
        raise RuntimeError("network")

    src.fetch_recent_trades = down
    clock.t = at(10)
    try:
        r.tick()
    except RuntimeError:
        pass
    src.fetch_recent_trades = real
    clock.t = at(20)
    assert [(f.quantity, f.at) for f in r.tick()] == [(D(3), at(20))]
    assert src.calls[-1] == at(0) - OVERLAP  # read from where the last good read left off
    assert at(10) not in store.maker_ticks()  # the failed tick isn't recorded, so replay skips it too


def test_trades_read_while_the_book_is_unusable_arrive_at_the_next_good_tick(tmp_path):
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    good = src.fetch_books
    r = runner(src, store, clock)
    r.tick()
    src.trades = [PublicTrade("a", at(5), D("0.40"), D(2), False)]
    src.fetch_books = lambda markets: {}  # no book this tick
    clock.t = at(10)
    r.tick()
    assert store.maker_trades("M") == []
    src.fetch_books = good
    clock.t = at(20)
    r.tick()
    assert [(t.trade_id, tick) for t, tick in store.maker_trade_ticks("M")] == [("a", at(20))]
