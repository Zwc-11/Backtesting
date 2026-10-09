from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import polars as pl
import pytest
from conftest import bars_at
from test_equity_backfill import setup

from xasset.relmap.estimators import adjusted_pvalues, cointegration, hayashi_yoshida, regression
from xasset.relmap.pipeline import MapDesign, instruments_for, matrix, register
from xasset.research.trials import Registry


def test_global_correction_counts_unestimable_cells_and_controls_dependency():
    assert adjusted_pvalues([0.01, 0.02, 0.03], dependent=False) == pytest.approx([0.03] * 3)
    assert adjusted_pvalues([0.01, 0.02, 0.03]) == pytest.approx([0.055] * 3)
    assert adjusted_pvalues([0.01, None])[0] == pytest.approx(0.03)
    assert adjusted_pvalues([None, None]) == [1, 1]


def test_known_directional_lag_is_recovered_without_reverse_claim():
    rng = np.random.default_rng(193)
    stamps = pd.date_range("2026-01-01", periods=60 * 24, freq="1h", tz="UTC")
    x = rng.normal(size=len(stamps))
    y = 0.8 * np.roll(x, 2) + rng.normal(scale=0.1, size=len(stamps))
    frame = pd.DataFrame({"DRIVER": x, "FOLLOWER": y}, index=stamps)
    forward = regression(frame, "DRIVER", "FOLLOWER", lag=2)
    backward = regression(frame, "FOLLOWER", "DRIVER", lag=2)
    assert forward["strength"] == pytest.approx(0.8, abs=0.03)
    assert forward["p"] < 0.0001
    assert abs(backward["strength"]) < 0.15


def test_intraday_matrix_keeps_gaps_and_does_not_shift_across_sessions(instrument):
    first = datetime(2026, 7, 1, 13, 31, tzinfo=UTC)
    stamps = [first + timedelta(minutes=i) for i in range(120)]
    stamps += [first + timedelta(days=1, minutes=i) for i in range(120)]
    bars = bars_at(stamps)
    returns, prices = matrix(bars, [instrument], "5m")
    assert pd.isna(returns.loc[datetime(2026, 7, 2, 13, 35, tzinfo=UTC), "TEST"])
    assert pd.isna(returns.loc[datetime(2026, 7, 2, 10, tzinfo=UTC), "TEST"])
    assert len(prices) == 48
    missing = bars.filter(pl.col("ts_end") != first + timedelta(minutes=14))
    incomplete, _ = matrix(missing, [instrument], "5m")
    assert pd.isna(incomplete.loc[datetime(2026, 7, 1, 13, 45, tzinfo=UTC), "TEST"])
    assert pd.isna(incomplete.loc[datetime(2026, 7, 1, 13, 50, tzinfo=UTC), "TEST"])


def test_hayashi_yoshida_uses_overlapping_intervals_not_touching_boundaries():
    assert hayashi_yoshida([(0, 1, 1)], [(1, 2, 2)]) == 0
    assert hayashi_yoshida([(0, 2, 1)], [(1, 3, 2)]) == 1
    assert hayashi_yoshida([(0, 1, 0)], [(0, 1, 1)]) is None
    with pytest.raises(ValueError, match="disjoint"):
        hayashi_yoshida([(0, 2, 1), (1, 3, 1)], [(0, 1, 1)])


def test_cointegration_cannot_claim_support_from_short_history():
    result = cointegration(pd.Series(range(1, 31)), pd.Series(range(2, 32)))
    assert result["p"] is None and "60" in result["reason"]


def test_map_registration_is_immutable_and_cannot_cross_research_vault(tmp_path):
    registry = Registry(tmp_path)
    suite = setup(registry)
    design = MapDesign(
        id="map-fixture",
        suite=suite.id,
        symbols=["DRIVER", "COIN"],
        start=datetime(2026, 7, 1, tzinfo=UTC),
        split=datetime(2026, 8, 1, tzinfo=UTC),
        end=suite.cases[0].experiment.discovery_end,
    )
    first = register(tmp_path, design)
    assert register(tmp_path, design) == first
    with pytest.raises(ValueError, match="immutable"):
        register(tmp_path, design.model_copy(update={"q": 0.1}))
    with pytest.raises(ValueError, match="discovery"):
        instruments_for(
            registry, design.model_copy(update={"end": datetime(2026, 10, 1, tzinfo=UTC)})
        )
    assert registry.list_runs() == []
