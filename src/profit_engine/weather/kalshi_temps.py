"""Settled values of Kalshi daily temperature series (e.g. KXHIGHNY).

Every market in an event settles on the same number, Kalshi's
`expiration_value`. These are the targets a temperature model must predict,
and the rules text records which agency's number was used.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from profit_engine.venues.http import ReadOnlyHttp

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Bucket:
    """One market in a temperature event, described by Kalshi's strike fields.

    strike_type "greater": value > floor; "less": value < cap;
    "between": floor <= value <= cap (inclusive, per the contract terms).
    """

    ticker: str
    strike_type: str
    floor: Decimal | None
    cap: Decimal | None

    def contains(self, value: Decimal) -> bool:
        if self.strike_type == "greater":
            return value > self.floor
        if self.strike_type == "less":
            return value < self.cap
        if self.strike_type == "between":
            return self.floor <= value <= self.cap
        raise ValueError(f"unknown strike_type {self.strike_type!r}")


def bucket_from(raw: dict[str, Any]) -> Bucket:
    def dec(key: str) -> Decimal | None:
        value = raw.get(key)
        return None if value in (None, "") else Decimal(str(value))

    return Bucket(raw["ticker"], raw.get("strike_type", ""), dec("floor_strike"), dec("cap_strike"))


@dataclass(frozen=True, slots=True)
class SettledTemperature:
    event_ticker: str
    day: date
    value: Decimal | None  # None when Kalshi published no expiration_value
    source: str  # "twc", "nws", or "unspecified"
    buckets: tuple[Bucket, ...] = ()


def event_day(event_ticker: str) -> date:
    """`KXHIGHNY-26OCT02` -> 2026-10-02 (also works for old `HIGHNY-21AUG06` tickers)."""
    return datetime.strptime(event_ticker.rsplit("-", 1)[1], "%y%b%d").date()


def settlement_source(rules: str) -> str:
    if "Weather Company" in rules:
        return "twc"
    if "National Weather Service" in rules or "Climatological Report" in rules:
        return "nws"
    return "unspecified"


def group_events(markets: list[dict[str, Any]]) -> list[SettledTemperature]:
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for market in markets:
        by_event[market["event_ticker"]].append(market)
    settled = []
    for event, members in by_event.items():
        values = {Decimal(m["expiration_value"]) for m in members if m.get("expiration_value") not in (None, "")}
        sources = {settlement_source(m.get("rules_primary", "")) for m in members}
        if len(values) > 1:
            log.warning("%s: markets disagree on expiration_value %s", event, sorted(values))
        settled.append(
            SettledTemperature(
                event_ticker=event,
                day=event_day(event),
                value=values.pop() if len(values) == 1 else None,
                source=sources.pop() if len(sources) == 1 else "unspecified",
                buckets=tuple(sorted((bucket_from(m) for m in members), key=lambda b: b.ticker)),
            )
        )
    settled.sort(key=lambda s: s.day)
    return settled


def settled_temperatures(http: ReadOnlyHttp, series_ticker: str) -> list[SettledTemperature]:
    """One row per settled event, from both the live and the historical (archived) data sets."""
    markets = list(_paginate(http, "/markets", {"series_ticker": series_ticker, "status": "settled", "limit": 1000}))
    markets += _paginate(http, "/historical/markets", {"series_ticker": series_ticker, "limit": 1000})
    return group_events(markets)


def _paginate(http: ReadOnlyHttp, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
    cursor = ""
    while True:
        data = http.get_json(path, dict(params, cursor=cursor) if cursor else params)
        yield from data.get("markets", [])
        cursor = data.get("cursor") or ""
        if not cursor:
            return


@dataclass(frozen=True, slots=True)
class Quote:
    """Best YES bid and ask at the close of an hourly candle. 0 bid / 1 ask mean that side was empty."""

    at: datetime
    bid: Decimal
    ask: Decimal

    @property
    def midpoint(self) -> Decimal | None:
        if self.bid <= 0 or self.ask >= 1 or self.bid >= self.ask:
            return None
        return (self.bid + self.ask) / 2


def _close(side: dict[str, Any] | None) -> Decimal | None:
    if not side:
        return None
    value = side.get("close_dollars", side.get("close"))
    return None if value in (None, "") else Decimal(value)


def parse_candles(candles: list[dict[str, Any]]) -> list[Quote]:
    quotes = []
    for c in candles:
        bid, ask = _close(c.get("yes_bid")), _close(c.get("yes_ask"))
        if bid is None or ask is None:
            continue
        quotes.append(Quote(datetime.fromtimestamp(c["end_period_ts"], tz=timezone.utc), bid, ask))
    quotes.sort(key=lambda q: q.at)
    return quotes


def quote_at(quotes: list[Quote], when: datetime) -> Quote | None:
    """Last quote whose candle closed at or before `when` (never a later one)."""
    best = None
    for q in quotes:
        if q.at > when:
            break
        best = q
    return best


def price_history(
    http: ReadOnlyHttp, tickers: list[str], start: datetime, end: datetime, historical: bool
) -> dict[str, list[Quote]]:
    """Hourly quotes per ticker. Archived markets need one call each; live ones batch up to 100."""
    params = {"start_ts": str(int(start.timestamp())), "end_ts": str(int(end.timestamp())), "period_interval": "60"}
    out: dict[str, list[Quote]] = {}
    if historical:
        for ticker in tickers:
            data = http.get_json(f"/historical/markets/{ticker}/candlesticks", params)
            out[ticker] = parse_candles(data.get("candlesticks") or [])
        return out
    for i in range(0, len(tickers), 100):
        data = http.get_json("/markets/candlesticks", dict(params, market_tickers=",".join(tickers[i : i + 100])))
        for market in data.get("markets") or []:
            out[market["market_ticker"]] = parse_candles(market.get("candlesticks") or [])
    return out
