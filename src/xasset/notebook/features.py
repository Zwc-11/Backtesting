"""Prior-only panel features: eligibility, controls and forward outcomes.

Every value at day ``t`` uses bars up to and including ``t`` (the decision is made
after day ``t`` completes). Forward outcomes start at the next day's open, so a
signal is never traded at the close that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl

from xasset.notebook.daily import Panel


def rolling_stat(values: np.ndarray, window: int, minimum: int, kind: str) -> np.ndarray:
    """Column-wise rolling statistic over the trailing ``window`` rows (inclusive)."""
    frame = pl.DataFrame(values, schema=[f"c{i}" for i in range(values.shape[1])], orient="row")
    frame = frame.with_columns(pl.all().fill_nan(None))
    if kind == "mean":
        out = frame.select(pl.all().rolling_mean(window, min_samples=minimum))
    elif kind == "std":
        out = frame.select(pl.all().rolling_std(window, min_samples=minimum))
    elif kind == "median":
        out = frame.select(pl.all().rolling_median(window, min_samples=minimum))
    elif kind == "sum":
        out = frame.select(pl.all().rolling_sum(window, min_samples=minimum))
    elif kind == "max":
        out = frame.select(pl.all().rolling_max(window, min_samples=minimum))
    elif kind == "min":
        out = frame.select(pl.all().rolling_min(window, min_samples=minimum))
    else:
        raise ValueError(kind)
    return out.to_numpy().astype(float)


def rolling_quantile(values: np.ndarray, window: int, minimum: int, q: float) -> np.ndarray:
    frame = pl.DataFrame(values, schema=[f"c{i}" for i in range(values.shape[1])], orient="row")
    frame = frame.with_columns(pl.all().fill_nan(None))
    out = frame.select(
        pl.all().rolling_quantile(
            q, interpolation="linear", window_size=window, min_samples=minimum
        )
    )
    return out.to_numpy().astype(float)


def shift(values: np.ndarray, rows: int) -> np.ndarray:
    """Move values down by ``rows`` (positive: use the past)."""
    output = np.full_like(values, np.nan)
    if rows > 0:
        output[rows:] = values[:-rows]
    elif rows < 0:
        output[:rows] = values[-rows:]
    else:
        output[:] = values
    return output


@dataclass
class UniverseRules:
    min_age_days: int = 90
    min_liquidity: float = 5_000_000.0
    top: int = 100
    benchmark: str = "BTCUSDT"
    symbols: tuple[str, ...] | None = None  # optional fixed list (e.g. coins with minute data)


@dataclass
class Context:
    panel: Panel
    rules: UniverseRules
    r: np.ndarray
    market: int
    rm: np.ndarray
    index: np.ndarray
    liquidity: np.ndarray
    rank: np.ndarray
    eligible: np.ndarray
    sigma20: np.ndarray
    beta60: np.ndarray
    controls: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int]:
        rows, columns = self.r.shape
        return int(rows), int(columns)

    def forward(self, horizon: int, delay: int = 0) -> np.ndarray:
        """Gross log return from the open of t+1+delay to the open of t+1+delay+horizon."""
        o = self.panel.open
        start = shift(o, -(1 + delay))
        end = shift(o, -(1 + delay + horizon))
        with np.errstate(divide="ignore", invalid="ignore"):
            values: np.ndarray = np.log(end / start)
        return values


def build_context(panel: Panel, rules: UniverseRules) -> Context:
    r = panel.returns
    t_count, n_count = r.shape
    if rules.benchmark not in panel.symbols:
        raise ValueError("The benchmark is missing from the panel")
    market = panel.symbols.index(rules.benchmark)
    rm = r[:, market]
    liquidity = rolling_stat(panel.notional, 30, 20, "median")
    age = np.arange(t_count)[:, None] - panel.first[None, :]
    candidate = (
        (age >= rules.min_age_days)
        & (liquidity >= rules.min_liquidity)
        & np.isfinite(panel.close)
        & np.isfinite(r)
    )
    if rules.symbols is not None:
        allowed = np.array([symbol in rules.symbols for symbol in panel.symbols])
        candidate &= allowed[None, :]
    masked = np.where(candidate, liquidity, -np.inf)
    order = np.argsort(-masked, axis=1, kind="stable")
    rank = np.empty_like(order)
    rank[np.arange(t_count)[:, None], order] = np.arange(n_count)[None, :]
    rank = rank + 1
    eligible = candidate & (rank <= rules.top)
    others = eligible.copy()
    others[:, market] = False
    with np.errstate(invalid="ignore"):
        index = np.where(
            others.sum(axis=1) > 0,
            np.nansum(np.where(others, r, 0.0), axis=1) / np.maximum(others.sum(axis=1), 1),
            np.nan,
        )
    sigma20 = rolling_stat(r, 20, 15, "std")
    rm_matrix = np.repeat(rm[:, None], n_count, axis=1)
    both = np.isfinite(r) & np.isfinite(rm_matrix)
    x = np.where(both, rm_matrix, np.nan)
    y = np.where(both, r, np.nan)
    mean_x = rolling_stat(x, 60, 40, "mean")
    mean_y = rolling_stat(y, 60, 40, "mean")
    mean_xy = rolling_stat(x * y, 60, 40, "mean")
    mean_xx = rolling_stat(x * x, 60, 40, "mean")
    with np.errstate(divide="ignore", invalid="ignore"):
        beta60 = (mean_xy - mean_x * mean_y) / (mean_xx - mean_x * mean_x)
    context = Context(
        panel=panel,
        rules=rules,
        r=r,
        market=market,
        rm=rm,
        index=index,
        liquidity=liquidity,
        rank=rank,
        eligible=eligible,
        sigma20=sigma20,
        beta60=beta60,
    )
    context.controls = controls(context)
    return context


def controls(context: Context) -> dict[str, np.ndarray]:
    """Ordinary explanations (Z): recent returns, volatility, market, liquidity, range."""
    panel = context.panel
    r = context.r
    t_count, n_count = r.shape
    with np.errstate(divide="ignore", invalid="ignore"):
        r5 = rolling_stat(r, 5, 5, "sum")
        r20 = rolling_stat(r, 20, 18, "sum")
        log_range = np.log(panel.high / panel.low)
        range5 = rolling_stat(log_range, 5, 4, "mean")
        high20 = rolling_stat(panel.high, 20, 15, "max")
        low20 = rolling_stat(panel.low, 20, 15, "min")
        dist_high20 = np.log(panel.close / high20)
        dist_low20 = np.log(panel.close / low20)
        surge = np.log(panel.notional / context.liquidity)
        buy5 = rolling_stat(panel.buy_notional, 5, 4, "sum") / rolling_stat(
            panel.notional, 5, 4, "sum"
        )
        log_liquidity = np.log(context.liquidity)
    market = np.repeat(context.rm[:, None], n_count, axis=1)
    market5 = np.repeat(rolling_stat(context.rm[:, None], 5, 5, "sum"), n_count, axis=1)
    market_sigma = np.repeat(rolling_stat(context.rm[:, None], 20, 15, "std"), n_count, axis=1)
    index = np.repeat(context.index[:, None], n_count, axis=1)
    weekday = np.array([d.weekday() >= 5 for d in panel.dates], dtype=float)
    weekend = np.repeat(weekday[:, None], n_count, axis=1)
    return {
        "r1": r,
        "r5": r5,
        "r20": r20,
        "sigma20": context.sigma20,
        "beta60": context.beta60,
        "range5": range5,
        "log_liquidity": log_liquidity,
        "market1": market,
        "market5": market5,
        "market_sigma20": market_sigma,
        "index1": index,
        "weekend": weekend,
        "dist_high20": dist_high20,
        "dist_low20": dist_low20,
        "notional_surge": surge,
        "buy_share5": buy5,
    }


Z_NAMES = (
    "r1",
    "r5",
    "r20",
    "sigma20",
    "beta60",
    "range5",
    "log_liquidity",
    "market1",
    "market5",
    "market_sigma20",
    "index1",
    "weekend",
)


def spread_bps(context: Context, tiers: list[tuple[int, float]]) -> np.ndarray:
    """Per-side half spread plus impact by liquidity rank (an uncalibrated assumption)."""
    output = np.full(context.shape, np.nan)
    previous = 0
    for top, bps in tiers:
        mask = (context.rank > previous) & (context.rank <= top)
        output[mask] = bps
        previous = top
    return output
