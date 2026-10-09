"""Deterministic synthetic smoke data, isolated from real market history."""

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from xasset.config import Instrument
from xasset.research.costs import CostProfile, Costs
from xasset.research.engine import source_revision
from xasset.research.experiment import Experiment
from xasset.research.trials import Registry
from xasset.store.schema import BAR_SCHEMA
from xasset.store.writer import merge_bars, writer_lock


def create_demo(root: Path) -> str:
    if (root / "bars").exists() or (root / "registry.duckdb").exists():
        raise ValueError("Demo requires a fresh data directory; existing research is preserved")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=10)
    instruments = [
        Instrument(
            id=name,
            asset_class="crypto",
            tier="C",
            venue="SYNTHETIC",
            session="all",
            yahoo_symbol=name,
            lot_size=0.01,
        )
        for name in ("DRIVER", "FOLLOWER")
    ]
    with writer_lock(root):
        for item in instruments:
            rows = []
            lag = 0 if item.id == "DRIVER" else 2
            for minute in range(1, 10 * 1440 + 1):
                opened = 100 + 2 * math.sin((minute - lag - 1) / 15)
                closed = 100 + 2 * math.sin((minute - lag) / 15)
                rows.append(
                    dict(
                        symbol=item.id,
                        asset_class="crypto",
                        ts_end=start + timedelta(minutes=minute),
                        open=opened,
                        high=max(opened, closed) + 0.01,
                        low=min(opened, closed) - 0.01,
                        close=closed,
                        volume=1000.0,
                        source="yahoo",
                        flags=["synthetic", "single_source"],
                    )
                )
            merge_bars(root, item, pl.DataFrame(rows, schema=BAR_SCHEMA))
    spec = Experiment(
        family="native-smoke",
        hypothesis="Synthetic lead-lag fixture validates plumbing only",
        strategy="cross_asset_leadlag",
        strategy_revision=source_revision(),
        purpose="smoke",
        symbols=[item.id for item in instruments],
        start=start,
        end=end,
        frequency="1m",
        parameter_grid={
            "driver": ["DRIVER"],
            "follower": ["FOLLOWER"],
            "lookback": [1, 2],
            "threshold": [0.001],
            "hold_minutes": [3],
            "stop_pct": [0.01],
            "target_pct": [0.01],
        },
        max_holding_minutes=3,
        embargo_minutes=3,
        training_days=1,
        test_days=2,
    )
    costs = Costs(
        currency="USD",
        profiles={
            "crypto": CostProfile(
                commission_bps=1,
                spread_bps=1,
                slippage_bps=1,
                evidence="Synthetic test assumptions, not calibrated execution costs",
            )
        },
    )
    Registry(root).register(spec, instruments, costs)
    return spec.family
