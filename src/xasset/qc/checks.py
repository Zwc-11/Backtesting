"""Structural gate, distinct from future cross-source and corporate-action checks."""

from dataclasses import asdict, dataclass
from typing import Any

import polars as pl

from xasset.store.schema import BAR_SCHEMA, QUOTE_COLUMNS


@dataclass(frozen=True)
class QualityReport:
    rows: int
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


def check_bars(bars: pl.DataFrame) -> QualityReport:
    errors: list[str] = []
    warnings: list[str] = []
    if bars.schema != BAR_SCHEMA:
        return QualityReport(bars.height, ("schema mismatch",), ())
    if bars.is_empty():
        return QualityReport(0, ("no bars",), ())
    required = [
        name for name in BAR_SCHEMA.names() if name != "volume" and name not in QUOTE_COLUMNS
    ]
    if bars.select(pl.any_horizontal(pl.col(required).is_null()).any()).item():
        errors.append("null required fields")
    if bars.select(pl.struct("symbol", "ts_end").is_duplicated().any()).item():
        errors.append("duplicate symbol/timestamp")
    if bars.filter(
        pl.any_horizontal(~pl.col(name).is_finite() for name in ("open", "high", "low", "close"))
        | (pl.col("high") < pl.max_horizontal("open", "close", "low"))
        | (pl.col("low") > pl.min_horizontal("open", "close", "high"))
    ).height:
        errors.append("invalid OHLC")
    # Negative futures prices are possible; never impose a universal price floor.
    if bars.filter(pl.col("volume") < 0).height:
        errors.append("negative volume")
    if bars.filter(pl.col("volume").is_not_null() & ~pl.col("volume").is_finite()).height:
        errors.append("nonfinite volume")
    quoted = bars.filter(pl.any_horizontal(pl.col(QUOTE_COLUMNS).is_not_null()))
    if (
        quoted.height
        and quoted.filter(
            pl.any_horizontal(pl.col(QUOTE_COLUMNS).is_null())
            | pl.any_horizontal(~pl.col(QUOTE_COLUMNS).is_finite())
            | pl.any_horizontal(
                pl.col(f"bid_{name}") > pl.col(f"ask_{name}")
                for name in ("open", "high", "low", "close")
            )
            | pl.any_horizontal(
                (
                    pl.col(f"{side}_high")
                    < pl.max_horizontal(f"{side}_open", f"{side}_close", f"{side}_low")
                )
                | (
                    pl.col(f"{side}_low")
                    > pl.min_horizontal(f"{side}_open", f"{side}_close", f"{side}_high")
                )
                for side in ("bid", "ask")
            )
        ).height
    ):
        errors.append("invalid bid/ask OHLC")
    if bars.filter(pl.col("ts_end").dt.epoch("us") % 60_000_000 != 0).height:
        errors.append("timestamps are not minute-aligned")
    zero = bars.filter(pl.col("volume") == 0).height
    missing = bars.filter(pl.col("volume").is_null()).height
    if zero:
        warnings.append(f"{zero} zero-volume bars; ineligible for trade fills")
    if missing:
        warnings.append(f"{missing} bars without reported volume")
    return QualityReport(bars.height, tuple(errors), tuple(warnings))
