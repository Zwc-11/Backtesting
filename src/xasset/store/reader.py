"""Calendar-aligned minute reads with explicit missing/stale/closed states."""

from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from xasset.config import Instrument, Source
from xasset.normalize.calendars import expected_bar_ends
from xasset.normalize.timebase import utc
from xasset.store.writer import load_bars


def load(
    root: Path,
    instruments: list[Instrument],
    start: datetime,
    end: datetime,
    source: Source | None = None,
) -> pl.DataFrame:
    start, end = utc(start), utc(end)
    if start >= end or any(value.second or value.microsecond for value in (start, end)):
        raise ValueError("Read a positive interval aligned to UTC minutes")
    frames = []
    times = pl.datetime_range(start + timedelta(minutes=1), end, interval="1m", eager=True)
    for instrument in instruments:
        bars = load_bars(root, instrument, source).filter(
            (pl.col("ts_end") > start) & (pl.col("ts_end") <= end)
        )
        regular = instrument.session == "regular" and instrument.calendar is not None
        continuous = instrument.asset_class == "crypto" and instrument.session == "all"
        expected = (
            expected_bar_ends(instrument.calendar, start, end)
            if regular and instrument.calendar
            else set(times.to_list())
            if continuous
            else None
        )
        grid = pl.DataFrame({"ts_end": times})
        aligned = grid.join(bars, on="ts_end", how="left")
        observed = pl.col("close").is_not_null()
        known_open = (
            pl.col("ts_end").is_in(sorted(expected))
            if expected is not None
            else pl.lit(None, dtype=pl.Boolean)
        )
        aligned = aligned.with_columns(
            pl.lit(instrument.id).alias("symbol"),
            pl.lit(instrument.asset_class).alias("asset_class"),
            known_open.alias("session_open"),
            observed.alias("observed"),
            pl.when(observed)
            .then(pl.col("ts_end"))
            .otherwise(None)
            .forward_fill()
            .alias("last_observed_at"),
        ).with_columns(
            (pl.col("session_open") & ~pl.col("observed")).alias("stale"),
            (pl.col("ts_end") - pl.col("last_observed_at")).dt.total_seconds().alias("age_seconds"),
        )
        # Price/volume fields stay null when missing or closed. Only observation
        # metadata carries forward; there are no imputed tradable prices.
        frames.append(aligned)
    if not frames:
        raise ValueError("Select at least one instrument")
    return pl.concat(frames).sort("ts_end", "symbol")
