"""Notebook strategy 3: Downside Separation With Upside Reconnection.

Each coin's and the market's (BTC) daily returns are mapped to tail indicators with
quantiles of their own previous 365 days (prior-only, at least 200 observations):
U <= q and U >= 1 - q with q = 0.10. Finite-tail co-movement probabilities

    l- = P(coin in its lower tail | market in its lower tail)
    l+ = P(coin in its upper tail | market in its upper tail)

are estimated over a recent 90-day window and a reference window (the 365 days
before it), each shrunk toward the cross-sectional average with a prior worth ten
market-tail days, so a few extreme observations cannot swing them. The signal is

    S = (l+_recent - l+_reference) - (l-_recent - l-_reference)

and a candidate needs both parts to improve (l+ up and l- down). Positions last five
sessions and leave early when either part reverses.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import numpy as np

from xasset.notebook.engine import Rule
from xasset.notebook.features import Context, rolling_quantile, rolling_stat, shift

Q = 0.10
RECENT, REFERENCE, HISTORY = 90, 365, 365
PRIOR_DAYS = 10.0
HORIZON = 5


def tails(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Indicators (1/0, NaN when unknown) of the lower and upper 10% vs prior history."""
    previous = shift(values, 1)
    low_cut = rolling_quantile(previous, HISTORY, 200, Q)
    high_cut = rolling_quantile(previous, HISTORY, 200, 1 - Q)
    known = np.isfinite(values) & np.isfinite(low_cut) & np.isfinite(high_cut)
    with np.errstate(invalid="ignore"):
        low = np.where(known, (values <= low_cut).astype(float), np.nan)
        high = np.where(known, (values >= high_cut).astype(float), np.nan)
    return low, high


class DownsideSeparation:
    id = "n03"
    title = "Downside separation with upside reconnection"
    horizon = HORIZON
    rule = Rule(horizon=HORIZON)

    def __init__(self, root: Path | None = None):
        self.root = root

    def prepare(self, context: Context) -> None:
        r = context.r
        low, high = tails(r)
        m_low, m_high = tails(context.rm[:, None])
        n = r.shape[1]
        m_low = np.repeat(m_low, n, axis=1)
        m_high = np.repeat(m_high, n, axis=1)
        parts = {}
        for name, coin, market in (("down", low, m_low), ("up", high, m_high)):
            both = np.isfinite(coin) & np.isfinite(market)
            joint = np.where(both, coin * market, np.nan)
            base = np.where(both, market, np.nan)
            recent_k = rolling_stat(joint, RECENT, 60, "sum")
            recent_n = rolling_stat(base, RECENT, 60, "sum")
            reference_k = shift(rolling_stat(joint, REFERENCE, 240, "sum"), RECENT)
            reference_n = shift(rolling_stat(base, REFERENCE, 240, "sum"), RECENT)
            with np.errstate(divide="ignore", invalid="ignore"):
                raw_reference = reference_k / reference_n
            # Cross-sectional prior: the average reference probability among eligible coins.
            pooled = np.where(context.eligible, raw_reference, np.nan)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)  # days with no eligible coin
                prior = np.nanmean(pooled, axis=1, keepdims=True)
            prior = np.where(np.isfinite(prior), prior, Q)
            with np.errstate(divide="ignore", invalid="ignore"):
                recent = (recent_k + PRIOR_DAYS * prior) / (recent_n + PRIOR_DAYS)
                reference = (reference_k + PRIOR_DAYS * prior) / (reference_n + PRIOR_DAYS)
            parts[name] = (recent, reference)
        self.up_change = parts["up"][0] - parts["up"][1]
        self.down_change = parts["down"][0] - parts["down"][1]
        self.score = self.up_change - self.down_change
        self.score[:, context.market] = np.nan  # the market's own row is not a candidate
        self.forward = context.forward(HORIZON)

    def outcome(self, context: Context) -> np.ndarray:
        return self.forward

    def signals(self, context: Context, fit_until: int) -> dict[str, np.ndarray]:
        # Model-free and prior-only by construction; nothing is fitted.
        return {"S": self.score, "up": self.up_change, "down": self.down_change}

    def precondition(self, context: Context, signals: dict[str, np.ndarray]) -> np.ndarray:
        with np.errstate(invalid="ignore"):
            return (signals["up"] > 0) & (signals["down"] < 0)

    def exit_mask(
        self, context: Context, signals: dict[str, np.ndarray], qualifies: np.ndarray
    ) -> np.ndarray | None:
        """Leave when either tail change reverses (or the coin leaves the universe)."""
        with np.errstate(invalid="ignore"):
            holds = (signals["up"] > 0) & (signals["down"] < 0)
        leave: np.ndarray = ~(holds & context.eligible)
        return leave

    def diagnostics(
        self, context: Context, signals: dict[str, np.ndarray], rows: np.ndarray
    ) -> dict[str, Any]:
        output: dict[str, Any] = {}
        use = rows & np.isfinite(context.beta60)
        selected_beta = context.beta60[use]
        universe = context.eligible & np.isfinite(context.beta60)
        universe &= np.any(rows, axis=1, keepdims=True)
        if selected_beta.size:
            output["candidate_mean_beta60"] = float(selected_beta.mean())
            output["universe_mean_beta60"] = float(context.beta60[universe].mean())
        s = signals["S"]
        both = rows & np.isfinite(s) & np.isfinite(context.beta60)
        if both.sum() > 100:
            output["signal_beta_correlation"] = float(
                np.corrcoef(s[both], context.beta60[both])[0, 1]
            )
        return output
