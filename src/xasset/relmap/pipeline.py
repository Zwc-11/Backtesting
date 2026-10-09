"""Immutable map designs, bounded discovery reads, global FDR and following-window stability."""

import hashlib
import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import networkx as nx
import pandas as pd
import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from xasset.config import Instrument
from xasset.normalize.calendars import calendar
from xasset.normalize.resample import resample
from xasset.normalize.timebase import utc
from xasset.relmap.estimators import (
    adjusted_pvalues,
    cointegration,
    partial_correlations,
    regression,
)
from xasset.research.data import snapshot
from xasset.research.experiment import canonical_json, digest
from xasset.research.suite import saved_suite
from xasset.research.trials import Registry
from xasset.store.writer import write_json, writer_lock


class MapDesign(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    suite: str
    symbols: list[str] = Field(min_length=2)
    start: datetime
    split: datetime
    end: datetime
    horizons: list[str] = Field(default=["5m", "1h", "1d"])
    intraday_lags: list[int] = Field(default=list(range(1, 13)))
    daily_lags: list[int] = Field(default=list(range(1, 6)))
    factor: str = "SPY_SIP"
    q: float = Field(default=0.05, gt=0, lt=1)
    minimum_clusters: int = Field(default=20, ge=10)
    graphical_alpha: float = Field(default=0.02, gt=0)

    @model_validator(mode="after")
    def validate_design(self) -> "MapDesign":
        if not utc(self.start) < utc(self.split) < utc(self.end):
            raise ValueError("Map windows must be ordered and timezone aware")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Map symbols must be unique")
        if not self.horizons or set(self.horizons) - {"5m", "1h", "1d"}:
            raise ValueError("Unsupported map horizon")
        if any(lag < 1 for lag in self.intraday_lags + self.daily_lags):
            raise ValueError("Lead lags must be strictly positive")
        return self


def code_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "relmap").glob("*.py")) + [
        root / relative
        for relative in (
            "config.py",
            "normalize/resample.py",
            "normalize/calendars.py",
            "research/data.py",
            "research/strategy.py",
            "store/schema.py",
        )
    ]
    return hashlib.sha256(b"".join(path.read_bytes() for path in files)).hexdigest()


def instruments_for(registry: Registry, design: MapDesign) -> list[Instrument]:
    suite = saved_suite(registry, design.suite)
    if any(
        design.start < c.experiment.start or design.end > c.experiment.discovery_end
        for c in suite.cases
    ):
        raise ValueError("Map may read only the suite's common discovery interval")
    instruments: dict[str, Instrument] = {}
    for case in suite.cases:
        family = registry.family(case.experiment.family)
        if family["vault_run"] is not None:
            raise ValueError("Exploratory maps are closed after any source family vault release")
        for raw in family["instruments"]:
            item = Instrument.model_validate(raw)
            if item.id in instruments and item != instruments[item.id]:
                raise ValueError("Suite has conflicting instrument definitions")
            instruments[item.id] = item
    if set(design.symbols) - instruments.keys():
        raise ValueError("Map symbols must belong to the registered suite")
    return [instruments[name] for name in design.symbols]


def register(root: Path, design: MapDesign) -> dict[str, Any]:
    items = instruments_for(Registry(root), design)
    payload = {
        "design": design.model_dump(mode="json"),
        "code_hash": code_hash(),
        "instruments": [item.model_dump(mode="json") for item in items],
    }
    path = root / "maps" / design.id / "registration.json"
    fingerprint = digest(payload)
    with writer_lock(root):
        if path.exists():
            if json.loads(path.read_text())["spec_hash"] != fingerprint:
                raise ValueError("Map design is immutable; choose a new ID")
        else:
            write_json(
                path,
                {
                    **payload,
                    "spec_hash": fingerprint,
                    "registered_at": datetime.now(UTC).isoformat(),
                },
            )
    return {"id": design.id, "spec_hash": fingerprint}


def matrix(bars: pl.DataFrame, items: list[Instrument], horizon: str) -> tuple[Any, Any]:
    returns, prices = {}, {}
    cal = calendar("XNYS")
    first, last = bars["ts_end"].min(), bars["ts_end"].max()
    if not isinstance(first, datetime) or not isinstance(last, datetime):
        raise ValueError("Map inputs require dated observations")
    closes_by_session = [
        cal.session_close(s).to_pydatetime()
        for s in cal.sessions_in_range(first.date(), last.date())
    ]
    for item in items:
        minute_bars = bars.filter(pl.col("symbol") == item.id)
        if horizon != "1d":
            minutes = {"5m": 5, "1h": 60}[horizon]
            frame = (
                minute_bars.sort("ts_end")
                .with_columns(
                    (
                        (pl.col("ts_end") - pl.duration(minutes=1)).dt.truncate(f"{minutes}m")
                        + pl.duration(minutes=minutes)
                    ).alias("bucket")
                )
                .group_by("bucket")
                .agg(
                    pl.len().alias("n"),
                    pl.col("ts_end").n_unique().alias("unique"),
                    pl.col("open").first(),
                    pl.col("close").last(),
                    pl.col("source").first(),
                    pl.col("source").n_unique().alias("sources"),
                    pl.col("flags").explode().unique(),
                )
                .filter(
                    (pl.col("n") == minutes)
                    & (pl.col("unique") == minutes)
                    & (pl.col("sources") == 1)
                )
                .rename({"bucket": "ts_end"})
                .sort("ts_end")
            )
        else:
            frame = resample(minute_bars, item, horizon)
        if horizon == "1d" and item.asset_class == "crypto":
            lookup = {r["ts_end"]: r for r in minute_bars.iter_rows(named=True)}
            complete = [lookup[closes_by_session[0]]] if closes_by_session[0] in lookup else []
            for previous_close, close in zip(
                closes_by_session, closes_by_session[1:], strict=False
            ):
                expected = [
                    previous_close + timedelta(minutes=i)
                    for i in range(1, int((close - previous_close).total_seconds() // 60) + 1)
                ]
                if all(t in lookup for t in expected):
                    complete.append(lookup[close])
            frame = pl.DataFrame(complete, schema=bars.schema)
        rows = frame.select("ts_end", "open", "close", "source", "flags").to_dicts()
        values: dict[datetime, float] = {}
        closes: dict[datetime, float] = {}
        previous = None
        step = timedelta(minutes={"5m": 5, "1h": 60, "1d": 1440}[horizon])
        for row in rows:
            stamp = row["ts_end"]
            closes[stamp] = row["close"]
            # Intraday observations never bridge overnight gaps or missing buckets.
            valid = previous is not None and (
                stamp - previous["ts_end"] == step
                if horizon != "1d"
                else stamp - previous["ts_end"] <= timedelta(days=4)
            )
            if (
                valid
                and previous is not None
                and previous["close"] > 0
                and row["source"] == previous["source"]
                and "roll_boundary" not in row["flags"]
            ):
                values[stamp] = row["close"] / previous["close"] - 1
            previous = row
        returns[item.id] = pd.Series(values, dtype=float)
        prices[item.id] = pd.Series(closes, dtype=float)
    r, p = pd.DataFrame(returns).sort_index(), pd.DataFrame(prices).sort_index()
    if horizon != "1d" and len(r):
        # Preserve empty intervals so shifting never jumps overnight or across a gap.
        grid = pd.date_range(r.index.min(), r.index.max(), freq={"5m": "5min", "1h": "1h"}[horizon])
        r = r.reindex(grid)
    elif horizon == "1d":
        r, p = r.reindex(closes_by_session), p.reindex(closes_by_session)
        # Compare the same reference-session intervals for all assets.
        r = p.pct_change(fill_method=None)
    return r, p


def score(bars: pl.DataFrame, items: list[Instrument], design: MapDesign) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for horizon in design.horizons:
        returns, prices = matrix(bars, items, horizon)
        windows = [
            returns[(returns.index > a) & (returns.index <= b)]
            for a, b in [(design.start, design.split), (design.split, design.end)]
        ]
        # The first validation observation can span the split; remove it universally.
        if len(windows[1]):
            windows[1] = windows[1].iloc[1:]
        minimum = {"5m": 120, "1h": 60, "1d": 20}[horizon]
        for source, target in itertools.combinations(design.symbols, 2):
            for kind in ("pearson", "spearman"):
                measurements = [
                    regression(
                        window,
                        source,
                        target,
                        ranked=kind == "spearman",
                        daily=horizon == "1d",
                        minimum=minimum,
                        minimum_clusters=design.minimum_clusters,
                    )
                    for window in windows
                ]
                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "kind": kind,
                        "horizon": horizon,
                        "lag": 0,
                        "train": measurements[0],
                        "validation": measurements[1],
                    }
                )
            if horizon == "1d":
                measurements = [
                    cointegration(p[source], p[target])
                    for p in (
                        prices[prices.index <= design.split],
                        prices[prices.index > design.split],
                    )
                ]
                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "kind": "cointegration",
                        "horizon": horizon,
                        "lag": 0,
                        "train": measurements[0],
                        "validation": measurements[1],
                    }
                )
        lags = design.daily_lags if horizon == "1d" else design.intraday_lags
        for source, target in itertools.permutations(design.symbols, 2):
            for lag in lags:
                measurements = [
                    regression(
                        window,
                        source,
                        target,
                        lag=lag,
                        factor=design.factor,
                        daily=horizon == "1d",
                        minimum=minimum,
                        minimum_clusters=design.minimum_clusters,
                    )
                    for window in windows
                ]
                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "kind": "lead_lag",
                        "horizon": horizon,
                        "lag": lag,
                        "train": measurements[0],
                        "validation": measurements[1],
                    }
                )
        if horizon in {"1h", "1d"}:
            try:
                partial = [
                    partial_correlations(window, design.graphical_alpha) for window in windows
                ]
            except (ValueError, ArithmeticError):
                partial = [{}, {}]
            for source, target in itertools.combinations(design.symbols, 2):
                estimates = [
                    {
                        "strength": p.get((source, target)),
                        "p": None,
                        "reason": "penalized partial correlation has no calibrated p-value",
                    }
                    for p in partial
                ]
                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "kind": "partial",
                        "horizon": horizon,
                        "lag": 0,
                        "train": estimates[0],
                        "validation": estimates[1],
                    }
                )
    train_q = adjusted_pvalues([edge["train"]["p"] for edge in edges])
    validation_q = adjusted_pvalues([edge["validation"]["p"] for edge in edges])
    for edge, qt, qv in zip(edges, train_q, validation_q, strict=True):
        edge.update(q_train=qt, q_validation=qv, status="candidate", tradable=False)
        left, right = edge["train"]["strength"], edge["validation"]["strength"]
        if qt <= design.q and left is not None:
            edge["status"] = "significant"
            if (
                qv <= design.q
                and right is not None
                and left * right > 0
                and abs(right) >= 0.5 * abs(left)
            ):
                edge["status"] = "stable"
        edge["id"] = digest({k: edge[k] for k in ("source", "target", "kind", "horizon", "lag")})
    return edges


def _execute(root: Path, design_id: str) -> dict[str, Any]:
    path = root / "maps" / design_id
    saved = json.loads((path / "registration.json").read_text())
    if saved.get("code_hash") != code_hash():
        raise ValueError("Map code changed since registration; register a new design")
    design = MapDesign.model_validate(saved["design"])
    items = instruments_for(Registry(root), design)
    if (path / "result.json").exists():
        cached: dict[str, Any] = json.loads((path / "result.json").read_text())
        return cached
    with writer_lock(root):
        bars, checksum = snapshot(
            root, items, design.start, design.end, {item.id: item.sources[0] for item in items}
        )
        bars.write_parquet(path / "input.parquet", compression="zstd")
    edges = score(bars, items, design)
    graph = nx.Graph()
    graph.add_nodes_from(design.symbols)
    for edge in edges:
        if edge["status"] == "stable":
            graph.add_edge(edge["source"], edge["target"])
    communities = list(nx.connected_components(graph))
    result = {
        "id": design.id,
        "spec_hash": saved["spec_hash"],
        "data_sha256": checksum,
        "generated_at": datetime.now(UTC).isoformat(),
        "design": design.model_dump(mode="json"),
        "input_rows": bars.height,
        "registered_cells": len(edges),
        "edges": edges,
        "nodes": [
            {
                "id": item.id,
                "asset_class": item.asset_class,
                "cluster": next(i for i, group in enumerate(communities) if item.id in group),
            }
            for item in items
        ],
        "counts": {
            state: sum(e["status"] == state for e in edges)
            for state in ("candidate", "significant", "stable")
        },
        "tradable": 0,
        "limitations": [
            "Descriptive associations; no causality or trading acceptance",
            "Global Benjamini-Yekutieli across every declared pair, lag and horizon",
            "Intraday date-cluster inference; daily HAC asymptotics",
            "Partial correlations are descriptive, without calibrated inference",
            "Cointegration needs 60 observations in each separate window",
            "Current-survivor universe; historical observations are revised data",
        ],
    }
    with writer_lock(root):
        with duckdb.connect(str(root / "relationships.duckdb")) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS map_runs (id VARCHAR PRIMARY KEY, result VARCHAR)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS edges (run_id VARCHAR, edge_id VARCHAR, "
                "source VARCHAR, target VARCHAR, kind VARCHAR, horizon VARCHAR, lag INTEGER, "
                "status VARCHAR, evidence VARCHAR, PRIMARY KEY(run_id, edge_id))"
            )
            db.execute("BEGIN TRANSACTION")
            db.execute(
                "INSERT OR REPLACE INTO map_runs VALUES (?, ?)", [design.id, canonical_json(result)]
            )
            db.executemany(
                "INSERT OR REPLACE INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        design.id,
                        e["id"],
                        e["source"],
                        e["target"],
                        e["kind"],
                        e["horizon"],
                        e["lag"],
                        e["status"],
                        canonical_json(e),
                    )
                    for e in edges
                ],
            )
            db.execute("COMMIT")
        write_json(path / "result.json", result)
        write_json(root / "maps" / "latest.json", result)
    return result


def run(root: Path, design_id: str) -> dict[str, Any]:
    path = root / "maps" / design_id
    with writer_lock(path):
        attempt = path / "attempt.json"
        if attempt.exists() and not (path / "result.json").exists():
            raise ValueError("Map attempt failed or was interrupted; preserve it and use a new ID")
        if not (path / "result.json").exists():
            write_json(attempt, {"status": "running", "started_at": datetime.now(UTC).isoformat()})
        try:
            result = _execute(root, design_id)
        except Exception as exc:
            write_json(attempt, {"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            raise
        write_json(attempt, {"status": "completed", "data_sha256": result["data_sha256"]})
        return result
