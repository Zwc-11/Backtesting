"""Event counts, trade and daily statistics, episodes and a family-wise max-T test.

Statistics follow the handbook: daily (not minute) returns, a Bartlett HAC long-run
variance with a declared lag, disjoint 14-day episodes reported as historical
frequencies only, and a centred moving-block bootstrap of the maximum studentized
statistic across all registered candidates for multiplicity.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from xasset.lab.ledger import Ledger, TradeRecord


def hac_lag(n: int) -> int:
    return max(1, int(math.floor(4 * (n / 100) ** (2 / 9))))


def long_run_variance(values: np.ndarray, lag: int) -> float:
    centered = values - values.mean()
    n = centered.size
    omega = float(centered @ centered) / n
    for k in range(1, min(lag, n - 1) + 1):
        gamma = float(centered[k:] @ centered[:-k]) / n
        omega += 2 * (1 - k / (lag + 1)) * gamma
    return omega


def hac_t(values: list[float], hurdle: float = 0.0) -> float | None:
    array = np.asarray(values, dtype=float)
    if array.size < 3:
        return None
    omega = long_run_variance(array, hac_lag(array.size))
    if omega <= 0:
        return None
    return float(math.sqrt(array.size) * (array.mean() - hurdle) / math.sqrt(omega))


def max_drawdown(levels: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for level in levels:
        peak = max(peak, level)
        if peak > 0:
            worst = max(worst, 1 - level / peak)
    return worst


def calendar_days(start: datetime, end: datetime) -> list[date]:
    days, day = [], start.date()
    last = (end - timedelta(microseconds=1)).date()
    while day <= last:
        days.append(day)
        day += timedelta(days=1)
    return days


def daily_pnl(trades: list[TradeRecord], days: list[date]) -> list[float]:
    """Net P&L attributed to the UTC day of each trade's exit (zero on days without)."""
    pnl: dict[date, float] = defaultdict(float)
    for trade in trades:
        pnl[trade.exit_time.date()] += trade.net
    return [pnl.get(day, 0.0) for day in days]


def trade_summary(
    trades: list[TradeRecord], initial_nav: float, days: list[date]
) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    nets = [t.net for t in trades]
    returns_bps = [10_000 * t.net_return for t in trades]
    risk_multiples = [
        t.net / (t.qty * t.entry_price * t.risk_distance) for t in trades if t.risk_distance > 0
    ]
    removed = max(1, math.ceil(0.05 * len(nets)))
    by_day: dict[date, float] = defaultdict(float)
    by_asset: dict[str, float] = defaultdict(float)
    for trade in trades:
        by_day[trade.exit_time.date()] += trade.net
        by_asset[trade.symbol] += trade.net
    total = sum(nets)
    daily = daily_pnl(trades, days)
    daily_returns = [value / initial_nav for value in daily]
    cumulative = np.cumsum([initial_nav, *daily]).tolist()
    ambiguous = [t for t in trades if t.ambiguous_bar]
    bound = sum(
        t.direction * t.qty * ((t.alternative_exit_price or t.exit_price) - t.exit_price)
        for t in ambiguous
    )
    return {
        "trades": len(trades),
        "wins": sum(1 for v in nets if v > 0),
        "win_rate": sum(1 for v in nets if v > 0) / len(nets),
        "net_pnl": total,
        "gross_pnl": sum(t.gross for t in trades),
        "fees": sum(t.fees for t in trades),
        "funding": sum(t.funding for t in trades),
        "mean_net_bps": statistics.mean(returns_bps),
        "median_net_bps": statistics.median(returns_bps),
        "mean_r": statistics.mean(risk_multiples) if risk_multiples else None,
        "average_hold_minutes": statistics.mean(
            (t.exit_time - t.entry_time).total_seconds() / 60 for t in trades
        ),
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
        "longs": sum(1 for t in trades if t.direction > 0),
        "shorts": sum(1 for t in trades if t.direction < 0),
        "net_without_best_5pct": sum(sorted(nets, reverse=True)[removed:]),
        "largest_day_share": max(by_day.values()) / total if total > 0 else None,
        "largest_asset_share": max(by_asset.values()) / total if total > 0 else None,
        "ambiguous_bars": len(ambiguous),
        "ambiguity_bound_pnl": bound,
        "daily_t_hac": hac_t(daily_returns),
        "daily_sharpe": (
            statistics.mean(daily_returns) / statistics.stdev(daily_returns)
            if len(daily_returns) > 2 and statistics.stdev(daily_returns) > 0
            else None
        ),
        "max_drawdown": max_drawdown(cumulative),
        "active_days": sum(1 for v in daily if v != 0),
    }


def event_summary(ledger: Ledger, strategy: str) -> dict[str, Any]:
    counts = ledger.event_counts(strategy)
    expiries = ledger.expiry_reasons(strategy)
    orders = [o for o in ledger.orders if o["strategy"] == strategy]
    return {
        "armed": counts.get("armed", 0),
        "expired": counts.get("expired", 0),
        "confirmed": counts.get("confirmed", 0),
        "orders": sum(1 for o in orders if o["status"] == "submitted"),
        "rejected": sum(1 for o in orders if o["status"] == "rejected"),
        "cancelled": sum(1 for o in orders if o["status"] == "cancelled"),
        "filled": counts.get("filled", 0),
        "exited": counts.get("exited", 0),
        "expiry_reasons": dict(expiries.most_common(8)),
        "rejection_reasons": dict(
            Counter(o.get("reason") for o in orders if o["status"] == "rejected").most_common(6)
        ),
        "cancel_reasons": dict(
            Counter(o.get("reason") for o in orders if o["status"] == "cancelled").most_common(6)
        ),
    }


def nav_daily(ledger: Ledger) -> list[tuple[date, float]]:
    last: dict[date, float] = {}
    for at, value in ledger.nav:
        last[(at - timedelta(microseconds=1)).date()] = value
    return sorted(last.items())


def episodes(
    marks: list[tuple[datetime, float]], initial: float, start: datetime, length: int = 14
) -> dict[str, Any]:
    """Disjoint ``length``-day episodes on a fixed UTC boundary from ``start``.

    Uses every minute NAV mark, so drawdowns include intraday marks (intraminute
    paths remain unobserved). Results are historical frequencies in this sample.
    """
    if not marks:
        return {"episodes": 0}
    results: list[dict[str, Any]] = []
    begin = start
    base = initial
    index = 0
    last = marks[-1][0]
    span = timedelta(days=length)
    while begin + span <= last + timedelta(minutes=1):
        path = [base]
        while index < len(marks) and marks[index][0] <= begin + span:
            path.append(marks[index][1])
            index += 1
        r = path[-1] / path[0] - 1
        results.append(
            {"start": begin.isoformat(), "return": r, "max_drawdown": max_drawdown(path)}
        )
        base = path[-1]
        begin += span
    returns: list[float] = [float(e["return"]) for e in results]
    return {
        "episodes": len(results),
        "at_least_10pct": sum(1 for r in returns if r >= 0.10),
        "losing": sum(1 for r in returns if r < 0),
        "best": max(returns) if returns else None,
        "worst": min(returns) if returns else None,
        "detail": results,
        "note": "Historical frequencies in this sample, not probabilities of future outcomes.",
    }


def max_t_test(
    series: dict[str, list[float]],
    hurdle: float = 0.0,
    block: int = 5,
    draws: int = 2000,
    seed: int = 20261009,
) -> dict[str, dict[str, float | None]]:
    """One-sided studentized max-T p-values with a centred moving-block bootstrap.

    Every registered candidate enters; aligned daily series are resampled jointly in
    contiguous day blocks so cross-sectional and serial dependence are preserved.
    """
    names = sorted(series)
    if not names:
        return {}
    matrix = np.array([series[name] for name in names], dtype=float)
    k, n = matrix.shape
    if n < 2 * block or n < 10:
        return {name: {"t": None, "p_adjusted": None} for name in names}
    lag = hac_lag(n)
    observed = []
    for row in matrix:
        omega = long_run_variance(row, lag)
        observed.append(
            math.sqrt(n) * (row.mean() - hurdle) / math.sqrt(omega) if omega > 0 else None
        )
    centered = matrix - matrix.mean(axis=1, keepdims=True)
    rng = np.random.default_rng(seed)
    starts_max = n - block + 1
    maxima = np.empty(draws)
    for b in range(draws):
        starts = rng.integers(0, starts_max, size=math.ceil(n / block))
        index = np.concatenate([np.arange(s, s + block) for s in starts])[:n]
        sample = centered[:, index]
        stats = []
        for row in sample:
            omega = long_run_variance(row, lag)
            stats.append(math.sqrt(n) * row.mean() / math.sqrt(omega) if omega > 0 else -math.inf)
        maxima[b] = max(stats)
    output: dict[str, dict[str, float | None]] = {}
    for name, t in zip(names, observed, strict=True):
        if t is None:
            output[name] = {"t": None, "p_adjusted": None}
        else:
            exceed = int((maxima >= t).sum())
            output[name] = {"t": float(t), "p_adjusted": (1 + exceed) / (draws + 1)}
    return output
