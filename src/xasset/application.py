"""Operations entry point kept separate from frozen research commands."""

import argparse
import json
import os
from pathlib import Path

import uvicorn
import yaml

from xasset.monitor.config import load
from xasset.monitor.service import serve
from xasset.relmap.pipeline import MapDesign, register, run
from xasset.store.writer import write_json


def main() -> None:
    parser = argparse.ArgumentParser(prog="xasset-app")
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("XASSET_DATA_DIR", "data")))
    commands = parser.add_subparsers(dest="command", required=True)
    mapping = commands.add_parser("map")
    mapping.add_argument("design", type=Path)
    monitor = commands.add_parser("monitor")
    monitor.add_argument("--config", type=Path, default=Path("config/monitor.yaml"))
    monitor.add_argument("--once", action="store_true")
    web = commands.add_parser("serve")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    compare = commands.add_parser("reconcile-trades")
    compare.add_argument("native", type=Path)
    compare.add_argument("external", type=Path)
    compare.add_argument("--output", type=Path, required=True)
    paper = commands.add_parser("paper-audit")
    paper.add_argument("statement", type=Path)
    paper.add_argument("--symbols", nargs="+", required=True)
    paper.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "map":
        design = MapDesign.model_validate(yaml.safe_load(args.design.read_text()))
        register(args.data_dir, design)
        result = run(args.data_dir, design.id)
        print(
            json.dumps(
                {k: result[k] for k in ("id", "registered_cells", "counts", "tradable")}, indent=2
            )
        )
    elif args.command == "monitor":
        serve(args.data_dir, load(args.config), args.once)
    elif args.command == "reconcile-trades":
        import hashlib

        from xasset.crosscheck.reconcile import reconcile
        from xasset.research.contracts import Trade

        records = [
            [Trade.model_validate(row) for row in json.loads(path.read_text())]
            for path in (args.native, args.external)
        ]
        comparison = reconcile(records[0], records[1])
        comparison["source_hashes"] = {
            label: hashlib.sha256(path.read_bytes()).hexdigest()
            for label, path in (("native", args.native), ("external", args.external))
        }
        comparison["independent_execution_verified"] = False
        write_json(args.output, comparison)
        print(json.dumps(comparison, indent=2))
        if not comparison["ok"]:
            raise SystemExit(1)
    elif args.command == "paper-audit":
        from xasset.crosscheck.reconcile import paper_statement

        audit = paper_statement(args.statement, set(args.symbols))
        write_json(args.output, audit)
        print(json.dumps(audit, indent=2))
    else:
        if args.host not in {"127.0.0.1", "::1", "localhost"} and not os.getenv(
            "XASSET_DASHBOARD_TOKEN"
        ):
            parser.error("Set XASSET_DASHBOARD_TOKEN before binding outside loopback")
        from xasset.monitor.api import app

        uvicorn.run(app(args.data_dir), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
