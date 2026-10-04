from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from factories import make_market

from profit_engine.core import Level, OrderBook, Resolution
from profit_engine.maker.queue import PublicTrade
from profit_engine.maker.quoter import MakerFill, QuoterConfig
from profit_engine.maker.runner import MakerRunner, RunnerConfig, results, summary
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

    def fetch_trades(self, ticker, since):
        self.calls.append(since)
        return [t for t in self.trades if t.at >= since]

    def fetch_markets(self, tickers):
        return {t: make_market(market_id=t) for t in tickers}

    def fetch_resolutions(self, markets):
        return {m.market_id: self.resolved[m.market_id] for m in markets if m.market_id in self.resolved}


def runner(source, store, clock):
    return MakerRunner(source, store, QuoterConfig(maker_fee_rate=D(0)), RunnerConfig(market_refresh=timedelta(seconds=30)),
                       now=clock)


def test_fills_once_per_trade_and_survive_restart(tmp_path):
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    r = runner(src, store, clock)
    r.tick()  # t=0: quotes posted, no trades read yet
    src.trades = [PublicTrade("a", at(5), D("0.44"), D(8), True)]  # 5 ahead on the ask, 3 to us
    clock.t = at(10)
    assert [f.quantity for f in r.tick()] == [D(3)]
    clock.t = at(20)
    assert r.tick() == []  # re-read with overlap, de-duplicated by id
    assert src.calls[-1] == at(10) - timedelta(seconds=2)
    again = runner(src, store, clock)  # restart: position and cash rebuilt from the store
    assert again.makers["M"].position == D(-3) and again.makers["M"].cash == D("1.32")


def test_closed_market_stops_quoting_and_records_resolution(tmp_path):
    store, src, clock = Store(tmp_path / "m.db"), FakeSource(), Clock()
    r = runner(src, store, clock)
    r.tick()
    src.trades = [PublicTrade("a", at(5), D("0.39"), D(1), False)]  # swept through our 0.40 bid: +10
    clock.t = at(10)
    r.tick()
    assert r.makers["M"].position == D(10)
    src.open, src.resolved = [], {"M": Resolution("kalshi", "M", D(1), at(40))}
    clock.t = at(40)
    r.tick()
    assert r.makers["M"].quotes == {}
    assert store.resolution("kalshi", "M").yes_value == D(1)


def test_results_hand_computed():
    fills = [MakerFill("M", "bid", D("0.40"), D(10), D("0.01"), T0), MakerFill("M", "ask", D("0.44"), D(4), D("0.01"), T0)]
    (row,) = results(fills, {"M": D(0)})
    # cash -4.00 - 0.01 + 1.76 - 0.01 = -2.26; position 6; settles at 0 -> pnl -2.26
    assert (row.cash, row.position, row.pnl, row.contracts) == (D("-2.26"), D(6), D("-2.26"), D(14))
    assert "per contract: -16.14c" in summary([row])
    assert results(fills, {})[0].pnl is None


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
    r = runner(src, store, clock)
    for s in (0, 10, 20):
        clock.t = at(s)
        r.tick()
    assert src.calls == [] and r.makers["M"].quotes == {}
