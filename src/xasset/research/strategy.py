"""Causal, long-only cross-asset lead/lag baseline; no fitted hidden state."""

from datetime import datetime, timedelta
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from xasset.config import Instrument
from xasset.normalize.resample import resample
from xasset.research.contracts import Parameter

FREQUENCY_MINUTES = {"1m": 1, "5m": 5, "1h": 60, "1d": 1440}


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    driver: str
    follower: str
    lookback: int = Field(ge=1, strict=True)
    threshold: float = Field(gt=0)
    direction: int = Field(default=1, strict=True)
    hold_minutes: int = Field(gt=0, strict=True)
    stop_pct: float = Field(gt=0, lt=1)
    target_pct: float = Field(gt=0)
    anchor: Literal["previous_close", "regional_session"] = "previous_close"
    event_kind: str = "eia_petroleum"
    observation_minutes: int = Field(default=5, ge=1, strict=True)

    @model_validator(mode="after")
    def valid(self) -> "Parameters":
        if self.driver == self.follower or self.direction not in {-1, 1}:
            raise ValueError("Use distinct driver/follower symbols and direction +1 or -1")
        return self


def signals(
    bars: pl.DataFrame,
    instrument: Instrument,
    frequency: str,
    parameters: dict[str, Parameter],
) -> dict[datetime, datetime]:
    """Map next-minute opening instants to the completed driver signal instant.

    No crossing gaps, session breaks, provider seams, zero-volume bars or rolls.
    The close at t may first trade the following bar [t, t+1m).
    """
    params = Parameters.model_validate(parameters)
    frame = bars.filter(pl.col("symbol") == params.driver).sort("ts_end")
    if frame.is_empty():
        return {}
    if frequency != "1m":
        if instrument.asset_class in {"fx", "cfd", "index", "futures"}:
            frame = quote_aggregates(frame, frequency)
        else:
            frame = resample(frame, instrument, frequency)
    history: list[float] = []
    previous: datetime | None = None
    source: str | None = None
    output: dict[datetime, datetime] = {}
    step = timedelta(minutes=FREQUENCY_MINUTES[frequency])
    for row in frame.iter_rows(named=True):
        stamp = row["ts_end"]
        if (
            previous is None
            or stamp - previous != step
            or source != row["source"]
            or "roll_boundary" in row["flags"]
        ):
            history.clear()
        previous, source = stamp, row["source"]
        quoted = instrument.asset_class in {"fx", "cfd", "index", "futures"}
        if row["close"] <= 0 or not observable(row, quoted):
            history.clear()
            continue
        history.append(row["close"])
        if len(history) > params.lookback:
            change = history[-1] / history[-params.lookback - 1] - 1
            if change * params.direction >= params.threshold:
                output[stamp] = stamp
            history = history[-params.lookback :]
    return output


def observable(row: dict[str, Any], quoted: bool) -> bool:
    if quoted:
        return row["bid_close"] is not None and row["ask_close"] is not None
    volume = row["volume"]
    return isinstance(volume, (int, float)) and volume > 0


def quote_aggregates(bars: pl.DataFrame, frequency: str) -> pl.DataFrame:
    """Complete UTC driver windows only; no claim about quote-market sessions.

    These are signal inputs, never executable instruments or replacement exchange
    bars. Missing quote minutes or provider seams omit the entire window.
    """
    from xasset.store.schema import BAR_SCHEMA

    window = FREQUENCY_MINUTES[frequency]
    groups: dict[datetime, list[dict[str, Any]]] = {}
    for row in bars.sort("ts_end").iter_rows(named=True):
        stamp = row["ts_end"]
        start = stamp - timedelta(minutes=1)
        bucket = start.replace(hour=0, minute=0) + timedelta(
            minutes=((start.hour * 60 + start.minute) // window + 1) * window
        )
        groups.setdefault(bucket, []).append(row)
    output = []
    for end, rows in groups.items():
        expected = [end - timedelta(minutes=window - 1 - index) for index in range(window)]
        if (
            [row["ts_end"] for row in rows] != expected
            or len({str(row["source"]) for row in rows}) != 1
            or any(not observable(row, True) for row in rows)
        ):
            continue
        row = dict(rows[-1])
        row["ts_end"] = end
        # The causal strategy consumes only close/quotes/source/flags. Full OHLC
        # remains consistent for inspecting its intermediate signal windows.
        row["open"] = rows[0]["open"]
        row["high"] = max(float(item["high"]) for item in rows)
        row["low"] = min(float(item["low"]) for item in rows)
        row["flags"] = sorted({str(flag) for item in rows for flag in item["flags"]})
        for side in ("bid", "ask"):
            row[f"{side}_open"] = rows[0][f"{side}_open"]
            row[f"{side}_high"] = max(item[f"{side}_high"] for item in rows)
            row[f"{side}_low"] = min(item[f"{side}_low"] for item in rows)
        output.append(row)
    return pl.DataFrame(output, schema=BAR_SCHEMA)
