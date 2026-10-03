"""Polymarket adapter tests. Payloads are trimmed copies of real responses (2026-10-03)."""

import json
from datetime import datetime, timezone
from decimal import Decimal as D

import httpx
import pytest
from factories import L

from profit_engine.core import InvalidOrderBook, MarketStatus
from profit_engine.venues.http import ReadOnlyHttp, VenueHttpError
from profit_engine.venues.polymarket import (
    PolymarketSource,
    fee_schedule_from,
    parse_book,
    parse_market,
    parse_resolution,
)
from profit_engine.venues.polymarket.adapter import parse_status

NOW = datetime(2026, 10, 3, 22, 30, tzinfo=timezone.utc)
COND = "0x90eb090b7cfb854b88e4252c9c2a74dcc875558212b71c10dc86188323e3e5c3"


def gamma_market(cond=COND, **overrides):
    raw = {
        "id": "540817",
        "question": "Will Indiana enact a data center moratorium by December 31, 2027?",
        "conditionId": cond,
        "slug": "indiana-data-center-moratorium",
        "endDate": "2027-12-31T12:00:00Z",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["111", "222"]',
        "active": True,
        "closed": False,
        "archived": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "orderMinSize": 5,
        "negRisk": False,
        "feesEnabled": True,
        "feeSchedule": {"exponent": 1, "rate": 0.04, "takerOnly": True, "rebateRate": 0.25},
    }
    raw.update(overrides)
    return raw


# CLOB /book for the YES token: bids ascending, asks descending (best last in both).
CLOB_BOOK = {
    "market": COND,
    "asset_id": "111",
    "bids": [{"price": "0.07", "size": "61918.7"}, {"price": "0.08", "size": "9560"}, {"price": "0.09", "size": "6455"}],
    "asks": [{"price": "0.12", "size": "62521.74"}, {"price": "0.11", "size": "19105.11"}, {"price": "0.1", "size": "4951.88"}],
    "min_order_size": "5",
    "tick_size": "0.01",
    "hash": "abc",
}


class TestParsing:
    def test_market(self):
        m = parse_market(gamma_market())
        assert m.market_id == COND
        assert m.yes_label == "Yes"
        assert m.status is MarketStatus.OPEN
        assert m.close_time == datetime(2027, 12, 31, 12, tzinfo=timezone.utc)
        assert m.fee_schedule.rate == D("0.04")
        assert m.min_order_size == D("5")
        assert m.venue_meta["yes_token_id"] == "111"

    def test_named_outcomes(self):
        m = parse_market(gamma_market(outcomes='["Florida", "Missouri"]'))
        assert m.yes_label == "Florida"

    @pytest.mark.parametrize("outcomes, tokens", [('["A", "B", "C"]', '["1", "2", "3"]'), ("[]", "[]"), ("not json", "[]")])
    def test_non_binary_skipped(self, outcomes, tokens):
        assert parse_market(gamma_market(outcomes=outcomes, clobTokenIds=tokens)) is None

    @pytest.mark.parametrize(
        "overrides, expected",
        [
            ({}, MarketStatus.OPEN),
            ({"acceptingOrders": False}, MarketStatus.PAUSED),
            ({"active": False, "acceptingOrders": False}, MarketStatus.UPCOMING),
            ({"closed": True}, MarketStatus.CLOSED),
            ({"closed": True, "umaResolutionStatus": "resolved"}, MarketStatus.RESOLVED),
            ({"archived": True}, MarketStatus.CLOSED),
            ({"enableOrderBook": False}, MarketStatus.CLOSED),
        ],
    )
    def test_status(self, overrides, expected):
        assert parse_status(gamma_market(**overrides)) is expected

    def test_book_sorted_best_first(self):
        book = parse_book(COND, CLOB_BOOK, NOW)
        assert book.bids == (L("0.09", "6455"), L("0.08", "9560"), L("0.07", "61918.7"))
        assert book.asks == (L("0.1", "4951.88"), L("0.11", "19105.11"), L("0.12", "62521.74"))

    def test_book_crossed_or_bad(self):
        with pytest.raises(InvalidOrderBook):
            parse_book(COND, {"bids": [{"price": "0.5", "size": "1"}], "asks": [{"price": "0.4", "size": "1"}]}, NOW)
        with pytest.raises(InvalidOrderBook):
            parse_book(COND, {"bids": [{"price": "0.5"}], "asks": []}, NOW)


class TestFees:
    def test_rate_from_schedule(self):
        assert fee_schedule_from(gamma_market()).rate == D("0.04")

    def test_zero_rate_market(self):
        raw = gamma_market(feeSchedule={"exponent": 1, "rate": 0, "takerOnly": True, "rebateRate": 0})
        assert fee_schedule_from(raw).rate == 0

    def test_fees_disabled_without_schedule(self):
        assert fee_schedule_from(gamma_market(feeSchedule=None, feesEnabled=False)).rate == 0

    def test_missing_schedule_is_unknown(self):
        assert fee_schedule_from(gamma_market(feeSchedule=None)) is None

    def test_other_exponent_is_unknown(self):
        raw = gamma_market(feeSchedule={"exponent": 2, "rate": 0.04, "takerOnly": True, "rebateRate": 0})
        assert fee_schedule_from(raw) is None


class TestResolution:
    def row(self, payouts, status="resolved"):
        return {"condition_id": COND, "status": status, "payouts": payouts, "resolved_at": "2026-10-03T22:31:47Z"}

    @pytest.mark.parametrize(
        "payouts, value", [([1000000, 0], "1"), ([0, 1000000], "0"), ([1, 1], "0.5")]
    )
    def test_payout_vector(self, payouts, value):
        assert parse_resolution(self.row(payouts)).yes_value == D(value)

    def test_not_resolved(self):
        assert parse_resolution(self.row([1, 0], status="posed")) is None

    def test_bad_payouts(self):
        assert parse_resolution(self.row([0, 0])) is None
        assert parse_resolution(self.row([1, 0, 0])) is None


class FakePolymarket:
    def __init__(self):
        self.markets = []  # gamma rows
        self.books = {}  # token id -> payload or status code
        self.resolutions = []
        self.page = 100
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        self.requests.append(request)
        host, path, params = request.url.host, request.url.path, request.url.params
        if host == "gamma.test" and path == "/markets/keyset":
            closed = params.get("closed") == "true"
            rows = [m for m in self.markets if bool(m["closed"]) == closed]
            if "condition_ids" in params:
                wanted = set(params.get_list("condition_ids"))
                rows = [m for m in rows if m["conditionId"] in wanted]
            start = int(params.get("after_cursor") or 0)
            size = min(int(params["limit"]), self.page)
            page = rows[start : start + size]
            cursor = str(start + size) if start + size < len(rows) else None
            return httpx.Response(200, json={"markets": page, "next_cursor": cursor})
        if host == "clob.test" and path == "/book":
            book = self.books.get(params["token_id"], 404)
            if isinstance(book, int):
                return httpx.Response(book, json={"error": "No orderbook exists"})
            return httpx.Response(200, json=book)
        if host == "data.test" and path == "/v2/resolutions":
            wanted = set(params["condition"].split(","))
            return httpx.Response(200, json={"data": [r for r in self.resolutions if r["condition_id"] in wanted]})
        raise AssertionError(f"unexpected {request.url}")


def source(fake, top_n=50):
    def http(host):
        return ReadOnlyHttp(f"https://{host}", transport=httpx.MockTransport(fake), min_interval=0, max_retries=0)

    return PolymarketSource(http("gamma.test"), http("clob.test"), http("data.test"), top_n=top_n, now=lambda: NOW)


class TestPolymarketSource:
    def test_list_markets_pages_and_filters(self):
        fake = FakePolymarket()
        fake.page = 2
        fake.markets = [gamma_market(cond=f"0x{i}", clobTokenIds=f'["{i}a", "{i}b"]') for i in range(5)]
        fake.markets[1]["acceptingOrders"] = False  # paused: excluded
        fake.markets[2]["outcomes"] = '["A", "B", "C"]'  # not binary: excluded
        markets = source(fake, top_n=10).list_markets()
        assert [m.market_id for m in markets] == ["0x0", "0x3", "0x4"]

    def test_top_n(self):
        fake = FakePolymarket()
        fake.markets = [gamma_market(cond=f"0x{i}") for i in range(5)]
        assert len(source(fake, top_n=2).list_markets()) == 2

    def test_fetch_markets_includes_closed(self):
        fake = FakePolymarket()
        fake.markets = [gamma_market(cond="0xopen"), gamma_market(cond="0xshut", closed=True)]
        found = source(fake).fetch_markets(["0xopen", "0xshut", "0xmissing"])
        assert set(found) == {"0xopen", "0xshut"}
        assert found["0xshut"].status is MarketStatus.CLOSED

    def test_fetch_books(self):
        fake = FakePolymarket()
        fake.markets = [gamma_market(cond="0xa", clobTokenIds='["ta", "na"]'), gamma_market(cond="0xb", clobTokenIds='["tb", "nb"]')]
        fake.books["ta"] = CLOB_BOOK
        src = source(fake)
        results = src.fetch_books(src.list_markets())
        assert results["0xa"].best_bid == L("0.09", "6455")
        assert results["0xa"].received_at == NOW
        assert isinstance(results["0xb"], InvalidOrderBook)  # 404: no book
        # Only YES tokens are requested; the NO book is a mirror.
        assert [r.url.params["token_id"] for r in fake.requests if r.url.path == "/book"] == ["ta", "tb"]

    def test_server_error_propagates(self):
        fake = FakePolymarket()
        fake.markets = [gamma_market(cond="0xa", clobTokenIds='["ta", "na"]')]
        fake.books["ta"] = 500
        src = source(fake)
        with pytest.raises(VenueHttpError):
            src.fetch_books(src.list_markets())

    def test_resolutions(self):
        fake = FakePolymarket()
        fake.markets = [gamma_market(cond="0xa"), gamma_market(cond="0xb")]
        fake.resolutions = [
            {"condition_id": "0xa", "status": "resolved", "payouts": [0, 1000000], "resolved_at": "2026-10-03T22:31:47Z"},
            {"condition_id": "0xb", "status": "posed"},
        ]
        src = source(fake)
        res = src.fetch_resolutions(src.list_markets())
        assert list(res) == ["0xa"] and res["0xa"].yes_value == 0


def test_gamma_json_strings_round_trip():
    # Gamma encodes lists as JSON strings; make sure our fixture matches that shape.
    assert json.loads(gamma_market()["clobTokenIds"]) == ["111", "222"]
