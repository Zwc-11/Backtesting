"""Preregister, run, inspect, and explicitly release native research experiments."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from xasset.config import load_universe
from xasset.research import suite as suites
from xasset.research.costs import load_costs
from xasset.research.demo import create_demo
from xasset.research.engine import source_revision, validate_design
from xasset.research.experiment import load_experiment
from xasset.research.report import markdown
from xasset.research.runner import run
from xasset.research.suite_report import publish
from xasset.research.trials import Registry
from xasset.research.vault import abandon, open_vault
from xasset.store.writer import atomic_path


def add_parser(parent: argparse._SubParsersAction[Any]) -> None:
    parser = parent.add_parser("research", help="Native backtesting and research governance")
    commands = parser.add_subparsers(dest="research_command", required=True)
    for name in (
        "register",
        "run",
        "runs",
        "report",
        "vault-open",
        "abandon",
        "demo",
        "suite-register",
        "suite-run",
        "suite-report",
    ):
        command = commands.add_parser(name)
        command.add_argument(
            "--data-dir",
            type=Path,
            default=Path(
                "data/research-demo" if name == "demo" else os.getenv("XASSET_DATA_DIR", "data")
            ),
        )
        if name in {"register", "suite-register"}:
            command.add_argument("specification", type=Path)
            command.add_argument("--universe", type=Path, required=True)
            command.add_argument("--costs", type=Path, required=True)
        if name in {"run", "vault-open"}:
            command.add_argument("family")
        if name == "run":
            command.add_argument("--phase", choices=["discovery", "vault"], default="discovery")
        if name == "runs":
            command.add_argument("--family")
        if name in {"report", "abandon"}:
            command.add_argument("run_id")
        if name == "report":
            command.add_argument("--output", type=Path)
        if name == "vault-open":
            command.add_argument("--discovery-run", required=True)
        if name in {"vault-open", "abandon"}:
            command.add_argument("--reason", required=True)
        if name in {"suite-run", "suite-report"}:
            command.add_argument("suite")
        if name == "suite-run":
            command.add_argument(
                "--retry-blocked",
                action="store_true",
                help="Explicitly retry input-blocked runs; counts as extra attempts",
            )
        if name == "suite-report":
            command.add_argument("--output", type=Path, required=True)


def main(args: argparse.Namespace) -> int:
    registry = Registry(args.data_dir)
    try:
        report: Any
        command = args.research_command
        if command == "suite-register":
            report = suites.register(
                registry,
                suites.load(args.specification),
                load_universe(args.universe),
                load_costs(args.costs),
            )
        elif command == "suite-run":
            report = suites.execute(registry, args.suite, args.retry_blocked)
            print(json.dumps(report, indent=2))
            return 0 if all(item["status"] == "completed" for item in report["cases"]) else 1
        elif command == "suite-report":
            report = publish(registry, args.suite, args.output)
        elif command == "register":
            spec = load_experiment(args.specification)
            if spec.strategy_revision == "current":
                spec = spec.model_copy(update={"strategy_revision": source_revision()})
            instruments = load_universe(args.universe, spec.symbols)
            costs = load_costs(args.costs)
            validate_design(spec, instruments, costs)
            report = registry.register(spec, instruments, costs)
        elif command in {"run", "demo"}:
            family = create_demo(args.data_dir) if command == "demo" else args.family
            report = run(registry, family, phase="discovery" if command == "demo" else args.phase)
            print(json.dumps(report, indent=2))
            if report["status"] != "completed":
                return 2
            return (
                0
                if command == "demo"
                or report["result"]["gate"]["status"] in {"candidate", "awaiting_review"}
                else 1
            )
        elif command == "runs":
            report = registry.list_runs(args.family)
        elif command == "report":
            document = markdown(registry.run(args.run_id))
            if args.output:
                with atomic_path(args.output) as temporary:
                    temporary.write_text(document)
            print(document)
            return 0
        elif command == "vault-open":
            report = open_vault(registry, args.family, args.discovery_run, args.reason)
        else:
            abandon(registry, args.run_id, args.reason)
            report = registry.run(args.run_id)
        print(json.dumps(report, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError, duckdb.Error, pl.exceptions.PolarsError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 2
