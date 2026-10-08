"""Stored books keep the top levels only; existing databases can be compacted."""

from datetime import datetime, timezone
from decimal import Decimal as D

from profit_engine import cli
from profit_engine.core import Level, OrderBook
from profit_engine.storage import Store

T = datetime(2026, 10, 8, 15, tzinfo=timezone.utc)


def deep_book(n=12, market="M", base=0):
    bids = tuple(Level(D("0.40") - D("0.01") * i, D(10 + i + base)) for i in range(n))
    asks = tuple(Level(D("0.44") + D("0.01") * i, D(20 + i + base)) for i in range(n))
    return OrderBook("kalshi", market, bids, asks, T)


def test_top_five_levels_stored_best_first(tmp_path):
    store = Store(tmp_path / "s.db")
    store.add_snapshot(deep_book())
    b = store.latest_book("kalshi", "M")
    assert [lv.price for lv in b.bids] == [D("0.40"), D("0.39"), D("0.38"), D("0.37"), D("0.36")]
    assert [lv.price for lv in b.asks] == [D("0.44"), D("0.45"), D("0.46"), D("0.47"), D("0.48")]


def test_full_depth_when_asked(tmp_path):
    store = Store(tmp_path / "s.db", book_levels=None)
    store.add_snapshot(deep_book())
    assert len(store.latest_book("kalshi", "M").bids) == 12


def test_compaction_trims_old_books_and_reclaims_space(tmp_path):
    path = tmp_path / "s.db"
    full = Store(path, book_levels=None)
    for i in range(3000):  # distinct deep books (identical ones are stored once)
        full.add_snapshot(deep_book(n=40, market=f"M{i}", base=i))
    full.close()
    before = path.stat().st_size
    store = Store(path)
    assert store.compact_books(5) == 3000
    assert store.compact_books(5) == 0  # idempotent
    store.vacuum()
    assert len(store.latest_book("kalshi", "M7").asks) == 5
    assert path.stat().st_size < before / 3


def test_compact_cli_reports_and_handles_a_busy_database(tmp_path, monkeypatch, capsys):
    import sqlite3

    path = tmp_path / "s.db"
    Store(path, book_levels=None).add_snapshot(deep_book())
    assert cli.main(["--db", str(path), "compact-books"]) == 0
    assert "trimmed 1 stored books to 5 levels" in capsys.readouterr().out

    def busy(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "vacuum", busy)
    assert cli.main(["--db", str(path), "compact-books"]) == 1
    assert "Stop the always-on task" in capsys.readouterr().err
