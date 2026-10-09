"""Immutable experiment campaigns: register every candidate before any data read."""

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from xasset.config import Instrument
from xasset.research.costs import Costs
from xasset.research.engine import source_revision, validate_design
from xasset.research.experiment import Experiment, canonical_json
from xasset.research.runner import run
from xasset.research.trials import Registry


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1)
    original_scope: str = Field(min_length=10)
    implemented_scope: str = Field(min_length=10)
    deferred_scope: list[str] = Field(default_factory=list)
    experiment: Experiment


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1)
    cases: list[Case] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique(self) -> "Suite":
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Suite case IDs must be unique")
        if len({case.experiment.family for case in self.cases}) != len(self.cases):
            raise ValueError("Every case must have its own immutable family")
        return self


def load(path: Path) -> Suite:
    return Suite.model_validate(yaml.safe_load(path.read_text()))


def register(
    registry: Registry, suite: Suite, instruments: list[Instrument], costs: Costs
) -> dict[str, Any]:
    revision = source_revision()
    cases = [
        case.model_copy(
            update={
                "experiment": case.experiment.model_copy(update={"strategy_revision": revision})
                if case.experiment.strategy_revision == "current"
                else case.experiment
            }
        )
        for case in suite.cases
    ]
    frozen = suite.model_copy(update={"cases": cases})
    for case in cases:
        chosen = [item for item in instruments if item.id in case.experiment.symbols]
        validate_design(case.experiment, chosen, costs)
    payload = canonical_json(frozen.model_dump(mode="json"))
    with registry.connection() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS suites "
            "(suite_id VARCHAR PRIMARY KEY, payload VARCHAR NOT NULL)"
        )
        saved = db.execute("SELECT payload FROM suites WHERE suite_id = ?", [suite.id]).fetchone()
        if saved and saved[0] != payload:
            raise ValueError("Suite is already registered with a different immutable definition")
    # Registration touches configuration and the registry only. If interrupted,
    # repeat it idempotently; no campaign is runnable until all families exist.
    registrations = [
        registry.register(
            case.experiment,
            [item for item in instruments if item.id in case.experiment.symbols],
            costs,
        )
        for case in cases
    ]
    with registry.connection() as db:
        db.execute("INSERT INTO suites VALUES (?, ?) ON CONFLICT DO NOTHING", [suite.id, payload])
    return {
        "suite": suite.id,
        "cases": registrations,
        "registered_candidates": sum(item["registered_trials"] for item in registrations),
    }


def saved_suite(registry: Registry, suite_id: str) -> Suite:
    with registry.connection() as db:
        exists = db.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'suites'"
        ).fetchone()
        row = (
            db.execute("SELECT payload FROM suites WHERE suite_id = ?", [suite_id]).fetchone()
            if exists
            else None
        )
    if not row:
        raise ValueError("Register the entire suite before running or reporting it")
    return Suite.model_validate(json.loads(row[0]))


def execute(registry: Registry, suite_id: str, retry_blocked: bool = False) -> dict[str, Any]:
    suite = saved_suite(registry, suite_id)
    outcomes = []
    for case in suite.cases:
        previous = registry.list_runs(case.experiment.family)
        latest = previous[-1] if previous else None
        # Default resume is read-only for any existing attempt. Retrying blocked
        # input checks is explicit; completed/failing strategies are never retuned.
        if latest is None or (retry_blocked and latest["status"] == "blocked"):
            latest = run(registry, case.experiment.family)
            reused = False
        else:
            reused = True
        outcomes.append(
            {
                "case": case.id,
                "run_id": latest["run_id"],
                "status": latest["status"],
                "reused": reused,
            }
        )
    return {"suite": suite.id, "cases": outcomes, "accepted": False}
