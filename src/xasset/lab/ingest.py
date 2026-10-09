"""Checksum-verified Binance archives with aggressor-labelled flow and funding.

Binance klines carry total base and quote volume plus the taker-buy share. Every
Binance trade has a taker, so buyer-initiated notional is the taker-buy quote volume
and seller-initiated notional is the remainder: aggressor coverage is 100% and the
unclassified component is zero. Klines contain trades only, no quotes, so these bars
feed trade-bar strategy variants.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
import polars as pl

from xasset.ingest.base import download
from xasset.lab.bars import FLOW_SCHEMA, lab_bar_path
from xasset.store.writer import atomic_path, writer_lock

Market = Literal["spot", "um"]
BASE = "https://data.binance.vision/data"


def month_starts(start: datetime, end: datetime) -> Iterator[datetime]:
    month = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month < end:
        yield month
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)


def verify(payload: bytes, checksum: bytes, filename: str) -> None:
    parts = checksum.decode("ascii").strip().split()
    if (
        len(parts) != 2
        or re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]) is None
        or parts[1].lstrip("*") != filename
        or hashlib.sha256(payload).hexdigest() != parts[0].lower()
    ):
        raise ValueError(f"Binance checksum or filename mismatch: {filename}")


def csv_rows(payload: bytes, filename: str) -> Iterator[list[str]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        expected = filename.removesuffix(".zip") + ".csv"
        if archive.namelist() != [expected]:
            raise ValueError("Binance archive must contain exactly the expected CSV")
        if archive.getinfo(expected).file_size > 256 * 1024 * 1024:
            raise ValueError("Uncompressed Binance archive is too large")
        with archive.open(expected) as stream:
            yield from csv.reader(io.TextIOWrapper(stream, encoding="utf-8"))


def decode_klines(payload: bytes, filename: str, symbol: str, source: str) -> pl.DataFrame:
    rows = []
    for index, row in enumerate(csv_rows(payload, filename)):
        if index == 0 and row and row[0] == "open_time":
            continue  # USD-M files include a header row; spot files do not.
        if len(row) != 12:
            raise ValueError(f"Binance kline row {index + 1} must contain 12 fields")
        opened, closed = int(row[0]), int(row[6])
        # Spot archives switched from milliseconds to microseconds on 2025-01-01.
        divisor = 1_000_000 if opened >= 100_000_000_000_000 else 1_000
        if opened % (60 * divisor) or closed != opened + 60 * divisor - 1:
            raise ValueError("Binance kline has invalid 1-minute bounds")
        end = datetime.fromtimestamp(opened / divisor, tz=UTC) + timedelta(minutes=1)
        o, h, lo, c = (float(value) for value in row[1:5])
        volume, notional = float(row[5]), float(row[7])
        buy_notional = float(row[10])
        if not (0 <= buy_notional <= notional * (1 + 1e-12) + 1e-12):
            raise ValueError("Taker-buy notional exceeds total notional")
        buy_notional = min(buy_notional, notional)
        rows.append(
            {
                "symbol": symbol,
                "ts_end": end,
                # Archives carry no receipt time; replay applies its declared latency.
                "available_at": end,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": volume,
                "notional": notional,
                "trades": int(row[8]),
                "buy_notional": buy_notional,
                "sell_notional": notional - buy_notional,
                "unclassified_notional": 0.0,
                "source": source,
            }
        )
    frame = pl.DataFrame(rows, schema_overrides={k: FLOW_SCHEMA[k] for k in FLOW_SCHEMA.names()})
    for name in FLOW_SCHEMA.names():
        if name not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=FLOW_SCHEMA[name]).alias(name))
    return frame.select(FLOW_SCHEMA.names()).sort("ts_end")


def raw_archive(root: Path, source: str, symbol: str, payload: bytes, suffix: str) -> Path:
    digest = hashlib.sha256(payload).hexdigest()
    target = root / "lab" / "raw" / source / symbol / f"{digest}.{suffix}"
    if not target.exists():
        with atomic_path(target) as temporary:
            temporary.write_bytes(payload)
    return target


def ingest_klines(
    client: httpx.Client,
    root: Path,
    market: Market,
    symbol: str,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    """Download completed monthly 1-minute klines into ``data/lab/bars``."""
    source = f"binance-{market}"
    path = "spot" if market == "spot" else "futures/um"
    months: list[dict[str, object]] = []
    for month in month_starts(start, end):
        filename = f"{symbol}-1m-{month:%Y-%m}.zip"
        target = lab_bar_path(root, source, symbol) / f"{month:%Y-%m}.parquet"
        if target.exists():
            months.append({"month": f"{month:%Y-%m}", "rows": None, "cached": True})
            continue
        url = f"{BASE}/{path}/monthly/klines/{symbol}/1m/{filename}"
        checksum = download(client, url + ".CHECKSUM", limit=1024)
        payload = download(client, url, limit=128 * 1024 * 1024)
        verify(payload, checksum, filename)
        frame = decode_klines(payload, filename, symbol, source)
        with writer_lock(root / "lab"):
            raw_archive(root, source, symbol, payload, "zip")
            with atomic_path(target) as temporary:
                frame.write_parquet(temporary, compression="zstd")
        months.append({"month": f"{month:%Y-%m}", "rows": frame.height, "cached": False})
    return {"symbol": symbol, "source": source, "months": months}


def ingest_funding(
    client: httpx.Client, root: Path, symbol: str, start: datetime, end: datetime
) -> pl.DataFrame:
    """USD-M funding events: (time, rate). Positive rate means longs pay shorts."""
    frames = []
    for month in month_starts(start, end):
        filename = f"{symbol}-fundingRate-{month:%Y-%m}.zip"
        target = root / "lab" / "funding" / symbol / f"{month:%Y-%m}.parquet"
        if target.exists():
            frames.append(pl.read_parquet(target))
            continue
        url = f"{BASE}/futures/um/monthly/fundingRate/{symbol}/{filename}"
        checksum = download(client, url + ".CHECKSUM", limit=1024)
        payload = download(client, url, limit=16 * 1024 * 1024)
        verify(payload, checksum, filename)
        rows = []
        for index, row in enumerate(csv_rows(payload, filename)):
            if index == 0 and row and not row[0].isdigit():
                continue
            if len(row) < 3:
                raise ValueError("Funding row must contain time, interval and rate")
            stamp = int(row[0])
            divisor = 1_000_000 if stamp >= 100_000_000_000_000 else 1_000
            rows.append(
                {
                    "symbol": symbol,
                    "time": datetime.fromtimestamp(stamp / divisor, tz=UTC),
                    "rate": float(row[2]),
                }
            )
        frame = pl.DataFrame(
            rows,
            schema={"symbol": pl.String, "time": pl.Datetime("us", "UTC"), "rate": pl.Float64},
        )
        with writer_lock(root / "lab"):
            raw_archive(root, "binance-um-funding", symbol, payload, "zip")
            with atomic_path(target) as temporary:
                frame.write_parquet(temporary, compression="zstd")
        frames.append(frame)
    if not frames:
        return pl.DataFrame(
            schema={"symbol": pl.String, "time": pl.Datetime("us", "UTC"), "rate": pl.Float64}
        )
    return pl.concat(frames).sort("time")


def monthly_notional(client: httpx.Client, market: Market, symbol: str, month: datetime) -> float:
    """Total quote notional for one completed month from the daily kline archive."""
    filename = f"{symbol}-1d-{month:%Y-%m}.zip"
    path = "spot" if market == "spot" else "futures/um"
    url = f"{BASE}/{path}/monthly/klines/{symbol}/1d/{filename}"
    try:
        checksum = download(client, url + ".CHECKSUM", limit=1024)
        payload = download(client, url, limit=8 * 1024 * 1024)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return 0.0  # Not listed in that month: ineligible for a point-in-time universe.
        raise
    verify(payload, checksum, filename)
    total = 0.0
    for index, row in enumerate(csv_rows(payload, filename)):
        if index == 0 and row and row[0] == "open_time":
            continue
        total += float(row[7])
    return total
