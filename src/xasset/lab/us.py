"""Large, liquid US stocks chosen month by month, from Alpaca SIP bars.

Candidates are a fixed list of large US stocks with heavy trading, written down before
any replay (it is drawn from companies listed in 2026, so delisted names are absent:
a declared survivorship caveat). Each month's members are the ``top`` candidates by
dollar volume (VWAP x volume from daily bars) over the previous complete month. The
broad-market and sector ETFs are always members: SPY is the benchmark and each stock's
sector ETF enters its residual model.

Minute bars come from Alpaca's SIP feed with raw prices, regular session only.
Notional is the bar's trade VWAP times its volume, so the volume-weighted trading
center (strategy 7) is computed from actual trades. Alpaca bars carry no
buyer/seller labels, so flow-dependent strategies cannot run on them.

Credentials are read from ALPACA_API_KEY and ALPACA_SECRET_KEY (environment or a
``.env`` file in the working directory); only historical market-data GETs are made.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from xasset.ingest.base import download
from xasset.lab.bars import FLOW_SCHEMA, lab_bar_path
from xasset.lab.ingest import month_starts
from xasset.lab.sessions import exchange_session
from xasset.store.writer import atomic_path

DATA = "https://data.alpaca.markets/v2/stocks"
SOURCE = "alpaca-sip"
ETFS: dict[str, str] = {
    "SPY": "broad market", "QQQ": "large-cap growth", "IWM": "small caps",
    "XLK": "technology", "XLF": "financials", "XLE": "energy", "XLV": "health care",
    "XLY": "consumer discretionary", "XLP": "consumer staples", "XLI": "industrials",
    "XLB": "materials", "XLU": "utilities", "XLRE": "real estate", "XLC": "communication",
    "SMH": "semiconductors",
}  # fmt: skip
# Candidate stock -> sector ETF (its residual-model factor and exposure cluster).
CANDIDATES: dict[str, str] = {
    "AAPL": "XLK", "MSFT": "XLK", "ORCL": "XLK", "CRM": "XLK", "ADBE": "XLK", "NOW": "XLK",
    "IBM": "XLK", "CSCO": "XLK", "DELL": "XLK", "HPE": "XLK", "ANET": "XLK", "PLTR": "XLK",
    "PANW": "XLK", "CRWD": "XLK", "SNOW": "XLK", "NET": "XLK", "DDOG": "XLK", "ZS": "XLK",
    "SHOP": "XLK", "APP": "XLK", "SMCI": "XLK", "IONQ": "XLK", "U": "XLK",
    "NVDA": "SMH", "AMD": "SMH", "AVGO": "SMH", "MU": "SMH", "INTC": "SMH", "QCOM": "SMH",
    "TXN": "SMH", "AMAT": "SMH", "LRCX": "SMH", "KLAC": "SMH", "MRVL": "SMH", "ARM": "SMH",
    "TSM": "SMH", "ASML": "SMH",
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "LOW": "XLY", "NKE": "XLY", "MCD": "XLY",
    "SBUX": "XLY", "TGT": "XLY", "ABNB": "XLY", "DASH": "XLY", "UBER": "XLY", "RIVN": "XLY",
    "LCID": "XLY", "F": "XLY", "GM": "XLY", "CVNA": "XLY", "BABA": "XLY", "PDD": "XLY",
    "JD": "XLY", "NIO": "XLY",
    "GOOGL": "XLC", "META": "XLC", "NFLX": "XLC", "DIS": "XLC", "T": "XLC", "VZ": "XLC",
    "CMCSA": "XLC", "RBLX": "XLC",
    "JPM": "XLF", "BAC": "XLF", "WFC": "XLF", "C": "XLF", "GS": "XLF", "MS": "XLF",
    "V": "XLF", "MA": "XLF", "PYPL": "XLF", "HOOD": "XLF", "SOFI": "XLF", "COIN": "XLF",
    "MSTR": "XLK", "MARA": "XLF", "RIOT": "XLF", "SCHW": "XLF", "BX": "XLF",
    "XOM": "XLE", "CVX": "XLE", "OXY": "XLE", "COP": "XLE", "SLB": "XLE", "HAL": "XLE",
    "UNH": "XLV", "LLY": "XLV", "JNJ": "XLV", "PFE": "XLV", "MRK": "XLV", "ABBV": "XLV",
    "NVO": "XLV", "HIMS": "XLV",
    "WMT": "XLP", "COST": "XLP", "KO": "XLP", "PEP": "XLP", "PG": "XLP",
    "BA": "XLI", "CAT": "XLI", "GE": "XLI", "GEV": "XLI", "LMT": "XLI", "RTX": "XLI",
    "DE": "XLI", "UPS": "XLI",
    "VST": "XLU", "CEG": "XLU", "OKLO": "XLU", "NEE": "XLU", "SMR": "XLU",
    "FCX": "XLB", "NEM": "XLB",
}  # fmt: skip


def credentials() -> dict[str, str]:
    """Alpaca headers from the environment, or from ./.env when not set there."""
    names = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
    values = {name: os.environ.get(name, "") for name in names}
    env = Path(".env")
    if not all(values.values()) and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() in names and not values[key.strip()]:
                values[key.strip()] = value.strip().strip('"').strip("'")
    missing = [name for name in names if not values[name]]
    if missing:
        raise ValueError("Missing Alpaca credentials: " + ", ".join(missing))
    return {"APCA-API-KEY-ID": values[names[0]], "APCA-API-SECRET-KEY": values[names[1]]}


def bars(
    client: httpx.Client, symbol: str, timeframe: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Every Alpaca SIP bar in [start, end), following page tokens."""
    headers = credentials()
    params: dict[str, str | int] = {
        "timeframe": timeframe,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "adjustment": "raw",
        "feed": "sip",
        "sort": "asc",
        "limit": 10000,
    }
    rows: list[dict[str, Any]] = []
    while True:
        body = json.loads(download(client, f"{DATA}/{symbol}/bars", params=params, headers=headers))
        if not isinstance(body, dict) or "bars" not in body:
            raise ValueError(f"Malformed Alpaca response for {symbol}")
        rows += body.get("bars") or []
        token = body.get("next_page_token")
        if not token:
            return rows
        params["page_token"] = token


def daily_path(root: Path, symbol: str) -> Path:
    return root / "lab" / "us-daily" / f"{symbol}.parquet"


def fetch_daily(client: httpx.Client, root: Path, symbol: str, start: date, end: date) -> int:
    begin = datetime(start.year, start.month, start.day, tzinfo=UTC)
    finish = datetime(end.year, end.month, end.day, tzinfo=UTC)
    rows = bars(client, symbol, "1Day", begin, finish)
    frame = pl.DataFrame(
        {
            "symbol": [symbol] * len(rows),
            "date": [datetime.fromisoformat(r["t"].replace("Z", "+00:00")).date() for r in rows],
            "close": [float(r["c"]) for r in rows],
            "volume": [float(r["v"]) for r in rows],
            "notional": [float(r.get("vw") or r["c"]) * float(r["v"]) for r in rows],
        },
        schema={
            "symbol": pl.String,
            "date": pl.Date,
            "close": pl.Float64,
            "volume": pl.Float64,
            "notional": pl.Float64,
        },
    )
    with atomic_path(daily_path(root, symbol)) as temporary:
        frame.write_parquet(temporary)
    return frame.height


def monthly_members(root: Path, first: date, last: date, top: int) -> dict[str, list[str]]:
    files = [daily_path(root, s) for s in CANDIDATES if daily_path(root, s).exists()]
    daily = pl.concat([pl.read_parquet(f) for f in files])
    out: dict[str, list[str]] = {}
    begin = datetime(first.year, first.month, 1, tzinfo=UTC)
    finish = datetime(last.year, last.month, 1, tzinfo=UTC) + timedelta(days=1)
    for month in month_starts(begin, finish):
        prior_end = month.date()
        prior_start = (month - timedelta(days=1)).date().replace(day=1)
        window = daily.filter((pl.col("date") >= prior_start) & (pl.col("date") < prior_end))
        sessions = window["date"].n_unique()
        ranked = (
            window.group_by("symbol")
            .agg(pl.col("notional").sum(), pl.col("date").n_unique().alias("days"))
            .filter((pl.col("days") == sessions) & (pl.col("notional") > 0))
            .sort(["notional", "symbol"], descending=[True, False])
            .head(top)
        )
        out[f"{month:%Y-%m}"] = [*ETFS, *ranked["symbol"].to_list()]
    return out


def universe_document(membership: dict[str, list[str]], top: int) -> dict[str, Any]:
    stocks = sorted({s for members in membership.values() for s in members if s not in ETFS})
    instruments: list[dict[str, Any]] = []
    for symbol, label in ETFS.items():
        instruments.append(
            {
                "id": symbol, "kind": "etf", "currency": "USD", "tick": 0.01, "lot": 1.0,
                "calendar": "XNYS", "cluster": f"etf-{label.replace(' ', '-')}",
                "cost_class": "us_etf", "breadth_member": False, "history": SOURCE,
                "live_feed": "alpaca", "live_symbol": symbol,
            }
        )  # fmt: skip
    for symbol in stocks:
        sector = CANDIDATES[symbol]
        instruments.append(
            {
                "id": symbol, "kind": "equity", "currency": "USD", "tick": 0.01, "lot": 1.0,
                "calendar": "XNYS", "cluster": ETFS[sector].replace(" ", "-"),
                "sector": sector, "cost_class": "us_stock", "history": SOURCE,
                "live_feed": "alpaca", "live_symbol": symbol,
            }
        )  # fmt: skip
    pairs = []
    for etf in ("XLK", "SMH", "XLF", "XLE", "XLY", "XLC", "XLV"):
        laggards = [s for s in stocks if CANDIDATES[s] == etf]
        if laggards:
            pairs.append({"leader": etf, "laggards": laggards})
    months = sorted(membership)
    return {
        "id": f"us-large-top{top}",
        "description": (
            f"The {top} US stocks with the highest dollar volume each month among large "
            "candidates, with SPY, QQQ, IWM, SMH and the sector ETFs; Alpaca SIP minute bars."
        ),
        "benchmark": "SPY",
        "minimum_peers": 20,
        "pairs": pairs,
        "selection_note": (
            f"Members for each month from {months[0]} to {months[-1]} are the top {top} of a "
            f"fixed list of {len(CANDIDATES)} large US stocks by the previous month's dollar "
            "volume (VWAP x volume, every session traded). The candidate list was written "
            "from companies listed in 2026, so delisted names are absent (survivorship "
            "caveat). Sector factors and pairs follow each stock's sector ETF, not returns."
        ),
        "instruments": instruments,
        "membership": membership,
    }


def ingest_minutes(
    client: httpx.Client, root: Path, symbol: str, months: set[str]
) -> dict[str, object]:
    """Regular-session SIP minute bars for the given months into data/lab/bars."""
    written = []
    for month_key in sorted(months):
        target = lab_bar_path(root, SOURCE, symbol) / f"{month_key}.parquet"
        if target.exists():
            continue
        year, number = (int(part) for part in month_key.split("-"))
        start = datetime(year, number, 1, tzinfo=UTC)
        end = (start + timedelta(days=32)).replace(day=1)
        if end > datetime.now(UTC) - timedelta(minutes=20):
            continue  # month not complete (and SIP data is delayed on the free plan)
        rows = bars(client, symbol, "1Min", start, end)
        sessions: dict[date, tuple[datetime, datetime] | None] = {}
        records = []
        for raw in rows:
            opened = datetime.fromisoformat(raw["t"].replace("Z", "+00:00")).astimezone(UTC)
            stamp = opened + timedelta(minutes=1)
            day = opened.date()
            if day not in sessions:
                session = exchange_session("XNYS", day)
                sessions[day] = None if session is None else (session.open, session.close)
            bounds = sessions[day]
            if bounds is None or not bounds[0] < stamp <= bounds[1]:
                continue  # regular session only
            volume = float(raw["v"])
            vwap = float(raw.get("vw") or raw["c"])
            records.append(
                {
                    "symbol": symbol, "ts_end": stamp, "available_at": stamp,
                    "open": float(raw["o"]), "high": float(raw["h"]), "low": float(raw["l"]),
                    "close": float(raw["c"]), "volume": volume, "notional": vwap * volume,
                    "trades": int(raw.get("n") or 0), "source": SOURCE,
                }
            )  # fmt: skip
        frame = pl.DataFrame(
            records, schema_overrides={k: FLOW_SCHEMA[k] for k in FLOW_SCHEMA.names()}
        )
        for name in FLOW_SCHEMA.names():
            if name not in frame.columns:
                frame = frame.with_columns(pl.lit(None, dtype=FLOW_SCHEMA[name]).alias(name))
        frame = frame.select(FLOW_SCHEMA.names()).sort("ts_end")
        with atomic_path(target) as temporary:
            frame.write_parquet(temporary, compression="zstd")
        written.append({"month": month_key, "rows": frame.height})
    return {"symbol": symbol, "source": SOURCE, "months": written}
