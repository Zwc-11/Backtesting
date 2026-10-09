import importlib.util
import json
from datetime import UTC, datetime, timedelta

import httpx
import polars as pl
import pytest
from test_native_engine import PARAMS, profile

from xasset.config import Instrument
from xasset.normalize.calendars import expected_bar_ends
from xasset.research.costs import Costs
from xasset.research.engine import source_revision
from xasset.research.experiment import Experiment
from xasset.research.suite import Case, Suite, register
from xasset.research.trials import Registry

module_spec = importlib.util.spec_from_file_location(
    "equity_backfill", "scripts/backfill_equities.py"
)
backfill = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(backfill)


def setup(registry):
    driver = Instrument(
        id="DRIVER",
        asset_class="crypto",
        tier="A",
        venue="BINANCE",
        currency="USDT",
        session="all",
        binance_symbol="BTCUSDT",
        sources=["binance"],
    )
    follower = Instrument(
        id="COIN",
        asset_class="equity",
        tier="A",
        venue="XNYS",
        session="regular",
        calendar="XNYS",
        alpaca_symbol="COIN",
        sources=["alpaca"],
    )
    params = {**PARAMS, "follower": "COIN"}
    spec = Experiment(
        family="backfill-fixture",
        hypothesis="Synthetic discovery-only download fixture",
        strategy="cross_asset_leadlag",
        strategy_revision=source_revision(),
        symbols=["DRIVER", "COIN"],
        start=datetime(2026, 7, 1, tzinfo=UTC),
        end=datetime(2026, 10, 1, tzinfo=UTC),
        parameter_grid={key: [value] for key, value in params.items()},
        training_days=1,
        test_days=1,
        embargo_minutes=390,
        max_holding_minutes=2,
        minimum_coverage=0.95,
        required_sources={"DRIVER": "binance", "COIN": "alpaca"},
    )
    suite = Suite(
        id="backfill-suite",
        title="Synthetic download campaign",
        cases=[
            Case(
                id="coin",
                title="COIN fixture",
                original_scope="Synthetic original scope",
                implemented_scope="Synthetic executable scope",
                experiment=spec,
            )
        ],
    )
    register(
        registry, suite, [driver, follower], Costs(currency="USD", profiles={"equity": profile()})
    )
    return suite


def credentials(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "synthetic-test-secret")


def transport(requests):
    def respond(request):
        requests.append(request)
        assert request.url.host == "data.alpaca.markets"
        assert request.headers["APCA-API-KEY-ID"] == "synthetic-test-key"
        assert request.url.params["feed"] == "iex"
        start = datetime.fromisoformat(request.url.params["start"])
        end = datetime.fromisoformat(request.url.params["end"])
        stamps = sorted(expected_bar_ends("XNYS", start, end))[:3]
        bars = [
            {
                "t": (stamp - timedelta(minutes=1)).isoformat(),
                "o": 100,
                "h": 102,
                "l": 99,
                "c": 101,
                "v": 20,
            }
            for stamp in stamps
        ]
        return httpx.Response(200, json={"symbol": "COIN", "bars": bars, "next_page_token": None})

    return httpx.MockTransport(respond)


def test_plan_uses_frozen_metadata_and_never_reads_market_data(tmp_path, monkeypatch):
    registry = Registry(tmp_path)
    suite = setup(registry)

    def forbidden(*args, **kwargs):
        pytest.fail("Planning read market data")

    monkeypatch.setattr(backfill, "snapshot", forbidden)
    plan = backfill.plan(registry, suite.id)
    assert plan["symbols"] == ["COIN"]
    assert len(plan["jobs"]) == 3
    assert plan["end"] == suite.cases[0].experiment.discovery_end.isoformat()
    assert all(job["instrument"]["alpaca_feed"] == "iex" for job in plan["jobs"])
    assert all(
        datetime.fromisoformat(job["end"]) <= suite.cases[0].experiment.discovery_end
        for job in plan["jobs"]
    )
    assert registry.list_runs() == []
    with pytest.raises(ValueError, match="registered suite"):
        backfill.plan(registry, suite.id, ["UNKNOWN"])


def test_missing_bindings_never_make_a_request_or_a_research_attempt(tmp_path, monkeypatch):
    registry = Registry(tmp_path)
    suite = setup(registry)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    def forbidden(request):
        pytest.fail("Sent a request without credential bindings")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        result = backfill.run(registry, backfill.plan(registry, suite.id), client)
    assert result["status"] == "blocked"
    assert result["missing"] == ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"]
    assert registry.list_runs() == []
    assert not (tmp_path / "backfills").exists()


def test_backfill_resumes_valid_data_and_does_not_claim_coverage(tmp_path, monkeypatch):
    credentials(monkeypatch)
    registry = Registry(tmp_path)
    suite = setup(registry)
    plan = backfill.plan(registry, suite.id)
    requests = []
    with httpx.Client(transport=transport(requests)) as client:
        first = backfill.run(registry, plan, client)
        assert first["status"] == "downloaded"
        assert first["downloaded_jobs"] == 3
        assert len(requests) == 4  # small access probe, then three bounded chunks
        assert not first["readiness"][0]["ready"]  # Sparse fixture is not healthy coverage.
        second = backfill.run(registry, plan, client)
        assert second["reused_jobs"] == 3 and second["downloaded_jobs"] == 0
        assert len(requests) == 4
        assert registry.list_runs() == []
        # A checkpoint cannot conceal a missing source partition.
        (tmp_path / "sources/alpaca/bars/equity/COIN/2026-07.parquet").unlink()
        restored = backfill.run(registry, plan, client)
        assert restored["reused_jobs"] == 2 and restored["downloaded_jobs"] == 1
        assert len(requests) == 6  # probe and only the invalidated July chunk
        (tmp_path / "sources/alpaca/bars/equity/COIN/2026-09.parquet").unlink()
        later = backfill.run(registry, plan, client)
        assert later["reused_jobs"] == 2 and later["downloaded_jobs"] == 1
        assert all(req.url.params["start"].startswith("2026-09") for req in requests[-2:])
    assert registry.family("backfill-fixture")["vault_run"] is None


def test_unauthorized_probe_stops_before_bulk_download_without_leaking_keys(
    tmp_path, monkeypatch, capsys
):
    credentials(monkeypatch)
    registry = Registry(tmp_path)
    suite = setup(registry)
    requests = []

    def reject(request):
        requests.append(request)
        return httpx.Response(401, json={"message": "unauthorized"})

    with httpx.Client(transport=httpx.MockTransport(reject)) as client:
        result = backfill.run(registry, backfill.plan(registry, suite.id), client)
    assert result["status"] == "failed" and result["stage"] == "probe"
    assert len(requests) == 1
    checkpoint = json.loads((tmp_path / "backfills/backfill-suite-alpaca.json").read_text())
    assert not checkpoint["jobs"]
    output = json.dumps(result) + capsys.readouterr().out
    assert "synthetic-test-key" not in output and "synthetic-test-secret" not in output


def test_different_family_windows_are_rejected_before_access(tmp_path):
    registry = Registry(tmp_path)
    suite = setup(registry)
    first = suite.cases[0]
    changed = first.experiment.model_copy(
        update={"family": "another-window", "end": datetime(2026, 9, 1, tzinfo=UTC)}
    )
    other = first.model_copy(update={"id": "different-window", "experiment": changed})
    family = registry.family(first.experiment.family)
    both = suite.model_copy(update={"id": "mismatched-suite", "cases": [first, other]})
    register(
        registry,
        both,
        [Instrument.model_validate(item) for item in family["instruments"]],
        Costs.model_validate(family["costs"]),
    )
    with pytest.raises(ValueError, match="identical discovery bounds"):
        backfill.plan(registry, both.id)


def completed_backfill(tmp_path, monkeypatch):
    credentials(monkeypatch)
    registry = Registry(tmp_path)
    suite = setup(registry)
    design = backfill.plan(registry, suite.id)
    with httpx.Client(transport=transport([])) as client:
        backfill.run(registry, design, client)
    return registry, design


def test_offline_audit_preserves_trials_and_distinguishes_sparse_raw_data(tmp_path, monkeypatch):
    registry, design = completed_backfill(tmp_path, monkeypatch)
    monkeypatch.delenv("ALPACA_API_KEY")
    monkeypatch.delenv("ALPACA_SECRET_KEY")

    def forbidden(*args, **kwargs):
        pytest.fail("Offline audit tried to download or start a research trial")

    monkeypatch.setattr(backfill, "download", forbidden)
    monkeypatch.setattr(registry, "begin", forbidden)
    result = backfill.audit(registry, design)
    assert result["status"] == "verified" and result["attempts_unchanged"]
    assert len(result["jobs"]) == 3
    assert result["symbols"][0]["rows"] == 9
    assert not result["symbols"][0]["coverage_met"]
    assert all(job["missing_minutes"] == job["raw_missing_minutes"] for job in result["jobs"])
    assert sum(row["observed"] for row in result["sessions"]) == 9
    assert registry.list_runs() == []
    assert registry.family("backfill-fixture")["vault_run"] is None


def test_audit_detects_raw_corruption_without_repairing_it(tmp_path, monkeypatch):
    registry, design = completed_backfill(tmp_path, monkeypatch)
    result = backfill.audit(registry, design)
    raw = tmp_path / result["jobs"][0]["artifacts"][0]["path"]
    corrupt = raw.read_bytes() + b" "
    raw.write_bytes(corrupt)
    failed = backfill.audit(registry, design)
    assert failed["status"] == "failed"
    assert "Raw archive checksum mismatch" in failed["jobs"][0]["errors"]
    assert raw.read_bytes() == corrupt
    assert registry.list_runs() == []


def test_audit_is_invariant_to_future_rows_and_detects_missing_discovery_rows(
    tmp_path, monkeypatch
):
    registry, design = completed_backfill(tmp_path, monkeypatch)
    before = backfill.audit(registry, design)
    partition = tmp_path / "sources/alpaca/bars/equity/COIN/2026-09.parquet"
    bars = pl.read_parquet(partition)
    future = bars.head(1).with_columns(
        pl.lit(datetime(2026, 9, 30, 14, 0, tzinfo=UTC)).alias("ts_end")
    )
    pl.concat([bars, future]).write_parquet(partition)
    after = backfill.audit(registry, design)
    assert after["status"] == "verified"
    for key in ("jobs", "symbols", "sessions", "readiness"):
        assert after[key] == before[key]
    pl.concat([bars.tail(bars.height - 1), future]).write_parquet(partition)
    failed = backfill.audit(registry, design)
    assert failed["status"] == "failed"
    errors = failed["jobs"][-1]["errors"]
    assert "Normalized rows differ from the completed checkpoint" in errors
    assert "Normalized timestamps differ from bounded raw responses" in errors


def test_audit_report_escapes_source_errors_and_writes_session_csv(tmp_path, monkeypatch):
    registry, design = completed_backfill(tmp_path, monkeypatch)
    result = backfill.audit(registry, design)
    result["jobs"][0]["errors"] = ["<script>alert(1)</script>"]
    output = tmp_path / "reports/audit.json"
    backfill.publish_audit(output, result)
    document = output.with_suffix(".html").read_text()
    assert "<script>" not in document and "&lt;script&gt;" in document
    assert len(output.with_suffix(".csv").read_text().splitlines()) == len(result["sessions"]) + 1
