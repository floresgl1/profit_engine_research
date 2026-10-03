"""Polymarket read-only adapter (public Gamma, CLOB and Data APIs; no wallet)."""

from profit_engine.venues.polymarket.adapter import (
    CLOB_URL,
    DATA_URL,
    GAMMA_URL,
    PolymarketSource,
    fee_schedule_from,
    parse_book,
    parse_market,
    parse_resolution,
)

__all__ = [
    "CLOB_URL",
    "DATA_URL",
    "GAMMA_URL",
    "PolymarketSource",
    "fee_schedule_from",
    "parse_book",
    "parse_market",
    "parse_resolution",
]
