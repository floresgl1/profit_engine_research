from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from profit_engine.core import (
    ZERO_FEES,
    Fill,
    FillStatus,
    Level,
    Market,
    MarketStatus,
    Outcome,
    PaperOrder,
    Resolution,
    Side,
    parse_utc,
)

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def market(**overrides):
    kwargs = dict(
        venue="kalshi",
        market_id="KXTEST",
        title="Test?",
        yes_label="Yes",
        status=MarketStatus.OPEN,
        close_time=NOW,
        fee_schedule=ZERO_FEES,
        contract_step=D("0.01"),
        min_order_size=D("0.01"),
    )
    kwargs.update(overrides)
    return Market(**kwargs)


def order(**overrides):
    kwargs = dict(
        venue="kalshi",
        market_id="KXTEST",
        outcome=Outcome.YES,
        side=Side.BUY,
        quantity=D("10"),
        decided_at=NOW,
    )
    kwargs.update(overrides)
    return PaperOrder(**kwargs)


class TestMarket:
    def test_valid(self):
        assert market().status is MarketStatus.OPEN

    def test_unknown_fees_allowed(self):
        assert market(fee_schedule=None).fee_schedule is None

    def test_venue_meta_is_read_only(self):
        meta = {"yes_token_id": "123"}
        m = market(venue_meta=meta)
        meta["yes_token_id"] = "changed"  # caller's dict must not leak in
        assert m.venue_meta["yes_token_id"] == "123"
        with pytest.raises(TypeError):
            m.venue_meta["yes_token_id"] = "x"

    def test_status_must_be_enum(self):
        with pytest.raises(TypeError):
            market(status="open")

    @pytest.mark.parametrize("field", ["contract_step", "min_order_size"])
    def test_sizes_positive(self, field):
        with pytest.raises(ValueError):
            market(**{field: D("0")})

    def test_close_time_utc(self):
        with pytest.raises(ValueError):
            market(close_time=datetime(2026, 10, 3))


class TestResolution:
    @pytest.mark.parametrize("value", ["0", "0.5", "1", "0.37"])
    def test_valid_values(self, value):
        assert Resolution("polymarket", "0xabc", D(value), NOW).yes_value == D(value)

    @pytest.mark.parametrize("value", ["-0.1", "1.01"])
    def test_out_of_range(self, value):
        with pytest.raises(ValueError):
            Resolution("kalshi", "KX", D(value), NOW)


class TestPaperOrder:
    def test_valid(self):
        assert order().quantity == D("10")

    @pytest.mark.parametrize("qty", [D("0"), D("-1"), 10])
    def test_bad_quantity(self, qty):
        with pytest.raises(ValueError):
            order(quantity=qty)

    @pytest.mark.parametrize("limit", [D("0"), D("1"), 0.5])
    def test_bad_limit(self, limit):
        with pytest.raises(ValueError):
            order(limit_price=limit)

    def test_enums_required(self):
        with pytest.raises(TypeError):
            order(side="buy")


class TestFill:
    def test_buy_cash_and_average(self):
        f = Fill(
            order(),
            FillStatus.FILLED,
            (Level(D("0.44"), D("17")), Level(D("0.55"), D("20")), Level(D("0.56"), D("3"))),
            D("40"),
            D("20.16"),
            D("0.6915"),
            NOW,
        )
        assert f.average_price == D("0.504")
        assert f.cash_change == D("-20.8515")

    def test_sell_cash(self):
        f = Fill(order(side=Side.SELL), FillStatus.FILLED, (), D("10"), D("4.20"), D("0.10"), NOW)
        assert f.cash_change == D("4.10")

    def test_rejected_has_no_average(self):
        f = Fill(order(), FillStatus.REJECTED, (), D("0"), D("0"), D("0"), None, "no book")
        assert f.average_price is None


class TestTime:
    @pytest.mark.parametrize(
        "text",
        ["2026-10-03T12:00:00Z", "2026-10-03 12:00:00+00", "2026-10-03T08:00:00-04:00"],
    )
    def test_parse_utc(self, text):
        assert parse_utc(text) == NOW

    def test_naive_rejected(self):
        with pytest.raises(ValueError):
            parse_utc("2026-10-03T12:00:00")

    def test_result_is_utc(self):
        assert parse_utc("2026-10-03T08:00:00-04:00").utcoffset() == timedelta(0)
