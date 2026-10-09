"""One-minute bars with optional quote midpoints and aggressor-labelled flow.

A bar labelled ``end`` covers trades and quotes in ``[end - 1 minute, end)`` and is
usable only from ``available_at``. Trade-only bars (historical archives) leave the
quote fields empty; strategies then run their separately registered trade-bar
variant, never silently substituting trades for midpoints.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import polars as pl

MINUTE = timedelta(minutes=1)
Basis = Literal["mid", "trade"]

FLOW_SCHEMA = pl.Schema(
    {
        "symbol": pl.String(),
        "ts_end": pl.Datetime("us", "UTC"),
        "available_at": pl.Datetime("us", "UTC"),
        "open": pl.Float64(),
        "high": pl.Float64(),
        "low": pl.Float64(),
        "close": pl.Float64(),
        "volume": pl.Float64(),
        "notional": pl.Float64(),
        "trades": pl.Int64(),
        "buy_notional": pl.Float64(),
        "sell_notional": pl.Float64(),
        "unclassified_notional": pl.Float64(),
        "mid_open": pl.Float64(),
        "mid_high": pl.Float64(),
        "mid_low": pl.Float64(),
        "mid_close": pl.Float64(),
        "bid_close": pl.Float64(),
        "ask_close": pl.Float64(),
        "spread_rel": pl.Float64(),
        "quote_age": pl.Float64(),
        "source": pl.String(),
    }
)


@dataclass(slots=True)
class FlowBar:
    symbol: str
    end: datetime
    available_at: datetime
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float = 0.0
    notional: float = 0.0
    trades: int | None = None
    buy_notional: float | None = None
    sell_notional: float | None = None
    unclassified_notional: float | None = None
    mid_open: float | None = None
    mid_high: float | None = None
    mid_low: float | None = None
    mid_close: float | None = None
    bid_close: float | None = None
    ask_close: float | None = None
    spread_rel: float | None = None
    quote_age: float | None = None
    source: str = ""
    # Live only (not persisted): relative spreads sampled once per second.
    spread_samples: tuple[float, ...] | None = None

    @property
    def start(self) -> datetime:
        return self.end - MINUTE

    def price(self, basis: Basis) -> tuple[float, float, float, float] | None:
        """(open, high, low, close) on the requested basis, or None when unobserved."""
        if basis == "mid":
            values = (self.mid_open, self.mid_high, self.mid_low, self.mid_close)
        else:
            values = (self.open, self.high, self.low, self.close)
        if any(value is None or not math.isfinite(value) or value <= 0 for value in values):
            return None
        o, h, lo, c = (float(value) for value in values if value is not None)
        return o, h, lo, c

    @property
    def classified_notional(self) -> float | None:
        if self.buy_notional is None or self.sell_notional is None:
            return None
        return self.buy_notional + self.sell_notional

    @property
    def vwap(self) -> float | None:
        if self.volume > 0 and self.notional > 0:
            return self.notional / self.volume
        return None


def validate(bar: FlowBar) -> list[str]:
    """Structural problems that make a bar unusable; empty means valid."""
    problems = []
    if bar.end.second or bar.end.microsecond:
        problems.append("bar end is not a whole minute")
    if bar.available_at < bar.end:
        problems.append("bar available before it completed")
    trade = bar.price("trade")
    if trade is not None:
        o, h, lo, c = trade
        if not lo <= min(o, c) <= max(o, c) <= h:
            problems.append("trade OHLC out of order")
    mid = bar.price("mid")
    if mid is not None:
        o, h, lo, c = mid
        if not lo <= min(o, c) <= max(o, c) <= h:
            problems.append("midpoint OHLC out of order")
    if bar.bid_close is not None and bar.ask_close is not None and bar.bid_close > bar.ask_close:
        problems.append("crossed quote")
    if bar.volume < 0 or bar.notional < 0:
        problems.append("negative volume")
    for name in ("buy_notional", "sell_notional", "unclassified_notional"):
        value = getattr(bar, name)
        if value is not None and value < -1e-9:
            problems.append(f"negative {name}")
    classified = bar.classified_notional
    if classified is not None:
        total = classified + (bar.unclassified_notional or 0.0)
        if bar.notional > 0 and abs(total - bar.notional) > max(1e-6, 1e-6 * bar.notional):
            problems.append("flow components do not sum to total notional")
    return problems


def to_frame(bars: list[FlowBar]) -> pl.DataFrame:
    rows = [
        {
            "symbol": bar.symbol,
            "ts_end": bar.end,
            "available_at": bar.available_at,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "notional": bar.notional,
            "trades": bar.trades,
            "buy_notional": bar.buy_notional,
            "sell_notional": bar.sell_notional,
            "unclassified_notional": bar.unclassified_notional,
            "mid_open": bar.mid_open,
            "mid_high": bar.mid_high,
            "mid_low": bar.mid_low,
            "mid_close": bar.mid_close,
            "bid_close": bar.bid_close,
            "ask_close": bar.ask_close,
            "spread_rel": bar.spread_rel,
            "quote_age": bar.quote_age,
            "source": bar.source,
        }
        for bar in bars
    ]
    return pl.DataFrame(rows, schema=FLOW_SCHEMA)


def from_frame(frame: pl.DataFrame) -> list[FlowBar]:
    frame = frame.select(FLOW_SCHEMA.names())
    output = []
    for row in frame.iter_rows(named=True):
        output.append(
            FlowBar(
                symbol=row["symbol"],
                end=row["ts_end"],
                available_at=row["available_at"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"] or 0.0,
                notional=row["notional"] or 0.0,
                trades=row["trades"],
                buy_notional=row["buy_notional"],
                sell_notional=row["sell_notional"],
                unclassified_notional=row["unclassified_notional"],
                mid_open=row["mid_open"],
                mid_high=row["mid_high"],
                mid_low=row["mid_low"],
                mid_close=row["mid_close"],
                bid_close=row["bid_close"],
                ask_close=row["ask_close"],
                spread_rel=row["spread_rel"],
                quote_age=row["quote_age"],
                source=row["source"],
            )
        )
    return output


def lab_bar_path(root: Path, source: str, symbol: str) -> Path:
    return root / "lab" / "bars" / source / symbol


def read_lab_bars(
    root: Path, source: str, symbol: str, start: datetime, end: datetime
) -> pl.DataFrame:
    """Stored bars with ``start < ts_end <= end`` for one source and symbol."""
    files = sorted(lab_bar_path(root, source, symbol).glob("*.parquet"))
    if not files:
        return pl.DataFrame(schema=FLOW_SCHEMA)
    return (
        pl.scan_parquet([str(path) for path in files])
        .filter((pl.col("ts_end") > start) & (pl.col("ts_end") <= end))
        .collect()
        .select(FLOW_SCHEMA.names())
        .sort("ts_end")
    )


def from_store_bars(frame: pl.DataFrame, symbol: str, latency: timedelta) -> pl.DataFrame:
    """Convert canonical trade bars (OHLCV, no aggressor labels) to the lab schema.

    Notional is approximated as volume times the bar's typical price because the
    canonical store has no traded value; flows stay unknown (null), never zero.
    """
    typical = (pl.col("high") + pl.col("low") + pl.col("close")) / 3
    return frame.select(
        pl.lit(symbol).alias("symbol"),
        pl.col("ts_end"),
        (pl.col("ts_end") + latency).alias("available_at"),
        "open",
        "high",
        "low",
        "close",
        pl.col("volume").cast(pl.Float64),
        (pl.col("volume").cast(pl.Float64) * typical).alias("notional"),
        pl.lit(None, dtype=pl.Int64).alias("trades"),
        *(
            pl.lit(None, dtype=pl.Float64).alias(name)
            for name in (
                "buy_notional",
                "sell_notional",
                "unclassified_notional",
                "mid_open",
                "mid_high",
                "mid_low",
                "mid_close",
                "bid_close",
                "ask_close",
                "spread_rel",
                "quote_age",
            )
        ),
        pl.col("source"),
    ).select(FLOW_SCHEMA.names())
