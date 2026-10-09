"""Explicit, as-of split adjustment; never infer corporate actions from price jumps."""

import math
from dataclasses import dataclass
from datetime import datetime

import polars as pl

from xasset.normalize.timebase import utc
from xasset.qc.checks import check_bars
from xasset.store.schema import QUOTE_COLUMNS


@dataclass(frozen=True)
class Split:
    symbol: str
    effective_at: datetime  # first opening instant trading on the new share basis
    known_at: datetime  # timestamp at which this action was actually available
    new_shares_per_old: float

    def __post_init__(self) -> None:
        utc(self.effective_at)
        utc(self.known_at)
        if not math.isfinite(self.new_shares_per_old) or self.new_shares_per_old <= 0:
            raise ValueError("Split ratio must be finite and positive")


def adjust_splits(bars: pl.DataFrame, actions: list[Split], as_of: datetime) -> pl.DataFrame:
    as_of = utc(as_of)
    if not check_bars(bars).ok:
        raise ValueError("Split adjustment requires structurally valid raw bars")
    if bars.filter(pl.col("flags").list.contains("split_adjusted")).height:
        raise ValueError("Do not apply split adjustments twice; start with raw bars")
    if len({(action.symbol, action.effective_at) for action in actions}) != len(actions):
        raise ValueError("Duplicate split events")
    result = bars.filter(pl.col("ts_end") <= as_of)
    for action in sorted(actions, key=lambda item: item.effective_at):
        if action.known_at > as_of or action.effective_at > as_of:
            continue
        if bars.filter(
            (pl.col("symbol") == action.symbol) & ~pl.col("asset_class").is_in(["equity", "etf"])
        ).height:
            raise ValueError("Share split adjustment is only defined for equities and ETFs")
        earlier = (pl.col("symbol") == action.symbol) & (pl.col("ts_end") <= action.effective_at)
        result = result.with_columns(
            [
                pl.when(earlier)
                .then(pl.col(name) / action.new_shares_per_old)
                .otherwise(pl.col(name))
                .alias(name)
                for name in ["open", "high", "low", "close", *QUOTE_COLUMNS]
            ]
            + [
                pl.when(earlier)
                .then(pl.col("volume") * action.new_shares_per_old)
                .otherwise(pl.col("volume"))
                .alias("volume"),
                pl.when(earlier)
                .then(
                    pl.col("flags")
                    .list.set_difference(["unadjusted"])
                    .list.set_union(["split_adjusted"])
                )
                .otherwise(pl.col("flags"))
                .alias("flags"),
            ]
        )
    return result
