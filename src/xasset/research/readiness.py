"""Discovery-only prerequisite checks, after immutable registration."""

from pathlib import Path
from typing import Any

import polars as pl

from xasset.config import Instrument
from xasset.normalize.calendars import expected_bar_ends
from xasset.research.experiment import Experiment
from xasset.store.writer import instrument_path


def inspect(root: Path, spec: Experiment, instruments: list[Instrument]) -> dict[str, Any]:
    blockers = list(spec.prerequisites)
    if spec.strategy == "event_response" and not any(
        spec.start < event.at < spec.discovery_end for event in spec.events
    ):
        blockers.append("A verified, point-in-time event calendar is required for discovery")
    coverage = []
    for item in instruments:
        source = spec.required_sources.get(item.id)
        base = root / "sources" / source if source else root
        paths = sorted(instrument_path(base, item).glob("*.parquet"))
        entry: dict[str, Any] = {
            "symbol": item.id,
            "required_source": source,
            "rows": 0,
            "coverage": None,
        }
        if spec.required_sources and item.tier != "A":
            blockers.append(f"{item.id}: Phase 3 requires Tier A observations")
        if (
            spec.minimum_coverage > 0
            and not item.calendar
            and not (item.asset_class == "crypto" and item.session == "all")
        ):
            blockers.append(f"{item.id}: verified session calendar required to assess coverage")
        if not paths:
            blockers.append(f"{item.id}: missing {source or 'canonical'} historical bars")
            coverage.append(entry)
            continue
        stamps = pl.concat(
            [
                pl.scan_parquet(path)
                .filter((pl.col("ts_end") > spec.start) & (pl.col("ts_end") <= spec.discovery_end))
                .select("ts_end", "source")
                .collect()
                for path in paths
            ]
        )
        if source:
            stamps = stamps.filter(pl.col("source") == source)
        observed = set(stamps["ts_end"].to_list())
        entry.update(
            rows=len(observed),
            first=min(observed).isoformat() if observed else None,
            last=max(observed).isoformat() if observed else None,
        )
        if not observed:
            blockers.append(f"{item.id}: no bars inside the permitted discovery interval")
        if spec.minimum_coverage > 0:
            if item.calendar:
                expected = expected_bar_ends(item.calendar, spec.start, spec.discovery_end)
                ratio = len(observed & expected) / len(expected) if expected else 0.0
            elif item.asset_class == "crypto" and item.session == "all":
                expected_count = int((spec.discovery_end - spec.start).total_seconds() // 60)
                ratio = len(observed) / expected_count if expected_count else 0.0
            else:
                ratio = None
                blockers.append(f"{item.id}: verified session calendar required to assess coverage")
            entry["coverage"] = ratio
            if ratio is not None and ratio < spec.minimum_coverage:
                blockers.append(
                    f"{item.id}: coverage {ratio:.2%} below {spec.minimum_coverage:.2%}"
                )
        coverage.append(entry)
    return {
        "ready": not blockers,
        "blockers": sorted(set(blockers)),
        "coverage": coverage,
        "start": spec.start.isoformat(),
        "end": spec.discovery_end.isoformat(),
    }
