"""Bounded research reads after preregistration; return no holdout rows in discovery."""

import hashlib
from datetime import datetime
from pathlib import Path

import polars as pl

from xasset.config import Instrument, Source
from xasset.qc.checks import check_bars
from xasset.store.schema import BAR_SCHEMA, QUOTE_COLUMNS
from xasset.store.writer import instrument_path


def snapshot(
    root: Path,
    instruments: list[Instrument],
    start: datetime,
    end: datetime,
    required_sources: dict[str, Source] | None = None,
) -> tuple[pl.DataFrame, str]:
    frames = []
    for instrument in instruments:
        required = (required_sources or {}).get(instrument.id)
        base = root / "sources" / required if required else root
        paths = sorted(instrument_path(base, instrument).glob("*.parquet"))
        bounded = []
        for path in paths:
            frame = (
                pl.scan_parquet(path)
                .filter((pl.col("ts_end") > start) & (pl.col("ts_end") <= end))
                .collect()
            )
            for column in QUOTE_COLUMNS:
                if column not in frame.columns:
                    frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
            bounded.append(
                frame.with_columns(pl.col("volume").cast(pl.Float64)).select(BAR_SCHEMA.names())
            )
        if not bounded:
            raise ValueError(f"No stored research bars for {instrument.id}")
        bars = pl.concat(bounded).sort("ts_end")
        if (
            not check_bars(bars).ok
            or bars.filter(
                (pl.col("symbol") != instrument.id)
                | (pl.col("asset_class") != instrument.asset_class)
            ).height
        ):
            raise ValueError(f"Missing or invalid research bars for {instrument.id}")
        if required and bars.filter(pl.col("source") != required).height:
            raise ValueError(f"Stored source differs from preregistered source for {instrument.id}")
        frames.append(bars)
    combined = pl.concat(frames).sort("symbol", "ts_end")
    # Hash canonical logical rows, not Arrow buffers: slicing a rewritten future
    # partition may change buffer offsets/chunking without changing any past row.
    serialized = combined.write_ndjson().encode()
    # Fingerprint only the permitted rows; changing the future cannot change a
    # discovery fingerprint. The caller holds the data-directory writer lock.
    return combined, hashlib.sha256(serialized).hexdigest()
