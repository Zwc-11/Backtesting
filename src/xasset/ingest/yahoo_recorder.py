"""Public, unofficial Yahoo chart adapter. No split or futures roll adjustment."""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import polars as pl

from xasset.config import Instrument
from xasset.normalize.calendars import expected_bar_ends
from xasset.normalize.timebase import minute_floor, utc
from xasset.store.schema import BAR_SCHEMA

ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/"


class FeedError(ValueError):
    pass


@dataclass(frozen=True)
class Normalized:
    bars: pl.DataFrame
    null_rows: int
    excluded_rows: int


def fetch(client: httpx.Client, symbol: str, start: datetime, end: datetime) -> bytes:
    start, end = utc(start), utc(end)
    if not timedelta(0) < end - start <= timedelta(days=7):
        raise ValueError("Yahoo requests must cover more than zero and at most seven days")
    for attempt in range(3):
        response = client.get(
            ENDPOINT + quote(symbol, safe=""),
            params={
                "period1": int(start.timestamp()),
                "period2": int(end.timestamp()),
                "interval": "1m",
                "includePrePost": "true",
                "events": "div,splits",
            },
        )
        if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
            response.raise_for_status()
            return response.content
        retry_after = response.headers.get("retry-after", "")
        delay = min(float(retry_after), 30) if retry_after.isdecimal() else 2**attempt
        time.sleep(max(0, delay))
    raise AssertionError("unreachable")


def normalize(
    payload: bytes,
    instrument: Instrument,
    start: datetime,
    end: datetime,
    as_of: datetime,
    finalization_delay: timedelta = timedelta(minutes=20),
) -> Normalized:
    """Only completed, settled minutes enter storage; no forward filling."""
    start, end, as_of = utc(start), utc(end), utc(as_of)
    if finalization_delay < timedelta(0):
        raise ValueError("finalization delay cannot be negative")
    cutoff = min(end, minute_floor(as_of - finalization_delay))
    try:
        body: dict[str, Any] = json.loads(payload)
        chart = body["chart"]
        if chart.get("error"):
            raise FeedError(f"Yahoo chart error: {chart['error']}")
        results = chart["result"]
        if not results:
            raise FeedError("Yahoo returned no chart result")
        result = results[0]
        if result["meta"]["symbol"] != instrument.yahoo_symbol:
            raise FeedError("Yahoo response symbol does not match the requested symbol")
        timestamps = result.get("timestamp") or []
        quotes = result["indicators"]["quote"][0]
        for name in ("open", "high", "low", "close", "volume"):
            if len(quotes.get(name, [])) != len(timestamps):
                raise FeedError(f"Yahoo {name} array length does not match timestamps")
    except (KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise FeedError("Malformed Yahoo chart response") from exc

    allowed = (
        expected_bar_ends(instrument.calendar, start, cutoff)
        if instrument.session == "regular" and instrument.calendar
        else None
    )
    rows: list[dict[str, Any]] = []
    null_rows = excluded_rows = 0
    for index, timestamp in enumerate(timestamps):
        try:
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                raise ValueError("timestamp must be numeric epoch seconds")
            ts_start = datetime.fromtimestamp(timestamp, tz=start.tzinfo)
        except (ValueError, OverflowError, OSError) as exc:
            raise FeedError("Invalid Yahoo timestamp") from exc
        ts_end = ts_start + timedelta(minutes=1)
        if (
            ts_start.second != 0
            or ts_start.microsecond != 0
            or ts_start < start
            or ts_end > cutoff
            or (allowed is not None and ts_end not in allowed)
        ):
            excluded_rows += 1
            continue
        prices = {name: quotes[name][index] for name in ("open", "high", "low", "close")}
        if any(value is None for value in prices.values()):
            null_rows += 1
            continue
        volume = quotes["volume"][index]
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in prices.values()
        ):
            raise FeedError("Invalid Yahoo price type")
        if volume is not None and (isinstance(volume, bool) or not isinstance(volume, int)):
            raise FeedError("Yahoo volume must be an integer or null")
        flags = ["unadjusted", "single_source"]
        if instrument.proxy:
            flags.append("front_month_proxy")
        if volume is None:
            flags.append("volume_missing")
        elif volume == 0:
            flags.append("zero_volume")
        rows.append(
            {
                "symbol": instrument.id,
                "asset_class": instrument.asset_class,
                "ts_end": ts_end,
                **prices,
                "volume": volume,
                "source": "yahoo",
                "flags": flags,
            }
        )
    return Normalized(
        pl.DataFrame(rows, schema=BAR_SCHEMA).sort("ts_end"), null_rows, excluded_rows
    )
