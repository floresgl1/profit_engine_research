"""Polymarket adapter: public market data only. No wallet, no signing, no CLOB auth.

Endpoints (docs.polymarket.com, checked 2026-10-03):
  Gamma  GET /markets/keyset         market metadata, fee schedule, token ids
  CLOB   GET /book?token_id=         one outcome token's book (bids and asks)
  Data   GET /v2/resolutions?condition=a,b   resolution status and payout vector

A binary market has two outcome tokens whose books mirror each other
(token 1 levels are 1 - token 0 levels; verified live), so the first
outcome's book is the whole YES book.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
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
from profit_engine.venues.http import ReadOnlyHttp, VenueHttpError

log = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
DATA_URL = "https://data-api.polymarket.com"
VENUE = "polymarket"

FEE_QUANTUM = Decimal("0.00001")  # fees rounded to 5 dp
CONTRACT_STEP = Decimal("0.01")
_PAGE = 100  # Gamma keyset maximum
_ID_BATCH = 50


def fee_schedule_from(raw: dict[str, Any]) -> FeeSchedule | None:
    """Taker fee from Gamma's `feeSchedule`. None when it cannot be modeled."""
    schedule = raw.get("feeSchedule")
    if not schedule:
        if raw.get("feesEnabled") is False:
            return FeeSchedule(Decimal(0), FEE_QUANTUM, ROUND_HALF_UP)
        return None
    if Decimal(str(schedule.get("exponent", 1))) != 1:
        # The docs do not give the exact formula for other exponents; don't guess.
        return None
    return FeeSchedule(Decimal(str(schedule["rate"])), FEE_QUANTUM, ROUND_HALF_UP)


def parse_status(raw: dict[str, Any]) -> MarketStatus:
    if raw.get("closed"):
        return MarketStatus.RESOLVED if raw.get("umaResolutionStatus") == "resolved" else MarketStatus.CLOSED
    if raw.get("archived") or not raw.get("enableOrderBook", True):
        return MarketStatus.CLOSED
    if raw.get("active") and raw.get("acceptingOrders"):
        return MarketStatus.OPEN
    if raw.get("active"):
        return MarketStatus.PAUSED
    return MarketStatus.UPCOMING


def parse_market(raw: dict[str, Any]) -> Market | None:
    """Gamma market -> Market, or None if it is not a two-outcome order-book market."""
    try:
        outcomes = json.loads(raw.get("outcomes") or "[]")
        tokens = json.loads(raw.get("clobTokenIds") or "[]")
    except json.JSONDecodeError:
        return None
    if len(outcomes) != 2 or len(tokens) != 2 or not raw.get("conditionId"):
        return None
    end = raw.get("endDate")
    min_size = raw.get("orderMinSize")
    return Market(
        venue=VENUE,
        market_id=raw["conditionId"],
        title=raw.get("question", ""),
        yes_label=str(outcomes[0]),
        status=parse_status(raw),
        close_time=parse_utc(end) if end else None,
        fee_schedule=fee_schedule_from(raw),
        contract_step=CONTRACT_STEP,
        min_order_size=Decimal(str(min_size)) if min_size else CONTRACT_STEP,
        venue_meta={
            "yes_token_id": str(tokens[0]),
            "no_token_id": str(tokens[1]),
            "gamma_id": str(raw.get("id", "")),
            "slug": raw.get("slug", ""),
            "neg_risk": str(bool(raw.get("negRisk"))).lower(),
        },
    )


def parse_book(market_id: str, raw: dict[str, Any], received_at: datetime) -> OrderBook:
    """CLOB book for the YES token -> OrderBook. Sorted explicitly; zero sizes dropped."""
    try:
        bids = [(Decimal(lvl["price"]), Decimal(lvl["size"])) for lvl in raw.get("bids") or []]
        asks = [(Decimal(lvl["price"]), Decimal(lvl["size"])) for lvl in raw.get("asks") or []]
    except (KeyError, ValueError, ArithmeticError, TypeError) as exc:
        raise InvalidOrderBook(f"{market_id}: unparseable level: {exc}") from exc
    return OrderBook(
        VENUE,
        market_id,
        tuple(Level(p, s) for p, s in sorted(bids, key=lambda lvl: -lvl[0]) if s != 0),
        tuple(Level(p, s) for p, s in sorted(asks, key=lambda lvl: lvl[0]) if s != 0),
        received_at,
    )


def parse_resolution(raw: dict[str, Any]) -> Resolution | None:
    """Data API resolution row -> Resolution. YES value = first payout / total payout."""
    if raw.get("status") != "resolved":
        return None
    payouts = [Decimal(str(p)) for p in raw.get("payouts") or []]
    total = sum(payouts, Decimal(0))
    if len(payouts) != 2 or total <= 0 or not raw.get("resolved_at"):
        log.warning("resolved Polymarket market %s has unusable payouts %r", raw.get("condition_id"), payouts)
        return None
    return Resolution(VENUE, raw["condition_id"], payouts[0] / total, parse_utc(raw["resolved_at"]))


class PolymarketSource:
    venue = VENUE

    def __init__(
        self,
        gamma: ReadOnlyHttp,
        clob: ReadOnlyHttp,
        data: ReadOnlyHttp,
        *,
        top_n: int = 50,
        tag_id: int | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.gamma, self.clob, self.data = gamma, clob, data
        self.top_n = top_n
        self.tag_id = tag_id
        self._now = now

    def list_markets(self) -> list[Market]:
        """Top `top_n` open markets by 24h volume (optionally within one tag)."""
        params: dict[str, Any] = {"closed": "false", "order": "volume24hr", "ascending": "false"}
        if self.tag_id is not None:
            params["tag_id"] = str(self.tag_id)
        markets: list[Market] = []
        for raw in self._keyset(params):
            market = parse_market(raw)
            if market is not None and market.status is MarketStatus.OPEN:
                markets.append(market)
                if len(markets) >= self.top_n:
                    break
        return markets

    def fetch_markets(self, market_ids: Sequence[str]) -> dict[str, Market]:
        found: dict[str, Market] = {}
        for chunk in _chunks(list(market_ids), _ID_BATCH):
            # Gamma hides closed markets unless asked, so query both states.
            for closed in ("false", "true"):
                for raw in self._keyset({"condition_ids": chunk, "closed": closed}):
                    market = parse_market(raw)
                    if market is not None:
                        found[market.market_id] = market
        return found

    def fetch_books(self, markets: Sequence[Market]) -> dict[str, OrderBook | InvalidOrderBook]:
        results: dict[str, OrderBook | InvalidOrderBook] = {}
        for market in markets:
            token = market.venue_meta.get("yes_token_id")
            if not token:
                results[market.market_id] = InvalidOrderBook(f"{market.market_id}: no YES token id")
                continue
            try:
                raw = self.clob.get_json("/book", {"token_id": token})
            except VenueHttpError as exc:
                if exc.status == 404:  # venue says there is no book for this token
                    results[market.market_id] = InvalidOrderBook(f"{market.market_id}: no order book (404)")
                    continue
                raise
            try:
                results[market.market_id] = parse_book(market.market_id, raw, self._now())
            except InvalidOrderBook as exc:
                results[market.market_id] = exc
        return results

    def fetch_resolutions(self, markets: Sequence[Market]) -> dict[str, Resolution]:
        resolutions: dict[str, Resolution] = {}
        for chunk in _chunks([m.market_id for m in markets], _ID_BATCH):
            rows = self.data.get_json("/v2/resolutions", {"condition": ",".join(chunk)}).get("data") or []
            for row in rows:
                resolution = parse_resolution(row)
                if resolution is not None and resolution.market_id in chunk:
                    resolutions[resolution.market_id] = resolution
        return resolutions

    def _keyset(self, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            page_params = dict(params, limit=str(_PAGE))
            if cursor:
                page_params["after_cursor"] = cursor
            data = self.gamma.get_json("/markets/keyset", page_params)
            yield from data.get("markets") or []
            cursor = data.get("next_cursor")
            if not cursor or not data.get("markets"):
                return


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
