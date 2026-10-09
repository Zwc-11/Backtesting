"""Persistent preregistration and run accounting in a separate DuckDB registry."""

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import duckdb

from xasset.config import Instrument
from xasset.research.costs import Costs
from xasset.research.experiment import Experiment, canonical_json, digest
from xasset.store.writer import writer_lock


class Registry:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "registry.duckdb"

    @contextmanager
    def connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        with writer_lock(self.root):
            with duckdb.connect(str(self.path)) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS families (family VARCHAR PRIMARY KEY, "
                    "spec_json VARCHAR NOT NULL, spec_hash VARCHAR NOT NULL, "
                    "created_at TIMESTAMPTZ NOT NULL, vault_run VARCHAR, "
                    "vault_reason VARCHAR, vault_opened_at TIMESTAMPTZ, vault_used_run VARCHAR)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS trials (trial_id VARCHAR PRIMARY KEY, "
                    "family VARCHAR NOT NULL, parameters_json VARCHAR NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS runs (run_id VARCHAR PRIMARY KEY, "
                    "family VARCHAR NOT NULL, phase VARCHAR NOT NULL, status VARCHAR NOT NULL, "
                    "started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ, "
                    "trial_count BIGINT NOT NULL, result_json VARCHAR, error VARCHAR)"
                )
                db.execute("BEGIN TRANSACTION")
                try:
                    yield db
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise

    def register(
        self, spec: Experiment, instruments: list[Instrument], costs: Costs
    ) -> dict[str, Any]:
        selected = {item.id: item for item in instruments}
        if set(selected) != set(spec.symbols):
            raise ValueError("Universe must resolve every preregistered symbol exactly")
        followers = {str(params.get("follower")) for params in spec.parameters()}
        if any(
            item.asset_class not in costs.profiles for item in instruments if item.id in followers
        ):
            raise ValueError("Every asset class needs a frozen cost profile")
        payload = {
            "experiment": spec.model_dump(mode="json"),
            "instruments": [selected[name].model_dump(mode="json") for name in sorted(selected)],
            "costs": costs.model_dump(mode="json"),
        }
        fingerprint = digest(payload)
        with self.connection() as db:
            existing = db.execute(
                "SELECT spec_hash FROM families WHERE family = ?", [spec.family]
            ).fetchone()
            if existing and existing[0] != fingerprint:
                raise ValueError(
                    "Family is already registered with a different immutable specification"
                )
            if not existing:
                db.execute(
                    "INSERT INTO families VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL)",
                    [spec.family, canonical_json(payload), fingerprint, datetime.now(UTC)],
                )
                for params in spec.parameters():
                    db.execute(
                        "INSERT INTO trials VALUES (?, ?, ?)",
                        [
                            digest({"family": spec.family, "parameters": params}),
                            spec.family,
                            canonical_json(params),
                        ],
                    )
            count = db.execute(
                "SELECT count(*) FROM trials WHERE family = ?", [spec.family]
            ).fetchone()
        return {
            "family": spec.family,
            "spec_hash": fingerprint,
            "registered_trials": count[0] if count else 0,
            "vault_start": spec.vault_start.isoformat(),
            "discovery_end": spec.discovery_end.isoformat(),
            "already_registered": bool(existing),
        }

    def family(self, name: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT spec_json, spec_hash, vault_run, vault_used_run "
                "FROM families WHERE family = ?",
                [name],
            ).fetchone()
        if row is None:
            raise ValueError("Register the experiment before accessing research data")
        return {
            **json.loads(row[0]),
            "spec_hash": row[1],
            "vault_run": row[2],
            "vault_used_run": row[3],
        }

    @staticmethod
    def count_trials(db: duckdb.DuckDBPyConnection) -> int:
        # Count all declared candidates, plus extra attempts (including failures).
        # This deliberately errs toward a conservative selection penalty.
        row = db.execute(
            "SELECT (SELECT count(*) FROM trials) + "
            "(SELECT count(*) - count(DISTINCT family) FROM runs)"
        ).fetchone()
        return int(row[0]) if row else 0

    def begin(self, family: str, phase: Literal["discovery", "vault"]) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        with self.connection() as db:
            saved = db.execute(
                "SELECT vault_run, vault_used_run FROM families WHERE family = ?", [family]
            ).fetchone()
            if saved is None:
                raise ValueError("Register the experiment before running it")
            if db.execute(
                "SELECT 1 FROM runs WHERE family = ? AND status = 'running'", [family]
            ).fetchone():
                raise ValueError(
                    "Family already has an active run; explicitly abandon interrupted runs"
                )
            if phase == "discovery" and saved[0] is not None:
                raise ValueError("Discovery is closed after the family vault is opened")
            if phase == "vault":
                if saved[0] is None or saved[1] is not None:
                    raise ValueError("Vault must be explicitly opened and is consumable only once")
                db.execute(
                    "UPDATE families SET vault_used_run = ? WHERE family = ?", [run_id, family]
                )
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, 'running', ?, NULL, 0, NULL, NULL)",
                [run_id, family, phase, datetime.now(UTC)],
            )
            count = self.count_trials(db)
            db.execute("UPDATE runs SET trial_count = ? WHERE run_id = ?", [count, run_id])
        return {"run_id": run_id, "family": family, "phase": phase, "trial_count": count}

    def finish(
        self,
        run_id: str,
        status: Literal["completed", "blocked", "failed"],
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connection() as db:
            row = db.execute("SELECT status FROM runs WHERE run_id = ?", [run_id]).fetchone()
            if row is None or row[0] != "running":
                raise ValueError("Only an active run can be finalized")
            db.execute(
                "UPDATE runs SET status = ?, finished_at = ?, result_json = ?, error = ? "
                "WHERE run_id = ?",
                [
                    status,
                    datetime.now(UTC),
                    canonical_json(result) if result is not None else None,
                    error,
                    run_id,
                ],
            )

    def run(self, run_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT run_id, family, phase, status, started_at, finished_at, "
                "trial_count, result_json, error FROM runs WHERE run_id = ?",
                [run_id],
            ).fetchone()
        if row is None:
            raise ValueError("Unknown run ID")
        names = (
            "run_id",
            "family",
            "phase",
            "status",
            "started_at",
            "finished_at",
            "trial_count",
            "result",
            "error",
        )
        result = dict(zip(names, row, strict=True))
        result["result"] = json.loads(row[7]) if row[7] else None
        for name in ("started_at", "finished_at"):
            result[name] = result[name].isoformat() if result[name] else None
        return result

    def list_runs(self, family: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT run_id FROM runs WHERE (? IS NULL OR family = ?) ORDER BY started_at",
                [family, family],
            ).fetchall()
        return [self.run(row[0]) for row in rows]
