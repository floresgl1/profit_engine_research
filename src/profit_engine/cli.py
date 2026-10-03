"""Command line entry point: `profit-engine ingest | score | status`."""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from profit_engine.ingest import Pipeline, PipelineConfig, SkipMonitor
from profit_engine.models import MidpointBaseline
from profit_engine.paper import EdgeStrategy, FillConfig, LiveBookProvider, PaperTrader, rebuild_portfolio
from profit_engine.scoring import full_report
from profit_engine.storage import Store
from profit_engine.venues.base import MarketDataSource
from profit_engine.venues.http import ReadOnlyHttp
from profit_engine.venues.kalshi import BASE_URL as KALSHI_URL
from profit_engine.venues.kalshi import KalshiSource
from profit_engine.venues.polymarket import CLOB_URL, DATA_URL, GAMMA_URL, PolymarketSource

DEFAULT_DB = Path("data/research.db")


def build_sources(args: argparse.Namespace) -> dict[str, MarketDataSource]:
    sources: dict[str, MarketDataSource] = {}
    series = [s.strip() for s in (args.kalshi_series or "").split(",") if s.strip()]
    if series:
        sources["kalshi"] = KalshiSource(ReadOnlyHttp(KALSHI_URL), series)
    if args.polymarket_top > 0:
        sources["polymarket"] = PolymarketSource(
            ReadOnlyHttp(GAMMA_URL),
            ReadOnlyHttp(CLOB_URL),
            ReadOnlyHttp(DATA_URL),
            top_n=args.polymarket_top,
            tag_id=args.polymarket_tag,
        )
    return sources


def cmd_ingest(args: argparse.Namespace) -> int:
    sources = build_sources(args)
    if not sources:
        print("Nothing to ingest: pass --kalshi-series and/or --polymarket-top.", file=sys.stderr)
        return 2
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(args.db)
    models = [MidpointBaseline()]

    strategy = trader = None
    if args.paper_trade:
        fill_config = FillConfig(
            latency=timedelta(milliseconds=args.latency_ms),
            max_depth_fraction=Decimal(args.depth_fraction),
        )
        portfolio = rebuild_portfolio(Decimal(args.cash), store.fills(), store.resolutions())
        trader = PaperTrader(LiveBookProvider(sources), portfolio, {venue: fill_config for venue in sources})
        strategy = EdgeStrategy(min_edge=Decimal(args.min_edge), order_size=Decimal(args.order_size))
        logging.info("paper trading with cash %s (rebuilt from %d stored fills)", portfolio.cash, len(store.fills()))

    pipeline = Pipeline(
        store,
        sources,
        models,
        monitor=SkipMonitor(threshold=args.skip_alert),
        strategy=strategy,
        trader=trader,
        trading_model=models[0].name if strategy else None,
        config=PipelineConfig(poll_interval=timedelta(seconds=args.interval)),
    )
    try:
        pipeline.run_forever(cycles=args.cycles)
    except KeyboardInterrupt:
        logging.info("stopped")
    finally:
        store.close()
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        print(full_report(store.scored_predictions(), store.models(), args.buckets))
    finally:
        store.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        markets = store.markets()
        counts = Counter((m.venue, m.status.value) for m in markets)
        print("markets:")
        for (venue, status), n in sorted(counts.items()):
            print(f"  {venue:<11} {status:<9} {n}")
        snaps, states = store.snapshot_count()
        print(f"book snapshots: {snaps} ({states} distinct states)")
        print(f"resolutions: {len(store.resolutions())}")
        print(f"resolved predictions: {len(store.scored_predictions())}")
        fills = store.fills()
        if fills:
            portfolio = rebuild_portfolio(Decimal(args.cash), fills, store.resolutions())
            by_status = Counter(f.status.value for f in fills)
            print(f"paper orders: {len(fills)} {dict(by_status)}")
            print(f"paper cash: {portfolio.cash}  realized pnl: {portfolio.realized_pnl}  open positions: {len(portfolio.positions)}")
    finally:
        store.close()
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="profit-engine", description="Read-only prediction market research engine.")
    p.add_argument("--db", default=str(DEFAULT_DB), help=f"SQLite file (default {DEFAULT_DB})")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="poll venues, store books, log predictions, paper trade")
    ing.add_argument("--kalshi-series", help="comma-separated Kalshi series tickers, e.g. KXHIGHNY,KXHIGHCHI")
    ing.add_argument("--polymarket-top", type=int, default=0, help="track the top N open Polymarket markets by 24h volume")
    ing.add_argument("--polymarket-tag", type=int, default=None, help="restrict Polymarket markets to one tag id")
    ing.add_argument("--interval", type=float, default=30, help="seconds between polls (default 30)")
    ing.add_argument("--cycles", type=int, default=None, help="stop after N cycles (default: run until Ctrl-C)")
    ing.add_argument("--skip-alert", type=float, default=0.2, help="alert above this skip rate (default 0.2)")
    ing.add_argument("--paper-trade", action="store_true", help="run the placeholder EdgeStrategy against paper fills")
    ing.add_argument("--cash", default="1000", help="starting paper cash (default 1000)")
    ing.add_argument("--latency-ms", type=float, default=250, help="decision-to-fill latency (default 250)")
    ing.add_argument("--depth-fraction", default="0.25", help="max order size as a fraction of visible depth")
    ing.add_argument("--min-edge", default="0.03", help="EdgeStrategy minimum edge after fees")
    ing.add_argument("--order-size", default="10", help="EdgeStrategy contracts per order")
    ing.set_defaults(func=cmd_ingest)

    score = sub.add_parser("score", help="Brier scores and calibration, model vs market")
    score.add_argument("--buckets", type=int, default=10)
    score.set_defaults(func=cmd_score)

    status = sub.add_parser("status", help="what is in the database")
    status.add_argument("--cash", default="1000", help="starting paper cash used when trading")
    status.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.INFO if args.verbose else logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
