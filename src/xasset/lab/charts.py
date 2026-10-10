"""Chart data for one trade or one skipped setup: bars, levels, timeline, fills.

The window runs from 30 minutes before the setup armed to 15 minutes after it ended.
Bars come from the local lab store (the same archives the replay read); when they are
missing the payload says which command fetches them. Levels are the strategy's
declared price anchors (``Strategy.levels``) taken from the setup timeline; a mirror
strategy recorded them on the inverted scale (1/P), so they are inverted back and
their labels read the other way round (a "low" on 1/P is a high on P).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from xasset.lab.bars import read_lab_bars
from xasset.lab.mirror import MirrorStrategy
from xasset.lab.strategies import REGISTRY
from xasset.lab.universe import Universe
from xasset.store.writer import load_bars

BEFORE = timedelta(minutes=30)
AFTER = timedelta(minutes=15)
LONGEST = timedelta(hours=8)
SWAPS = {
    "low": "high", "high": "low", "lows": "highs", "highs": "lows",
    "bottom": "top", "top": "bottom", "below": "above", "above": "below",
    "drop": "rally", "rally": "drop", "pre-drop": "pre-rally", "pullback": "bounce",
    "peak": "trough", "trough": "peak", "breakout": "breakdown", "resistance": "support",
}  # fmt: skip


def flip_words(text: str) -> str:
    """Read a label on the inverted scale (whole words only, case kept)."""

    def swap(match: re.Match[str]) -> str:
        word = match.group(0)
        out = SWAPS.get(word.lower())
        if out is None:
            return word
        if word.isupper() and len(word) > 1:
            return out.upper()
        return out.capitalize() if word[:1].isupper() else out

    return re.sub(r"[A-Za-z][A-Za-z-]*", swap, text)


def bars(
    root: Path, universe: Universe, symbol: str, start: datetime, end: datetime
) -> list[list[Any]]:
    """[[end_iso, open, high, low, close, buy_notional, sell_notional], ...]."""
    item = universe.get(symbol)
    if item.history == "store":
        from xasset.lab.backtest import store_instrument

        frame = load_bars(root, store_instrument(item)).filter(
            (pl.col("ts_end") > start) & (pl.col("ts_end") <= end)
        )
        buy = sell = [None] * frame.height
    else:
        frame = read_lab_bars(root, item.history, item.archive_symbol, start, end)
        buy, sell = frame["buy_notional"].to_list(), frame["sell_notional"].to_list()
    rows = []
    stamps = frame["ts_end"].to_list()
    for k in range(frame.height):
        rows.append(
            [
                stamps[k].isoformat(),
                frame["open"][k],
                frame["high"][k],
                frame["low"][k],
                frame["close"][k],
                buy[k],
                sell[k],
            ]
        )
    return rows


def levels_from(timeline: list[dict[str, Any]], strategy_id: str) -> list[dict[str, Any]]:
    cls = REGISTRY.get(strategy_id)
    if cls is None:
        return []
    mirrored = issubclass(cls, MirrorStrategy)
    declared = cls.levels
    seen: dict[str, dict[str, Any]] = {}
    for event in timeline:
        detail = dict(event.get("detail") or {})
        detail.update(detail.pop("anchors", None) or {})
        for key, (series, label) in declared.items():
            value = detail.get(key)
            if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
                continue
            real = 1.0 / value if mirrored else float(value)
            entry = seen.get(key)
            if entry is not None and entry["value"] == real:
                continue
            seen[key] = {
                "key": key,
                "series": series,
                "label": flip_words(label) if mirrored else label,
                "value": real,
                "from": event.get("at"),
            }
    return list(seen.values())


def chart(
    root: Path,
    universe: Universe,
    strategy_id: str,
    symbol: str,
    timeline: list[dict[str, Any]],
    trade: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not timeline and trade is None:
        raise ValueError("A chart needs a timeline or a trade")
    first = datetime.fromisoformat(timeline[0]["at"]) if timeline else None
    last = datetime.fromisoformat(timeline[-1]["at"]) if timeline else None
    if trade is not None:
        first = min(filter(None, [first, datetime.fromisoformat(trade["signal_time"])]))
        last = max(filter(None, [last, datetime.fromisoformat(trade["exit_time"])]))
    assert first is not None and last is not None
    start = (first - BEFORE).replace(second=0, microsecond=0)
    end = min(last + AFTER, start + LONGEST)
    anchors: dict[str, Any] = {}
    for event in timeline:
        anchors.update((event.get("detail") or {}).get("anchors") or {})
        anchors.update(event.get("detail") or {})
    series = {"asset": symbol, "benchmark": universe.benchmark}
    leader = anchors.get("leader")
    if isinstance(leader, str) and leader in {i.id for i in universe.instruments}:
        series["leader"] = leader
    payload: dict[str, Any] = {
        "strategy": strategy_id,
        "mirror": issubclass(REGISTRY[strategy_id], MirrorStrategy)
        if strategy_id in REGISTRY
        else False,
        "window": [start.isoformat(), end.isoformat()],
        "series": {},
        "levels": levels_from(timeline, strategy_id),
        "timeline": timeline,
        "trade": trade,
        "missing": [],
    }
    for name, sym in series.items():
        if name != "asset" and sym == symbol:
            continue  # the traded instrument is itself the benchmark or the leader
        rows = bars(root, universe, sym, start, end)
        payload["series"][name] = {"symbol": sym, "bars": rows}
        if not rows:
            payload["missing"].append(sym)
    return payload
