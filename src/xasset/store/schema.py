import polars as pl

QUOTE_COLUMNS = [
    f"{side}_{price}" for side in ("bid", "ask") for price in ("open", "high", "low", "close")
]

BAR_SCHEMA = pl.Schema(
    {
        "symbol": pl.String(),
        "asset_class": pl.String(),
        "ts_end": pl.Datetime("us", "UTC"),
        "open": pl.Float64(),
        "high": pl.Float64(),
        "low": pl.Float64(),
        "close": pl.Float64(),
        "volume": pl.Float64(),
        "source": pl.String(),
        "flags": pl.List(pl.String()),
        **{name: pl.Float64() for name in QUOTE_COLUMNS},
    }
)


def empty_bars() -> pl.DataFrame:
    return pl.DataFrame(schema=BAR_SCHEMA)


def read_partition(path: str | list[str]) -> pl.DataFrame:
    """Read v1 integer-volume partitions without modifying them on disk."""
    frame = pl.read_parquet(path, missing_columns="insert")
    for name in QUOTE_COLUMNS:
        if name not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(name))
    return frame.with_columns(pl.col("volume").cast(pl.Float64)).select(BAR_SCHEMA.names())
