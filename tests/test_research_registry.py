from dataclasses import replace
from datetime import timedelta

import polars as pl
import pytest
from test_native_engine import START, frame, request

from xasset.cli import main
from xasset.research.data import snapshot
from xasset.research.runner import run
from xasset.research.trials import Registry
from xasset.research.vault import abandon, open_vault
from xasset.store.writer import merge_bars, writer_lock


def registered(tmp_path):
    original = request()
    registry = Registry(tmp_path)
    registration = registry.register(original.experiment, original.instruments, original.costs)
    return original, registry, registration


def test_preregistration_is_idempotent_immutable_and_counts_unrun_trials(tmp_path):
    original, registry, registration = registered(tmp_path)
    assert registration["registered_trials"] == 1
    assert registry.register(original.experiment, original.instruments, original.costs)[
        "already_registered"
    ]
    with pytest.raises(ValueError, match="immutable"):
        registry.register(
            original.experiment.model_copy(update={"initial_capital": 200000}),
            original.instruments,
            original.costs,
        )
    # No reads/runs are required for the whole declared grid to count.
    second = original.experiment.model_copy(
        update={
            "family": "another-family",
            "parameter_grid": {**original.experiment.parameter_grid, "lookback": [1, 2, 3]},
        }
    )
    registry.register(second, original.instruments, original.costs)
    first = registry.begin(original.experiment.family, "discovery")
    assert first["trial_count"] == 4
    registry.finish(first["run_id"], "failed", error="Deliberate fixture failure")
    retry = registry.begin(original.experiment.family, "discovery")
    assert retry["trial_count"] == 5


def test_unregistered_and_parallel_runs_are_refused(tmp_path):
    original, registry, _ = registered(tmp_path)
    with pytest.raises(ValueError, match="Register"):
        registry.begin("unknown", "discovery")
    attempt = registry.begin(original.experiment.family, "discovery")
    with pytest.raises(ValueError, match="active run"):
        registry.begin(original.experiment.family, "discovery")
    abandon(registry, attempt["run_id"], "Fixture simulates a killed process")
    assert registry.run(attempt["run_id"])["status"] == "failed"
    with pytest.raises(ValueError, match="active run"):
        registry.finish(attempt["run_id"], "completed", {})


def test_vault_is_explicit_requires_latest_candidate_and_consumes_failed_attempt(tmp_path):
    original, registry, _ = registered(tmp_path)
    family = original.experiment.family
    with pytest.raises(ValueError, match="explicitly opened"):
        registry.begin(family, "vault")
    failed = registry.begin(family, "discovery")
    registry.finish(failed["run_id"], "completed", {"gate": {"status": "incomplete"}})
    with pytest.raises(ValueError, match="candidate"):
        open_vault(registry, family, failed["run_id"], "Fixture release request")
    candidate = registry.begin(family, "discovery")
    # Fabricated ONLY in this governance unit test, never an actual research report.
    registry.finish(candidate["run_id"], "completed", {"gate": {"status": "candidate"}})
    newer = registry.begin(family, "discovery")
    registry.finish(newer["run_id"], "failed", error="Fixture newer failure")
    with pytest.raises(ValueError, match="latest"):
        open_vault(registry, family, candidate["run_id"], "Fixture release request")
    latest = registry.begin(family, "discovery")
    registry.finish(latest["run_id"], "completed", {"gate": {"status": "candidate"}})
    open_vault(registry, family, latest["run_id"], "Fixture release request")
    with pytest.raises(ValueError, match="Discovery is closed"):
        registry.begin(family, "discovery")
    holdout = registry.begin(family, "vault")
    registry.finish(holdout["run_id"], "failed", error="Fixture fails after reservation")
    with pytest.raises(ValueError, match="only once"):
        registry.begin(family, "vault")


def store_request(root, original):
    with writer_lock(root):
        for item in original.instruments:
            merge_bars(root, item, original.bars.filter(pl.col("symbol") == item.id))


def test_discovery_snapshot_and_hash_are_unchanged_by_future_vault_rows(tmp_path):
    original, _, _ = registered(tmp_path)
    store_request(tmp_path, original)
    before, fingerprint = snapshot(
        tmp_path, original.instruments, original.data_start, original.data_end
    )
    for item in original.instruments:
        future = frame([(400, 500, 300, 450)], item.id, original.experiment.vault_start)
        with writer_lock(tmp_path):
            merge_bars(tmp_path, item, future)
    after, updated = snapshot(
        tmp_path, original.instruments, original.data_start, original.data_end
    )
    assert fingerprint == updated
    assert before.equals(after)
    assert after["ts_end"].max() <= original.experiment.discovery_end


def test_native_runner_saves_reproducible_input_and_report(tmp_path, capsys):
    original, registry, _ = registered(tmp_path)
    store_request(tmp_path, original)
    result = run(registry, original.experiment.family)
    assert result["status"] == "completed", result["error"]
    assert result["result"]["engine_result"]["engine"] == "xasset"
    assert not result["result"]["gate"]["accepted"]
    assert (tmp_path / result["result"]["input_snapshot"]).exists()
    assert main(["research", "report", result["run_id"], "--data-dir", str(tmp_path)]) == 0
    assert "not an accepted strategy verdict" in capsys.readouterr().out


def test_failed_data_read_is_recorded_and_counts_as_attempt(tmp_path):
    original, registry, _ = registered(tmp_path)
    first = run(registry, original.experiment.family)
    second = run(registry, original.experiment.family)
    assert first["status"] == second["status"] == "failed"
    assert second["trial_count"] == first["trial_count"] + 1
    assert len(registry.list_runs()) == 2


def test_vault_engine_uses_only_frozen_parameters_without_training_reads():
    from xasset.research.engine import evaluate

    original = request()
    discovery = evaluate(original)
    # Separate, non-overlapping holdout data. The engine cannot read training rows here.
    delta = original.experiment.vault_start - START
    bars = original.bars.with_columns(pl.col("ts_end") + delta).filter(
        pl.col("ts_end") <= original.experiment.end
    )
    holdout = replace(
        original,
        phase="vault",
        bars=bars,
        data_start=original.experiment.vault_start,
        data_end=original.experiment.end,
        frozen_parameters=discovery.selected_parameters,
        discovery_result={"engine_result": discovery.model_dump(mode="json")},
    )
    result = evaluate(holdout)
    assert result.diagnostics["training_scores"] == []
    assert result.selected_parameters == discovery.selected_parameters
    for scenario in result.scenarios:
        for current in scenario.folds:
            assert current.parameters == discovery.selected_parameters
            assert current.train_end <= original.experiment.discovery_end
            assert current.test_start >= original.experiment.vault_start


def test_invalid_embargo_is_rejected_before_data_read():
    from pydantic import ValidationError

    from xasset.research.experiment import Experiment

    spec = request().experiment.model_dump()
    with pytest.raises(ValidationError, match="Embargo"):
        Experiment.model_validate({**spec, "embargo_minutes": 1})
    with pytest.raises(ValidationError, match="whole timezone-aware minutes"):
        Experiment.model_validate({**spec, "start": START + timedelta(seconds=1)})
