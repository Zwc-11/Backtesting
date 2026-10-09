import json
from dataclasses import replace
from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError
from test_native_engine import PARAMS, request
from test_research_registry import store_request

from xasset.config import load_universe
from xasset.research.costs import load_costs
from xasset.research.data import snapshot
from xasset.research.engine import evaluate, source_revision, validate_design
from xasset.research.experiment import Experiment, ScheduledEvent
from xasset.research.readiness import inspect
from xasset.research.suite import Case, Suite, execute, load, register, saved_suite
from xasset.research.suite_report import publish
from xasset.research.trials import Registry


def campaign(required=False):
    original = request()
    cases = []
    for i in range(2):
        spec = original.experiment.model_copy(
            update={
                "family": f"suite-case-{i}",
                "required_sources": dict.fromkeys(original.experiment.symbols, "alpaca")
                if required
                else {},
            }
        )
        cases.append(
            Case(
                id=f"case-{i}",
                title=f"Case {i} <script>alert(1)</script>",
                original_scope="Synthetic original scope",
                implemented_scope="Synthetic executable scope",
                experiment=spec,
            )
        )
    return original, Suite(id="fixture-suite", title="Synthetic campaign", cases=cases)


def test_registers_whole_suite_without_reading_bars_and_freezes_metadata(tmp_path, monkeypatch):
    original, suite = campaign()

    def no_read(*args, **kwargs):
        pytest.fail("Registration read market data")

    monkeypatch.setattr(pl, "scan_parquet", no_read)
    registry = Registry(tmp_path)
    result = register(registry, suite, original.instruments, original.costs)
    assert result["registered_candidates"] == 2
    assert saved_suite(registry, suite.id) == suite
    assert (
        register(registry, suite, original.instruments, original.costs)["registered_candidates"]
        == 2
    )
    with pytest.raises(ValueError, match="immutable"):
        register(
            registry,
            suite.model_copy(update={"title": "Changed after registration"}),
            original.instruments,
            original.costs,
        )


def test_blocked_campaign_persists_all_attempts_resumes_and_exports_pages(tmp_path):
    original, suite = campaign(required=True)
    registry = Registry(tmp_path)
    register(registry, suite, original.instruments, original.costs)
    first = execute(registry, suite.id)
    assert all(item["status"] == "blocked" for item in first["cases"])
    assert [item["trial_count"] for item in registry.list_runs()] == [2, 2]
    second = execute(registry, suite.id)
    assert all(item["reused"] for item in second["cases"])
    assert len(registry.list_runs()) == 2
    retry = execute(registry, suite.id, retry_blocked=True)
    assert all(not item["reused"] for item in retry["cases"])
    assert [item["trial_count"] for item in registry.list_runs()] == [2, 2, 3, 4]
    output = tmp_path / "reports"
    report = publish(registry, suite.id, output)
    assert report["pages"] == 2
    summary = json.loads((output / "summary.json").read_text())
    assert all(
        item["verdict"] == "not_evaluated" and not item["accepted"] for item in summary["cases"]
    )
    document = (output / "case-0.html").read_text()
    assert "<script>" not in document and "&lt;script&gt;" in document
    assert "Inputs needed" in document
    inputs = json.loads((output / "required-inputs.json").read_text())
    assert inputs["discovery_only"]
    assert all(
        item["end"] == original.experiment.discovery_end.isoformat() for item in inputs["inputs"]
    )
    assert all(registry.family(case.experiment.family)["vault_run"] is None for case in suite.cases)


def test_available_synthetic_campaign_runs_once_and_reports_metrics(tmp_path):
    original, suite = campaign()
    registry = Registry(tmp_path)
    register(registry, suite, original.instruments, original.costs)
    store_request(tmp_path, original)
    first = execute(registry, suite.id)
    assert all(item["status"] == "completed" for item in first["cases"])
    retry = execute(registry, suite.id, retry_blocked=True)
    assert all(item["reused"] for item in retry["cases"])
    output = tmp_path / "reports"
    publish(registry, suite.id, output)
    assert "Out-of-sample results" in (output / "case-0.html").read_text()
    assert len(registry.list_runs()) == 2


def test_exact_source_cannot_silently_fall_back_to_canonical_yahoo(tmp_path):
    original = request()
    store_request(tmp_path, original)
    required = dict.fromkeys(original.experiment.symbols, "alpaca")
    spec = original.experiment.model_copy(update={"required_sources": required})
    status = inspect(tmp_path, spec, original.instruments)
    assert not status["ready"]
    assert sum("missing alpaca" in reason for reason in status["blockers"]) == 2
    assert any("Tier A" in reason for reason in status["blockers"])
    with pytest.raises(ValueError, match="No stored research bars"):
        snapshot(tmp_path, original.instruments, original.data_start, original.data_end, required)


def test_future_vault_bars_do_not_change_readiness(tmp_path):
    original = request()
    spec = original.experiment.model_copy(update={"minimum_coverage": 0.95})
    store_request(tmp_path, original)
    before = inspect(tmp_path, spec, original.instruments)
    assert not before["ready"]
    future = original.bars.with_columns(pl.col("ts_end") + (spec.vault_start - spec.start))
    store_request(tmp_path, replace(original, bars=future))
    after = inspect(tmp_path, spec, original.instruments)
    assert before == after


def test_explicit_candidates_preserve_correlated_driver_direction():
    spec = request().experiment.model_dump()
    choices = [{**PARAMS, "direction": 1}, {**PARAMS, "direction": -1}]
    explicit = Experiment.model_validate({**spec, "parameter_grid": {}, "parameter_sets": choices})
    assert explicit.parameters() == choices
    with pytest.raises(ValidationError, match="not both"):
        Experiment.model_validate({**spec, "parameter_sets": choices})
    with pytest.raises(ValidationError, match="unique"):
        Experiment.model_validate(
            {**spec, "parameter_grid": {}, "parameter_sets": [choices[0]] * 2}
        )
    with pytest.raises(ValidationError):
        Experiment.model_validate({**spec, "required_sources": {"DRIVER": "../../private"}})


def test_actual_seven_designs_resolve_all_symbols_and_preserve_original_scopes():
    suite = load(Path("config/phase3.yaml"))
    instruments = load_universe(Path("config/phase3-universe.yaml"))
    costs = load_costs(Path("config/costs.yaml"))
    assert len(suite.cases) == 7
    assert sum(len(case.experiment.parameters()) for case in suite.cases) == 316
    for case in suite.cases:
        spec = case.experiment.model_copy(update={"strategy_revision": source_revision()})
        chosen = [item for item in instruments if item.id in spec.symbols]
        validate_design(spec, chosen, costs)
        assert all(item.tier == "A" for item in chosen)
        assert all(source != "yahoo" for source in spec.required_sources.values())
        assert set(spec.required_sources) == set(spec.symbols)
        assert case.implemented_scope and case.deferred_scope
    assert not suite.cases[5].experiment.events  # Never invent release timestamps.


def test_event_strategy_runs_full_walk_forward_with_base_and_stress_costs():
    from datetime import timedelta

    original = request()
    events = [
        ScheduledEvent(
            id=f"e-{i}",
            kind="eia_petroleum",
            at=original.data_start + timedelta(days=i, hours=1, minutes=1),
            known_at=original.data_start,
            reference="Synthetic preannounced event",
        )
        for i in range(4)
    ]
    params = {**PARAMS, "observation_minutes": 1}
    spec = original.experiment.model_copy(
        update={
            "strategy": "event_response",
            "events": events,
            "parameter_grid": {key: [value] for key, value in params.items()},
        }
    )
    result = evaluate(replace(original, experiment=spec))
    assert [scenario.multiplier for scenario in result.scenarios] == [1, 2]
    assert all(scenario.trades for scenario in result.scenarios)
    assert result.scenarios[0].folds == result.scenarios[1].folds
    assert all(scenario.checks["feature_truncation"].passed for scenario in result.scenarios)
