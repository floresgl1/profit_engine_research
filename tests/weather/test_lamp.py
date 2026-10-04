from datetime import datetime, timedelta, timezone

import httpx

from profit_engine.venues.http import ReadOnlyHttp
from profit_engine.weather.lamp import AVAILABILITY_LAG, LampClient, parse_run


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def rows(runtime, hours=38):
    return {"data": [{"ftime": (runtime + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M"), "tmp": 70 + h % 10} for h in range(1, hours + 1)]}


def test_parse_and_max_between():
    run = parse_run(rows(utc(2026, 10, 2, 14), hours=4), utc(2026, 10, 2, 14))
    # 15Z 71, 16Z 72, 17Z 73, 18Z 74
    assert run.max_between(utc(2026, 10, 2, 15), utc(2026, 10, 2, 18)) == 73.0
    assert run.max_between(utc(2026, 10, 3, 0), utc(2026, 10, 3, 4)) is None
    assert run.available_at == utc(2026, 10, 2, 14) + AVAILABILITY_LAG


def client(archived):
    asked = []

    def handler(request):
        runtime = datetime.strptime(request.url.params["runtime"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        asked.append(runtime)
        return httpx.Response(200, json=rows(runtime) if runtime in archived else {"data": []})

    return LampClient(ReadOnlyHttp("https://iem.test", transport=httpx.MockTransport(handler), min_interval=0)), asked


def test_latest_run_respects_lag_and_coverage():
    c, asked = client({utc(2026, 10, 2, 12), utc(2026, 10, 2, 13)})
    # At 13:30Z the 13Z run is only 30 min old (< 1 h lag): use 12Z.
    run = c.latest_run(utc(2026, 10, 2, 13, 30), until=utc(2026, 10, 3, 4))
    assert run.runtime == utc(2026, 10, 2, 12)
    assert max(asked) == utc(2026, 10, 2, 12)


def test_sparse_history_skips_off_hours():
    c, asked = client({utc(2025, 7, 1, 12)})
    run = c.latest_run(utc(2025, 7, 1, 18), until=utc(2025, 7, 2, 4), sparse_before=utc(2026, 9, 27))
    assert run.runtime == utc(2025, 7, 1, 12)
    assert all(r.hour % 6 == 0 for r in asked)


def test_run_must_cover_the_day():
    c, _ = client({utc(2026, 10, 2, 12)})
    # A run reaching only to 2026-10-04 02Z cannot cover a window ending 2026-10-05 04Z.
    assert c.latest_run(utc(2026, 10, 2, 14), until=utc(2026, 10, 5, 4)) is None
