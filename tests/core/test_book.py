from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from profit_engine.core import InvalidOrderBook, Level, OrderBook

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def L(price: str, size: str) -> Level:
    return Level(D(price), D(size))


def book(bids=(), asks=(), received_at=NOW) -> OrderBook:
    return OrderBook("kalshi", "KXTEST", tuple(bids), tuple(asks), received_at)


# Kalshi docs orderbook example, already converted to YES terms (hand-computed):
# YES bids 0.42/13, 0.41/10, 0.35/11; NO bids 0.56/17, 0.45/20, 0.44/29 -> YES asks 1 - p.
KALSHI_DOC_BIDS = (L("0.42", "13"), L("0.41", "10"), L("0.35", "11"))
KALSHI_DOC_ASKS = (L("0.44", "17"), L("0.55", "20"), L("0.56", "29"))


class TestLevel:
    def test_valid(self):
        level = L("0.4200", "13.00")
        assert level.price == D("0.42")
        assert level.size == D("13")

    def test_fractional_and_subcent_ok(self):
        L("0.0001", "0.01")

    @pytest.mark.parametrize("bad", [0.42, 1, True, "0.42"])
    def test_price_must_be_decimal(self, bad):
        with pytest.raises(TypeError):
            Level(bad, D("1"))

    def test_size_must_be_decimal(self):
        with pytest.raises(TypeError):
            Level(D("0.5"), 10)

    @pytest.mark.parametrize("price", ["0", "1", "-0.1", "1.5"])
    def test_price_strictly_inside_0_1(self, price):
        with pytest.raises(InvalidOrderBook):
            L(price, "1")

    @pytest.mark.parametrize("size", ["0", "-1"])
    def test_size_positive(self, size):
        with pytest.raises(InvalidOrderBook):
            L("0.5", size)

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "sNaN"])
    def test_non_finite_rejected(self, value):
        with pytest.raises(InvalidOrderBook):
            Level(D(value), D("1"))

    def test_frozen(self):
        level = L("0.5", "1")
        with pytest.raises(FrozenInstanceError):
            level.price = D("0.6")


class TestOrderBook:
    def test_kalshi_doc_example(self):
        ob = book(KALSHI_DOC_BIDS, KALSHI_DOC_ASKS)
        assert ob.best_bid == L("0.42", "13")
        assert ob.best_ask == L("0.44", "17")
        assert ob.best_ask.price - ob.best_bid.price == D("0.02")

    def test_empty_sides_allowed(self):
        ob = book()
        assert ob.best_bid is None
        assert ob.best_ask is None

    def test_one_sided_allowed(self):
        assert book(bids=KALSHI_DOC_BIDS).best_ask is None
        assert book(asks=KALSHI_DOC_ASKS).best_bid is None

    def test_bids_must_descend(self):
        with pytest.raises(InvalidOrderBook, match="bids"):
            book(bids=tuple(reversed(KALSHI_DOC_BIDS)))

    def test_asks_must_ascend(self):
        with pytest.raises(InvalidOrderBook, match="asks"):
            book(asks=tuple(reversed(KALSHI_DOC_ASKS)))

    def test_duplicate_price_level_rejected(self):
        with pytest.raises(InvalidOrderBook):
            book(bids=(L("0.42", "13"), L("0.42", "5")))

    def test_crossed_rejected(self):
        with pytest.raises(InvalidOrderBook, match="crossed"):
            book(bids=(L("0.45", "1"),), asks=(L("0.44", "1"),))

    def test_locked_rejected(self):
        with pytest.raises(InvalidOrderBook, match="crossed or locked"):
            book(bids=(L("0.44", "1"),), asks=(L("0.44", "1"),))

    def test_sides_must_be_tuples(self):
        with pytest.raises(TypeError):
            OrderBook("kalshi", "KXTEST", list(KALSHI_DOC_BIDS), (), NOW)

    def test_sides_must_contain_levels(self):
        with pytest.raises(TypeError):
            OrderBook("kalshi", "KXTEST", ((D("0.42"), D("13")),), (), NOW)

    @pytest.mark.parametrize("field", ["venue", "market_id"])
    def test_ids_non_empty(self, field):
        kwargs = dict(venue="kalshi", market_id="KXTEST", bids=(), asks=(), received_at=NOW)
        kwargs[field] = ""
        with pytest.raises(ValueError) as exc:
            OrderBook(**kwargs)
        assert not isinstance(exc.value, InvalidOrderBook)

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError, match="UTC") as exc:
            book(received_at=datetime(2026, 10, 3, 12, 0))
        assert not isinstance(exc.value, InvalidOrderBook)

    def test_non_utc_datetime_rejected(self):
        with pytest.raises(ValueError, match="UTC") as exc:
            book(received_at=datetime(2026, 10, 3, 12, 0, tzinfo=timezone(timedelta(hours=-4))))
        assert not isinstance(exc.value, InvalidOrderBook)

    def test_frozen(self):
        ob = book(KALSHI_DOC_BIDS, KALSHI_DOC_ASKS)
        with pytest.raises(FrozenInstanceError):
            ob.bids = ()
