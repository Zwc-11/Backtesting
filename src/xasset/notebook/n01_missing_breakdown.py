"""Notebook strategy 1: Missing Breakdown.

A market decline (BTC or the equal-weight index of the other eligible coins closing
in the lowest 10% of its previous 365 days) opens an observation window of h = 3
days for every eligible coin. A regularized logistic model, refitted at the start of
each calendar year on events resolved before it, predicts the probability that the
coin's lows fall below its close by more than a = 1 three-day scale

    p_j = P(min_{0<u<=3} log(L_{s+u} / C_s) < -a sigma_3 | F_s).

After the window ends the surprise e_j = p_j - B_j is known. Surprises are averaged
within an episode (decline days at most three days apart count as one shock, so a
long selloff is not counted as many independent tests) and aggregated with a 90-day
half-life over the past year,

    S = sum_E w_E e_E / sqrt(sum_E w_E^2 v_E + 0.01),  v_E = mean p(1 - p),

where v_E is the surprise variance under calibration. Trades also require a positive
response to market gains (upside capture of at least one half over 180 days) and a
five-session forecast that beats the controls-only model; positions leave when the
forecast stops qualifying.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from xasset.notebook.engine import Rule
from xasset.notebook.features import Context, rolling_quantile, rolling_stat, shift
from xasset.notebook.models import Logistic, complete

H_WINDOW, A_SCALE = 3, 1.0
HALF_LIFE, LOOKBACK = 90, 365
EPISODE_GAP = 3
HORIZON = 5
FEATURES = (
    "beta60", "sigma20", "r1", "r5", "r20", "market1", "index1", "log_liquidity",
    "dist_high20", "range5", "buy_share5",
)  # fmt: skip


class MissingBreakdown:
    id = "n01"
    title = "Missing breakdown"
    horizon = HORIZON
    rule = Rule(horizon=HORIZON)

    def __init__(self, root: Path | None = None):
        self.root = root

    def prepare(self, context: Context) -> None:
        panel = context.panel
        t_count, n_count = context.shape
        market_cut = rolling_quantile(shift(context.rm[:, None], 1), 365, 200, 0.10)[:, 0]
        index_cut = rolling_quantile(shift(context.index[:, None], 1), 365, 200, 0.10)[:, 0]
        with np.errstate(invalid="ignore"):
            trigger = (context.rm <= market_cut) | (context.index <= index_cut)
        trigger &= np.isfinite(market_cut) & np.isfinite(index_cut)
        self.trigger_days = np.flatnonzero(trigger)
        # Episodes: trigger days at most EPISODE_GAP apart belong together.
        episode = np.full(t_count, -1)
        current, last = -1, -(10**9)
        for day in self.trigger_days.tolist():
            if day - last > EPISODE_GAP:
                current += 1
            episode[day] = current
            last = day
        self.episode = episode
        r3 = rolling_stat(context.r, 3, 3, "sum")
        sigma3 = rolling_stat(r3, 90, 60, "std")
        with np.errstate(divide="ignore", invalid="ignore"):
            lows = np.stack(
                [np.log(shift(panel.low, -u) / panel.close) for u in range(1, H_WINDOW + 1)]
            )
        path = np.isfinite(lows).all(axis=0)
        breakdown = np.where(
            path & np.isfinite(sigma3), lows.min(axis=0) < -A_SCALE * sigma3, False
        ).astype(float)
        breakdown[~(path & np.isfinite(sigma3))] = np.nan
        self.breakdown = breakdown
        x = np.stack([context.controls[name] for name in FEATURES], axis=-1)
        event_rows = np.zeros((t_count, n_count), dtype=bool)
        event_rows[self.trigger_days] = True
        event_rows &= context.eligible & complete(x.reshape(-1, x.shape[-1])).reshape(
            t_count, n_count
        )
        # Out-of-sample probabilities: each year uses a model fitted on earlier events.
        probability = np.full((t_count, n_count), np.nan)
        years = sorted({d.year for d in panel.dates})
        self.models: dict[int, dict[str, Any]] = {}
        for year in years:
            first = next(i for i, d in enumerate(panel.dates) if d.year == year)
            last_day = max(i for i, d in enumerate(panel.dates) if d.year == year)
            resolved = event_rows.copy()
            resolved[max(first - H_WINDOW, 0) :] = False  # outcomes known before the year
            use = resolved & np.isfinite(breakdown)
            if use.sum() < 300:
                continue
            model = Logistic.fit(x[use], breakdown[use].astype(int))
            if model is None:
                continue
            target = event_rows.copy()
            target[:first] = False
            target[last_day + 1 :] = False
            if target.any():
                probability[target] = model.probability(x[target])
            self.models[year] = {
                "events": int(use.sum()),
                "base_rate": float(breakdown[use].mean()),
            }
        self.probability = probability
        surprise = probability - breakdown
        variance = probability * (1 - probability)
        # Aggregate per episode; an episode counts once its last window has resolved.
        episodes = sorted({int(e) for e in episode[self.trigger_days]})
        mean_surprise = np.full((len(episodes), n_count), np.nan)
        mean_variance = np.full((len(episodes), n_count), np.nan)
        resolved_at = np.zeros(len(episodes), dtype=int)
        for k, e in enumerate(episodes):
            days = np.flatnonzero(episode == e)
            block = surprise[days]
            known = np.isfinite(block)
            counts = known.sum(axis=0)
            with np.errstate(invalid="ignore", divide="ignore"):
                mean_surprise[k] = np.where(counts > 0, np.nansum(block, axis=0) / counts, np.nan)
                mean_variance[k] = np.where(
                    counts > 0,
                    np.nansum(np.where(known, variance[days], 0), axis=0) / counts,
                    np.nan,
                )
            resolved_at[k] = days.max() + H_WINDOW
        score = np.full((t_count, n_count), np.nan)
        for t in range(t_count):
            usable = (resolved_at <= t) & (resolved_at > t - LOOKBACK)
            if not usable.any():
                continue
            ages = float(t) - resolved_at[usable]
            weights = 0.5 ** (ages / HALF_LIFE)
            surprises = mean_surprise[usable]
            variances = mean_variance[usable]
            known = np.isfinite(surprises)
            numerator = np.nansum(np.where(known, weights[:, None] * surprises, 0.0), axis=0)
            denominator = np.nansum(np.where(known, (weights**2)[:, None] * variances, 0.0), axis=0)
            has = known.any(axis=0)
            score[t, has] = numerator[has] / np.sqrt(denominator[has] + 0.01)
        self.score = score
        # Upside capture over 180 days: mean coin return on market-up days / market mean.
        market = np.repeat(context.rm[:, None], n_count, axis=1)
        up_days = market > 0
        coin_up = rolling_stat(np.where(up_days, context.r, np.nan), 180, 40, "mean")
        market_up = rolling_stat(np.where(up_days, market, np.nan), 180, 40, "mean")
        with np.errstate(divide="ignore", invalid="ignore"):
            self.capture = coin_up / market_up
        self.forward = context.forward(HORIZON)

    def outcome(self, context: Context) -> np.ndarray:
        return self.forward

    def signals(self, context: Context, fit_until: int) -> dict[str, np.ndarray]:
        # Yearly refits use only events resolved before each year: already causal.
        return {"S": self.score, "capture": self.capture}

    def precondition(self, context: Context, signals: dict[str, np.ndarray]) -> np.ndarray:
        with np.errstate(invalid="ignore"):
            return signals["capture"] >= 0.5

    def exit_mask(
        self, context: Context, signals: dict[str, np.ndarray], qualifies: np.ndarray
    ) -> np.ndarray | None:
        """Leave when the forecast no longer qualifies (or the coin leaves the universe)."""
        return ~qualifies

    def diagnostics(
        self, context: Context, signals: dict[str, np.ndarray], rows: np.ndarray
    ) -> dict[str, Any]:
        output: dict[str, Any] = {}
        days = np.flatnonzero(rows.any(axis=1))
        if days.size:
            window = np.zeros(context.shape, dtype=bool)
            window[days.min() : days.max() + 1] = True
            use = window & np.isfinite(self.probability) & np.isfinite(self.breakdown)
            if use.sum() > 50:
                p, b = self.probability[use], self.breakdown[use]
                base = float(b.mean())
                brier = float(np.mean((p - b) ** 2))
                climate = float(np.mean((base - b) ** 2))
                output["breakdown_model"] = {
                    "events": int(use.sum()),
                    "observed_rate": base,
                    "mean_predicted": float(p.mean()),
                    "brier": brier,
                    "climatology_brier": climate,
                    "skill": 1 - brier / climate if climate > 0 else None,
                }
        s = signals["S"]
        for name, other in (("sigma20", context.sigma20), ("beta60", context.beta60)):
            both = rows & np.isfinite(s) & np.isfinite(other)
            if both.sum() > 100:
                output[f"signal_{name}_correlation"] = float(
                    np.corrcoef(s[both], other[both])[0, 1]
                )
        output["trigger_days"] = int(self.trigger_days.size)
        output["episodes"] = int(len({int(e) for e in self.episode[self.trigger_days]}))
        return output
