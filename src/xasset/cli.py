import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import httpx
import polars as pl

from xasset.config import load_universe
from xasset.ingest.history import ingest
from xasset.lab import cli as lab_cli
from xasset.normalize.resample import resample
from xasset.normalize.timebase import utc
from xasset.notebook import cli as study_cli
from xasset.qc.audit import audit_bars
from xasset.qc.checks import check_bars
from xasset.qc.reconcile import reconcile
from xasset.qc.report import health
from xasset.recorder import record
from xasset.research import cli as research_cli
from xasset.store.catalog import rebuild_catalog
from xasset.store.writer import atomic_path, load_bars, write_json, writer_lock


def parse_time(value: str) -> datetime:
    try:
        return utc(datetime.fromisoformat(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Use an ISO timestamp with timezone, e.g. 2026-10-07T22:00Z"
        ) from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="xasset", description="Cross-asset data recorder and QC")
    subparsers = result.add_subparsers(dest="command", required=True)
    research_cli.add_parser(subparsers)
    lab_cli.add_parser(subparsers)
    study_cli.add_parser(subparsers)
    for name, help_text in (
        ("record", "Record recent completed Yahoo 1-minute bars"),
        ("qc", "Validate stored bar structure; exits nonzero for missing/invalid data"),
        ("health", "Check the last five completed regular sessions"),
        ("catalog", "Rebuild and display DuckDB coverage from Parquet"),
        ("ingest", "Backfill Binance, Dukascopy, or Alpaca history"),
        ("audit", "Report price jumps and zero-volume runs without repairing data"),
        ("reconcile", "Compare separately stored observations from two providers"),
        ("build", "Build complete session-aware 5m, 1h, or 1d bars"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "--data-dir", type=Path, default=Path(os.getenv("XASSET_DATA_DIR", "data"))
        )
        if name != "catalog":
            default_universe = "config/history.yaml" if name == "ingest" else "config/universe.yaml"
            sub.add_argument("--universe", type=Path, default=Path(default_universe))
            sub.add_argument("--symbols", nargs="+", help="Internal IDs from the universe")
        if name == "record":
            sub.add_argument("--days", type=int, choices=range(1, 31), default=7, metavar="1..30")
            sub.add_argument(
                "--end", type=parse_time, help="End of request; defaults to now in UTC"
            )
        if name == "health":
            sub.add_argument("--sessions", type=int, default=5)
            sub.add_argument(
                "--as-of",
                type=parse_time,
                help="Defaults to now minus the 20-minute settlement delay",
            )
            sub.add_argument("--output", type=Path, help="Also atomically save the JSON report")
        if name == "ingest":
            sub.add_argument("--source", choices=["binance", "dukascopy", "alpaca"], required=True)
            sub.add_argument("--start", type=parse_time, required=True)
            sub.add_argument("--end", type=parse_time, required=True)
        if name in {"audit", "reconcile"}:
            sub.add_argument("--output", type=Path)
        if name == "audit":
            sub.add_argument("--jump-threshold", type=float, default=0.15)
        if name == "reconcile":
            sub.add_argument(
                "--left", choices=["yahoo", "binance", "dukascopy", "alpaca"], required=True
            )
            sub.add_argument(
                "--right", choices=["yahoo", "binance", "dukascopy", "alpaca"], required=True
            )
            sub.add_argument("--relative-tolerance", type=float, default=0.005)
            sub.add_argument("--minimum-overlap", type=float, default=0.95)
            sub.add_argument("--start", type=parse_time, required=True)
            sub.add_argument("--end", type=parse_time, required=True)
        if name == "build":
            sub.add_argument("--freq", choices=["5m", "1h", "1d"], required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "research":
        return research_cli.main(args)
    if args.command == "lab":
        return lab_cli.main(args)
    if args.command == "study":
        return study_cli.main(args)
    try:
        report: dict[str, Any]
        if args.command == "catalog":
            with writer_lock(args.data_dir):
                report = {"coverage": rebuild_catalog(args.data_dir)}
            ok = True
        else:
            instruments = load_universe(args.universe, args.symbols)
            if args.command == "ingest":
                if args.symbols is None:
                    instruments = [item for item in instruments if args.source in item.sources]
                with httpx.Client(
                    timeout=45, headers={"User-Agent": "xasset/0.1 market-data-research"}
                ) as client:
                    report = ingest(
                        args.data_dir, instruments, args.source, args.start, args.end, client=client
                    )
                ok = report["status"] == "ok"
            elif args.command == "record":
                now = datetime.now(UTC)
                end = args.end or now
                with httpx.Client(
                    timeout=30, headers={"User-Agent": "xasset/0.1 market-data-research"}
                ) as client:
                    report = record(
                        args.data_dir,
                        instruments,
                        end - timedelta(days=args.days),
                        end,
                        client=client,
                        as_of=now,
                    )
                ok = report["status"] == "ok"
            elif args.command == "health":
                report = health(
                    args.data_dir,
                    instruments,
                    args.as_of or datetime.now(UTC) - timedelta(minutes=20),
                    args.sessions,
                )
                if args.output:
                    write_json(args.output, report)
                ok = report["ok"]
            elif args.command == "audit":
                reports = [
                    {
                        "symbol": item.id,
                        **audit_bars(
                            load_bars(args.data_dir, item), jump_threshold=args.jump_threshold
                        ),
                    }
                    for item in instruments
                ]
                ok = bool(reports) and all(item["ok"] for item in reports)
                report = {"ok": ok, "instruments": reports}
                if args.output:
                    write_json(args.output, report)
            elif args.command == "reconcile":
                if args.start >= args.end:
                    raise ValueError("Reconciliation interval must be positive")
                comparisons = []
                for item in instruments:
                    left = load_bars(args.data_dir, item, args.left)
                    right = load_bars(args.data_dir, item, args.right)
                    interval = (pl.col("ts_end") > args.start) & (pl.col("ts_end") <= args.end)
                    comparisons.append(
                        {
                            "symbol": item.id,
                            **reconcile(
                                left.filter(interval),
                                right.filter(interval),
                                relative_tolerance=args.relative_tolerance,
                                minimum_overlap=args.minimum_overlap,
                            ),
                        }
                    )
                ok = bool(comparisons) and all(item["ok"] for item in comparisons)
                report = {"ok": ok, "instruments": comparisons}
                if args.output:
                    write_json(args.output, report)
            elif args.command == "build":
                outputs: list[dict[str, Any]] = []
                with writer_lock(args.data_dir):
                    for item in instruments:
                        frame = resample(load_bars(args.data_dir, item), item, args.freq)
                        target = (
                            args.data_dir
                            / "derived"
                            / args.freq
                            / item.asset_class
                            / f"{item.id}.parquet"
                        )
                        # An empty rebuild must replace any obsolete previous output.
                        with atomic_path(target) as temporary:
                            frame.write_parquet(temporary, compression="zstd")
                        outputs.append(
                            {"symbol": item.id, "rows": frame.height, "path": str(target)}
                        )
                ok = bool(outputs) and all(item["rows"] > 0 for item in outputs)
                report = {"ok": ok, "outputs": outputs}
            else:
                reports = [
                    {"symbol": item.id, **check_bars(load_bars(args.data_dir, item)).to_dict()}
                    for item in instruments
                ]
                ok = bool(reports) and all(item["ok"] for item in reports)
                report = {"ok": ok, "instruments": reports}
        print(json.dumps(report, indent=2))
        return 0 if ok else 1
    except (
        ValueError,
        OSError,
        RuntimeError,
        httpx.HTTPError,
        duckdb.Error,
        pl.exceptions.PolarsError,
    ) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
