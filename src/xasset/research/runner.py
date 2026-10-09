"""Run the native engine against only an authorized research interval."""

from typing import Any, Literal

from xasset.config import Instrument
from xasset.research.contracts import Request
from xasset.research.costs import Costs
from xasset.research.data import snapshot
from xasset.research.engine import evaluate as backtest
from xasset.research.engine import validate_design
from xasset.research.experiment import Experiment
from xasset.research.gate import evaluate
from xasset.research.readiness import inspect
from xasset.research.trials import Registry
from xasset.store.writer import atomic_path, writer_lock


def run(
    registry: Registry,
    family: str,
    phase: Literal["discovery", "vault"] = "discovery",
) -> dict[str, Any]:
    saved = registry.family(family)
    spec = Experiment.model_validate(saved["experiment"])
    costs = Costs.model_validate(saved["costs"])
    instruments = [Instrument.model_validate(item) for item in saved["instruments"]]
    attempt = registry.begin(family, phase)
    run_id = attempt["run_id"]
    try:
        validate_design(spec, instruments, costs)  # Before any market-data read.
        discovery = registry.run(saved["vault_run"])["result"] if phase == "vault" else None
        start, end = (
            (spec.start, spec.discovery_end)
            if phase == "discovery"
            else (spec.vault_start, spec.end)
        )
        with writer_lock(registry.root):
            readiness = inspect(registry.root, spec, instruments) if phase == "discovery" else None
            blocked = bool(
                readiness is not None
                and not readiness["ready"]
                and (
                    spec.required_sources
                    or spec.minimum_coverage
                    or spec.prerequisites
                    or spec.strategy == "event_response"
                )
            )
            if not blocked:
                bars, fingerprint = snapshot(
                    registry.root, instruments, start, end, spec.required_sources
                )
                target = registry.root / "research" / run_id / "input.parquet"
                with atomic_path(target) as temporary:
                    bars.write_parquet(temporary, compression="zstd")
        if blocked:
            registry.finish(
                run_id,
                "blocked",
                {
                    "spec_hash": saved["spec_hash"],
                    "readiness": readiness,
                    "gate": {"status": "not_evaluated", "accepted": False},
                },
                error="Required experiment inputs are unavailable; no strategy evaluated",
            )
            return registry.run(run_id)
        request = Request(
            1,
            spec,
            instruments,
            costs,
            bars,
            phase,
            attempt["trial_count"],
            start,
            end,
            discovery["engine_result"]["selected_parameters"] if discovery else None,
            discovery,
        )
        result = backtest(request)
        report = {
            "spec_hash": saved["spec_hash"],
            "data_sha256": fingerprint,
            "data_start": start.isoformat(),
            "data_end": end.isoformat(),
            "input_rows": bars.height,
            "input_snapshot": str(target.relative_to(registry.root)),
            "engine_result": result.model_dump(mode="json"),
            "gate": evaluate(result, request),
        }
        registry.finish(run_id, "completed", report)
    except Exception as exc:
        # Persist every failed attempt, including engine/data exceptions. The
        # caller gets a nonzero outcome; no fallback engine or swallowed verdict.
        registry.finish(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
    return registry.run(run_id)
