from datetime import timedelta
from decimal import Decimal as D

import pytest
from factories import (
    DOC_ASKS,
    DOC_BIDS,
    KALSHI_DIRECT_FEES,
    T0,
    L,
    make_book,
    make_market,
)

from profit_engine.core import (
    FillStatus,
    InvalidOrderBook,
    MarketStatus,
    OrderBook,
    Resolution,
)
from profit_engine.ingest import Pipeline, PipelineConfig, SkipMonitor
from profit_engine.models import MidpointBaseline
from profit_engine.paper import (
    EdgeStrategy,
    FillConfig,
    LiveBookProvider,
    PaperTrader,
    Portfolio,
    rebuild_portfolio,
)
from profit_engine.storage import Store
from profit_engine.venues.http import VenueHttpError


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


class FakeSource:
    """In-memory MarketDataSource. Books are rebuilt with the clock's time on each fetch."""

    def __init__(self, venue, clock, markets):
        self.venue = venue
        self.clock = clock
        self.markets = {m.market_id: m for m in markets}
        self.levels = {m.market_id: (DOC_BIDS, DOC_ASKS) for m in markets}
        self.bad = set()  # market ids whose book fails validation
        self.resolved = {}
        self.fail_with = None

    def list_markets(self):
        if self.fail_with:
            raise self.fail_with
        return [m for m in self.markets.values() if m.status is MarketStatus.OPEN]

    def fetch_markets(self, ids):
        return {i: self.markets[i] for i in ids if i in self.markets}

    def fetch_books(self, markets):
        out = {}
        for m in markets:
            if m.market_id in self.bad:
                out[m.market_id] = InvalidOrderBook(f"{m.market_id}: crossed")
            else:
                bids, asks = self.levels[m.market_id]
                out[m.market_id] = OrderBook(self.venue, m.market_id, bids, asks, self.clock())
        return out

    def fetch_resolutions(self, markets):
        return {m.market_id: self.resolved[m.market_id] for m in markets if m.market_id in self.resolved}


class AlwaysSure:
    """Test model: says YES is 90% likely."""

    name = "sure"

    def predict(self, market, book):
        return D("0.9")


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def kalshi_markets(n=3, **overrides):
    return [make_market(market_id=f"KX-{i}", **overrides) for i in range(n)]


def pipeline(store, sources, clock, **kwargs):
    kwargs.setdefault("models", [MidpointBaseline()])
    models = kwargs.pop("models")
    return Pipeline(store, sources, models, now=clock, config=PipelineConfig(), **kwargs)


class TestCycle:
    def test_stores_markets_snapshots_predictions(self, store):
        clock = Clock()
        src = FakeSource("kalshi", clock, kalshi_markets(3))
        stats = pipeline(store, {"kalshi": src}, clock).run_cycle()
        assert (stats.snapshots, stats.skipped, stats.predictions) == (3, 0, 3)
        assert len(store.markets()) == 3
        assert store.latest_book("kalshi", "KX-0").best_ask == L("0.44", "17")

    def test_invalid_book_skipped_others_continue(self, store):
        clock = Clock()
        src = FakeSource("kalshi", clock, kalshi_markets(3))
        src.bad.add("KX-1")
        p = pipeline(store, {"kalshi": src}, clock)
        stats = p.run_cycle()
        assert (stats.snapshots, stats.skipped) == (2, 1)
        assert store.latest_book("kalshi", "KX-1") is None
        assert p.monitor.rate("kalshi") == pytest.approx(1 / 3)

    def test_skip_rate_alert_fires_once_and_recovers(self, store):
        clock = Clock()
        src = FakeSource("kalshi", clock, kalshi_markets(10))
        src.bad.update(src.markets)  # every book "crossed": the adapter-bug scenario
        alerts = []
        monitor = SkipMonitor(threshold=0.2, window=20, min_samples=20, on_alert=alerts.append)
        p = pipeline(store, {"kalshi": src}, clock, monitor=monitor)
        p.run_cycle()
        assert alerts == []  # 10 samples: below min_samples
        p.run_cycle()
        p.run_cycle()
        assert len(alerts) == 1 and alerts[0].rate == 1.0
        src.bad.clear()
        for _ in range(2):
            p.run_cycle()
        assert not monitor.alerting("kalshi")

    def test_venue_error_skips_only_that_venue(self, store):
        clock = Clock()
        down = FakeSource("polymarket", clock, [make_market(venue="polymarket", market_id="0xa")])
        down.fail_with = VenueHttpError("503", 503)
        up = FakeSource("kalshi", clock, kalshi_markets(2))
        stats = pipeline(store, {"polymarket": down, "kalshi": up}, clock).run_cycle()
        assert stats.snapshots == 2
        assert len(stats.venue_errors) == 1

    def test_bugs_stop_the_loop(self, store):
        clock = Clock()
        src = FakeSource("kalshi", clock, kalshi_markets(1))
        src.fail_with = TypeError("bug in adapter")
        with pytest.raises(TypeError):
            pipeline(store, {"kalshi": src}, clock).run_cycle()

    def test_closed_market_stops_being_polled(self, store):
        clock = Clock()
        src = FakeSource("kalshi", clock, kalshi_markets(2))
        p = pipeline(store, {"kalshi": src}, clock)
        p.run_cycle()
        src.markets["KX-0"] = make_market(market_id="KX-0", status=MarketStatus.CLOSED)
        clock.t += timedelta(minutes=11)  # past market_refresh
        assert p.run_cycle().snapshots == 1
        assert store.market("kalshi", "KX-0").status is MarketStatus.CLOSED

    def test_resolutions_recorded_and_scored(self, store):
        clock = Clock()
        src = FakeSource("kalshi", clock, kalshi_markets(2))
        p = pipeline(store, {"kalshi": src}, clock)
        p.run_cycle()
        src.resolved["KX-0"] = Resolution("kalshi", "KX-0", D("1"), T0 + timedelta(minutes=5))
        clock.t += timedelta(minutes=11)
        assert p.run_cycle().resolutions == 1
        rows = store.scored_predictions()
        # Two cycles of predictions on KX-0 are now scoreable; KX-1 is not resolved.
        assert {r.prediction.market_id for r in rows} == {"KX-0"}
        assert all(r.prediction.probability == D("0.43") for r in rows)

    def test_run_forever_paces_cycles(self, store):
        clock = Clock()
        src = FakeSource("kalshi", clock, kalshi_markets(1))
        p = pipeline(store, {"kalshi": src}, clock)
        p.run_forever(cycles=3, sleep=clock.sleep)
        assert store.snapshot_count()[0] == 3
        assert clock.t == T0 + timedelta(seconds=60)  # two waits of 30s between three cycles


class TestPaperTradingInLoop:
    def setup(self, store, clock):
        market = make_market(market_id="KX-0", fee_schedule=KALSHI_DIRECT_FEES)
        src = FakeSource("kalshi", clock, [market])
        config = FillConfig(latency=timedelta(milliseconds=250), max_depth_fraction=D("1"))
        trader = PaperTrader(
            LiveBookProvider({"kalshi": src}, now=clock, sleep=clock.sleep),
            Portfolio(cash=D("100")),
            {"kalshi": config},
        )
        strategy = EdgeStrategy(min_edge=D("0.03"), order_size=D("10"), max_position=D("10"))
        p = pipeline(
            store, {"kalshi": src}, clock, models=[AlwaysSure()], strategy=strategy, trader=trader, trading_model="sure"
        )
        return p, src, trader

    def test_trade_fill_settle_rebuild(self, store):
        clock = Clock()
        p, src, trader = self.setup(store, clock)
        stats = p.run_cycle()
        # YES edge = 0.9 - 0.44 - 0.07*0.44*0.56 = 0.442752 -> buy 10 YES, limit 0.87.
        # Fill against a fresh book 250 ms later: 10 @ 0.44 = 4.40; fee 0.07*10*0.44*0.56 = 0.17248 -> 0.1725
        assert stats.orders == 1
        fill = store.fills()[0]
        assert fill.status is FillStatus.FILLED
        assert fill.book_received_at == T0 + timedelta(milliseconds=250)
        assert (fill.gross, fill.fee) == (D("4.40"), D("0.1725"))
        assert trader.portfolio.cash == D("100") - D("4.5725")

        # Position cap reached: no second order next cycle.
        clock.t += timedelta(seconds=30)
        assert p.run_cycle().orders == 0

        # Resolve YES: payout 10 -> pnl 10 - 4.5725 = 5.4275
        src.resolved["KX-0"] = Resolution("kalshi", "KX-0", D("1"), clock.t)
        clock.t += timedelta(minutes=11)
        p.run_cycle()
        assert trader.portfolio.realized_pnl == D("5.4275")
        assert trader.portfolio.cash == D("105.4275")

        rebuilt = rebuild_portfolio(D("100"), store.fills(), store.resolutions())
        assert (rebuilt.cash, rebuilt.realized_pnl) == (trader.portfolio.cash, trader.portfolio.realized_pnl)

    def test_strategy_requires_trader(self, store):
        with pytest.raises(ValueError):
            Pipeline(store, {}, [MidpointBaseline()], strategy=EdgeStrategy())


def test_rebuild_interleaves_settlements(store):
    # Cash 5: buy 10 YES @ 0.44 (4.40), settle YES (+10), then buy 10 more (4.40).
    # Replaying all fills before settlements would run out of cash on the second buy.
    from factories import make_order

    from profit_engine.paper import simulate_fill

    config = FillConfig(max_depth_fraction=D("1"))
    first = simulate_fill(make_order("10", market_id="A"), make_market(market_id="A"), make_book(market_id="A"), config)
    later = T0 + timedelta(hours=1)
    second = simulate_fill(
        make_order("10", market_id="B", decided_at=later),
        make_market(market_id="B"),
        make_book(market_id="B", received_at=later + timedelta(seconds=1)),
        config,
    )
    settle_a = Resolution("kalshi", "A", D("1"), T0 + timedelta(minutes=30))
    p = rebuild_portfolio(D("5"), [first, second], [settle_a])
    assert p.cash == D("5") - D("4.40") + D("10") - D("4.40")
