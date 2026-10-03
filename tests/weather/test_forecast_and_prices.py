"""NBM client, bucket strikes and Kalshi price history. Payloads trimmed from real responses."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import httpx
import pytest

from profit_engine.venues.http import ReadOnlyHttp
from profit_engine.weather.kalshi_temps import Bucket, Quote, bucket_from, parse_candles, price_history, quote_at
from profit_engine.weather.nbm import AVAILABILITY_LAG, NbmClient, parse_run


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# NBS run 2026-10-01 12Z for KNYC (trimmed): max rows at 00Z, min rows at 12Z.
NBS_ROWS = {
    "data": [
        {"ftime": "2026-10-01 21:00", "txn": None, "xnd": None},
        {"ftime": "2026-10-02 00:00", "txn": 75.0, "xnd": 3.0},
        {"ftime": "2026-10-02 12:00", "txn": 66.0, "xnd": 2.0},
        {"ftime": "2026-10-03 00:00", "txn": 82.0, "xnd": 1.0},
    ]
}


class TestNbm:
    def test_parse_run_keeps_daytime_maxima(self):
        run = parse_run(NBS_ROWS, utc(2026, 10, 1, 12))
        # 00Z Oct 2 row is the Oct 1 max; 00Z Oct 3 row is the Oct 2 max; the 12Z row is a minimum.
        assert {d: (m.txn, m.xnd) for d, m in run.maxima.items()} == {
            date(2026, 10, 1): (75.0, 3.0),
            date(2026, 10, 2): (82.0, 1.0),
        }
        assert run.available_at == utc(2026, 10, 1, 12) + AVAILABILITY_LAG

    def test_latest_run_respects_availability(self):
        archived = {utc(2026, 10, 1, 6), utc(2026, 10, 1, 12)}
        asked = []

        def handler(request):
            runtime = datetime.strptime(request.url.params["runtime"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            asked.append(runtime)
            return httpx.Response(200, json=NBS_ROWS if runtime in archived else {"data": []})

        client = NbmClient(ReadOnlyHttp("https://iem.test", transport=httpx.MockTransport(handler), min_interval=0))
        # At 13:30Z the 12Z run is only 1.5 h old (< 2 h lag), so the 06Z run is the latest usable.
        assert client.latest_run(utc(2026, 10, 1, 13, 30)).runtime == utc(2026, 10, 1, 6)
        assert max(asked) <= utc(2026, 10, 1, 11, 30)
        # At 14:00Z the 12Z run becomes usable.
        assert client.latest_run(utc(2026, 10, 1, 14)).runtime == utc(2026, 10, 1, 12)

    def test_404_means_missing(self):
        def handler(request):
            return httpx.Response(404, json={"detail": "no data"})

        client = NbmClient(ReadOnlyHttp("https://iem.test", transport=httpx.MockTransport(handler), min_interval=0, max_retries=0))
        assert client.run(utc(2026, 10, 1, 12)) is None
        assert client.latest_run(utc(2026, 10, 1, 14), lookback=timedelta(hours=7)) is None


class TestBucket:
    @pytest.mark.parametrize(
        "bucket, inside, outside",
        [
            (Bucket("T70", "greater", D(70), None), ["71", "70.5"], ["70", "69"]),
            (Bucket("T63", "less", None, D(63)), ["62", "62.9"], ["63"]),
            (Bucket("B69.5", "between", D(69), D(70)), ["69", "70"], ["68", "71"]),
        ],
    )
    def test_contains(self, bucket, inside, outside):
        assert all(bucket.contains(D(v)) for v in inside)
        assert not any(bucket.contains(D(v)) for v in outside)

    def test_from_raw_market(self):
        raw = {"ticker": "KXHIGHNY-26SEP28-B70.5", "strike_type": "between", "floor_strike": 70, "cap_strike": 71}
        assert bucket_from(raw) == Bucket("KXHIGHNY-26SEP28-B70.5", "between", D(70), D(71))


class TestQuotes:
    def test_live_and_historical_field_names(self):
        live = [{"end_period_ts": 1790539200, "yes_bid": {"close_dollars": "0.0100"}, "yes_ask": {"close_dollars": "0.0200"}}]
        hist = [{"end_period_ts": 1751306400, "yes_bid": {"close": "0.0000"}, "yes_ask": {"close": "0.0600"}}]
        assert parse_candles(live)[0].midpoint == D("0.015")
        assert parse_candles(hist)[0].midpoint is None  # no bid: one-sided

    def test_quote_at_never_looks_ahead(self):
        quotes = [Quote(utc(2026, 9, 28, h), D("0.4"), D("0.5")) for h in (10, 11, 13)]
        assert quote_at(quotes, utc(2026, 9, 28, 12, 30)).at == utc(2026, 9, 28, 11)
        assert quote_at(quotes, utc(2026, 9, 28, 9)) is None

    def test_price_history_batches_live(self):
        seen = []

        def handler(request):
            seen.append(request)
            tickers = request.url.params["market_tickers"].split(",")
            return httpx.Response(200, json={"markets": [{"market_ticker": t, "candlesticks": []} for t in tickers]})

        http = ReadOnlyHttp("https://k.test", transport=httpx.MockTransport(handler), min_interval=0)
        out = price_history(http, [f"T{i}" for i in range(150)], utc(2026, 9, 1), utc(2026, 9, 2), historical=False)
        assert len(out) == 150 and len(seen) == 2
        assert all(r.method == "GET" for r in seen)
