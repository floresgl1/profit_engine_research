"""Replay must reproduce the live maker on what the live maker recorded."""

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from factories import make_market

from profit_engine import cli
from profit_engine.core import Level, OrderBook
from profit_engine.maker.queue import PublicTrade
from profit_engine.maker.quoter import MakerFill, MarketMaker, QuoterConfig
from profit_engine.maker.replay import fidelity, replay
from profit_engine.maker.runner import MakerRunner, RunnerConfig, Strategy
from profit_engine.maker.strategies import STRATEGIES
from profit_engine.storage import Store

T0 = datetime(2026, 10, 9, 14, tzinfo=timezone.utc)
TICK = timedelta(seconds=10)
TICKERS = ["KXHIGHNY-26OCT09-B68.5", "KXHIGHNY-26OCT09-B70.5"]


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class ScriptedSource:
    """Books that wander and trades that print between ticks, some just after a tick time."""

    def __init__(self, clock, seed=3, close=T0 + timedelta(hours=1)):
        self.clock, self.rng, self.close = clock, random.Random(seed), close
        self.mid = {t: 40 for t in TICKERS}
        self.trades: dict[str, list[PublicTrade]] = {t: [] for t in TICKERS}
        self.n = 0

    def list_markets(self):
        return [make_market(market_id=t, close_time=self.close) for t in TICKERS] if self.clock() < self.close else []

    def fetch_books(self, markets):
        out = {}
        for m in markets:
            t = m.market_id
            self.mid[t] = max(10, min(85, self.mid[t] + self.rng.choice([-1, 0, 0, 1])))  # keep 7 levels inside (0, 1)
            bid, ask = D(self.mid[t]) / 100, D(self.mid[t] + 2) / 100
            bids = tuple(Level(bid - D("0.01") * i, D(self.rng.randint(1, 30))) for i in range(7))
            asks = tuple(Level(ask + D("0.01") * i, D(self.rng.randint(1, 30))) for i in range(7))
            out[t] = OrderBook("kalshi", t, bids, asks, self.clock())
            # trades printed during the next 10 s; some land after the tick that will fetch them
            for _ in range(self.rng.randint(0, 3)):
                self.n += 1
                at = self.clock() + timedelta(seconds=self.rng.uniform(0.5, 12))
                taker_yes = self.rng.random() < 0.5
                price = ask + (D("0.01") if self.rng.random() < 0.1 else 0) if taker_yes else bid
                self.trades[t].append(PublicTrade(f"x{self.n}", at, price, D(self.rng.randint(1, 40)), taker_yes))
        return out

    def fetch_trades(self, ticker, since):
        # Live fetch happens a little after the tick: it sees trades up to tick + 3 s.
        return [t for t in self.trades[ticker] if since <= t.at <= self.clock() + timedelta(seconds=3)]

    def fetch_markets(self, tickers):
        return {}

    def fetch_resolutions(self, markets):
        return {}


def live_run(tmp_path, strategies, ticks=200, fair=None):
    store, clock = Store(tmp_path / "live.db"), Clock()
    src = ScriptedSource(clock)
    r = MakerRunner(src, store, strategies, RunnerConfig(market_refresh=timedelta(minutes=10)), now=clock, fair=fair)
    for i in range(ticks):
        clock.t = T0 + i * TICK
        r.tick()
    return store


def key(f: MakerFill):
    return (f.market_id, f.side, f.price, f.quantity, f.at)


def test_replay_reproduces_live_v1_exactly(tmp_path):
    v1 = STRATEGIES["join_touch_v1"]
    store = live_run(tmp_path, [v1])
    live = [f for _, _, f in store.maker_fills("join_touch_v1")]
    assert len(live) > 20  # the script produces real activity
    replayed = replay(store, [v1])["join_touch_v1"]
    assert sorted(map(key, replayed)) == sorted(map(key, live))


def test_replay_reproduces_live_v2_with_changing_model_price(tmp_path):
    v2 = STRATEGIES["model_veto_v2"]

    def fair(market, book):  # flips between a price that vetoes bids, none, and a neutral one
        phase = int((book.received_at - T0).total_seconds() // 300) % 3
        return [D("0.10"), None, D("0.45")][phase]

    store = live_run(tmp_path, [v2], fair=fair)
    live = [f for _, _, f in store.maker_fills("model_veto_v2")]
    assert sorted(map(key, replay(store, [v2])["model_veto_v2"])) == sorted(map(key, live))
    assert None in [f for _, f in store.maker_fair(TICKERS[0])]  # "no price" was recorded


def test_market_close_and_restart_gap(tmp_path):
    v1 = STRATEGIES["join_touch_v1"]
    store = live_run(tmp_path, [v1], ticks=420)  # runs past the 1 h close
    fills = replay(store, [v1])["join_touch_v1"]
    assert fills and max(f.at for f in fills) < T0 + timedelta(hours=1)
    # A gap of more than 2 minutes drops resting quotes: replaying from a later start gives no fill
    # from quotes posted before it.
    late = replay(store, [v1], start=T0 + timedelta(minutes=30))["join_touch_v1"]
    assert all(f.at >= T0 + timedelta(minutes=30) for f in late)


def test_untagged_trades_from_older_recordings_still_replay(tmp_path):
    # Older recordings have no delivering tick: trades are assigned to the first tick at or after them.
    store = Store(tmp_path / "old.db")
    t = TICKERS[0]
    store.upsert_market(make_market(market_id=t, close_time=T0 + timedelta(hours=1)), T0)
    for i in range(3):
        store.add_maker_tick(T0 + i * TICK)
    store.add_snapshot(OrderBook("kalshi", t, (Level(D("0.40"), D(5)),), (Level(D("0.44"), D(5)),), T0))
    store.add_maker_trades(t, [PublicTrade("a", T0 + timedelta(seconds=15), D("0.39"), D(1), False)])  # sweeps our bid
    fills = replay(store, [STRATEGIES["join_touch_v1"]])["join_touch_v1"]
    assert [(f.side, f.quantity, f.at) for f in fills] == [("bid", D(10), T0 + 2 * TICK)]


def test_skipped_ticks_deliver_their_trades_to_the_next_tick_used(tmp_path):
    # Three ticks; the trade sweeping our bid was delivered by the middle one. At every=2 the middle
    # tick is skipped, so the bid (posted at the first tick) fills at the third.
    store = Store(tmp_path / "every.db")
    t = TICKERS[0]
    store.upsert_market(make_market(market_id=t, close_time=T0 + timedelta(hours=1)), T0)
    for i in range(3):
        store.add_maker_tick(T0 + i * TICK)
    store.add_snapshot(OrderBook("kalshi", t, (Level(D("0.40"), D(5)),), (Level(D("0.44"), D(5)),), T0))
    store.add_maker_trades(t, [PublicTrade("a", T0 + timedelta(seconds=5), D("0.39"), D(1), False)], tick_at=T0 + TICK)
    v1 = STRATEGIES["join_touch_v1"]
    assert [f.at for f in replay(store, [v1])["join_touch_v1"]] == [T0 + TICK]
    assert [f.at for f in replay(store, [v1], every=2)["join_touch_v1"]] == [T0 + 2 * TICK]
    with pytest.raises(ValueError):
        replay(store, [v1], every=0)


class TimedSource(ScriptedSource):
    """The scripted market fixed in advance, so makers polling at different speeds see the same one."""

    def __init__(self, clock, seed=3, close=T0 + timedelta(hours=1)):
        gen_clock = Clock()
        gen = ScriptedSource(gen_clock, seed, close)
        markets = gen.list_markets()
        self.slots = []  # the books of each 10 s slot
        while gen_clock.t < close:
            self.slots.append(gen.fetch_books(markets))
            gen_clock.t += TICK
        super().__init__(clock, seed, close)
        self.trades = gen.trades

    def fetch_books(self, markets):
        slot = self.slots[(self.clock() - T0) // TICK]
        return {m.market_id: slot[m.market_id] for m in markets}


def test_replay_every_5_reproduces_a_live_maker_polling_5x_slower(tmp_path):
    def fair(market, book):  # as in the v2 test: vetoes bids, then no price, then neutral
        phase = int((book.received_at - T0).total_seconds() // 300) % 3
        return [D("0.10"), None, D("0.45")][phase]

    strategies = [STRATEGIES["join_touch_v1"], STRATEGIES["model_veto_v2"]]

    def run(path, step, ticks):
        store, clock = Store(path), Clock()
        r = MakerRunner(TimedSource(clock), store, strategies, RunnerConfig(market_refresh=timedelta(minutes=10)),
                        now=clock, fair=fair)  # fmt: skip
        for i in range(ticks):
            clock.t = T0 + i * step
            r.tick()
        return store

    fast = run(tmp_path / "fast.db", TICK, 360)
    slow = run(tmp_path / "slow.db", 5 * TICK, 72)
    replayed = replay(fast, strategies, every=5)
    for s in strategies:
        live_slow = sorted(map(key, (f for _, _, f in slow.maker_fills(s.name))))
        assert len(live_slow) > 10
        assert sorted(map(key, replayed[s.name])) == live_slow
    # polling speed changes the fills on this market, so the test can tell the two apart
    assert sorted(map(key, replay(fast, strategies[:1])["join_touch_v1"])) != sorted(
        map(key, (f for _, _, f in slow.maker_fills("join_touch_v1"))))


def test_v3_size_skew_hand_computed():
    from profit_engine.maker.quoter import MarketMaker

    book = OrderBook("kalshi", "M", (Level(D("0.40"), D(5)),), (Level(D("0.44"), D(5)),), T0)
    m = MarketMaker("M", QuoterConfig(skew_size=True))
    m.position = D(20)  # long 20 of 50: buying adds -> 10 x (1 - 0.4) = 6; selling reduces -> full 10
    m.step(book, [], T0)
    assert (m.quotes["bid"].size, m.quotes["ask"].size) == (D(6), D(10))
    m.position = D(30)  # longer: the resting bid shrinks in place to 10 x 0.4 = 4, keeping its queue place
    m.step(book, [], T0 + TICK)
    assert (m.quotes["bid"].size, m.quotes["bid"].posted_at) == (D(4), T0)
    short = MarketMaker("M", QuoterConfig(skew_size=True))
    short.position = D(-46)  # short: selling adds -> floor(10 x 0.08) = 0, no ask; buying reduces -> full 10
    short.step(book, [], T0)
    assert "ask" not in short.quotes and short.quotes["bid"].size == D(10)


def test_v3_back_one_tick_from_half_the_limit():
    book = OrderBook("kalshi", "M", (Level(D("0.40"), D(5)), Level(D("0.39"), D(7))), (Level(D("0.44"), D(5)),), T0)
    m = MarketMaker("M", QuoterConfig(skew_size=True, skew_back=True))
    m.position = D(24)  # under half: at the touch
    m.step(book, [], T0)
    assert m.quotes["bid"].price == D("0.40")
    m.position = D(25)  # half: one tick behind, behind the 7 showing there
    m.step(book, [], T0 + TICK)
    assert (m.quotes["bid"].price, m.quotes["bid"].queue_ahead, m.quotes["bid"].size) == (D("0.39"), D(7), D(5))
    assert m.quotes["ask"].price == D("0.44")  # the reducing side is untouched


def test_fidelity_counts_by_event_day():
    f = MakerFill(TICKERS[0], "bid", D("0.40"), D(10), D(0), T0)
    rows = fidelity([f, f], [f], T0, T0 + TICK)
    assert [(r.live_contracts, r.replay_contracts) for r in rows] == [(D(20), D(10))]


def test_replay_cli(tmp_path, capsys):
    store = live_run(tmp_path, [STRATEGIES["join_touch_v1"]], ticks=60)
    store.close()
    assert cli.main(["--db", str(tmp_path / "live.db"), "replay", "--strategies", "skew_size_v3a"]) == 0
    out = capsys.readouterr().out
    assert "[join_touch_v1]" in out and "[skew_size_v3a]" in out and "[fidelity" in out
    assert cli.main(["--db", str(tmp_path / "live.db"), "replay", "--strategies", "nope"]) == 2


def test_replay_cli_compares_polling_speeds(tmp_path, capsys):
    store = live_run(tmp_path, [STRATEGIES["join_touch_v1"]], ticks=60)
    store.close()
    db = str(tmp_path / "live.db")
    assert cli.main(["--db", db, "replay", "--strategies", "model_veto_v2", "--every", "5,1", "--breakdown"]) == 0
    out = capsys.readouterr().out
    assert "(median 10.0s apart); replayed at every 1 = 10s, every 5 = 50s" in out
    assert "[join_touch_v1@every5]" in out and "[breakdown model_veto_v2@every5" in out
    assert "[model_veto_v2 - join_touch_v1, paired" in out
    assert "[join_touch_v1 - join_touch_v1@every5, paired" in out and "[fidelity" in out
    assert cli.main(["--db", db, "replay", "--every", "0"]) == 2
    assert cli.main(["--db", db, "replay", "--every", "x"]) == 2


def test_live_strategies_unchanged_by_the_registry():
    assert STRATEGIES["join_touch_v1"].quoter == QuoterConfig()
    assert STRATEGIES["model_veto_v2"].quoter == QuoterConfig(model_margin=D("0.20"))
    assert isinstance(MarketMaker("M", STRATEGIES["skew_back_v3b"].quoter), MarketMaker)
    assert Strategy("x", QuoterConfig()).name == "x"


def test_swept_vs_queue_contracts():
    from profit_engine.maker.queue import RestingQuote, match_detail

    q = RestingQuote("bid", D("0.40"), D(10), queue_ahead=D(5), posted_at=T0)
    trades = [PublicTrade("a", T0 + TICK, D("0.40"), D(8), False),   # 5 ahead, 3 to us (queue)
              PublicTrade("b", T0 + 2 * TICK, D("0.38"), D(1), False)]  # sweeps the remaining 7
    filled, swept, left = match_detail(q, trades)
    assert (filled, swept, left.size) == (D(10), D(7), D(0))


def test_session_of_new_york_times():
    from profit_engine.maker.replay import session_of

    def at(day, hour):  # EDT = UTC-4
        return MakerFill("KXHIGHNY-26OCT09-B68.5", "bid", D("0.4"), D(1), D(0),
                         datetime(2026, 10, day, hour + 4, tzinfo=timezone.utc) if hour + 4 < 24 else
                         datetime(2026, 10, day + 1, hour + 4 - 24, tzinfo=timezone.utc))
    assert [session_of(at(8, 22)), session_of(at(9, 9)), session_of(at(9, 10)),
            session_of(at(9, 15)), session_of(at(9, 23))] == SESSIONS_EXPECTED


SESSIONS_EXPECTED = ["day before", "D 00-10", "D 10-14", "D 14-18", "D 18-close"]


def test_breakdown_hand_computed():
    from profit_engine.maker.replay import breakdown, render_breakdown

    afternoon = datetime(2026, 10, 9, 19, tzinfo=timezone.utc)  # 15:00 EDT
    m = "KXHIGHNY-26OCT09-B68.5"  # settles NO
    fills = [
        # bought 10 YES at 0.40, 4 of them swept: loses 4.00 + fee 0.10 -> queue 6 lose 2.46, swept 4 lose 1.64
        MakerFill(m, "bid", D("0.40"), D(10), D("0.10"), afternoon, D(4)),
        # sold 5 YES at 0.30 from the queue: wins 1.50
        MakerFill(m, "ask", D("0.30"), D(5), D(0), afternoon),
    ]
    rows = {(r.session, r.kind): r for r in breakdown(fills, {m: D(0)})}
    assert (rows[("D 14-18", "queue")].contracts, rows[("D 14-18", "queue")].pnl) == (D(11), D("-2.46") + D("1.50"))
    assert (rows[("D 14-18", "swept")].contracts, rows[("D 14-18", "swept")].pnl) == (D(4), D("-1.64"))
    assert rows[("D 14-18", "swept")].days_positive == 0 and rows[("D 14-18", "swept")].days == 1
    assert breakdown(fills, {}) == []  # unsettled markets are left out
    out = render_breakdown("v1", list(rows.values()))
    assert "D 14-18" in out and "all" in out


def test_replay_cli_breakdown(tmp_path, capsys):
    store = live_run(tmp_path, [STRATEGIES["join_touch_v1"]], ticks=60)
    store.close()
    assert cli.main(["--db", str(tmp_path / "live.db"), "replay", "--strategies", "join_touch_v1", "--breakdown"]) == 0
    assert "[breakdown join_touch_v1" in capsys.readouterr().out
