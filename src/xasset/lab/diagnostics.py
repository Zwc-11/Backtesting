"""Per-run diagnostics: do entries predict direction, and under which market regimes.

Markouts measure the price move after each signal, independent of exits and costs:
direction * log(close[t + h] / close[t]) in basis points, where t is the signal bar
(the last bar the strategy saw). Placebo entries use the same instrument, direction
and minute of day on randomly drawn other days of the reported period. Standard
errors are clustered by day: per-day means first, then their spread across days.

Regimes split realized trades by the wider market at the decision (recorded with
every signal): the benchmark's return since the previous session close, over the
last four hours, and whether its last hour was more volatile than usual.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from xasset.lab.bars import read_lab_bars
from xasset.lab.universe import Universe
from xasset.store.writer import load_bars

HORIZONS = (1, 5, 15, 30, 60)
PLACEBO_DRAWS = 20


def log_closes(
    root: Path, universe: Universe, symbol: str, origin: datetime, minutes: int
) -> np.ndarray:
    """Log close per minute since ``origin`` (index k is the bar ending origin + k min)."""
    from datetime import timedelta

    item = universe.get(symbol)
    end = origin + timedelta(minutes=minutes)
    if item.history == "store":
        from xasset.lab.backtest import store_instrument

        frame = load_bars(root, store_instrument(item)).filter(
            (pl.col("ts_end") > origin) & (pl.col("ts_end") <= end)
        )
    else:
        frame = read_lab_bars(root, item.history, item.archive_symbol, origin, end)
    series = np.full(minutes + 1, np.nan)
    if frame.height:
        index = ((frame["ts_end"] - origin).dt.total_minutes()).to_numpy()
        close = frame["close"].to_numpy().astype(float)
        keep = (index >= 0) & (index <= minutes) & np.isfinite(close) & (close > 0)
        series[index[keep].astype(int)] = np.log(close[keep])
    return series


def clustered(values: np.ndarray, days: np.ndarray) -> tuple[float | None, float | None]:
    good = np.isfinite(values)
    if not good.any():
        return None, None
    by_day: dict[int, list[float]] = defaultdict(list)
    for day, value in zip(days[good], values[good], strict=True):
        by_day[int(day)].append(float(value))
    means = np.array([np.mean(v) for v in by_day.values()])
    mean = float(np.mean(values[good]))
    if means.size < 2:
        return mean, None
    return mean, float(means.std(ddof=1) / math.sqrt(means.size))


def markouts(
    root: Path,
    universe: Universe,
    trades: list[dict[str, Any]],
    origin: datetime,
    end: datetime,
    seed: int = 7,
) -> dict[str, Any]:
    minutes = int((end - origin).total_seconds() // 60)
    total_days = max(1, minutes // 1440)
    random = np.random.default_rng(seed)
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_symbol[trade["symbol"]].append(trade)
    collected: dict[str, dict[str, list[np.ndarray]]] = defaultdict(lambda: defaultdict(list))
    for symbol, group in by_symbol.items():
        series = log_closes(root, universe, symbol, origin, minutes + max(HORIZONS))
        at = np.array(
            [
                int((datetime.fromisoformat(t["signal_time"]) - origin).total_seconds() // 60)
                for t in group
            ]
        )
        direction = np.array([float(t["direction"]) for t in group])
        names = np.array([t["strategy"] for t in group])
        signal = _markout(series, at, direction)
        draws = []
        draw_days = []
        for _ in range(PLACEBO_DRAWS):
            shift = random.integers(1, total_days, size=at.size) if total_days > 1 else 0
            placebo_at = (at % 1440) + ((at // 1440 + shift) % total_days) * 1440
            draws.append(_markout(series, placebo_at, direction))
            draw_days.append(placebo_at // 1440)
        for name in np.unique(names):
            mask = names == name
            collected[name]["signal"].append(signal[mask])
            collected[name]["days"].append(at[mask] // 1440)
            collected[name]["placebo"].append(np.vstack([d[mask] for d in draws]))
            collected[name]["placebo_days"].append(np.concatenate([d[mask] for d in draw_days]))
    output: dict[str, Any] = {}
    for name, parts in collected.items():
        signal = np.vstack(parts["signal"])
        days = np.concatenate(parts["days"])
        placebo = np.vstack([p.reshape(-1, len(HORIZONS)) for p in parts["placebo"]])
        placebo_days = np.concatenate(parts["placebo_days"])
        rows = []
        for k, h in enumerate(HORIZONS):
            mean, se = clustered(signal[:, k], days)
            placebo_mean, _ = clustered(placebo[:, k], placebo_days)
            rows.append({"minutes": h, "mean_bps": mean, "se_bps": se, "placebo_bps": placebo_mean})
        output[name] = {"signals": int(signal.shape[0]), "horizons": rows}
    return output


def _markout(series: np.ndarray, at: np.ndarray, direction: np.ndarray) -> np.ndarray:
    out = np.full((at.size, len(HORIZONS)), np.nan)
    ok = (at >= 0) & (at + max(HORIZONS) < series.size)
    for k, h in enumerate(HORIZONS):
        out[ok, k] = (series[at[ok] + h] - series[at[ok]]) * direction[ok] * 1e4
    return out


def _value(trade: dict[str, Any], key: str) -> float:
    value = trade.get(key)
    return float(value) if value is not None else math.nan


# Regime -> (context field, lower bound, upper bound); open intervals, NaN never matches.
REGIMES: dict[str, tuple[str, float, float]] = {
    "market up since prior close": ("market_day", 0.0, math.inf),
    "market down since prior close": ("market_day", -math.inf, 0.0),
    "market up last 4h": ("market_240m", 0.0, math.inf),
    "market down last 4h": ("market_240m", -math.inf, 0.0),
    "volatile market (last hour)": ("market_vol_ratio", 1.5, math.inf),
    "calm market (last hour)": ("market_vol_ratio", 0.0, 1.5),
}


def regimes(trades: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, float | int]]]:
    """Per strategy and regime: trades, win rate and mean net return in bps."""
    output: dict[str, dict[str, dict[str, float | int]]] = defaultdict(dict)
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_strategy[trade["strategy"]].append(trade)
    for strategy, group in by_strategy.items():
        for name, (field, low, high) in REGIMES.items():
            chosen = [t for t in group if low < _value(t, field) < high]
            if not chosen:
                continue
            net = [float(t["net_return"]) * 1e4 for t in chosen]
            output[strategy][name] = {
                "trades": len(chosen),
                "win_rate": sum(1 for v in net if v > 0) / len(net),
                "mean_net_bps": float(np.mean(net)),
            }
    return dict(output)
