"""``xasset lab``: ingest flow bars, register books, run discovery and the holdout."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def add_parser(parent: argparse._SubParsersAction[Any]) -> None:
    parser = parent.add_parser("lab", help="Strategy lab: replay books and run the paper desk")
    commands = parser.add_subparsers(dest="lab_command", required=True)
    default_root = Path(os.getenv("XASSET_DATA_DIR", "data"))

    ingest = commands.add_parser("ingest", help="Download Binance 1m klines with taker flow")
    ingest.add_argument("--market", choices=["spot", "um"], required=True)
    ingest.add_argument("--symbols", nargs="+", required=True, help="Binance symbols, e.g. BTCUSDT")
    ingest.add_argument("--start", type=parse_time, required=True)
    ingest.add_argument("--end", type=parse_time, required=True)
    ingest.add_argument("--funding", action="store_true", help="Also fetch USD-M funding")

    every = commands.add_parser(
        "ingest-universe", help="Download every Binance archive a universe file needs"
    )
    every.add_argument("universe", type=Path)
    every.add_argument("--start", type=parse_time, required=True)
    every.add_argument("--end", type=parse_time, required=True)

    every.add_argument("--workers", type=int, default=6, help="Parallel downloads")

    perp = commands.add_parser(
        "perp-universe", help="Rank Binance USD-M perpetuals month by month into a universe file"
    )
    perp.add_argument("--start", type=parse_time, required=True, help="First member month")
    perp.add_argument("--end", type=parse_time, required=True, help="Last member month")
    perp.add_argument("--top", type=int, default=50)
    perp.add_argument("--out", type=Path, required=True)
    perp.add_argument("--skip-download", action="store_true", help="Use cached daily archives")

    us = commands.add_parser(
        "us-universe", help="Rank large US stocks month by month (Alpaca daily bars)"
    )
    us.add_argument("--start", type=parse_time, required=True, help="First member month")
    us.add_argument("--end", type=parse_time, required=True, help="Last member month")
    us.add_argument("--top", type=int, default=50)
    us.add_argument("--out", type=Path, required=True)

    register = commands.add_parser("register", help="Freeze a book and its interval")
    register.add_argument("book", type=Path)
    register.add_argument("--start", type=parse_time, required=True)
    register.add_argument("--end", type=parse_time, required=True)

    run = commands.add_parser("run", help="Replay a registered book (discovery or holdout)")
    run.add_argument("book", type=Path)
    run.add_argument("--phase", choices=["discovery", "holdout"], default="discovery")
    run.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel scenario replays (default: cores, max 3)",
    )

    holdout = commands.add_parser("holdout-open", help="Open the sealed holdout once")
    holdout.add_argument("book_id")
    holdout.add_argument("--reason", required=True)

    commands.add_parser("catalog", help="List every strategy, its data needs and mode")

    for command in (ingest, every, perp, us, register, run, holdout):
        command.add_argument("--data-dir", type=Path, default=default_root)


def main(args: argparse.Namespace) -> int:
    from xasset.lab import research
    from xasset.lab.catalog import catalog
    from xasset.lab.ingest import ingest_funding, ingest_klines

    if args.lab_command == "catalog":
        print(json.dumps(catalog(), indent=2))
        return 0
    if args.lab_command == "ingest":
        reports = []
        with httpx.Client(timeout=120, headers={"User-Agent": "xasset/0.1 lab"}) as client:
            for symbol in args.symbols:
                report = ingest_klines(
                    client, args.data_dir, args.market, symbol, args.start, args.end
                )
                if args.funding and args.market == "um":
                    funding = ingest_funding(client, args.data_dir, symbol, args.start, args.end)
                    report["funding_events"] = funding.height
                reports.append(report)
        print(json.dumps(reports, indent=2))
        return 0
    if args.lab_command == "ingest-universe":
        return ingest_universe(args)
    if args.lab_command == "perp-universe":
        return perp_universe(args)
    if args.lab_command == "us-universe":
        return us_universe(args)
    if args.lab_command == "register":
        print(
            json.dumps(research.register(args.data_dir, args.book, args.start, args.end), indent=2)
        )
        return 0
    if args.lab_command == "holdout-open":
        print(json.dumps(research.open_holdout(args.data_dir, args.book_id, args.reason), indent=2))
        return 0
    result = research.run(args.data_dir, args.book, args.phase, args.workers)
    print(
        json.dumps(
            {
                "id": result["id"],
                "status": result["status"],
                "error": result.get("error"),
                "strategies": {
                    sid: data["summary"]
                    for sid, data in result.get("scenarios", {})
                    .get("base", {})
                    .get("strategies", {})
                    .items()
                },
            },
            indent=2,
            default=str,
        )
    )
    return 0 if result["status"] == "completed" else 1


def ingest_universe(args: argparse.Namespace) -> int:
    """Download every archive a universe needs; with membership, only the needed months."""
    from concurrent.futures import ThreadPoolExecutor

    from xasset.lab.ingest import Market, ingest_funding, ingest_klines, month_starts
    from xasset.lab.universe import load_universe

    universe = load_universe(args.universe)
    markets: dict[str, Market] = {"binance-spot": "spot", "binance-um": "um"}
    begin = args.start
    if universe.membership:  # two warm-up months before the first member month
        first = min(universe.membership)
        year, number = (int(part) for part in first.split("-"))
        index = year * 12 + number - 3
        begin = min(begin, datetime(index // 12, index % 12 + 1, 1, tzinfo=UTC))
    jobs: list[tuple[str, Market | str, str, set[str] | None]] = []
    for item in universe.instruments:
        market: Market | str | None = markets.get(item.history)
        if item.history == "alpaca-sip":
            market = "alpaca"
        if market is None:
            continue
        needed: set[str] | None = None
        if universe.membership:
            needed = {
                f"{m:%Y-%m}"
                for m in month_starts(begin, args.end)
                if item.id in (universe.loaded(f"{m:%Y-%m}") or ())
            }
            if not needed:
                continue
        jobs.append((item.id, market, item.archive_symbol, needed))
    keep_raw = not universe.membership  # bulk point-in-time downloads keep checksums only
    done = 0

    def fetch(job: tuple[str, Market | str, str, set[str] | None]) -> dict[str, object]:
        nonlocal done
        name, market, symbol, needed = job
        with httpx.Client(timeout=120, headers={"User-Agent": "xasset/0.1 lab"}) as client:
            if market == "alpaca":
                from xasset.lab.us import ingest_minutes

                months = needed or {f"{m:%Y-%m}" for m in month_starts(begin, args.end)}
                report = ingest_minutes(client, args.data_dir, symbol, months)
                done += 1
                print(f"[{done}/{len(jobs)}] {name}: alpaca SIP", flush=True)
                return report
            binance: Market = "spot" if market == "spot" else "um"
            report = ingest_klines(
                client, args.data_dir, binance, symbol, begin, args.end, needed, keep_raw
            )
            if market == "um":
                funding = ingest_funding(client, args.data_dir, symbol, begin, args.end, needed)
                report["funding_events"] = funding.height
        done += 1
        print(f"[{done}/{len(jobs)}] {name}: {market} {symbol}", flush=True)
        return report

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        reports = list(pool.map(fetch, jobs))
    print(json.dumps({"instruments": len(reports)}, indent=2))
    return 0


def perp_universe(args: argparse.Namespace) -> int:
    import yaml

    from xasset.lab.perps import (
        fetch_all_daily,
        load_daily,
        monthly_membership,
        perpetual_symbols,
        universe_document,
    )
    from xasset.lab.universe import Universe

    first = args.start.date().replace(day=1)
    last = args.end.date().replace(day=1)
    if not args.skip_download:
        with httpx.Client(timeout=120, headers={"User-Agent": "xasset/0.1 lab"}) as client:
            symbols = perpetual_symbols(client)
            prior = (args.start - timedelta(days=1)).date().replace(day=1)
            fetch_all_daily(client, args.data_dir, symbols, prior, last)
    daily = load_daily(args.data_dir)
    membership = monthly_membership(daily, first, last, args.top)
    document = universe_document(daily, membership, args.top)
    Universe.model_validate(document)  # refuse to write an invalid universe
    args.out.write_text(yaml.safe_dump(document, sort_keys=False, width=100))
    members = {s for ranked in membership.values() for s, _ in ranked}
    print(
        json.dumps({"months": len(membership), "instruments": len(members), "out": str(args.out)})
    )
    return 0


def us_universe(args: argparse.Namespace) -> int:
    import yaml

    from xasset.lab.universe import Universe
    from xasset.lab.us import CANDIDATES, ETFS, fetch_daily, monthly_members, universe_document

    first = args.start.date().replace(day=1)
    last = args.end.date().replace(day=1)
    prior = (args.start - timedelta(days=1)).date().replace(day=1)
    finish = (args.end + timedelta(days=32)).date().replace(day=1)
    with httpx.Client(timeout=120, headers={"User-Agent": "xasset/0.1 lab"}) as client:
        for count, symbol in enumerate([*ETFS, *CANDIDATES], start=1):
            rows = fetch_daily(client, args.data_dir, symbol, prior, finish)
            print(f"[{count}/{len(ETFS) + len(CANDIDATES)}] {symbol}: {rows} days", flush=True)
    membership = monthly_members(args.data_dir, first, last, args.top)
    document = universe_document(membership, args.top)
    Universe.model_validate(document)
    args.out.write_text(yaml.safe_dump(document, sort_keys=False, width=100))
    print(json.dumps({"months": len(membership), "out": str(args.out)}))
    return 0
