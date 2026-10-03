"""Weather client tests. Payloads are trimmed copies of real responses (2026-10-03)."""

from datetime import date, datetime, timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import httpx
import pytest

from profit_engine.venues.http import ReadOnlyHttp
from profit_engine.weather import AcisClient, IemAsosClient, clock_window, is_dst, is_transition, lst_window
from profit_engine.weather.acis import parse_value
from profit_engine.weather.iem import parse_csv
from profit_engine.weather.kalshi_temps import event_day, group_events, settlement_source

NY = ZoneInfo("America/New_York")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class TestWindows:
    def test_summer_lst_runs_1am_to_1am_edt(self):
        # LST = EST (UTC-5) all year: Oct 2 00:00 EST = 05:00 UTC = 01:00 EDT.
        assert lst_window(date(2026, 10, 2), NY) == (utc(2026, 10, 2, 5), utc(2026, 10, 3, 5))

    def test_summer_clock_runs_midnight_to_midnight_edt(self):
        # 00:00 EDT = 04:00 UTC
        assert clock_window(date(2026, 10, 2), NY) == (utc(2026, 10, 2, 4), utc(2026, 10, 3, 4))

    def test_winter_windows_identical(self):
        day = date(2026, 1, 15)
        assert lst_window(day, NY) == clock_window(day, NY) == (utc(2026, 1, 15, 5), utc(2026, 1, 16, 5))

    def test_dst_flags(self):
        assert is_dst(date(2026, 7, 4), NY) and not is_dst(date(2026, 1, 4), NY)

    def test_transition_days(self):
        # 2026: DST starts Mar 8, ends Nov 1.
        assert is_transition(date(2026, 3, 8), NY) and is_transition(date(2026, 11, 1), NY)
        assert not is_transition(date(2026, 3, 9), NY)


class TestAcis:
    def test_parse_value(self):
        assert parse_value("83") == 83 and parse_value("-4") == -4 and parse_value("M") is None
        with pytest.raises(ValueError):
            parse_value("83A")

    def test_daily_max(self):
        def handler(request):
            assert request.method == "GET" and request.url.path == "/StnData"
            assert request.url.params["sid"] == "NYCthr"
            return httpx.Response(200, json={
                "meta": {"name": "New York-Central Park Area"},
                "data": [["2026-10-01", "76"], ["2026-10-02", "83"], ["2026-10-03", "M"]],
            })  # fmt: skip

        client = AcisClient(ReadOnlyHttp("https://acis.test", transport=httpx.MockTransport(handler), min_interval=0))
        highs = client.daily_max("NYCthr", date(2026, 10, 1), date(2026, 10, 3))
        assert highs == {date(2026, 10, 1): 76, date(2026, 10, 2): 83, date(2026, 10, 3): None}


IEM_CSV = """station,valid,tmpf
NYC,2026-08-02 15:03,80.00
NYC,2026-08-02 14:51,79.00
NYC,2026-08-02 15:33,M
NYC,2026-08-02 15:51,78.50
"""


class TestIem:
    def test_parse_sorts_and_drops_missing(self):
        obs = parse_csv(IEM_CSV)
        assert [(o.at, o.tmpf) for o in obs] == [
            (utc(2026, 8, 2, 14, 51), D("79.00")),
            (utc(2026, 8, 2, 15, 3), D("80.00")),
            (utc(2026, 8, 2, 15, 51), D("78.50")),
        ]

    def test_request_params(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text=IEM_CSV)

        client = IemAsosClient(ReadOnlyHttp("https://iem.test", transport=httpx.MockTransport(handler), min_interval=0))
        client.temperatures("NYC", date(2026, 8, 1), date(2026, 8, 3))
        params = seen[0].url.params
        assert seen[0].method == "GET"
        assert (params["year1"], params["month1"], params["day1"], params["day2"]) == ("2026", "8", "1", "3")
        assert params["tz"] == "Etc/UTC"
        assert params.get_list("report_type") == ["3", "4"]


def market(event, value, rules):
    return {"event_ticker": event, "expiration_value": value, "rules_primary": rules}


TWC_RULES = "If the maximum temperature recorded at New York City for Aug 14, 2026, is greater than 92° fahrenheit according to The Weather Company, then the market resolves to Yes."
NWS_RULES = "If the highest temperature recorded in Central Park, New York for August 13, 2026 as reported by the National Weather Service's Climatological Report (Daily), is greater than 92°, then the market resolves to Yes."
OLD_RULES = "If the highest temperature recorded in Central Park, New York on August 6, 2021 is strictly greater than 86°F, then the market resolves to Yes."


class TestKalshiTemps:
    def test_event_day(self):
        assert event_day("KXHIGHNY-26OCT02") == date(2026, 10, 2)
        assert event_day("HIGHNY-21AUG06") == date(2021, 8, 6)

    def test_settlement_source(self):
        assert settlement_source(TWC_RULES) == "twc"
        assert settlement_source(NWS_RULES) == "nws"
        assert settlement_source(OLD_RULES) == "unspecified"

    def test_group_events(self):
        rows = group_events([
            market("KXHIGHNY-26AUG14", "91.00", TWC_RULES),
            market("KXHIGHNY-26AUG14", "", TWC_RULES),  # some markets carry no value
            market("KXHIGHNY-26AUG13", "88.00", NWS_RULES),
            market("KXHIGHNY-25DEC01", "", NWS_RULES),
        ])  # fmt: skip
        assert [(r.day, r.value, r.source) for r in rows] == [
            (date(2025, 12, 1), None, "nws"),
            (date(2026, 8, 13), D("88"), "nws"),
            (date(2026, 8, 14), D("91"), "twc"),
        ]

    def test_conflicting_values_become_none(self):
        rows = group_events([market("KXHIGHNY-26AUG14", "91", TWC_RULES), market("KXHIGHNY-26AUG14", "92", TWC_RULES)])
        assert rows[0].value is None
