from datetime import datetime, timezone
from decimal import Decimal as D

from factories import make_book, make_market

from profit_engine import cli


class AlwaysModel:
    def predict(self, market, book):
        return D("0.30")


def market(event="KXHIGHNY-26OCT05"):
    return make_market(market_id=f"{event}-B70.5", venue_meta={"series_ticker": "KXHIGHNY", "event_ticker": event,
                                                               "strike_type": "between", "floor_strike": "70",
                                                               "cap_strike": "71"})


def fair_at(monkeypatch, when):
    import profit_engine.core as core
    import profit_engine.models.kalshi_temperature as kt

    monkeypatch.setattr(kt.KalshiHighTemperatureLamp, "from_file", classmethod(lambda cls, *a, **kw: AlwaysModel()))
    monkeypatch.setattr(core, "utc_now", lambda: when)
    return cli.maker_fair_price(["KXHIGHNY"])(market(), make_book())


def test_veto_window_is_day_before_16_to_day_16_local(monkeypatch):
    # Oct 5 event, EDT (UTC-4): window 2026-10-04 20:00Z to 2026-10-05 20:00Z.
    assert fair_at(monkeypatch, datetime(2026, 10, 4, 19, 59, tzinfo=timezone.utc)) is None
    assert fair_at(monkeypatch, datetime(2026, 10, 4, 20, 0, tzinfo=timezone.utc)) == D("0.30")
    assert fair_at(monkeypatch, datetime(2026, 10, 5, 19, 59, tzinfo=timezone.utc)) == D("0.30")
    assert fair_at(monkeypatch, datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)) is None
