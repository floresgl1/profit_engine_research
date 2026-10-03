"""Kalshi adapter tests. Payloads are trimmed copies of real responses (2026-10-03)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import httpx
import pytest
from factories import L

from profit_engine.core import InvalidOrderBook, MarketStatus
from profit_engine.venues.http import ReadOnlyHttp
from profit_engine.venues.kalshi import KalshiSource, fee_schedule_for, parse_book
from profit_engine.venues.kalshi.adapter import parse_resolution, parse_status

NOW = datetime(2026, 10, 3, 22, 30, tzinfo=timezone.utc)

# Orderbook example from docs.kalshi.com/getting_started/orderbook_responses
DOC_BOOK = {
    "yes_dollars": [
        ["0.0100", "200.00"], ["0.1500", "100.00"], ["0.2000", "50.00"], ["0.2500", "20.00"],
        ["0.3000", "11.00"], ["0.3100", "10.00"], ["0.3200", "10.00"], ["0.3300", "11.00"],
        ["0.3400", "9.00"], ["0.3500", "11.00"], ["0.4100", "10.00"], ["0.4200", "13.00"],
    ],
    "no_dollars": [
        ["0.0100", "100.00"], ["0.1600", "3.00"], ["0.2500", "50.00"], ["0.2800", "19.00"],
        ["0.3600", "5.00"], ["0.3700", "50.00"], ["0.3800", "300.00"], ["0.4400", "29.00"],
        ["0.4500", "20.00"], ["0.5600", "17.00"],
    ],
}  # fmt: skip


def raw_market(ticker="KXHIGHNY-26OCT04-T70", event="KXHIGHNY-26OCT04", status="active", **extra):
    raw = {
        "ticker": ticker,
        "event_ticker": event,
        "title": "Will the maximum temperature be >70° on Oct 4, 2026?",
        "yes_sub_title": "71° or above",
        "status": status,
        "close_time": "2026-10-05T05:00:00Z",
        "market_type": "binary",
        "result": "",
    }
    raw.update(extra)
    return raw


class TestParseBook:
    def test_doc_example(self):
        # YES bids best first: 0.42 x 13, 0.41 x 10, 0.35 x 11 ...
        # YES asks = 1 - NO bids, best first: 0.44 x 17, 0.55 x 20, 0.56 x 29 ...
        book = parse_book("KX", DOC_BOOK, NOW)
        assert book.bids[:3] == (L("0.42", "13"), L("0.41", "10"), L("0.35", "11"))
        assert book.asks[:3] == (L("0.44", "17"), L("0.55", "20"), L("0.56", "29"))
        assert len(book.bids) == 12 and len(book.asks) == 10
        assert book.asks[-1] == L("0.99", "100")  # from NO bid 0.01

    def test_input_order_does_not_matter(self):
        shuffled = {k: list(reversed(v)) for k, v in DOC_BOOK.items()}
        assert parse_book("KX", shuffled, NOW) == parse_book("KX", DOC_BOOK, NOW)

    def test_zero_size_levels_dropped(self):
        book = parse_book("KX", {"yes_dollars": [["0.40", "0.00"], ["0.30", "5.00"]], "no_dollars": []}, NOW)
        assert book.bids == (L("0.30", "5"),)

    def test_empty_and_missing_sides(self):
        book = parse_book("KX", {"yes_dollars": [], "no_dollars": None}, NOW)
        assert book.bids == () and book.asks == ()

    def test_crossed_raises(self):
        # YES bid 0.60 vs YES ask 1 - 0.45 = 0.55
        with pytest.raises(InvalidOrderBook):
            parse_book("KX", {"yes_dollars": [["0.60", "1"]], "no_dollars": [["0.45", "1"]]}, NOW)

    def test_garbage_raises_invalid_book(self):
        with pytest.raises(InvalidOrderBook):
            parse_book("KX", {"yes_dollars": [["abc", "1"]], "no_dollars": []}, NOW)


class TestFees:
    @pytest.mark.parametrize(
        "fee_type, multiplier, rate",
        [
            ("quadratic", 1, "0.07"),
            ("quadratic", 0.5, "0.035"),
            ("quadratic_with_maker_fees", 1, "0.07"),
            ("quadratic_with_combo_maker_fees", 2, "0.14"),
        ],
    )
    def test_quadratic_types(self, fee_type, multiplier, rate):
        assert fee_schedule_for(fee_type, multiplier).rate == D(rate)

    @pytest.mark.parametrize("fee_type, multiplier", [("flat", 1), ("something_new", 1), (None, 1), ("quadratic", None)])
    def test_unmodeled_types_have_no_schedule(self, fee_type, multiplier):
        assert fee_schedule_for(fee_type, multiplier) is None


class TestParsing:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("initialized", MarketStatus.UPCOMING),
            ("active", MarketStatus.OPEN),
            ("inactive", MarketStatus.PAUSED),
            ("determined", MarketStatus.CLOSED),
            ("finalized", MarketStatus.RESOLVED),
            ("brand_new_status", MarketStatus.CLOSED),
        ],
    )
    def test_status(self, raw, expected):
        assert parse_status(raw) is expected

    @pytest.mark.parametrize("value", ["0.0000", "1.0000", "0.3700"])
    def test_resolution_value(self, value):
        raw = raw_market(status="finalized", settlement_value_dollars=value, settlement_ts="2026-10-03T11:10:17Z")
        res = parse_resolution(raw)
        assert res.yes_value == D(value)
        assert res.resolved_at == datetime(2026, 10, 3, 11, 10, 17, tzinfo=timezone.utc)

    def test_determined_is_not_resolved(self):
        assert parse_resolution(raw_market(status="determined", settlement_value_dollars="1.0000")) is None


class FakeKalshi:
    """Routes GET requests by path to canned payloads."""

    def __init__(self):
        self.series = {"KXHIGHNY": {"fee_type": "quadratic", "fee_multiplier": 1}}
        self.fee_changes = {}
        self.markets = {}
        self.books = {}
        self.historical = {}
        self.page_size = 1000
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        self.requests.append(request)
        path = request.url.path.removeprefix("/trade-api/v2")
        params = request.url.params
        if path.startswith("/series/"):
            return httpx.Response(200, json={"series": self.series[path.split("/")[-1]]})
        if path == "/events/fee_changes":
            return httpx.Response(200, json={"event_fee_changes": self.fee_changes.get(params["event_ticker"], []), "cursor": ""})
        if path.startswith("/events/"):
            return httpx.Response(200, json={"event": {"series_ticker": "KXHIGHNY"}})
        if path == "/markets/orderbooks":
            tickers = params.get_list("tickers")
            return httpx.Response(200, json={"orderbooks": [
                {"ticker": t, "orderbook_fp": self.books[t]} for t in tickers if t in self.books
            ]})  # fmt: skip
        if path == "/markets":
            if "tickers" in params:
                rows = [self.markets[t] for t in params["tickers"].split(",") if t in self.markets]
            else:
                rows = [m for m in self.markets.values() if m["status"] == "active"]
            start = int(params.get("cursor") or 0)
            page = rows[start : start + self.page_size]
            cursor = str(start + self.page_size) if start + self.page_size < len(rows) else ""
            return httpx.Response(200, json={"markets": page, "cursor": cursor})
        if path.startswith("/historical/markets/"):
            ticker = path.split("/")[-1]
            if ticker in self.historical:
                return httpx.Response(200, json={"market": self.historical[ticker]})
            return httpx.Response(404, json={"message": "not found"})
        raise AssertionError(f"unexpected path {path}")


def source(fake, now=NOW):
    http = ReadOnlyHttp(
        "https://external-api.kalshi.com/trade-api/v2", transport=httpx.MockTransport(fake), min_interval=0
    )
    return KalshiSource(http, ["KXHIGHNY"], now=lambda: now)


class TestKalshiSource:
    def test_list_markets_paginates(self):
        fake = FakeKalshi()
        for i in range(5):
            fake.markets[f"KX-{i}"] = raw_market(ticker=f"KX-{i}")
        fake.page_size = 2
        markets = source(fake).list_markets()
        assert [m.market_id for m in markets] == [f"KX-{i}" for i in range(5)]
        assert markets[0].fee_schedule.rate == D("0.07")
        assert markets[0].venue_meta["series_ticker"] == "KXHIGHNY"

    def test_event_fee_override_in_effect(self):
        fake = FakeKalshi()
        fake.markets["KX-1"] = raw_market(ticker="KX-1")
        fake.fee_changes["KXHIGHNY-26OCT04"] = [
            {"fee_type_override": "quadratic", "fee_multiplier_override": 0.5,
             "scheduled_ts": (NOW - timedelta(days=1)).isoformat()},
            {"fee_type_override": "quadratic", "fee_multiplier_override": 3,
             "scheduled_ts": (NOW + timedelta(days=1)).isoformat()},  # not yet in effect
        ]  # fmt: skip
        assert source(fake).list_markets()[0].fee_schedule.rate == D("0.035")

    def test_cleared_override_falls_back_to_series(self):
        fake = FakeKalshi()
        fake.markets["KX-1"] = raw_market(ticker="KX-1")
        fake.fee_changes["KXHIGHNY-26OCT04"] = [
            {"fee_type_override": "quadratic", "fee_multiplier_override": 0.5, "scheduled_ts": "2026-09-01T00:00:00Z"},
            {"fee_type_override": None, "fee_multiplier_override": None, "scheduled_ts": "2026-09-02T00:00:00Z"},
        ]
        assert source(fake).list_markets()[0].fee_schedule.rate == D("0.07")

    def test_flat_series_has_no_schedule(self):
        fake = FakeKalshi()
        fake.series["KXHIGHNY"] = {"fee_type": "flat", "fee_multiplier": 1}
        fake.markets["KX-1"] = raw_market(ticker="KX-1")
        assert source(fake).list_markets()[0].fee_schedule is None

    def test_series_fees_cached(self):
        fake = FakeKalshi()
        for i in range(3):
            fake.markets[f"KX-{i}"] = raw_market(ticker=f"KX-{i}")
        source(fake).list_markets()
        series_calls = [r for r in fake.requests if r.url.path.endswith("/series/KXHIGHNY")]
        assert len(series_calls) == 1

    def test_fetch_books_flags_bad_and_missing(self):
        fake = FakeKalshi()
        for t in ("GOOD", "CROSSED", "MISSING"):
            fake.markets[t] = raw_market(ticker=t)
        fake.books["GOOD"] = DOC_BOOK
        fake.books["CROSSED"] = {"yes_dollars": [["0.60", "1"]], "no_dollars": [["0.45", "1"]]}
        src = source(fake)
        results = src.fetch_books(src.fetch_markets(["GOOD", "CROSSED", "MISSING"]).values())
        assert results["GOOD"].best_ask == L("0.44", "17")
        assert results["GOOD"].received_at == NOW
        assert isinstance(results["CROSSED"], InvalidOrderBook)
        assert isinstance(results["MISSING"], InvalidOrderBook)

    def test_fetch_books_batches_by_100(self):
        fake = FakeKalshi()
        for i in range(150):
            fake.markets[f"KX-{i}"] = raw_market(ticker=f"KX-{i}")
            fake.books[f"KX-{i}"] = DOC_BOOK
        src = source(fake)
        results = src.fetch_books(src.list_markets())
        assert len(results) == 150
        book_calls = [r for r in fake.requests if r.url.path.endswith("/markets/orderbooks")]
        assert [len(r.url.params.get_list("tickers")) for r in book_calls] == [100, 50]

    def test_resolutions_with_historical_fallback(self):
        fake = FakeKalshi()
        fake.markets["OPEN"] = raw_market(ticker="OPEN")
        fake.markets["DONE"] = raw_market(
            ticker="DONE", status="finalized", settlement_value_dollars="1.0000", settlement_ts="2026-10-03T11:00:00Z"
        )
        fake.historical["OLD"] = raw_market(
            ticker="OLD", status="finalized", settlement_value_dollars="0.0000", settlement_ts="2026-01-01T00:00:00Z"
        )
        src = source(fake)
        markets = list(src.fetch_markets(["OPEN", "DONE"]).values())
        markets.append(src._parse_market(fake.historical["OLD"]))
        resolutions = src.fetch_resolutions(markets)
        assert set(resolutions) == {"DONE", "OLD"}
        assert resolutions["DONE"].yes_value == D("1")
        assert resolutions["OLD"].yes_value == D("0")
