"""Alpaca historical equities using explicit feed and raw adjustment semantics."""

import json
import os
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import httpx
import polars as pl

from xasset.config import Instrument
from xasset.ingest.base import Batch, RawArtifact, download
from xasset.normalize.calendars import expected_bar_ends
from xasset.normalize.timebase import utc
from xasset.store.schema import BAR_SCHEMA


def batches(
    client: httpx.Client, instrument: Instrument, start: datetime, end: datetime, as_of: datetime
) -> Iterator[Batch]:
    start, end, as_of = utc(start), utc(end), utc(as_of)
    if not instrument.alpaca_symbol or "alpaca" not in instrument.sources:
        raise ValueError("Instrument has no configured Alpaca mapping")
    names = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing environment bindings: " + ", ".join(missing))
    headers = {"APCA-API-KEY-ID": os.environ[names[0]], "APCA-API-SECRET-KEY": os.environ[names[1]]}
    url = f"https://data.alpaca.markets/v2/stocks/{instrument.alpaca_symbol}/bars"
    params: dict[str, str | int] = {
        "timeframe": "1Min",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "adjustment": "raw",
        "feed": instrument.alpaca_feed,
        "sort": "asc",
        "limit": 10000,
    }
    allowed = (
        expected_bar_ends(instrument.calendar, start, end)
        if instrument.session == "regular" and instrument.calendar
        else None
    )
    tokens: set[str] = set()
    while True:
        payload = download(client, url, params=params, headers=headers)
        body = json.loads(payload)
        if not isinstance(body, dict) or "bars" not in body:
            raise ValueError("Malformed Alpaca bars response")
        if body.get("symbol", instrument.alpaca_symbol) != instrument.alpaca_symbol:
            raise ValueError("Alpaca returned a different symbol")
        rows: list[dict[str, Any]] = []
        for raw in body.get("bars") or []:
            opened = utc(datetime.fromisoformat(raw["t"]))
            stamp = opened + timedelta(minutes=1)
            if not start < stamp <= min(end, as_of) or (
                allowed is not None and stamp not in allowed
            ):
                continue
            rows.append(
                {
                    "symbol": instrument.id,
                    "asset_class": instrument.asset_class,
                    "ts_end": stamp,
                    "open": raw["o"],
                    "high": raw["h"],
                    "low": raw["l"],
                    "close": raw["c"],
                    "volume": raw["v"],
                    "source": "alpaca",
                    "flags": ["single_source", "unadjusted", f"feed_{instrument.alpaca_feed}"],
                }
            )
        yield Batch(
            start,
            end,
            pl.DataFrame(rows, schema=BAR_SCHEMA),
            [RawArtifact(payload, "json", url)],
            {"feed": instrument.alpaca_feed},
        )
        token = body.get("next_page_token")
        if not token:
            break
        if not isinstance(token, str) or token in tokens:
            raise ValueError("Alpaca pagination token repeated or malformed")
        tokens.add(token)
        params["page_token"] = token
