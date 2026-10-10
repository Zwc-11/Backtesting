"""Historical replay: stream stored bars minute by minute through the shared runtime."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import polars as pl

from xasset.config import Instrument as StoreInstrument
from xasset.lab.bars import FLOW_SCHEMA, Basis, FlowBar, from_store_bars, read_lab_bars
from xasset.lab.execution import BarBroker
from xasset.lab.ingest import month_starts
from xasset.lab.ledger import Ledger
from xasset.lab.runtime import RunSettings, Runtime
from xasset.lab.strategies import resolve
from xasset.lab.universe import Book, LabInstrument, Universe
from xasset.store.writer import load_bars


@dataclass
class Replay:
    runtime: Runtime
    start: datetime
    end: datetime
    minutes: int
    bars: int


def store_instrument(item: LabInstrument) -> StoreInstrument:
    """The canonical-store identity used to locate an instrument's stored bars."""
    asset_class: Literal["etf", "equity"] = "etf" if item.kind == "etf" else "equity"
    assert item.store_id is not None
    return StoreInstrument(
        id=item.store_id,
        asset_class=asset_class,
        tier="A",
        venue="store",
        calendar=item.calendar,
        session="regular" if item.calendar else "all",
        alpaca_symbol=item.store_id.split("_")[0],
        sources=["alpaca"],
    )


def load_month(
    root: Path,
    universe: Universe,
    start: datetime,
    end: datetime,
    latency: timedelta,
) -> pl.DataFrame:
    frames = []
    loaded = universe.loaded(f"{start:%Y-%m}")
    for item in universe.instruments:
        if loaded is not None and item.id not in loaded:
            continue
        if item.history == "store":
            stored = load_bars(root, store_instrument(item))
            stored = stored.filter((pl.col("ts_end") > start) & (pl.col("ts_end") <= end))
            frame = from_store_bars(stored, item.id, latency)
        else:
            frame = read_lab_bars(root, item.history, item.archive_symbol, start, end)
            frame = frame.with_columns(
                pl.lit(item.id).alias("symbol"), (pl.col("ts_end") + latency).alias("available_at")
            )
        if frame.height:
            frames.append(frame.select(FLOW_SCHEMA.names()))
    if not frames:
        return pl.DataFrame(schema=FLOW_SCHEMA)
    return pl.concat(frames).sort("ts_end", "symbol")


def minutes(frame: pl.DataFrame, chunk: int = 50_000) -> Iterator[tuple[datetime, list[FlowBar]]]:
    """Bars grouped by minute (the frame must be sorted by ``ts_end``).

    Rows are converted to Python objects ``chunk`` rows at a time, so a month of
    minute bars for dozens of instruments never sits in memory as Python lists.
    """
    names = FLOW_SCHEMA.names()
    current: datetime | None = None
    batch: list[FlowBar] = []
    for offset in range(0, frame.height, chunk):
        piece = frame.slice(offset, chunk)
        columns = {name: piece[name].to_list() for name in names}
        for row in range(piece.height):
            end = columns["ts_end"][row]
            if current is not None and end != current:
                yield current, batch
                batch = []
            current = end
            batch.append(
                FlowBar(
                    symbol=columns["symbol"][row],
                    end=end,
                    available_at=columns["available_at"][row],
                    open=columns["open"][row],
                    high=columns["high"][row],
                    low=columns["low"][row],
                    close=columns["close"][row],
                    volume=columns["volume"][row] or 0.0,
                    notional=columns["notional"][row] or 0.0,
                    trades=columns["trades"][row],
                    buy_notional=columns["buy_notional"][row],
                    sell_notional=columns["sell_notional"][row],
                    unclassified_notional=columns["unclassified_notional"][row],
                    mid_open=columns["mid_open"][row],
                    mid_high=columns["mid_high"][row],
                    mid_low=columns["mid_low"][row],
                    mid_close=columns["mid_close"][row],
                    bid_close=columns["bid_close"][row],
                    ask_close=columns["ask_close"][row],
                    spread_rel=columns["spread_rel"][row],
                    quote_age=columns["quote_age"][row],
                    source=columns["source"][row],
                )
            )
    if current is not None:
        yield current, batch


def load_funding(root: Path, universe: Universe) -> dict[str, list[tuple[datetime, float]]]:
    output: dict[str, list[tuple[datetime, float]]] = {}
    for item in universe.instruments:
        if item.kind != "perp":
            continue
        files = sorted((root / "lab" / "funding" / item.archive_symbol).glob("*.parquet"))
        if files:
            frame = pl.read_parquet(files).sort("time")
            output[item.id] = list(
                zip(frame["time"].to_list(), frame["rate"].to_list(), strict=True)
            )
    return output


def replay(
    root: Path,
    book: Book,
    universe: Universe,
    start: datetime,
    end: datetime,
    settings: RunSettings,
    ledger: Ledger | None = None,
    strategies: list[str] | None = None,
) -> Replay:
    runtime = Runtime(book, universe, resolve(strategies or book.strategies), settings, ledger)

    def bounds() -> tuple[datetime, datetime] | None:
        session = runtime.market.session
        if session is None or universe.calendar is None:
            return None
        return session.open, session.close

    broker = BarBroker(runtime.accounting, settings.basis, bounds, load_funding(root, universe))
    broker.on_cancel = runtime.cancelled
    runtime.broker = broker
    latency = timedelta(milliseconds=book.execution.bar_settlement_ms)
    count = bars = 0
    for month in month_starts(start, end):
        low = max(start, month)
        high = min(end, (month.replace(day=28) + timedelta(days=4)).replace(day=1))
        frame = load_month(root, universe, low, high, latency)
        for minute_end, batch in minutes(frame):
            runtime.step(minute_end, batch)
            count += 1
            bars += len(batch)
    return Replay(runtime, start, end, count, bars)


def basis_for(universe: Universe) -> Basis:
    """Archives without quotes run the trade-bar variant; recorded live bars use midpoints."""
    histories = {item.history for item in universe.instruments}
    return "mid" if histories <= {"paper-recorded"} else "trade"
