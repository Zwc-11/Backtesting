"""Checksum-verified Binance monthly spot and USD-M 1-minute archives."""

import csv
import hashlib
import io
import re
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import polars as pl

from xasset.config import Instrument
from xasset.ingest.base import Batch, RawArtifact, download
from xasset.normalize.timebase import utc
from xasset.store.schema import BAR_SCHEMA


def next_month(value: datetime) -> datetime:
    return (value.replace(day=28) + timedelta(days=4)).replace(day=1)


def decode_archive(
    payload: bytes, checksum: bytes, filename: str, instrument: Instrument
) -> pl.DataFrame:
    parts = checksum.decode("ascii").strip().split()
    if (
        len(parts) != 2
        or re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]) is None
        or parts[1].lstrip("*") != filename
        or hashlib.sha256(payload).hexdigest() != parts[0].lower()
    ):
        raise ValueError("Binance archive checksum or filename mismatch")
    rows = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        expected = filename.removesuffix(".zip") + ".csv"
        if archive.namelist() != [expected]:
            raise ValueError("Binance archive must contain exactly the expected CSV")
        if archive.getinfo(expected).file_size > 64 * 1024 * 1024:
            raise ValueError("Uncompressed Binance archive is too large")
        with archive.open(expected) as stream:
            for index, row in enumerate(csv.reader(io.TextIOWrapper(stream, encoding="utf-8"))):
                if index == 0 and row and row[0] == "open_time":
                    continue  # USD-M files include a header; spot files do not.
                if len(row) != 12:
                    raise ValueError(f"Binance row {index + 1} must contain 12 fields")
                opened, closed = int(row[0]), int(row[6])
                # Binance switched spot timestamps to microseconds on 2025-01-01.
                divisor = 1_000_000 if opened >= 100_000_000_000_000 else 1_000
                if opened % (60 * divisor) or closed != opened + 60 * divisor - 1:
                    raise ValueError("Binance row has invalid 1-minute timestamp bounds")
                stamp = datetime.fromtimestamp(opened / divisor, tz=UTC) + timedelta(minutes=1)
                rows.append(
                    {
                        "symbol": instrument.id,
                        "asset_class": instrument.asset_class,
                        "ts_end": stamp,
                        **dict(
                            zip(("open", "high", "low", "close"), map(float, row[1:5]), strict=True)
                        ),
                        "volume": float(row[5]),
                        "source": "binance",
                        "flags": [
                            "single_source",
                            "unadjusted",
                            "exchange_volume",
                            "spot" if instrument.binance_market == "spot" else "perpetual",
                        ],
                    }
                )
    return pl.DataFrame(rows, schema=BAR_SCHEMA).sort("ts_end")


def batches(
    client: httpx.Client, instrument: Instrument, start: datetime, end: datetime, as_of: datetime
) -> Iterator[Batch]:
    start, end = utc(start), utc(end)
    month = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    completed_month = utc(as_of).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if not instrument.binance_symbol or "binance" not in instrument.sources:
        raise ValueError("Instrument has no configured Binance mapping")
    if end > completed_month:
        raise ValueError("Monthly Binance ingestion requires fully completed calendar months")
    market = "spot" if instrument.binance_market == "spot" else "futures/um"
    while month < end:
        filename = f"{instrument.binance_symbol}-1m-{month:%Y-%m}.zip"
        url = (
            f"https://data.binance.vision/data/{market}/monthly/klines/"
            f"{instrument.binance_symbol}/1m/{filename}"
        )
        checksum = download(client, url + ".CHECKSUM", limit=1024)
        payload = download(client, url)
        bars = decode_archive(payload, checksum, filename, instrument)
        bars = bars.filter((pl.col("ts_end") > start) & (pl.col("ts_end") <= end))
        yield Batch(
            max(start, month),
            min(end, next_month(month)),
            bars,
            [
                RawArtifact(checksum, "CHECKSUM", url + ".CHECKSUM"),
                RawArtifact(payload, "zip", url),
            ],
            {"checksum_verified": True},
        )
        month = next_month(month)
