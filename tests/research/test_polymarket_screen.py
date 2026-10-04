import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from polymarket_screen import Closed, build_rows, category_of, parse_market, parse_time, sample

RAW = {
    "id": "123",
    "outcomes": json.dumps(["Yes", "No"]),
    "outcomePrices": json.dumps(["0", "1"]),
    "clobTokenIds": json.dumps(["tokYes", "tokNo"]),
    "createdAt": "2025-04-10T12:00:00Z",
    "closedTime": "2025-04-19 05:45:53+00",
    "events": [{"id": "9"}],
    "tags": [{"label": "NBA"}, {"label": "Sports"}],
}


def test_parse_market():
    m = parse_market(RAW)
    assert (m.token, m.result, m.event_id, m.category, m.fee_rate) == ("tokYes", 0, "9", "Sports", D("0.05"))
    assert m.closed_at == datetime(2025, 4, 19, 5, 45, 53, tzinfo=timezone.utc)
    assert m.created_at == datetime(2025, 4, 10, 12, tzinfo=timezone.utc)
    assert parse_market(dict(RAW, createdAt=None)) is None
    assert parse_market(dict(RAW, outcomePrices=json.dumps(["0.5", "0.5"]))) is None  # 50-50 resolution
    assert parse_market(dict(RAW, outcomePrices=json.dumps(["0", "0"]))) is None  # never resolved
    assert parse_market(dict(RAW, closedTime=None)) is None


def test_category_priority_and_fees():
    assert category_of(["Politics", "Geopolitics"]) == ("Geopolitics", D("0"))  # fee-free wins
    assert category_of(["Pop Culture"]) == ("Culture", D("0.05"))
    assert category_of(["Crypto", "Sports"]) == ("Crypto", D("0.07"))
    assert category_of([]) == ("Other", D("0.05"))
    assert parse_time("2026-01-02T03:04:05Z") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def mk(i, event, category="Crypto", day=0, life=timedelta(days=3)):
    t = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)
    return Closed(f"m{i}", event, f"t{i}", t, t + life, i % 2, category, D("0.07"))


def test_sample_one_per_event_and_caps_category_day():
    # 20 events on one day in one category, 3 markets each -> 20 candidates, capped at 5
    ms = [mk(i * 3 + j, f"e{i}") for i in range(20) for j in range(3)]
    picks = sample(ms, per_category_day=5)
    assert len(picks) == 5 and len({p.event_id for p in picks}) == 5
    # a second category and a second day each get their own cap
    more = ms + [mk(100 + i, f"s{i}", "Sports") for i in range(3)] + [mk(200 + i, f"d{i}", day=1) for i in range(7)]
    assert len(sample(more, per_category_day=5)) == 5 + 3 + 5
    assert sample(more) == sample(more)  # seeded


def test_build_rows_pays_the_price_both_ways():
    m = mk(1, "e1")  # result 1
    rows, dropped = build_rows([m, mk(2, "e2"), mk(3, "e3")], {"m1": D("0.30"), "m2": None, "m3": D("1")})
    assert dropped == {"no price": 1, "price at 0 or 1": 1}
    r = rows[0]
    # buy first outcome at 0.30: fee 0.07 * 0.3 * 0.7 = 0.0147; wins -> 0.6853
    assert r.buy_yes == D("0.6853")
    assert r.mid == D("0.30") and r.cluster == "Crypto|2026-01-02"  # decision date: created + 24 h


def test_decision_time_ignores_close_and_skips_short_lived_markets():
    from polymarket_screen import decision_time

    a, b = mk(1, "e1"), mk(2, "e2", life=timedelta(days=30))
    assert decision_time(a) == decision_time(b) == datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert sample([mk(3, "e3", life=timedelta(hours=20))]) == []  # closed before the decision time
