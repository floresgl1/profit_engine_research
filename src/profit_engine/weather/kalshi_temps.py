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
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from profit_engine.venues.http import ReadOnlyHttp

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SettledTemperature:
    event_ticker: str
    day: date
    value: Decimal | None  # None when Kalshi published no expiration_value
    source: str  # "twc", "nws", or "unspecified"


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
