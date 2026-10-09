"""Dukascopy hourly BI5 quotes: big-endian milliseconds, ask, bid, ask/bid sizes."""

import lzma
import math
import struct
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import httpx
import polars as pl

from xasset.config import Instrument
from xasset.ingest.base import Batch, RawArtifact, download
from xasset.normalize.timebase import utc
from xasset.store.schema import BAR_SCHEMA

TICK = struct.Struct(">IIIff")


def decode_ticks(payload: bytes, hour: datetime, instrument: Instrument) -> pl.DataFrame:
    hour = utc(hour)
    if hour.minute or hour.second or hour.microsecond or not instrument.price_scale:
        raise ValueError("Dukascopy needs a UTC hour and explicit price scale")
    if not payload:
        return pl.DataFrame(schema=BAR_SCHEMA)
    decoder = lzma.LZMADecompressor()
    decoded = decoder.decompress(payload, max_length=64 * 1024 * 1024 + 1)
    if not decoder.eof or decoder.unused_data or len(decoded) > 64 * 1024 * 1024:
        raise ValueError("Truncated or oversized Dukascopy LZMA payload")
    if len(decoded) % TICK.size:
        raise ValueError("Dukascopy payload has a partial tick record")
    minutes: dict[int, list[tuple[float, float, float]]] = {}
    previous = -1
    for elapsed, ask_raw, bid_raw, ask_size, bid_size in TICK.iter_unpack(decoded):
        if elapsed >= 3_600_000 or elapsed < previous:
            raise ValueError("Dukascopy tick timestamp out of bounds or out of order")
        if bid_raw > ask_raw or any(not math.isfinite(x) or x < 0 for x in (ask_size, bid_size)):
            raise ValueError("Invalid Dukascopy quote or quoted size")
        previous = elapsed
        bid, ask = bid_raw / instrument.price_scale, ask_raw / instrument.price_scale
        minutes.setdefault(elapsed // 60_000, []).append((bid, ask, (bid + ask) / 2))
    rows: list[dict[str, Any]] = []
    for minute, ticks in minutes.items():
        row: dict[str, Any] = {
            "symbol": instrument.id,
            "asset_class": instrument.asset_class,
            "ts_end": hour + timedelta(minutes=minute + 1),
            "volume": None,
            "source": "dukascopy",
            "flags": ["single_source", "quote_mid", "volume_missing"],
        }
        if instrument.proxy:
            row["flags"].append("cfd_proxy")
        for index, side in enumerate(("bid_", "ask_", "")):
            prices = [tick[index] for tick in ticks]
            row.update(
                {
                    side + "open": prices[0],
                    side + "high": max(prices),
                    side + "low": min(prices),
                    side + "close": prices[-1],
                }
            )
        rows.append(row)
    return pl.DataFrame(rows, schema=BAR_SCHEMA).sort("ts_end")


def batches(
    client: httpx.Client, instrument: Instrument, start: datetime, end: datetime, as_of: datetime
) -> Iterator[Batch]:
    start, end, as_of = utc(start), utc(end), utc(as_of)
    if not instrument.dukascopy_symbol or "dukascopy" not in instrument.sources:
        raise ValueError("Instrument has no configured Dukascopy mapping")
    if (
        start.minute
        or start.second
        or end.minute
        or end.second
        or start.microsecond
        or end.microsecond
    ):
        raise ValueError("Dukascopy ingestion ranges must use whole UTC hours")
    if end > as_of.replace(minute=0, second=0, microsecond=0):
        raise ValueError("Dukascopy ingestion requires completed hours")
    if end - start > timedelta(days=31):
        raise ValueError("Limit Dukascopy ingestion to 31 days per run")
    hour = start
    while hour < end:
        # Dukascopy's URL month is zero-based, unlike calendar dates.
        url = (
            f"https://datafeed.dukascopy.com/datafeed/{instrument.dukascopy_symbol}/"
            f"{hour.year}/{hour.month - 1:02d}/{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
        )
        payload = download(client, url)
        bars = decode_ticks(payload, hour, instrument)
        yield Batch(
            hour,
            hour + timedelta(hours=1),
            bars,
            [RawArtifact(payload, "bi5", url)],
            {"empty_hour": bars.is_empty()},
        )
        hour += timedelta(hours=1)
        if hour < end:
            time.sleep(0.3)  # serialize requests; avoid bursts against the public feed
