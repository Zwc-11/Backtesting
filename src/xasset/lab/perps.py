"""Binance USD-M perpetual universe chosen month by month from information known then.

Every contract ever archived on data.binance.vision is listed (delisted contracts
included), its daily klines are downloaded with checksums, and for each month the
members are the ``top`` contracts by total quote notional over the previous complete
month. A contract qualifies for month m only if it traded on every day of month m-1,
so a listing in the middle of a month cannot rank on a partial month. Underlyings that
are not crypto assets are excluded: stablecoins, metals and composite indices by a fixed
list, and the stock, ETF and commodity perpetuals listed from 2026 by a reviewed list
plus a point-in-time weekday-trading rule (``tradfi``).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl

from xasset.ingest.base import download
from xasset.lab.ingest import BASE, csv_rows, month_starts, verify
from xasset.store.writer import atomic_path

BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
S3 = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
PERPETUAL = re.compile(r"^[A-Z0-9]+USDT$")
# Underlyings that are not crypto assets, plus stablecoins and composite indices.
EXCLUDED = {
    "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "USDPUSDT", "BUSDUSDT", "USDEUSDT", "USD1USDT",
    "XAUUSDT", "XAGUSDT", "PAXGUSDT", "XAUTUSDT",
    "BTCDOMUSDT", "DEFIUSDT", "BLUEBIRDUSDT", "FOOTBALLUSDT",
}  # fmt: skip
# Stock, ETF and commodity perpetuals Binance listed from 28 January 2026. Reviewed by
# ticker: every 2026 listing that ranked in any monthly top 120 by notional and traded
# weekdays only (weekend/weekday notional at most 0.35; the lowest crypto listing of
# the period is 0.43). ``tradfi`` below also excludes later or lower-ranked listings.
TRADFI = {
    "MUUUSDT", "KORUUSDT", "LITEUSDT", "METAUSDT", "AAOIUSDT", "SPCXUSDT", "SOXLUSDT",
    "RKLBUSDT", "SNDKUSDT", "MRVLUSDT", "SNXXUSDT", "MUUSDT", "SKHYUSDT", "ARMUSDT",
    "SOXSUSDT", "CBRSUSDT", "MVLLUSDT", "SAMSUNGUSDT", "NBISUSDT", "ZHIPUUSDT", "GLWUSDT",
    "INTCUSDT", "AMDUSDT", "TSLAUSDT", "SKHYNIXUSDT", "PLTRUSDT", "DRAMUSDT", "GOOGLUSDT",
    "AXTIUSDT", "NVDAUSDT", "AAPLUSDT", "HOODUSDT", "EWYUSDT", "CRCLUSDT", "COINUSDT",
    "NATGASUSDT", "MSTRUSDT", "QQQUSDT", "CLUSDT", "XPTUSDT", "BZUSDT", "SPYUSDT",
}  # fmt: skip
TRADFI_LISTING_START = date(2026, 1, 28)
WEEKEND_RATIO_LIMIT = 0.40
DAILY_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "notional": pl.Float64,
    "trades": pl.Int64,
}


def listing(client: httpx.Client, prefix: str) -> list[str]:
    """Every key or common prefix directly under ``prefix`` in the archive bucket."""
    names: list[str] = []
    marker = ""
    while True:
        params: dict[str, str | int] = {"delimiter": "/", "prefix": prefix}
        if marker:
            params["marker"] = marker
        root = ET.fromstring(download(client, BUCKET, limit=16 * 1024 * 1024, params=params))
        page = [node.text or "" for node in root.findall("s3:CommonPrefixes/s3:Prefix", S3)]
        page += [node.text or "" for node in root.findall("s3:Contents/s3:Key", S3)]
        names += page
        if root.findtext("s3:IsTruncated", namespaces=S3) != "true" or not page:
            return names
        marker = root.findtext("s3:NextMarker", namespaces=S3) or page[-1]


def perpetual_symbols(client: httpx.Client) -> list[str]:
    prefix = "data/futures/um/monthly/klines/"
    symbols = [name.removeprefix(prefix).strip("/") for name in listing(client, prefix)]
    return sorted(s for s in symbols if PERPETUAL.match(s) and s not in EXCLUDED)


def daily_path(root: Path, symbol: str) -> Path:
    return root / "lab" / "perp-daily" / f"{symbol}.parquet"


def fetch_daily(client: httpx.Client, root: Path, symbol: str, start: date, end: date) -> int:
    """Download the completed monthly 1d archives of one contract between two months."""
    prefix = f"data/futures/um/monthly/klines/{symbol}/1d/"
    available = {
        key.removeprefix(prefix) for key in listing(client, prefix) if key.endswith(".zip")
    }
    path = daily_path(root, symbol)
    cached = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=DAILY_SCHEMA)
    have = {f"{d:%Y-%m}" for d in cached["date"].to_list()}
    frames = [cached]
    begin = datetime(start.year, start.month, 1, tzinfo=UTC)
    finish = datetime(end.year, end.month, 1, tzinfo=UTC) + timedelta(days=32)
    for month in month_starts(begin, finish.replace(day=1)):
        filename = f"{symbol}-1d-{month:%Y-%m}.zip"
        if filename not in available or f"{month:%Y-%m}" in have:
            continue
        url = f"{BASE}/futures/um/monthly/klines/{symbol}/1d/{filename}"
        checksum = download(client, url + ".CHECKSUM", limit=1024)
        payload = download(client, url, limit=8 * 1024 * 1024)
        verify(payload, checksum, filename)
        rows = []
        for index, row in enumerate(csv_rows(payload, filename)):
            if index == 0 and row and row[0] == "open_time":
                continue
            stamp = int(row[0])
            divisor = 1_000_000 if stamp >= 100_000_000_000_000 else 1_000
            rows.append(
                {
                    "symbol": symbol,
                    "date": datetime.fromtimestamp(stamp / divisor, tz=UTC).date(),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "notional": float(row[7]),
                    "trades": int(row[8]),
                }
            )
        frames.append(pl.DataFrame(rows, schema=DAILY_SCHEMA))
    frame = pl.concat(frames).unique(["date"], keep="last").sort("date")
    if frame.height:
        with atomic_path(path) as temporary:
            frame.write_parquet(temporary)
    return frame.height


def fetch_all_daily(
    client: httpx.Client,
    root: Path,
    symbols: Iterable[str],
    start: date,
    end: date,
    workers: int = 8,
) -> dict[str, int]:
    symbols = list(symbols)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        counts = pool.map(lambda s: fetch_daily(client, root, s, start, end), symbols)
        return dict(zip(symbols, counts, strict=True))


def load_daily(root: Path) -> pl.DataFrame:
    files = sorted((root / "lab" / "perp-daily").glob("*.parquet"))
    if not files:
        return pl.DataFrame(schema=DAILY_SCHEMA)
    return pl.concat([pl.read_parquet(f) for f in files]).filter(
        ~pl.col("symbol").is_in(sorted(EXCLUDED | TRADFI))
    )


def tradfi(window: pl.DataFrame, first_listed: dict[str, date]) -> set[str]:
    """Contracts that look like weekday-only (non-crypto) underlyings in ``window``.

    Listed on or after the first TradFi listing and trading on weekends at under 40%
    of their weekday notional over the ranking month: information known at the time.
    """
    weekday = window.with_columns(pl.col("date").dt.weekday().alias("wd"))
    ratios = weekday.group_by("symbol").agg(
        pl.col("notional").filter(pl.col("wd") <= 5).mean().alias("wk"),
        pl.col("notional").filter(pl.col("wd") >= 6).mean().alias("we"),
    )
    out = set()
    for row in ratios.iter_rows(named=True):
        listed = first_listed.get(row["symbol"])
        if listed is None or listed < TRADFI_LISTING_START or not row["wk"]:
            continue
        if (row["we"] or 0.0) / row["wk"] < WEEKEND_RATIO_LIMIT:
            out.add(row["symbol"])
    return out


def monthly_membership(
    daily: pl.DataFrame, first: date, last: date, top: int
) -> dict[str, list[tuple[str, float]]]:
    """For each month in [first, last]: the ``top`` contracts by previous-month notional.

    Returns month -> [(symbol, previous-month notional), ...] in rank order. A contract
    needs a bar on every day of the previous month.
    """
    out: dict[str, list[tuple[str, float]]] = {}
    listed = dict(daily.group_by("symbol").agg(pl.col("date").min()).iter_rows())
    begin = datetime(first.year, first.month, 1, tzinfo=UTC)
    finish = datetime(last.year, last.month, 1, tzinfo=UTC) + timedelta(days=1)
    for month in month_starts(begin, finish):
        prior_end = month.date()
        prior_start = (month - timedelta(days=1)).date().replace(day=1)
        days = (prior_end - prior_start).days
        window = daily.filter((pl.col("date") >= prior_start) & (pl.col("date") < prior_end))
        window = window.filter(~pl.col("symbol").is_in(sorted(tradfi(window, listed))))
        ranked = (
            window.group_by("symbol")
            .agg(pl.col("notional").sum(), pl.col("date").n_unique().alias("days"))
            .filter((pl.col("days") == days) & (pl.col("notional") > 0))
            .sort(["notional", "symbol"], descending=[True, False])
            .head(top)
        )
        out[f"{month:%Y-%m}"] = list(
            zip(ranked["symbol"].to_list(), ranked["notional"].to_list(), strict=True)
        )
    return out


# Economic clusters for the 25%-of-NAV cluster exposure limit, declared by what each
# project is (not by returns). Contracts not listed here share the "other-alts" cluster.
CLUSTERS: dict[str, tuple[str, ...]] = {
    "bitcoin": ("BTC",),
    "ether": ("ETH",),
    "layer-1": (
        "SOL", "ADA", "AVAX", "BNB", "NEAR", "SUI", "DOT", "APT", "TRX", "TON", "SEI",
        "INJ", "ICP", "ATOM", "HBAR", "XLM", "ETC", "FTM", "S", "TIA", "HYPE", "BERA",
        "ALGO", "EOS", "NEO", "MINA", "CFX", "XPL", "MON", "1000LUNC", "LUNA2", "USTC",
        "KAS", "IOTA", "ZIL", "ONT", "MOVE", "SOMI", "0G", "PLUME",
    ),
    "payments": ("XRP", "LTC", "BCH", "ZEC", "DASH", "XMR"),
    "memes": (
        "DOGE", "1000PEPE", "1000SHIB", "1000BONK", "WIF", "1000FLOKI", "FARTCOIN",
        "TRUMP", "PENGU", "NEIRO", "PEOPLE", "BOME", "PNUT", "POPCAT", "MOODENG",
        "1000RATS", "1000SATS", "TURBO", "MEME", "MEW", "GOAT", "DOGS", "PIPPIN", "NOT",
        "1MBABYDOGE", "NEIROETH", "BANANAS31", "MUBARAK", "GIGGLE", "BROCCOLI714", "SPX",
        "TUT", "ORDI", "BAN", "CHILLGUY", "PUMP",
    ),
    "defi": (
        "UNI", "AAVE", "LINK", "CRV", "ENA", "ONDO", "PENDLE", "LDO", "JUP", "ETHFI",
        "JTO", "DYDX", "CAKE", "RUNE", "UMA", "API3", "PYTH", "TRB", "COW", "ORCA",
        "EIGEN", "ENS", "SNX", "SYN", "FRONT", "AEVO", "PERP", "ASTER", "WLFI", "USUAL",
        "RESOLV", "LISTA", "HAEDAL", "MYX", "DEXE", "EUL", "SKY", "MKR", "1INCH", "COMP",
        "SUSHI", "GMX", "BANK", "HOME", "AVNT", "OM",
    ),
    "layer-2": ("ARB", "OP", "MATIC", "POL", "STRK", "ZK", "ZRO", "MANTA", "LINEA", "ALT", "TAIKO", "METIS", "W"),  # noqa: E501
    "ai": (
        "TAO", "FET", "WLD", "VIRTUAL", "RNDR", "RENDER", "AGIX", "KAITO", "IO", "AI16Z",
        "AIXBT", "COOKIE", "SKYAI", "CGPT", "SWARMS", "ZEREBRO", "AI", "VVV", "AIA",
        "SAHARA", "NMR", "ALCH", "COAI", "ARKM",
    ),
    "infrastructure": ("FIL", "STX", "AR", "STORJ", "CKB", "ZEN", "GRT", "THETA", "LPT", "IP", "MASK", "ID", "JASMY", "BICO", "RIF"),  # noqa: E501
    "gaming-nft": ("GALA", "SAND", "APE", "AXS", "PIXEL", "MAGIC", "BIGTIME", "BLUR", "YGG", "IMX", "ENJ", "VOXEL", "XAI", "HIGH", "ACE", "BAKE", "GMT", "RARE", "CHZ", "TLM"),  # noqa: E501
}  # fmt: skip


def cluster_of(symbol: str) -> str:
    base = symbol.removesuffix("USDT")
    for name, members in CLUSTERS.items():
        if base in members:
            return name
    return "other-alts"


def decimals(values: Iterable[float], cap: int = 8) -> int:
    """Smallest number of decimals that represents every value (trade prices or sizes)."""
    worst = 0
    for value in values:
        if value <= 0:
            continue
        for k in range(cap + 1):
            scaled = value * 10**k
            if abs(scaled - round(scaled)) <= 1e-6 * max(1.0, abs(scaled)) * 1e-3:
                worst = max(worst, k)
                break
        else:
            worst = cap
    return worst


# Leader -> laggards declared on ecosystem grounds before any replay (strategy 8).
PAIRS: dict[str, tuple[str, ...]] = {
    "BTC": ("ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "LTC", "BCH", "DOT", "NEAR", "SUI", "TRX", "TON", "HBAR", "XLM", "ETC"),  # noqa: E501
    "ETH": ("ARB", "OP", "UNI", "AAVE", "LDO", "ENA", "ETHFI", "PENDLE", "ENS", "CRV", "MATIC", "STRK", "ZK", "ONDO"),  # noqa: E501
    "SOL": ("WIF", "1000BONK", "JUP", "PNUT", "POPCAT", "PENGU", "TRUMP", "FARTCOIN", "JTO", "PUMP", "MEW"),  # noqa: E501
}  # fmt: skip


def universe_document(
    daily: pl.DataFrame, membership: dict[str, list[tuple[str, float]]], top: int
) -> dict[str, object]:
    """The YAML document of the point-in-time perpetual universe."""
    from statistics import median

    ranks: dict[str, list[int]] = {}
    for ranked in membership.values():
        for position, (symbol, _) in enumerate(ranked, start=1):
            ranks.setdefault(symbol, []).append(position)
    symbols = sorted(ranks, key=lambda s: (median(ranks[s]), s))
    instruments = []
    for symbol in symbols:
        rows = daily.filter(pl.col("symbol") == symbol).tail(120)
        prices = [*rows["open"], *rows["high"], *rows["low"], *rows["close"]]
        tier = median(ranks[symbol])
        cost = "perp_major" if tier <= 2 else "perp_large" if tier <= 15 else "perp_alt"
        instruments.append(
            {
                "id": symbol,
                "kind": "perp",
                "currency": "USDT",
                "tick": float(f"1e-{decimals(prices)}"),
                "lot": float(f"1e-{decimals(rows['volume'].to_list())}"),
                "cluster": cluster_of(symbol),
                "cost_class": cost,
                "shortable": True,
                "history": "binance-um",
            }
        )
    known = set(symbols)
    pairs = []
    for leader, laggards in PAIRS.items():
        chosen = [f"{s}USDT" for s in laggards if f"{s}USDT" in known]
        if f"{leader}USDT" in known and chosen:
            pairs.append({"leader": f"{leader}USDT", "laggards": chosen})
    months = sorted(membership)
    return {
        "id": f"crypto-perp-top{top}",
        "description": (
            f"Binance USD-M perpetuals: the {top} largest crypto contracts each month by "
            "the previous month's quote notional, with 1-minute archives and taker-side flow."
        ),
        "benchmark": "BTCUSDT",
        "minimum_peers": 20,
        "pairs": pairs,
        "handoff_calendar": "XNYS",
        "selection_note": (
            f"Membership is recomputed for each month from {months[0]} to {months[-1]} using "
            "only the previous complete month (a contract needs a bar on every day of it), "
            "from every contract ever archived, delisted ones included. Stablecoins, metals, "
            "composite indices and the stock, ETF and commodity perpetuals listed from "
            "January 2026 are excluded (reviewed list plus a point-in-time weekday-trading "
            "rule). Ticks and lots are inferred from archive prices and volumes (the futures "
            "API is unavailable here). Cost tiers follow each contract's median rank."
        ),
        "instruments": instruments,
        "membership": {month: [s for s, _ in ranked] for month, ranked in membership.items()},
    }
