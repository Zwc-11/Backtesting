import json
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from xasset.config import Instrument
from xasset.store.schema import BAR_SCHEMA


@pytest.fixture
def instrument() -> Instrument:
    return Instrument(
        id="TEST",
        asset_class="equity",
        tier="B",
        venue="XNYS",
        calendar="XNYS",
        session="regular",
        yahoo_symbol="TEST",
    )


def bars_at(times: list[datetime], symbol: str = "TEST") -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "symbol": symbol,
                "asset_class": "equity",
                "ts_end": time,
                "open": 100.0 + index,
                "high": 102.0 + index,
                "low": 99.0 + index,
                "close": 101.0 + index,
                "volume": 10,
                "source": "yahoo",
                "flags": ["unadjusted", "single_source"],
            }
            for index, time in enumerate(times)
        ],
        schema=BAR_SCHEMA,
    )


@pytest.fixture
def bars() -> pl.DataFrame:
    start = datetime(2026, 10, 7, 13, 31, tzinfo=UTC)
    return bars_at([start + timedelta(minutes=index) for index in range(60)])


def chart_payload(symbol: str, times: list[datetime], **overrides: list[object]) -> bytes:
    quotes = {
        "open": [100.0] * len(times),
        "high": [102.0] * len(times),
        "low": [99.0] * len(times),
        "close": [101.0] * len(times),
        "volume": [10] * len(times),
        **overrides,
    }
    return json.dumps(
        {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {"symbol": symbol},
                        "timestamp": [int(time.timestamp()) for time in times],
                        "indicators": {"quote": [quotes]},
                    }
                ],
            }
        }
    ).encode()
