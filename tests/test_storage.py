import sqlite3
from datetime import timedelta
from decimal import Decimal as D

import pytest
from factories import KALSHI_DIRECT_FEES, T0, L, make_book, make_market, make_order

from profit_engine.core import FillStatus, MarketStatus, Prediction, Resolution
from profit_engine.paper import FillConfig, PaperTrader, Portfolio, simulate_fill
from profit_engine.storage import Store, StoredBookProvider


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def at(seconds: float):
    return T0 + timedelta(seconds=seconds)


class TestMarkets:
    def test_round_trip(self, store):
        market = make_market(fee_schedule=KALSHI_DIRECT_FEES, venue_meta={"series_ticker": "KXHIGHNY"})
        store.upsert_market(market, T0)
        assert store.market("kalshi", "KXTEST") == market

    def test_unknown_fee_round_trip(self, store):
        store.upsert_market(make_market(fee_schedule=None), T0)
        assert store.market("kalshi", "KXTEST").fee_schedule is None

    def test_upsert_updates_status(self, store):
        store.upsert_market(make_market(), T0)
        store.upsert_market(make_market(status=MarketStatus.CLOSED), at(60))
        assert store.market("kalshi", "KXTEST").status is MarketStatus.CLOSED
        assert len(store.markets()) == 1

    def test_filters(self, store):
        store.upsert_market(make_market(market_id="A"), T0)
        store.upsert_market(make_market(market_id="B", status=MarketStatus.CLOSED), T0)
        store.upsert_market(make_market(venue="polymarket", market_id="C"), T0)
        assert [m.market_id for m in store.markets(venue="kalshi")] == ["A", "B"]
        assert [m.market_id for m in store.markets(statuses=[MarketStatus.OPEN])] == ["A", "C"]

    def test_unresolved(self, store):
        store.upsert_market(make_market(market_id="A"), T0)
        store.upsert_market(make_market(market_id="B"), T0)
        store.upsert_resolution(Resolution("kalshi", "A", D("1"), at(1)))
        assert [m.market_id for m in store.unresolved_markets("kalshi")] == ["B"]


class TestBooks:
    def test_round_trip_is_exact(self, store):
        book = make_book(bids=(L("0.0001", "0.01"),), asks=(L("0.9999", "123456.78"),), received_at=at(1))
        store.add_snapshot(book)
        assert store.latest_book("kalshi", "KXTEST") == book

    def test_identical_books_share_one_state(self, store):
        store.add_snapshot(make_book(received_at=at(1)))
        store.add_snapshot(make_book(received_at=at(2)))
        store.add_snapshot(make_book(asks=(L("0.50", "1"),), received_at=at(3)))
        assert store.snapshot_count() == (3, 2)

    def test_book_at_or_after(self, store):
        for s in (1, 2, 3):
            store.add_snapshot(make_book(asks=(L(f"0.5{s}", "1"),), received_at=at(s)))
        assert store.book_at_or_after("kalshi", "KXTEST", at(1.5)).received_at == at(2)
        assert store.book_at_or_after("kalshi", "KXTEST", at(2)).received_at == at(2)
        assert store.book_at_or_after("kalshi", "KXTEST", at(3.1)) is None
        assert store.book_at_or_after("kalshi", "OTHER", at(0)) is None

    def test_microsecond_ordering(self, store):
        store.add_snapshot(make_book(received_at=T0 + timedelta(microseconds=999_999)))
        store.add_snapshot(make_book(received_at=T0 + timedelta(seconds=1)))
        found = store.book_at_or_after("kalshi", "KXTEST", T0 + timedelta(microseconds=999_999))
        assert found.received_at == T0 + timedelta(microseconds=999_999)

    def test_books_in_time_order(self, store):
        for s in (3, 1, 2):
            store.add_snapshot(make_book(received_at=at(s)))
        assert [b.received_at for b in store.books("kalshi", "KXTEST")] == [at(1), at(2), at(3)]

    def test_record_skip(self, store):
        store.record_skip("kalshi", "KXTEST", T0, "crossed")  # just must not raise


class TestReplay:
    def test_trader_uses_first_book_after_arrival(self, store):
        # Book at T0 is before arrival (T0 + 250ms) and must not be used; the T0 + 1s book is.
        # Using it would give 40 @ 0.43 = 17.20 instead of 20.16.
        store.add_snapshot(make_book(asks=(L("0.43", "100"),), received_at=T0))
        store.add_snapshot(make_book(received_at=at(1)))
        trader = PaperTrader(
            StoredBookProvider(store),
            Portfolio(cash=D("100")),
            {"kalshi": FillConfig(latency=timedelta(milliseconds=250), max_depth_fraction=D("1"))},
        )
        fill = trader.execute(make_order("40"), make_market())
        assert fill.book_received_at == at(1)
        assert fill.gross == D("20.16")


class TestResolutionsAndPredictions:
    def prediction(self, market_id="KXTEST", model="midpoint", probability="0.43"):
        book = make_book(market_id=market_id)
        return Prediction.from_book(model, D(probability), book, at(2))

    def test_resolution_round_trip(self, store):
        res = Resolution("kalshi", "KXTEST", D("0.5"), at(10))
        store.upsert_resolution(res)
        assert store.resolution("kalshi", "KXTEST") == res

    def test_prediction_records_market_price(self):
        p = self.prediction()
        # mid of 0.42 and 0.44
        assert p.market_probability == D("0.43")
        assert (p.best_bid, p.best_ask) == (D("0.42"), D("0.44"))

    def test_only_resolved_predictions_are_scored(self, store):
        store.add_prediction(self.prediction("A"))
        store.add_prediction(self.prediction("B"))
        store.add_prediction(self.prediction("A", model="other", probability="0.9"))
        store.upsert_resolution(Resolution("kalshi", "A", D("1"), at(10)))
        rows = store.scored_predictions()
        assert [(r.prediction.market_id, r.prediction.model, r.outcome) for r in rows] == [
            ("A", "midpoint", D("1")),
            ("A", "other", D("1")),
        ]
        assert [r.prediction.model for r in store.scored_predictions("other")] == ["other"]
        assert store.models() == ["midpoint", "other"]

    def test_scored_predictions_filter_by_series(self, store):
        for market_id, series in [("CHI-1", "KXHIGHCHI"), ("NY-1", "KXHIGHNY"), ("MIA-1", "KXHIGHMIA")]:
            store.upsert_market(make_market(market_id=market_id, venue_meta={"series_ticker": series}), at(0))
            store.add_prediction(self.prediction(market_id))
            store.upsert_resolution(Resolution("kalshi", market_id, D("1"), at(10)))
        assert len(store.scored_predictions()) == 3
        assert [r.prediction.market_id for r in store.scored_predictions(series=["KXHIGHCHI"])] == ["CHI-1"]
        both = store.scored_predictions(series=["KXHIGHNY", "KXHIGHMIA"])
        assert sorted(r.prediction.market_id for r in both) == ["MIA-1", "NY-1"]
        assert store.scored_predictions(model="other", series=["KXHIGHCHI"]) == []
        assert store.scored_predictions(series=["KXHIGHDEN"]) == []

    def test_prediction_round_trip(self, store):
        p = self.prediction("A")
        store.add_prediction(p)
        store.upsert_resolution(Resolution("kalshi", "A", D("0"), at(10)))
        assert store.scored_predictions()[0].prediction == p


def test_fill_round_trip(store):
    fill = simulate_fill(make_order("40"), make_market(), make_book(), FillConfig(max_depth_fraction=D("1")))
    store.add_fill(fill)
    row = store.fill_rows()[0]
    assert row["status"] == FillStatus.FILLED.value
    assert row["gross"] == "20.16"
    assert row["legs"] == '[["0.44","17"],["0.55","20"],["0.56","3"]]'


def test_schema_version_mismatch(tmp_path):
    path = tmp_path / "db.sqlite"
    Store(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version = 99")
    with pytest.raises(RuntimeError, match="schema"):
        Store(path)


def test_score_cli_filters_by_series(tmp_path, capsys):
    from profit_engine import cli

    db = tmp_path / "r.db"
    store = Store(db)
    for i, (series, outcome) in enumerate([("KXHIGHCHI", "1"), ("KXHIGHCHI", "0"), ("KXHIGHNY", "1")]):
        market_id = f"{series}-{i}"
        store.upsert_market(make_market(market_id=market_id, venue_meta={"series_ticker": series}), at(0))
        store.add_prediction(Prediction.from_book("midpoint", D("0.43"), make_book(market_id=market_id), at(2)))
        store.upsert_resolution(Resolution("kalshi", market_id, D(outcome), at(10)))
    store.close()

    assert cli.main(["--db", str(db), "score", "--series", "KXHIGHCHI"]) == 0
    chi = capsys.readouterr().out
    assert cli.main(["--db", str(db), "score", "--series", "KXHIGHDEN"]) == 0
    assert "No resolved predictions" in capsys.readouterr().out
    assert cli.main(["--db", str(db), "score"]) == 0
    everything = capsys.readouterr().out
    assert chi.startswith("series: KXHIGHCHI") and chi != everything
