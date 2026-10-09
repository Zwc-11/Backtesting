"""Causal close-to-close returns, for reuse by research and monitor code."""

import polars as pl

from xasset.qc.checks import check_bars


def trailing_features(bars: pl.DataFrame, window: int = 20) -> pl.DataFrame:
    if window < 2:
        raise ValueError("volatility window must be at least two bars")
    report = check_bars(bars)
    if not report.ok:
        raise ValueError(f"Invalid feature input: {report.errors}")
    ordered = bars.sort("symbol", "ts_end")
    previous = pl.col("close").shift(1).over("symbol")
    consecutive = (
        (pl.col("ts_end").diff().over("symbol") == pl.duration(minutes=1))
        & ~pl.col("flags").list.contains("roll_boundary")
        & (pl.col("source") == pl.col("source").shift(1).over("symbol"))
    )
    returns = ordered.with_columns(
        pl.when(consecutive & (previous > 0) & (pl.col("close") > 0))
        .then(pl.col("close") / previous - 1)
        .otherwise(None)
        .alias("return_1m")
    )
    # A gap resets the valid window via null returns. Do not bridge sessions.
    return returns.with_columns(
        pl.col("return_1m")
        .rolling_std(window_size=window, min_samples=window)
        .over("symbol")
        .alias("volatility")
    )
