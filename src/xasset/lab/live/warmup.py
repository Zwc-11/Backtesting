"""History that primes a paper book's calibrations before it trades live.

Every calibration is prior-only: a book needs about 20 complete sessions (60 for the
lead-lag stability screen) before setups can arm. Two warm-up sources exist:

* ``archive``: Binance spot one-minute klines (checksum-verified monthly and daily
  archives, then the public market-data API for the latest minutes). These match the
  bars the live aggregator builds from ``aggTrade`` prints, so they prime the
  trade-bar books. Klines carry no quotes and never prime midpoint calibrations.
* ``recorded``: bars this desk recorded from live feeds (trades and quotes). They
  prime midpoint books and every instrument without an archive (Hyperliquid).

Warm-up decisions are discarded; only calibrations, session statistics and models
carry into live trading.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import polars as pl

from xasset.ingest.base import download
from xasset.lab.bars import FLOW_SCHEMA, lab_bar_path, read_lab_bars
from xasset.lab.ingest import BASE, decode_klines, ingest_klines, verify
from xasset.lab.universe import LabInstrument, Universe
from xasset.store.writer import atomic_path, writer_lock

API = "https://data-api.binance.vision/api/v3/klines"
Warmup = Literal["archive", "recorded"]


def recorded_source(item: LabInstrument) -> str | None:
    return f"paper-{item.live_feed}" if item.live_feed else None


def kline_frame(rows: list[list[Any]], symbol: str, source: str, now: datetime) -> pl.DataFrame:
    """API kline rows (12 fields, millisecond times); the forming minute is dropped."""
    records = []
    for row in rows:
        opened, closed = int(row[0]), int(row[6])
        if opened % 60_000 or closed != opened + 59_999:
            raise ValueError("Binance kline has invalid 1-minute bounds")
        end = datetime.fromtimestamp(opened / 1000, tz=UTC) + timedelta(minutes=1)
        if end > now:
            continue
        notional = float(row[7])
        buy = min(float(row[10]), notional)
        records.append(
            {
                "symbol": symbol,
                "ts_end": end,
                "available_at": end,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "notional": notional,
                "trades": int(row[8]),
                "buy_notional": buy,
                "sell_notional": notional - buy,
                "unclassified_notional": 0.0,
                "source": source,
            }
        )
    frame = pl.DataFrame(records, schema_overrides={k: FLOW_SCHEMA[k] for k in FLOW_SCHEMA.names()})
    for name in FLOW_SCHEMA.names():
        if name not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=FLOW_SCHEMA[name]).alias(name))
    return frame.select(FLOW_SCHEMA.names())


def api_klines(
    client: httpx.Client, symbol: str, start: datetime, end: datetime, now: datetime
) -> pl.DataFrame:
    """Completed minutes with ``start < ts_end <= end`` from the public market-data API."""
    frames = []
    cursor = int(start.timestamp() * 1000)
    stop = int(end.timestamp() * 1000)
    while cursor < stop:
        response = client.get(
            API,
            params={
                "symbol": symbol,
                "interval": "1m",
                "startTime": cursor,
                "endTime": stop - 1,
                "limit": 1000,
            },
        )
        if response.status_code == 429:
            time.sleep(float(response.headers.get("retry-after", "5")))
            continue
        response.raise_for_status()
        rows = response.json()
        if not rows:
            break
        frames.append(kline_frame(rows, symbol, "binance-spot-api", now))
        cursor = int(rows[-1][0]) + 60_000
        time.sleep(0.05)  # well inside the public weight limit
    if not frames:
        return pl.DataFrame(schema=FLOW_SCHEMA)
    return pl.concat(frames).filter(pl.col("ts_end") > start).unique("ts_end").sort("ts_end")


def daily_archive(client: httpx.Client, root: Path, symbol: str, day: date) -> bool:
    """Cache one verified daily spot archive; False when Binance has not published it."""
    target = lab_bar_path(root, "binance-spot-daily", symbol) / f"{day:%Y-%m-%d}.parquet"
    if target.exists():
        return True
    filename = f"{symbol}-1m-{day:%Y-%m-%d}.zip"
    url = f"{BASE}/spot/daily/klines/{symbol}/1m/{filename}"
    try:
        checksum = download(client, url + ".CHECKSUM", limit=1024)
        payload = download(client, url, limit=16 * 1024 * 1024)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return False
        raise
    verify(payload, checksum, filename)
    frame = decode_klines(payload, filename, symbol, "binance-spot")
    with writer_lock(root / "lab"), atomic_path(target) as temporary:
        frame.write_parquet(temporary, compression="zstd")
    return True


def prepare_archive(
    client: httpx.Client,
    root: Path,
    symbol: str,
    start: datetime,
    now: datetime,
    progress: Callable[[str], None] = lambda _: None,
) -> pl.DataFrame:
    """Archive coverage for ``(start, now]``: months, then days, then the API tail.

    Returns the API tail (not cached); months and days are cached under ``data/lab``.
    """
    today = now.date()
    month = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    covered_until = start
    this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month < this_month:
        following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        progress(f"{symbol} {month:%Y-%m}")
        try:
            ingest_klines(client, root, "spot", symbol, month, following)
            covered_until = following
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            break  # Not yet published: fall through to daily files.
        month = following
    day = max(covered_until, start).date()
    while day < today:
        progress(f"{symbol} {day:%Y-%m-%d}")
        if not daily_archive(client, root, symbol, day):
            break
        day += timedelta(days=1)
    tail_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    progress(f"{symbol} latest minutes")
    return api_klines(client, symbol, max(tail_start, start), now, now)


def instrument_bars(
    root: Path,
    item: LabInstrument,
    warmup: Warmup,
    start: datetime,
    end: datetime,
    tail: pl.DataFrame | None,
    settlement: timedelta,
) -> pl.DataFrame:
    """Warm-up bars for one instrument, labelled with its universe id."""
    frames: list[pl.DataFrame] = []
    if warmup == "archive" and item.history == "binance-spot":
        symbol = item.archive_symbol
        for source in ("binance-spot", "binance-spot-daily"):
            frames.append(read_lab_bars(root, source, symbol, start, end))
        if tail is not None and tail.height:
            frames.append(tail.filter((pl.col("ts_end") > start) & (pl.col("ts_end") <= end)))
        frames = [f for f in frames if f.height]
        if not frames:
            return pl.DataFrame(schema=FLOW_SCHEMA)
        frame = pl.concat(frames).unique("ts_end", keep="first", maintain_order=True)
        frame = frame.with_columns((pl.col("ts_end") + settlement).alias("available_at"))
    else:
        recorded = recorded_source(item)
        if recorded is None or item.live_symbol is None:
            return pl.DataFrame(schema=FLOW_SCHEMA)
        frame = read_lab_bars(root, recorded, item.live_symbol, start, end)
    return frame.with_columns(pl.lit(item.id).alias("symbol")).select(FLOW_SCHEMA.names())


def warmup_frame(
    root: Path,
    universe: Universe,
    warmup: Warmup,
    start: datetime,
    end: datetime,
    tails: dict[str, pl.DataFrame],
    settlement: timedelta,
) -> pl.DataFrame:
    frames = [
        instrument_bars(root, item, warmup, start, end, tails.get(item.id), settlement)
        for item in universe.instruments
    ]
    frames = [f for f in frames if f.height]
    if not frames:
        return pl.DataFrame(schema=FLOW_SCHEMA)
    return pl.concat(frames).sort("ts_end", "symbol")
