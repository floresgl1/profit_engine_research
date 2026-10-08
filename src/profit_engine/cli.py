"""Command line entry point: `profit-engine ingest | score | status`."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from profit_engine.ingest import Pipeline, PipelineConfig, SkipMonitor
from profit_engine.models import MidpointBaseline
from profit_engine.paper import (
    EdgeStrategy,
    FillConfig,
    LiveBookProvider,
    PaperTrader,
    rebuild_portfolio,
)
from profit_engine.scoring import full_report
from profit_engine.storage import Store
from profit_engine.venues.base import MarketDataSource
from profit_engine.venues.http import ReadOnlyHttp
from profit_engine.venues.kalshi import BASE_URL as KALSHI_URL
from profit_engine.venues.kalshi import KalshiSource
from profit_engine.venues.polymarket import (
    CLOB_URL,
    DATA_URL,
    GAMMA_URL,
    PolymarketSource,
)
from profit_engine.weather import IemAsosClient

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
    maker_series = [s.strip() for s in (args.maker_series or "").split(",") if s.strip()]
    if maker_series:
        if Path(args.maker_db).resolve() == Path(args.db).resolve():
            print("--maker-db must be a different file from --db", file=sys.stderr)
            return 2
        start_maker_thread(args.maker_db, maker_series, args.maker_interval)
    store = Store(args.db)
    models = [MidpointBaseline()]
    for params_path in args.temperature_params or []:
        import json

        from profit_engine.models.kalshi_temperature import (
            KalshiHighTemperature,
            KalshiHighTemperatureLamp,
            TemperatureParams,
        )
        from profit_engine.weather import iem, lamp, nbm

        from profit_engine.weather.stations import CITIES

        raw = json.loads(Path(params_path).read_text())
        city = CITIES[raw.get("series", "KXHIGHNY")]
        iem_client = IemAsosClient(ReadOnlyHttp(iem.BASE_URL, timeout=60))
        if raw.get("model") == "lamp":
            lamp_client = lamp.LampClient(ReadOnlyHttp(lamp.BASE_URL, timeout=60), station=city.icao)
            models.append(KalshiHighTemperatureLamp.from_file(params_path, lamp_client, iem_client))
        else:
            nbm_client = nbm.NbmClient(ReadOnlyHttp(nbm.BASE_URL, timeout=60), station=city.icao)
            models.append(KalshiHighTemperature(TemperatureParams.load(params_path), nbm_client, iem_client, city=city))
    names = [m.name for m in models]
    if args.paper_trade and args.trade_model not in names:
        print(f"--trade-model must be one of {names}", file=sys.stderr)
        return 2

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
        trading_model=args.trade_model if strategy else None,
        config=PipelineConfig(poll_interval=timedelta(seconds=args.interval)),
    )
    try:
        pipeline.run_forever(cycles=args.cycles)
    except KeyboardInterrupt:
        logging.info("stopped")
    finally:
        store.close()
    return 0


MODEL_PARAMS = {"KXHIGHNY": "research/temperature_params_lamp_v3.json"}  # other cities: research/params/<series>.json


def maker_fair_price(series: list[str]):
    """The v3 temperature model's probability for a market, for the v2 quote veto.

    One model per series with fitted parameters, each with its own LAMP and
    IEM clients for the city's station. None (so v2 quotes like v1) when the
    model abstains, and outside the window research/maker_rules.md tested
    the veto in: 16:00 local the day before to 16:00 local on the day.
    """
    from datetime import datetime, time

    from profit_engine.core import utc_now
    from profit_engine.models.kalshi_temperature import KalshiHighTemperatureLamp
    from profit_engine.weather.kalshi_temps import event_day
    from profit_engine.weather import iem, lamp
    from profit_engine.weather.stations import CITIES

    models = []
    for name in series:
        path = Path(MODEL_PARAMS.get(name, f"research/params/{name}.json"))
        if name not in CITIES or not path.exists():
            logging.warning("no temperature model for %s: model_veto_v2 quotes like v1 there", name)
            continue
        lamp_client = lamp.LampClient(ReadOnlyHttp(lamp.BASE_URL, timeout=60), station=CITIES[name].icao)
        iem_client = IemAsosClient(ReadOnlyHttp(iem.BASE_URL, timeout=60))
        models.append(KalshiHighTemperatureLamp.from_file(path, lamp_client, iem_client, city=CITIES[name]))

    def fair(market, book):
        city = CITIES.get(market.venue_meta.get("series_ticker", ""))
        event = market.venue_meta.get("event_ticker", "")
        if city is None or not event:
            return None
        day = event_day(event)
        opens = datetime.combine(day - timedelta(days=1), time(16), tzinfo=city.zone)
        closes = datetime.combine(day, time(16), tzinfo=city.zone)
        if not opens <= utc_now() < closes:
            return None
        for model in models:
            p = model.predict(market, book)
            if p is not None:
                return p
        return None

    return fair


def run_maker(db: str, series: list[str], interval: float, cycles: int | None = None, size: str = "10",
              max_position: str = "50", maker_fee: str = "0.0175", model_margin: str = "0.20") -> None:
    """Run the paper market makers (v1 join-the-touch, v2 with the model veto) until `cycles` ticks or Ctrl-C.

    Both strategies see the same books and trades every tick. Opens its own
    database connection.
    """
    from profit_engine.maker.runner import MakerRunner, RunnerConfig, Strategy

    Path(db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(db)
    from dataclasses import replace

    from profit_engine.maker.strategies import LIVE, STRATEGIES

    overrides = {"size": Decimal(size), "max_position": Decimal(max_position), "maker_fee_rate": Decimal(maker_fee)}
    strategies = []
    for name in LIVE:
        quoter = replace(STRATEGIES[name].quoter, **overrides)
        if quoter.model_margin is not None:
            quoter = replace(quoter, model_margin=Decimal(model_margin))
        strategies.append(Strategy(name, quoter))
    runner = MakerRunner(
        KalshiSource(ReadOnlyHttp(KALSHI_URL), series),
        store,
        strategies,
        RunnerConfig(interval=timedelta(seconds=interval)),
        fair=maker_fair_price(series),
    )
    logging.info("paper market makers %s on %s (no orders are ever placed)", [s.name for s in strategies], ",".join(series))
    try:
        runner.run_forever(cycles=cycles)
    finally:
        store.close()


def start_maker_thread(db: str, series: list[str], interval: float) -> threading.Thread:
    """The maker beside the ingest loop, in one process (one always-on task).

    Its own thread, HTTP client and database file, so neither loop can block
    or lock the other; a daemon thread, so it stops with the process.
    """
    thread = threading.Thread(target=run_maker, args=(db, series, interval), name="maker", daemon=True)
    thread.start()
    return thread


def cmd_make(args: argparse.Namespace) -> int:
    series = [s.strip() for s in (args.kalshi_series or "").split(",") if s.strip()]
    if not series:
        print("pass --kalshi-series", file=sys.stderr)
        return 2
    try:
        run_maker(args.db, series, args.interval, args.cycles, args.size, args.max_position, args.maker_fee, args.model_margin)
    except KeyboardInterrupt:
        logging.info("stopped")
    return 0


def cmd_maker_report(args: argparse.Namespace) -> int:
    from datetime import date

    from profit_engine.core import midpoint
    from profit_engine.maker.report import render

    store = Store(args.db)
    try:
        resolutions = {r.market_id: r.yes_value for r in store.resolutions() if r.venue == "kalshi"}
        by_strategy: dict[str, list] = {}
        for strategy, _, fill in store.maker_fills():
            by_strategy.setdefault(strategy, []).append(fill)

        def mark(ticker: str):
            book = store.latest_book("kalshi", ticker)
            return midpoint(book) if book else None

        pair_from = date.fromisoformat(args.pair_from) if args.pair_from else None
        print(render(by_strategy, resolutions, mark, store.maker_runs(), pair_from=pair_from))
    finally:
        store.close()
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay strategies on the maker's recording and compare them (v1 baseline) on identical data."""
    from datetime import datetime, timezone

    from profit_engine.core import midpoint
    from profit_engine.maker.replay import breakdown, fidelity, render_breakdown, replay
    from profit_engine.maker.report import covered_from, render
    from profit_engine.maker.strategies import STRATEGIES

    names = [n.strip() for n in args.strategies.split(",") if n.strip()]
    unknown = [n for n in names if n not in STRATEGIES]
    if unknown:
        print(f"unknown strategies {unknown}; known: {sorted(STRATEGIES)}", file=sys.stderr)
        return 2
    if "join_touch_v1" not in names:
        names.insert(0, "join_touch_v1")  # the baseline every other strategy is paired against

    def day_start(text: str | None):
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc) if text else None

    store = Store(args.db)
    try:
        ticks = store.maker_ticks()
        if not ticks:
            print("Nothing recorded yet.")
            return 0
        start, end = day_start(args.start) or ticks[0], day_start(args.end) or ticks[-1] + timedelta(seconds=1)
        fills = replay(store, [STRATEGIES[n] for n in names], start, end)
        resolutions = {r.market_id: r.yes_value for r in store.resolutions() if r.venue == "kalshi"}

        def mark(ticker: str):
            book = store.latest_book("kalshi", ticker)
            return midpoint(book) if book else None

        print(f"replay {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC, {sum(1 for t in ticks if start <= t < end)} ticks")
        print(render(fills, resolutions, mark, {}, pair_from=covered_from(start)))
        if args.breakdown:
            for name in names:
                print(render_breakdown(name, breakdown(fills[name], resolutions)))
        live = [f for _, _, f in store.maker_fills("join_touch_v1")]
        rows = fidelity(live, fills["join_touch_v1"], start, end)
        if rows:
            print("[fidelity: join_touch_v1 contracts, live vs replay]")
            for r in rows:
                print(f"  {r.day}  live {r.live_contracts:.0f}  replay {r.replay_contracts:.0f}")
    finally:
        store.close()
    return 0


def cmd_compact_books(args: argparse.Namespace) -> int:
    """One-time: trim stored books to the top levels and give the space back. Stop the always-on task first."""
    import sqlite3

    def size(path: str) -> int:
        return sum(Path(path + suffix).stat().st_size for suffix in ("", "-wal") if Path(path + suffix).exists())

    before = size(args.db)
    store = Store(args.db)
    try:
        changed = store.compact_books(args.levels)
        print(f"{args.db}: trimmed {changed} stored books to {args.levels} levels per side; vacuuming...")
        store.vacuum()
    except sqlite3.OperationalError as exc:
        print(f"{args.db}: {exc}. Stop the always-on task (it holds the database) and run this again.", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"{args.db}: {before / 1e6:.0f} MB -> {size(args.db) / 1e6:.0f} MB")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        series = [s.strip() for s in (args.series or "").split(",") if s.strip()]
        if series:
            print(f"series: {', '.join(series)}")
        print(full_report(store.scored_predictions(series=series), store.models(), args.buckets))
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
    p.add_argument("--log-file", help="also write the log here (rotated at 5 MB, 3 backups), e.g. data/engine.log")
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
    ing.add_argument(
        "--temperature-params",
        action="append",
        help="add a KXHIGHNY temperature model from a fitted parameter file; repeatable "
        "(research/temperature_params.json = v1 NBM, research/temperature_params_lamp.json = v2 LAMP)",
    )
    ing.add_argument("--trade-model", default="midpoint", help="which model's predictions drive paper trades")
    ing.add_argument("--maker-series", help="also run the paper market maker on these Kalshi series, in this process")
    ing.add_argument("--maker-db", default="data/maker.db", help="paper market maker database (default data/maker.db)")
    ing.add_argument("--maker-interval", type=float, default=10, help="paper market maker seconds between ticks (default 10)")
    ing.set_defaults(func=cmd_ingest)

    score = sub.add_parser("score", help="Brier scores and calibration, model vs market")
    score.add_argument("--buckets", type=int, default=10)
    score.add_argument("--series", help="only score these comma-separated Kalshi series, e.g. KXHIGHCHI or KXHIGHNY,KXHIGHPHIL")
    score.set_defaults(func=cmd_score)

    make = sub.add_parser("make", help="paper market makers v1 and v2: pretend quotes at the touch, filled from public trades")
    make.add_argument("--kalshi-series", help="comma-separated Kalshi series, e.g. KXHIGHNY")
    make.add_argument("--interval", type=float, default=10, help="seconds between polls (default 10)")
    make.add_argument("--cycles", type=int, default=None, help="stop after N ticks (default: run until Ctrl-C)")
    make.add_argument("--size", default="10", help="contracts per quote (default 10)")
    make.add_argument("--max-position", default="50", help="max |position| per market (default 50)")
    make.add_argument("--maker-fee", default="0.0175", help="maker fee rate x P x (1-P) (default 0.0175, conservative)")
    make.add_argument("--model-margin", default="0.20", help="v2: no quote the model puts more than this against us (default 0.20)")
    make.set_defaults(func=cmd_make)

    mrep = sub.add_parser("maker-report", help="paper market-maker results by event day, with intervals and v2 - v1")
    mrep.add_argument("--pair-from", help="first event day (YYYY-MM-DD) for the paired comparison (default: when both ran)")
    mrep.set_defaults(func=cmd_maker_report)

    rep = sub.add_parser("replay", help="replay maker strategies on the recorded data, paired against v1")
    rep.add_argument("--strategies", default="join_touch_v1,model_veto_v2,skew_size_v3a,skew_back_v3b",
                     help="comma-separated names from profit_engine.maker.strategies")
    rep.add_argument("--start", help="first UTC date to replay (YYYY-MM-DD; default: first recorded tick)")
    rep.add_argument("--end", help="UTC date to stop before (YYYY-MM-DD; default: after the last tick)")
    rep.add_argument("--breakdown", action="store_true",
                     help="also split settled PnL by session and by queue vs swept fills")
    rep.set_defaults(func=cmd_replay)

    compact = sub.add_parser("compact-books", help="one-time: trim stored books to the top levels and reclaim space")
    compact.add_argument("--levels", type=int, default=5, help="price levels kept per side (default 5)")
    compact.set_defaults(func=cmd_compact_books)

    status = sub.add_parser("status", help="what is in the database")
    status.add_argument("--cash", default="1000", help="starting paper cash used when trading")
    status.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        from logging.handlers import RotatingFileHandler

        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(args.log_file, maxBytes=5_000_000, backupCount=3))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.INFO if args.verbose else logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
