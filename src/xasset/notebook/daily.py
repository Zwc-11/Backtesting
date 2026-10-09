"""Survivorship-aware daily panel of Binance USDT spot coins.

The symbol list is Binance's exchange information, which keeps delisted pairs
(status ``BREAK``) next to trading ones, and the public market-data API serves daily
klines for both, so coins that later collapsed or were removed stay in the history.
Stablecoins, fiat, leveraged tokens, wrapped duplicates and tokenized equities are
excluded by type. Eligibility on each day uses only information known that day.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import polars as pl

from xasset.store.writer import atomic_path, write_json

API = "https://data-api.binance.vision/api/v3"
SOURCE = "binance-spot-1d"
DAY_MS = 86_400_000

# Excluded by type, known before any data is seen.
STABLE_OR_FIAT = {
    "USDC", "BUSD", "TUSD", "USDP", "PAX", "DAI", "FDUSD", "UST", "USTC", "USDS", "USDSB",
    "USDSOLD", "SUSD", "GUSD", "LUSD", "FRAX", "USDJ", "USDE", "USD1", "XUSD", "RLUSD",
    "PYUSD", "BFUSD", "AEUR", "EUR", "EURI", "GBP", "AUD", "BRL", "TRY", "RUB", "UAH",
    "NGN", "ZAR", "IDRT", "BIDR", "BKRW", "PLN", "RON", "ARS", "JPY", "MXN", "COP", "CZK",
    "BVND", "VAI", "TUSDB",
}  # fmt: skip
COMMODITY_OR_WRAPPED = {"PAXG", "XAUT", "WBTC", "WBETH", "BETH", "BNSOL", "STETH", "WETH"}
LEVERAGED = ("UP", "DOWN", "BULL", "BEAR")


def classify(entries: list[dict[str, Any]]) -> dict[str, str | None]:
    """Exclusion reason per USDT symbol (None means included)."""
    bases = {e["baseAsset"] for e in entries}
    output: dict[str, str | None] = {}
    for entry in entries:
        base = entry["baseAsset"]
        reason = None
        if base in STABLE_OR_FIAT:
            reason = "stablecoin or fiat"
        elif base in COMMODITY_OR_WRAPPED:
            reason = "commodity token or wrapped duplicate"
        elif any(base.endswith(s) and base[: -len(s)] in bases for s in LEVERAGED):
            reason = "leveraged token"
        output[entry["symbol"]] = reason
    return output


def tokenized_equity(base: str, bases: set[str], first: date) -> bool:
    """Binance's 2025+ tokenized stocks are named <TICKER>B (e.g. AAPLB, NVDAB)."""
    return (
        len(base) >= 3
        and base.endswith("B")
        and base[:-1] not in bases
        and first >= date(2025, 1, 1)
    )


def exchange_symbols(client: httpx.Client) -> list[dict[str, Any]]:
    response = client.get(f"{API}/exchangeInfo", params={"permissions": "SPOT"}, timeout=60)
    response.raise_for_status()
    return [
        {"symbol": s["symbol"], "baseAsset": s["baseAsset"], "status": s["status"]}
        for s in response.json()["symbols"]
        if s["quoteAsset"] == "USDT"
    ]


SCHEMA = {
    "date": pl.Date(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
    "notional": pl.Float64(),
    "trades": pl.Int64(),
    "buy_notional": pl.Float64(),
}


def kline_rows(rows: list[list[Any]], now: datetime) -> pl.DataFrame:
    records = []
    for row in rows:
        opened, closed = int(row[0]), int(row[6])
        # A pair halted mid-day closes its final kline early; it is kept as that day.
        if opened % DAY_MS or not opened < closed <= opened + DAY_MS - 1:
            if float(row[5]) == 0 and float(row[7]) == 0:
                continue  # an empty placeholder left at a delisting carries no trades
            raise ValueError("Binance daily kline has invalid bounds")
        if datetime.fromtimestamp((opened + DAY_MS) / 1000, tz=UTC) > now:
            continue  # the forming day
        notional = float(row[7])
        records.append(
            {
                "date": datetime.fromtimestamp(opened / 1000, tz=UTC).date(),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "notional": notional,
                "trades": int(row[8]),
                "buy_notional": min(float(row[10]), notional),
            }
        )
    return pl.DataFrame(records, schema=SCHEMA)


def fetch_daily(
    client: httpx.Client, symbol: str, start: date, end: date, now: datetime
) -> pl.DataFrame:
    """Completed UTC days ``start <= date < end`` from the market-data API."""
    frames = []
    cursor = int(datetime.combine(start, datetime.min.time(), tzinfo=UTC).timestamp() * 1000)
    stop = int(datetime.combine(end, datetime.min.time(), tzinfo=UTC).timestamp() * 1000)
    while cursor < stop:
        response = client.get(
            f"{API}/klines",
            params={
                "symbol": symbol,
                "interval": "1d",
                "startTime": cursor,
                "endTime": stop - 1,
                "limit": 1000,
            },
        )
        if response.status_code == 429:
            time.sleep(float(response.headers.get("retry-after", "10")))
            continue
        response.raise_for_status()
        rows = response.json()
        if not rows:
            break
        frames.append(kline_rows(rows, now))
        cursor = int(rows[-1][0]) + DAY_MS
        time.sleep(0.08)
    if not frames:
        return pl.DataFrame(schema=SCHEMA)
    return pl.concat(frames).unique("date").sort("date")


def daily_dir(root: Path) -> Path:
    return root / "lab" / "daily" / SOURCE


def ingest_daily(
    root: Path,
    start: date,
    end: date,
    progress: Callable[[str], None] = lambda _: None,
) -> dict[str, Any]:
    """Fetch or extend every USDT symbol's daily history; returns the symbol manifest."""
    now = datetime.now(UTC)
    target = daily_dir(root)
    target.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60, headers={"User-Agent": "xasset/0.1 lab"}) as client:
        entries = exchange_symbols(client)
        reasons = classify(entries)
        fetched = 0
        for index, entry in enumerate(entries):
            symbol = entry["symbol"]
            if reasons[symbol] is not None:
                continue
            path = target / f"{symbol}.parquet"
            existing = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=SCHEMA)
            begin = start
            if existing.height:
                last = existing["date"].max()
                assert isinstance(last, date)
                begin = max(start, last + timedelta(days=1))
            if begin >= end:
                continue
            progress(f"{index + 1}/{len(entries)} {symbol}")
            frame = fetch_daily(client, symbol, begin, end, now)
            if frame.height:
                merged = pl.concat([existing, frame]).unique("date", keep="last").sort("date")
                with atomic_path(path) as temporary:
                    merged.write_parquet(temporary, compression="zstd")
                fetched += frame.height
    manifest = {
        "source": "Binance data-api.binance.vision exchangeInfo (TRADING and BREAK) and 1d klines",
        "fetched_at": now.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": [
            {**entry, "excluded": reasons[entry["symbol"]]}
            for entry in sorted(entries, key=lambda e: e["symbol"])
        ],
        "rows_fetched": fetched,
    }
    write_json(target / "_symbols.json", manifest)
    return manifest


def pegged(frame: pl.DataFrame) -> bool:
    """A dollar-pegged token not covered by the name list (it never trades away from 1)."""
    closes = frame["close"].to_numpy()
    if closes.size < 30:
        return False
    near_one = float(np.mean((closes > 0.95) & (closes < 1.05)))
    moves = np.abs(np.diff(np.log(closes[closes > 0])))
    return near_one >= 0.9 and float(np.median(moves)) < 0.003


@dataclass
class Panel:
    """Dates x symbols arrays; NaN where a coin did not trade."""

    dates: list[date]
    symbols: list[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    notional: np.ndarray
    buy_notional: np.ndarray
    first: np.ndarray  # index of each symbol's first day
    last: np.ndarray  # index of each symbol's last day
    status: dict[str, str]

    @property
    def returns(self) -> np.ndarray:
        """Close-to-close log returns (NaN across gaps)."""
        output = np.full_like(self.close, np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            output[1:] = np.log(self.close[1:] / self.close[:-1])
        return output

    def index(self, day: date) -> int:
        return self.dates.index(day)


MAX_GAP_DAYS = 7


def segments(frame: pl.DataFrame) -> list[pl.DataFrame]:
    """Split a symbol's history where trading stopped for more than a week.

    Binance has reused tickers after long halts (LUNAUSDT traded the collapsed coin until
    May 2022 and a new coin from the end of that month). A long gap therefore starts a
    new instrument with its own listing age; nothing is carried across it.
    """
    dates = frame["date"].to_list()
    cuts = [0]
    for i in range(1, len(dates)):
        if (dates[i] - dates[i - 1]).days > MAX_GAP_DAYS:
            cuts.append(i)
    cuts.append(len(dates))
    return [frame.slice(a, b - a) for a, b in zip(cuts[:-1], cuts[1:], strict=True) if b > a]


def load_panel(root: Path, start: date, end: date) -> Panel:
    """Every included symbol with data in ``[start, end)``, aligned on calendar days.

    A symbol whose trading stopped for more than a week becomes separate columns
    (``SYMBOL``, ``SYMBOL~2``, ...), one per contiguous trading history.
    """
    folder = daily_dir(root)
    manifest = json.loads((folder / "_symbols.json").read_text())
    included = {s["symbol"]: s for s in manifest["symbols"] if s["excluded"] is None}
    bases = {s["baseAsset"] for s in manifest["symbols"]}
    dates = [start + timedelta(days=i) for i in range((end - start).days)]
    position = {d: i for i, d in enumerate(dates)}
    columns: dict[str, list[np.ndarray]] = {
        k: [] for k in ("open", "high", "low", "close", "notional", "buy_notional")
    }
    symbols: list[str] = []
    firsts: list[int] = []
    lasts: list[int] = []
    status: dict[str, str] = {}
    for symbol in sorted(included):
        path = folder / f"{symbol}.parquet"
        if not path.exists():
            continue
        full = pl.read_parquet(path).sort("date")
        full = full.filter((pl.col("close") > 0) & (pl.col("notional") >= 0))
        if not full.height:
            continue
        first_day = full["date"].min()
        assert isinstance(first_day, date)
        if tokenized_equity(included[symbol]["baseAsset"], bases, first_day):
            continue
        if pegged(full):
            continue
        parts = segments(full)
        for k, part in enumerate(parts):
            valid = part.filter((pl.col("date") >= start) & (pl.col("date") < end))
            if not valid.height:
                continue
            name = symbol if k == 0 else f"{symbol}~{k + 1}"
            rows = [position[d] for d in valid["date"].to_list()]
            for column in columns:
                values = np.full(len(dates), np.nan)
                values[rows] = valid[column].to_numpy()
                columns[column].append(values)
            symbols.append(name)
            segment_start = part["date"].min()
            assert isinstance(segment_start, date)
            firsts.append((segment_start - start).days)
            lasts.append(max(rows))
            last_part = k == len(parts) - 1
            status[name] = included[symbol]["status"] if last_part else "ENDED"
    return Panel(
        dates=dates,
        symbols=symbols,
        open=np.column_stack(columns["open"]),
        high=np.column_stack(columns["high"]),
        low=np.column_stack(columns["low"]),
        close=np.column_stack(columns["close"]),
        notional=np.column_stack(columns["notional"]),
        buy_notional=np.column_stack(columns["buy_notional"]),
        first=np.array(firsts),
        last=np.array(lasts),
        status=status,
    )
