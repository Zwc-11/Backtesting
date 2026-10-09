"""``xasset lab``: ingest flow bars, register books, run discovery and the holdout."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
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

    register = commands.add_parser("register", help="Freeze a book and its interval")
    register.add_argument("book", type=Path)
    register.add_argument("--start", type=parse_time, required=True)
    register.add_argument("--end", type=parse_time, required=True)

    run = commands.add_parser("run", help="Replay a registered book (discovery or holdout)")
    run.add_argument("book", type=Path)
    run.add_argument("--phase", choices=["discovery", "holdout"], default="discovery")

    holdout = commands.add_parser("holdout-open", help="Open the sealed holdout once")
    holdout.add_argument("book_id")
    holdout.add_argument("--reason", required=True)

    commands.add_parser("catalog", help="List every strategy, its data needs and mode")

    for command in (ingest, register, run, holdout):
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
    if args.lab_command == "register":
        print(
            json.dumps(research.register(args.data_dir, args.book, args.start, args.end), indent=2)
        )
        return 0
    if args.lab_command == "holdout-open":
        print(json.dumps(research.open_holdout(args.data_dir, args.book_id, args.reason), indent=2))
        return 0
    result = research.run(args.data_dir, args.book, args.phase)
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
