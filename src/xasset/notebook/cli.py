"""``xasset study``: daily panel download, registration, walk-forward runs, holdout."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


def add_parser(parent: argparse._SubParsersAction[Any]) -> None:
    parser = parent.add_parser("study", help="Notebook strategies: registered walk-forward studies")
    commands = parser.add_subparsers(dest="study_command", required=True)
    default_root = Path(os.getenv("XASSET_DATA_DIR", "data"))

    ingest = commands.add_parser("ingest-daily", help="Download daily klines for every USDT pair")
    ingest.add_argument("--start", type=date.fromisoformat, default=date(2019, 1, 1))
    ingest.add_argument(
        "--end", type=date.fromisoformat, default=None, help="Exclusive; default today"
    )

    register = commands.add_parser("register", help="Freeze a study, its symbol manifest and code")
    register.add_argument("study", type=Path)

    run = commands.add_parser("run", help="Run discovery folds or the opened holdout")
    run.add_argument("study", type=Path)
    run.add_argument("--phase", choices=["discovery", "holdout"], default="discovery")

    holdout = commands.add_parser("holdout-open", help="Open a study's sealed holdout once")
    holdout.add_argument("study_id")
    holdout.add_argument("--reason", required=True)

    for command in (ingest, register, run, holdout):
        command.add_argument("--data-dir", type=Path, default=default_root)


def main(args: argparse.Namespace) -> int:
    from xasset.notebook import study
    from xasset.notebook.daily import ingest_daily

    if args.study_command == "ingest-daily":
        end = args.end or datetime.now(UTC).date()
        manifest = ingest_daily(
            args.data_dir, args.start, end, progress=lambda m: print(m, flush=True)
        )
        included = sum(1 for s in manifest["symbols"] if s["excluded"] is None)
        print(
            json.dumps(
                {
                    "symbols": len(manifest["symbols"]),
                    "included": included,
                    "rows_fetched": manifest["rows_fetched"],
                },
                indent=2,
            )
        )
        return 0
    if args.study_command == "register":
        print(json.dumps(study.register(args.data_dir, args.study), indent=2))
        return 0
    if args.study_command == "holdout-open":
        print(json.dumps(study.open_holdout(args.data_dir, args.study_id, args.reason), indent=2))
        return 0
    result = study.run(args.data_dir, args.study, args.phase)
    base = result.get("scenarios", {}).get("base", {})
    print(
        json.dumps(
            {
                "id": result["id"],
                "status": result["status"],
                "error": result.get("error"),
                "strategies": {
                    sid: data.get("summary") for sid, data in base.get("strategies", {}).items()
                },
            },
            indent=2,
            default=str,
        )
    )
    return 0 if result["status"] == "completed" else 1
