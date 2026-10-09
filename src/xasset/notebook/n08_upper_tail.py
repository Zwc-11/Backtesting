"""Notebook strategy 8: Upper-Tail Escape (five sessions, +10% target, -4% stop).

Nested touch probabilities relative to the next open P: p+(5%) and the conditional
probability of reaching +10% after +5% (fitted only on candidates that reached +5%),
so p+(10%) <= p+(5%) holds by construction; likewise downward. The signal is

    S = log q+ - log q-,   q± = P(touch ±10% | touched ±5%), floored at 0.01.

A discrete-time competing-risk model gives the probabilities of hitting +10% first,
-4% first, or neither within five daily intervals; with the training-period average
realized return of each outcome it yields the expected net payoff. A decision needs
S above a training-selected threshold and a positive expected payoff after costs and
the training optimism allowance.

Daily bars cannot order two barriers touched on the same day; the adverse barrier
is taken and the favourable one is recorded as a bound.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from xasset.notebook.engine import Rule
from xasset.notebook.features import Z_NAMES, Context, shift
from xasset.notebook.models import CompetingRisk, Logistic, complete

EXTRA = ("dist_high20", "dist_low20", "notional_surge", "buy_share5")
UP, DOWN, HORIZON = 0.10, 0.04, 5
FLOOR = 0.01


class UpperTailEscape:
    id = "n08"
    title = "Upper-tail escape"
    horizon = HORIZON
    rule = Rule(horizon=HORIZON, target=UP, stop=DOWN)

    def __init__(self, root: Path | None = None):
        self.root = root

    def prepare(self, context: Context) -> None:
        panel = context.panel
        p = shift(panel.open, -1)  # entry reference: the next open
        with np.errstate(divide="ignore", invalid="ignore"):
            highs = np.stack([np.log(shift(panel.high, -k) / p) for k in range(1, HORIZON + 1)])
            lows = np.stack([np.log(shift(panel.low, -k) / p) for k in range(1, HORIZON + 1)])
            opens = np.stack([np.log(shift(panel.open, -k) / p) for k in range(1, HORIZON + 1)])
            time_exit = np.log(shift(panel.open, -(HORIZON + 1)) / p)
        complete_path = np.isfinite(highs).all(axis=0) & np.isfinite(lows).all(axis=0)
        max_up = np.where(complete_path, highs.max(axis=0), np.nan)
        min_down = np.where(complete_path, lows.min(axis=0), np.nan)
        self.touch = {
            "up5": np.where(complete_path, max_up >= math.log(1.05), np.nan),
            "up10": np.where(complete_path, max_up >= math.log(1.10), np.nan),
            "down5": np.where(complete_path, min_down <= math.log(0.95), np.nan),
            "down10": np.where(complete_path, min_down <= math.log(0.90), np.nan),
        }
        # Competing outcome of the +10% / -4% rule with gaps and adverse ordering.
        up, down = math.log(1 + UP), math.log(1 - DOWN)
        outcome = np.zeros(p.shape)
        interval = np.full(p.shape, HORIZON, dtype=float)
        realized = time_exit.copy()
        ambiguous = np.zeros(p.shape, dtype=bool)
        decided = np.zeros(p.shape, dtype=bool)
        for k in range(HORIZON):
            o, hi, lo = opens[k], highs[k], lows[k]
            gap_down = (k > 0) & (o <= down) & ~decided
            gap_up = (k > 0) & (o >= up) & ~decided & ~gap_down
            for mask, label, value in ((gap_down, 2, o), (gap_up, 1, o)):
                outcome[mask], interval[mask], realized[mask] = label, k + 1, value[mask]
                decided |= mask
            hit_down = (lo <= down) & ~decided
            hit_up = (hi >= up) & ~decided
            both = hit_down & hit_up
            ambiguous |= both
            outcome[hit_down], interval[hit_down], realized[hit_down] = 2, k + 1, down
            decided |= hit_down
            only_up = hit_up & ~hit_down
            outcome[only_up], interval[only_up], realized[only_up] = 1, k + 1, up
            decided |= only_up
        valid = complete_path & np.isfinite(time_exit) | (decided & complete_path)
        self.competing = np.where(valid, outcome, np.nan)
        self.interval = np.where(valid, interval, np.nan)
        self.realized = np.where(valid, realized, np.nan)
        self.ambiguous = ambiguous & valid
        self.features = np.stack([context.controls[name] for name in (*Z_NAMES, *EXTRA)], axis=-1)

    def outcome(self, context: Context) -> np.ndarray:
        return self.realized

    def precondition(self, context: Context, signals: dict[str, np.ndarray]) -> np.ndarray:
        return np.isfinite(signals["S"]) & np.isfinite(signals["payoff"])

    def signals(self, context: Context, fit_until: int) -> dict[str, np.ndarray]:
        shape = context.shape
        last = fit_until - HORIZON - 2
        train = np.zeros(shape, dtype=bool)
        train[: max(last + 1, 0)] = True
        x = self.features
        flat = x.reshape(-1, x.shape[-1])
        good = complete(flat).reshape(shape) & context.eligible
        rows = train & good
        apply = good

        def fit(target: np.ndarray, mask: np.ndarray) -> Logistic | None:
            use = mask & np.isfinite(target)
            if use.sum() < 200:
                return None
            return Logistic.fit(x[use], target[use].astype(int))

        def probability(model: Logistic | None) -> np.ndarray:
            output = np.full(shape, np.nan)
            if model is not None and apply.any():
                output[apply] = model.probability(x[apply])
            return output

        up5 = self.touch["up5"]
        down5 = self.touch["down5"]
        q_up = probability(fit(self.touch["up10"], rows & (up5 == 1)))
        q_down = probability(fit(self.touch["down10"], rows & (down5 == 1)))
        p_up5 = probability(fit(up5, rows))
        p_down5 = probability(fit(down5, rows))
        s = np.log(np.maximum(q_up, FLOOR)) - np.log(np.maximum(q_down, FLOOR))
        payoff = np.full(shape, np.nan)
        probabilities = np.full((*shape, 3), np.nan)
        use = rows & np.isfinite(self.competing)
        model = None
        if use.sum() >= 500:
            model = CompetingRisk.fit(
                x[use], self.competing[use].astype(int), self.interval[use].astype(int), HORIZON
            )
        if model is not None:
            returns = [
                float(np.nanmean(self.realized[use & (self.competing == label)]))
                if (use & (self.competing == label)).any()
                else 0.0
                for label in (0, 1, 2)
            ]
            probabilities[apply] = model.outcome_probabilities(x[apply])
            payoff = (
                probabilities[..., 0] * returns[0]
                + probabilities[..., 1] * returns[1]
                + probabilities[..., 2] * returns[2]
            )
            self.conditional_returns = returns
        return {
            "S": s,
            "payoff": payoff,
            "p_up": probabilities[..., 1],
            "p_down": probabilities[..., 2],
            "p_up5": p_up5,
            "p_up10": p_up5 * q_up,
            "p_down5": p_down5,
            "p_down10": p_down5 * q_down,
        }

    def exit_mask(
        self, context: Context, signals: dict[str, np.ndarray], qualifies: np.ndarray
    ) -> np.ndarray | None:
        """Barriers and the five-session time exit only."""
        return None

    def diagnostics(
        self, context: Context, signals: dict[str, np.ndarray], rows: np.ndarray
    ) -> dict[str, Any]:
        """Calibration of the rare-event probabilities and the volatility check."""
        output: dict[str, Any] = {}
        for name, observed in (("p_up10", self.touch["up10"]), ("p_down10", self.touch["down10"])):
            predicted = signals[name]
            use = rows & np.isfinite(predicted) & np.isfinite(observed)
            if use.sum() < 100:
                continue
            p, o = predicted[use], observed[use]
            base = float(o.mean())
            brier = float(np.mean((p - o) ** 2))
            climatology = float(np.mean((base - o) ** 2))
            edges = np.quantile(p, np.linspace(0, 1, 6))
            bins = []
            for low, high in zip(edges[:-1], edges[1:], strict=True):
                inside = (p >= low) & (p <= high)
                if inside.any():
                    bins.append(
                        {
                            "predicted": float(p[inside].mean()),
                            "observed": float(o[inside].mean()),
                            "rows": int(inside.sum()),
                        }
                    )
            output[name] = {
                "brier": brier,
                "climatology_brier": climatology,
                "skill": 1 - brier / climatology if climatology > 0 else None,
                "reliability": bins,
            }
        s = signals["S"]
        use = rows & np.isfinite(s) & np.isfinite(context.sigma20)
        if use.sum() > 100:
            order = np.argsort(np.argsort(s[use]))
            vol_order = np.argsort(np.argsort(context.sigma20[use]))
            output["signal_volatility_rank_correlation"] = float(
                np.corrcoef(order, vol_order)[0, 1]
            )
        output["same_day_barrier_bars"] = int((self.ambiguous & rows).sum())
        return output
