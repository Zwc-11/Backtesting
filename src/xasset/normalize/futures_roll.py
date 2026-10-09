"""Explicit, unadjusted contract chains. No backward adjustments or inferred rolls."""

from dataclasses import dataclass
from datetime import datetime

import polars as pl

from xasset.normalize.timebase import utc
from xasset.qc.checks import check_bars
from xasset.store.schema import BAR_SCHEMA


@dataclass(frozen=True)
class Roll:
    contract: str
    effective_at: datetime  # new contract used for minutes OPENING at/after this instant
    known_at: datetime

    def __post_init__(self) -> None:
        utc(self.effective_at)
        utc(self.known_at)
        if self.effective_at.second or self.effective_at.microsecond:
            raise ValueError("Roll times must be minute-aligned")
        if self.known_at > self.effective_at:
            raise ValueError("Roll schedules must be known no later than their effective time")


def continuous(
    contracts: dict[str, pl.DataFrame], schedule: list[Roll], root: str, as_of: datetime
) -> pl.DataFrame:
    as_of = utc(as_of)
    if not schedule or not root:
        raise ValueError("Provide a root and explicit roll schedule")
    ordered = sorted(schedule, key=lambda row: row.effective_at)
    if len({row.effective_at for row in ordered}) != len(ordered):
        raise ValueError("Roll instants must be unique")
    frames = []
    for index, roll in enumerate(ordered):
        if roll.effective_at >= as_of:
            continue
        if roll.contract not in contracts:
            raise ValueError(f"Missing contract bars: {roll.contract}")
        frame = contracts[roll.contract]
        if (
            not check_bars(frame).ok
            or frame.filter(
                (pl.col("symbol") != roll.contract)
                | (pl.col("asset_class") != "futures")
                | pl.col("flags").list.contains("front_month_proxy")
                | pl.col("flags").list.contains("cfd_proxy")
            ).height
        ):
            raise ValueError("Use validated explicit contract bars, never front-month/CFD proxies")
        limit = min(as_of, ordered[index + 1].effective_at) if index + 1 < len(ordered) else as_of
        selected = frame.filter(
            (pl.col("ts_end") > roll.effective_at) & (pl.col("ts_end") <= limit)
        ).sort("ts_end")
        if selected.is_empty():
            raise ValueError(f"No bars in scheduled contract interval: {roll.contract}")
        first = selected["ts_end"][0]
        selected = selected.with_columns(
            pl.lit(root).alias("symbol"),
            pl.lit(roll.contract).alias("contract_symbol"),
            pl.when((pl.col("ts_end") == first) & pl.lit(index > 0))
            .then(pl.col("flags").list.set_union(["roll_boundary"]))
            .otherwise(pl.col("flags"))
            .alias("flags"),
        )
        frames.append(selected)
    return (
        pl.concat(frames).sort("ts_end")
        if frames
        else pl.DataFrame(schema={**dict(BAR_SCHEMA), "contract_symbol": pl.String})
    )
