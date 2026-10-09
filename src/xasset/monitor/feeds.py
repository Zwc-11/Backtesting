"""Read-only, bounded historical/live data adapters for three independent venues."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import polars as pl

from xasset.config import Instrument
from xasset.ingest.alpaca import batches as alpaca_batches
from xasset.ingest.base import Batch, RawArtifact, download
from xasset.normalize.calendars import calendar
from xasset.normalize.timebase import utc
from xasset.store.schema import BAR_SCHEMA, empty_bars


def market_window(
    item: Instrument, cutoff: datetime, minutes: int
) -> tuple[datetime, datetime, bool]:
    cutoff = utc(cutoff).replace(second=0, microsecond=0)
    if item.session == "regular" and item.calendar:
        cal = calendar(item.calendar)
        sessions = cal.sessions_in_range(cutoff.date() - timedelta(days=15), cutoff.date())
        available = [s for s in sessions if cal.session_open(s).to_pydatetime() < cutoff]
        if not available:
            raise ValueError("No recent session inside bounded monitor history")
        session = available[-1]
        opened, closed = (
            cal.session_open(session).to_pydatetime(),
            cal.session_close(session).to_pydatetime(),
        )
        end = min(cutoff, closed)
        return max(opened, end - timedelta(minutes=minutes)), end, opened <= cutoff <= closed
    return cutoff - timedelta(minutes=minutes), cutoff, True


def public_bars(body: Any, item: Instrument, start: datetime, end: datetime) -> pl.DataFrame:
    source = item.sources[0]
    rows = []
    if source == "kraken":
        if body.get("error"):
            raise ValueError("Kraken reported an API error")
        series = [value for key, value in body["result"].items() if key != "last"]
        if len(series) != 1:
            raise ValueError("Expected exactly one Kraken asset series")
        native = [
            {"t": int(r[0]) * 1000, "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[6]}
            for r in series[0]
        ]
    elif source == "hyperliquid":
        if not isinstance(body, list):
            raise ValueError("Malformed Hyperliquid candles")
        if any(row.get("s") != item.hyperliquid_symbol or row.get("i") != "1m" for row in body):
            raise ValueError("Hyperliquid symbol or candle interval mismatch")
        native = body
    else:
        raise ValueError("Unsupported public feed")
    for raw in native:
        opened = datetime.fromtimestamp(int(raw["t"]) / 1000, UTC)
        stamp = opened + timedelta(minutes=1)
        # Kraken always includes an unfinished candle; do not expose it to features.
        if opened.second or opened.microsecond or not start < stamp <= end:
            continue
        rows.append(
            {
                "symbol": item.id,
                "asset_class": item.asset_class,
                "ts_end": stamp,
                **{
                    name: float(raw[key])
                    for name, key in [
                        ("open", "o"),
                        ("high", "h"),
                        ("low", "l"),
                        ("close", "c"),
                        ("volume", "v"),
                    ]
                },
                "source": source,
                "flags": ["single_source", "public_market_data"],
            }
        )
    return pl.DataFrame(rows, schema=BAR_SCHEMA).sort("ts_end") if rows else empty_bars()


def fetch(client: httpx.Client, item: Instrument, start: datetime, end: datetime) -> Batch:
    source = item.sources[0]
    if source == "alpaca":
        pages = list(alpaca_batches(client, item, start, end, end))
        return Batch(
            start,
            end,
            pl.concat([p.bars for p in pages]) if pages else empty_bars(),
            [artifact for p in pages for artifact in p.artifacts],
            {"feed": item.alpaca_feed},
        )
    if source == "kraken":
        url = "https://api.kraken.com/0/public/OHLC"
        payload = download(
            client,
            url,
            params={
                "pair": str(item.kraken_symbol),
                "interval": 1,
                "since": int(start.timestamp()),
            },
        )
    elif source == "hyperliquid":
        url = "https://api.hyperliquid.xyz/info"
        response = client.post(
            url,
            json={
                "type": "candleSnapshot",
                "req": {
                    "coin": item.hyperliquid_symbol,
                    "interval": "1m",
                    "startTime": int(start.timestamp() * 1000),
                    "endTime": int(end.timestamp() * 1000),
                },
            },
        )
        response.raise_for_status()
        payload = response.content
        if len(payload) > 8 * 1024 * 1024:
            raise ValueError("Hyperliquid response exceeds bounded candle request")
    else:
        raise ValueError("Unsupported feed")
    return Batch(
        start,
        end,
        public_bars(json.loads(payload), item, start, end),
        [RawArtifact(payload, "json", url)],
        {"read_only": True},
    )
