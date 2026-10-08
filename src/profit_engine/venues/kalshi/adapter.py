"""Kalshi adapter: public REST market data only, no API key.

Endpoints (docs.kalshi.com, checked 2026-10-03):
  GET /markets                       market metadata, status, settlement
  GET /markets/orderbooks?tickers=   up to 100 books per call; bids only
  GET /series/{ticker}               fee_type, fee_multiplier
  GET /events/{ticker}               series_ticker for an event
  GET /events/fee_changes            per-event fee overrides
  GET /historical/markets/{ticker}   markets archived past the historical cutoff
  GET /markets/trades?ticker=&min_ts= public trade prints (for the paper market maker)
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from profit_engine.core import (
    FeeSchedule,
    InvalidOrderBook,
    Level,
    Market,
    MarketStatus,
    OrderBook,
    Resolution,
    parse_utc,
    utc_now,
)
from profit_engine.maker.queue import PublicTrade
from profit_engine.venues.http import ReadOnlyHttp, VenueHttpError

log = logging.getLogger(__name__)

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
VENUE = "kalshi"

# General Trading Fees coefficient; the series' fee_multiplier scales it.
# Confirmed by the worked example at docs.kalshi.com/getting_started/fee_rounding.
TAKER_COEFFICIENT = Decimal("0.07")
QUADRATIC_FEE_TYPES = {"quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}
FILL_FEE_QUANTUM = Decimal("0.000001")
DIRECT_MEMBER_BALANCE_QUANTUM = Decimal("0.0001")
CONTRACT_STEP = Decimal("0.01")  # fractional contracts, 0.01 granularity

_STATUS = {
    "initialized": MarketStatus.UPCOMING,
    "active": MarketStatus.OPEN,
    "inactive": MarketStatus.PAUSED,
    "closed": MarketStatus.CLOSED,
    "determined": MarketStatus.CLOSED,  # result can still be disputed
    "disputed": MarketStatus.CLOSED,
    "amended": MarketStatus.CLOSED,
    "finalized": MarketStatus.RESOLVED,
}

_BOOK_BATCH = 100
_MARKET_BATCH = 100
TRADE_PAGES = 20  # exchange-wide trades pages per fetch (1,000 each); ~2 is normal, 20 is a runaway guard
_ONE = Decimal(1)


def parse_status(raw: str) -> MarketStatus:
    status = _STATUS.get(raw)
    if status is None:
        log.warning("unknown Kalshi market status %r, treating as closed", raw)
        return MarketStatus.CLOSED
    return status


def fee_schedule_for(
    fee_type: str | None,
    fee_multiplier: Any,
    balance_quantum: Decimal = DIRECT_MEMBER_BALANCE_QUANTUM,
) -> FeeSchedule | None:
    """Taker FeeSchedule for a Kalshi fee type, or None if we cannot model it."""
    if fee_type not in QUADRATIC_FEE_TYPES or fee_multiplier is None:
        return None
    multiplier = Decimal(str(fee_multiplier))
    return FeeSchedule(
        rate=TAKER_COEFFICIENT * multiplier,
        fill_quantum=FILL_FEE_QUANTUM,
        fill_rounding=ROUND_CEILING,
        cash_quantum=balance_quantum,
    )


def parse_book(ticker: str, raw: dict[str, Any], received_at: datetime) -> OrderBook:
    """Convert Kalshi's bids-only book into a YES book.

    YES bids stay bids. A NO bid at q is a YES ask at 1 - q. Kalshi sorts
    ascending (best last); we sort explicitly rather than trust the order.
    Zero-size levels carry no liquidity and are dropped.
    """
    try:
        yes = [(Decimal(p), Decimal(s)) for p, s in raw.get("yes_dollars") or []]
        no = [(Decimal(p), Decimal(s)) for p, s in raw.get("no_dollars") or []]
    except (ValueError, ArithmeticError, TypeError) as exc:
        raise InvalidOrderBook(f"{ticker}: unparseable level: {exc}") from exc
    bids = tuple(Level(p, s) for p, s in sorted(yes, key=lambda lvl: -lvl[0]) if s != 0)
    asks = tuple(Level(_ONE - p, s) for p, s in sorted(no, key=lambda lvl: -lvl[0]) if s != 0)
    return OrderBook(VENUE, ticker, bids, asks, received_at)


def parse_trade(raw: dict[str, Any]) -> PublicTrade | None:
    """A public trade print; None for block trades or anything malformed."""
    if raw.get("is_block_trade"):
        return None
    side = raw.get("taker_outcome_side") or raw.get("taker_side")
    try:
        if side not in ("yes", "no"):
            raise ValueError(f"taker side {side!r}")
        return PublicTrade(
            trade_id=str(raw["trade_id"]),
            at=parse_utc(raw["created_time"]),
            yes_price=Decimal(raw["yes_price_dollars"]),
            count=Decimal(raw["count_fp"]),
            taker_yes=side == "yes",
        )
    except (KeyError, ValueError, ArithmeticError) as exc:
        log.warning("skipping malformed Kalshi trade %s: %s", raw.get("trade_id"), exc)
        return None


def parse_resolution(raw: dict[str, Any]) -> Resolution | None:
    if raw.get("status") != "finalized":
        return None
    value = raw.get("settlement_value_dollars")
    settled = raw.get("settlement_ts")
    if value in (None, "") or not settled:
        log.warning("finalized Kalshi market %s lacks settlement fields", raw.get("ticker"))
        return None
    return Resolution(VENUE, raw["ticker"], Decimal(value), parse_utc(settled))


class KalshiSource:
    venue = VENUE

    def __init__(
        self,
        http: ReadOnlyHttp,
        series_tickers: Sequence[str],
        *,
        now: Callable[[], datetime] = utc_now,
        balance_quantum: Decimal = DIRECT_MEMBER_BALANCE_QUANTUM,
        fee_cache_ttl: timedelta = timedelta(hours=1),
    ) -> None:
        self.http = http
        self.series_tickers = list(series_tickers)
        self._now = now
        self._balance_quantum = balance_quantum
        self._fee_ttl = fee_cache_ttl
        self._series_fees: dict[str, tuple[datetime, tuple[str | None, Any]]] = {}
        self._event_overrides: dict[str, tuple[datetime, tuple[str | None, Any]]] = {}
        self._event_series: dict[str, str] = {}

    # --- MarketDataSource -------------------------------------------------

    def list_markets(self) -> list[Market]:
        markets = []
        for series in self.series_tickers:
            for raw in self._paginate("/markets", {"series_ticker": series, "status": "open", "limit": 1000}):
                self._event_series.setdefault(raw["event_ticker"], series)
                markets.append(self._parse_market(raw))
        return markets

    def fetch_markets(self, market_ids: Sequence[str]) -> dict[str, Market]:
        return {ticker: self._parse_market(raw) for ticker, raw in self._fetch_raw_markets(market_ids).items()}

    def fetch_books(self, markets: Sequence[Market]) -> dict[str, OrderBook | InvalidOrderBook]:
        results: dict[str, OrderBook | InvalidOrderBook] = {}
        tickers = [m.market_id for m in markets]
        for chunk in _chunks(tickers, _BOOK_BATCH):
            data = self.http.get_json("/markets/orderbooks", {"tickers": chunk})
            received_at = self._now()
            for entry in data.get("orderbooks", []):
                ticker = entry.get("ticker")
                if ticker not in chunk:
                    continue
                try:
                    results[ticker] = parse_book(ticker, entry.get("orderbook_fp") or {}, received_at)
                except InvalidOrderBook as exc:
                    results[ticker] = exc
            for ticker in chunk:
                results.setdefault(ticker, InvalidOrderBook(f"{ticker}: missing from orderbooks response"))
        return results

    def fetch_resolutions(self, markets: Sequence[Market]) -> dict[str, Resolution]:
        raw_markets = self._fetch_raw_markets([m.market_id for m in markets])
        resolutions = {}
        for ticker, raw in raw_markets.items():
            resolution = parse_resolution(raw)
            if resolution is not None:
                resolutions[ticker] = resolution
        return resolutions

    # --- internals --------------------------------------------------------

    def _fetch_raw_markets(self, tickers: Sequence[str]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(list(tickers), _MARKET_BATCH):
            for raw in self._paginate("/markets", {"tickers": ",".join(chunk), "limit": 1000}):
                found[raw["ticker"]] = raw
        # Markets settled before the historical cutoff only exist in the archive.
        for ticker in tickers:
            if ticker in found:
                continue
            try:
                found[ticker] = self.http.get_json(f"/historical/markets/{ticker}")["market"]
            except (VenueHttpError, KeyError):
                log.warning("Kalshi market %s not found in live or historical data", ticker)
        return found

    def fetch_trades(self, ticker: str, since: datetime) -> list[PublicTrade]:
        """Public non-block trades in `ticker` at or after `since` (whole seconds), oldest first."""
        params = {"ticker": ticker, "min_ts": int(since.timestamp()), "limit": 1000}
        trades = [t for raw in self._paginate("/markets/trades", params, key="trades") if (t := parse_trade(raw))]
        return sorted(trades, key=lambda t: t.at)

    def fetch_recent_trades(
        self, tickers: Iterable[str], since: datetime, max_pages: int = TRADE_PAGES
    ) -> dict[str, list[PublicTrade]]:
        """Public non-block trades at or after `since` (whole seconds) in any of `tickers`, oldest first.

        One exchange-wide request per page instead of one per market: Kalshi limits anonymous
        requests per second, and per-market requests at 2-second maker ticks drew a stream of 429s
        (2026-10-08). The exchange prints about 100 trades a second, so a ~6 s window is ~1 page.
        Every page is read whatever the feed's order (the docs don't state it); trades are sorted here.
        """
        wanted = set(tickers)
        out: dict[str, list[PublicTrade]] = {t: [] for t in wanted}
        params: dict[str, Any] = {"min_ts": int(since.timestamp()), "limit": 1000}
        cursor = ""
        for _ in range(max_pages):
            data = self.http.get_json("/markets/trades", dict(params, cursor=cursor) if cursor else params)
            for raw in data.get("trades", []):
                if raw.get("ticker") in wanted and (t := parse_trade(raw)):
                    out[raw["ticker"]].append(t)
            cursor = data.get("cursor") or ""
            if not cursor:
                break
        else:
            log.warning("trades feed: stopped after %d pages; some trades since %s may be missing", max_pages, since)
        return {ticker: sorted(trades, key=lambda t: t.at) for ticker, trades in out.items()}

    def _paginate(self, path: str, params: dict[str, Any], key: str = "markets") -> Iterator[dict[str, Any]]:
        cursor = ""
        while True:
            page_params = dict(params, cursor=cursor) if cursor else params
            data = self.http.get_json(path, page_params)
            yield from data.get(key, [])
            cursor = data.get("cursor") or ""
            if not cursor:
                return

    def _parse_market(self, raw: dict[str, Any]) -> Market:
        ticker = raw["ticker"]
        event = raw.get("event_ticker", "")
        close = raw.get("close_time")
        return Market(
            venue=VENUE,
            market_id=ticker,
            title=raw.get("title", ""),
            yes_label=raw.get("yes_sub_title") or "Yes",
            status=parse_status(raw.get("status", "")),
            close_time=parse_utc(close) if close else None,
            fee_schedule=self._fee_schedule(event),
            contract_step=CONTRACT_STEP,
            min_order_size=CONTRACT_STEP,
            venue_meta={
                "event_ticker": event,
                "series_ticker": self._series_of(event) if event else "",
                "strike_type": raw.get("strike_type") or "",
                "floor_strike": "" if raw.get("floor_strike") is None else str(raw["floor_strike"]),
                "cap_strike": "" if raw.get("cap_strike") is None else str(raw["cap_strike"]),
            },
        )

    def _series_of(self, event_ticker: str) -> str:
        if event_ticker not in self._event_series:
            data = self.http.get_json(f"/events/{event_ticker}")
            self._event_series[event_ticker] = data["event"]["series_ticker"]
        return self._event_series[event_ticker]

    def _fee_schedule(self, event_ticker: str) -> FeeSchedule | None:
        if not event_ticker:
            return None
        fee_type, multiplier = self._cached(self._series_fees, self._series_of(event_ticker), self._load_series_fee)
        override_type, override_multiplier = self._cached(
            self._event_overrides, event_ticker, self._load_event_override
        )
        if override_type is not None:
            fee_type = override_type
        if override_multiplier is not None:
            multiplier = override_multiplier
        return fee_schedule_for(fee_type, multiplier, self._balance_quantum)

    def _cached(self, cache, key, loader):
        now = self._now()
        hit = cache.get(key)
        if hit is None or now - hit[0] > self._fee_ttl:
            hit = (now, loader(key))
            cache[key] = hit
        return hit[1]

    def _load_series_fee(self, series_ticker: str) -> tuple[str | None, Any]:
        series = self.http.get_json(f"/series/{series_ticker}")["series"]
        return series.get("fee_type"), series.get("fee_multiplier")

    def _load_event_override(self, event_ticker: str) -> tuple[str | None, Any]:
        """Latest override already in effect; (None, None) means use the series fees."""
        data = self.http.get_json("/events/fee_changes", {"event_ticker": event_ticker, "limit": 1000})
        now = self._now()
        effective = [
            change
            for change in data.get("event_fee_changes", [])
            if change.get("scheduled_ts") and parse_utc(change["scheduled_ts"]) <= now
        ]
        if not effective:
            return None, None
        latest = max(effective, key=lambda change: parse_utc(change["scheduled_ts"]))
        return latest.get("fee_type_override"), latest.get("fee_multiplier_override")


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
