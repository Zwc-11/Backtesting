"""Purged, nested walk-forward evaluation shared by the notebook strategies.

For each test fold ``[start, end)``:

1. Inner step (selection inside training): models are fitted on rows whose label
   window ends before the validation start; the ridge penalty and the signal
   threshold are chosen on the validation slice (the last quarter of training).
2. Outer step: models are refitted on every training row whose label window ends
   before the fold starts (purge = horizon + 1 days), with the chosen settings, and
   applied unchanged to the fold.

The proposed signal S is judged by incremental predictive value: a return model
with S (m1) against the same model without it (m0), both on the ordinary controls Z.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import numpy as np

from xasset.notebook.engine import Decision, Rule
from xasset.notebook.features import Z_NAMES, Context
from xasset.notebook.models import Ridge, complete, spearman

LAMBDAS = (1e-3, 1e-2, 1e-1)
QUANTILES = (0.5, 0.7, 0.8, 0.9)
MIN_SELECTED = 30


class NotebookStrategy(Protocol):
    id: str
    title: str
    horizon: int
    rule: Rule

    def prepare(self, context: Context) -> None: ...

    def signals(self, context: Context, fit_until: int) -> dict[str, np.ndarray]:
        """Signal arrays using only models fitted on labels resolved before ``fit_until``."""
        ...

    def outcome(self, context: Context) -> np.ndarray:
        """Realized gross log return of the trade rule for a decision on each row."""
        ...

    def precondition(self, context: Context, signals: dict[str, np.ndarray]) -> np.ndarray: ...

    def diagnostics(
        self, context: Context, signals: dict[str, np.ndarray], rows: np.ndarray
    ) -> dict[str, Any]: ...

    def exit_mask(
        self, context: Context, signals: dict[str, np.ndarray], qualifies: np.ndarray
    ) -> np.ndarray | None:
        """Where an open position should leave at the next open (None: no early exit)."""
        ...


@dataclass(frozen=True)
class Fold:
    start: int
    end: int

    def label(self, dates: list[date]) -> str:
        return f"{dates[self.start]} to {dates[self.end - 1]}"


@dataclass
class FoldResult:
    fold: Fold
    lam: float
    quantile: float
    threshold: float
    allowance: float
    train_rows: int
    decisions: dict[int, list[Decision]]
    baseline: dict[int, list[Decision]]
    m0: np.ndarray
    m1: np.ndarray
    signal: np.ndarray
    early: np.ndarray | None  # True where an open position leaves at the next open
    test_rows: np.ndarray
    notes: dict[str, Any] = field(default_factory=dict)


def design(context: Context, extra: list[np.ndarray]) -> np.ndarray:
    """(T, N, k) controls Z followed by any extra columns."""
    arrays = [context.controls[name] for name in Z_NAMES] + extra
    return np.stack(arrays, axis=-1)


def rows_between(shape: tuple[int, int], first: int, last: int) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    first = max(first, 0)
    if last >= first:
        mask[first : last + 1] = True
    return mask


def fit_ridge(x: np.ndarray, y: np.ndarray, rows: np.ndarray, lam: float) -> Ridge | None:
    usable = rows & complete(x.reshape(-1, x.shape[-1])).reshape(rows.shape) & np.isfinite(y)
    if usable.sum() < 200:
        return None
    return Ridge.fit(x[usable], y[usable], lam)


def predict(model: Ridge | None, x: np.ndarray) -> np.ndarray:
    output = np.full(x.shape[:2], np.nan)
    if model is None:
        return output
    flat = x.reshape(-1, x.shape[-1])
    good = complete(flat)
    values = np.full(flat.shape[0], np.nan)
    if good.any():
        values[good] = model.predict(flat[good])
    return values.reshape(x.shape[:2])


def round_trip(cost: np.ndarray, fee_bps: float) -> np.ndarray:
    """Round-trip cost as a log fraction: two half-spread/impact legs plus two fees."""
    values: np.ndarray = 2 * (np.nan_to_num(cost, nan=np.nanmax(cost)) + fee_bps) / 10_000
    return values


def select(
    rows: np.ndarray,
    signal: np.ndarray,
    threshold: float,
    m1: np.ndarray,
    m0: np.ndarray,
    cost: np.ndarray,
    allowance: float,
    beat_baseline: bool,
) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        chosen = rows & (signal >= threshold) & (m1 - cost > allowance)
        if beat_baseline:
            chosen &= m1 > m0
    result: np.ndarray = chosen & np.isfinite(signal) & np.isfinite(m1)
    return result


def run_folds(
    context: Context,
    strategy: NotebookStrategy,
    folds: list[Fold],
    study_start: int,
    spread: np.ndarray,
    fee_bps: float,
    beat_baseline: bool = True,
) -> list[FoldResult]:
    horizon = strategy.horizon
    shape = context.shape
    outcome = strategy.outcome(context)
    cost = round_trip(spread, fee_bps)
    net = outcome - cost
    results = []
    for fold in folds:
        fit_end = fold.start - horizon - 2
        validation_start = study_start + int(0.75 * (fit_end - study_start))
        inner_end = validation_start - horizon - 2
        # --- inner selection ---------------------------------------------------
        inner_signals = strategy.signals(context, validation_start)
        pre = strategy.precondition(context, inner_signals) & context.eligible
        s_inner = inner_signals["S"]
        x0 = design(context, [])
        x1 = design(context, [s_inner])
        train = pre & rows_between(shape, study_start, inner_end) & np.isfinite(s_inner)
        valid = pre & rows_between(shape, validation_start, fit_end) & np.isfinite(s_inner)
        best_lam, best_error = LAMBDAS[0], math.inf
        for lam in LAMBDAS:
            model = fit_ridge(x1, outcome, train, lam)
            prediction = predict(model, x1)
            use = valid & np.isfinite(prediction) & np.isfinite(outcome)
            if use.sum() < 50:
                continue
            error = float(np.mean((prediction[use] - outcome[use]) ** 2))
            if error < best_error:
                best_lam, best_error = lam, error
        m1_inner = predict(fit_ridge(x1, outcome, train, best_lam), x1)
        m0_inner = predict(fit_ridge(x0, outcome, train, best_lam), x0)
        train_signal = s_inner[train]
        best_q, best_score = 0.8, -math.inf
        for q in QUANTILES:
            if train_signal.size < 50:
                break
            threshold = float(np.quantile(train_signal, q))
            chosen = select(valid, s_inner, threshold, m1_inner, m0_inner, cost, 0.0, beat_baseline)
            if chosen.sum() < MIN_SELECTED:
                continue
            score = float(np.nanmean(net[chosen]))
            if score > best_score:
                best_q, best_score = q, score
        # --- outer fit and application --------------------------------------------
        signals = strategy.signals(context, fold.start)
        pre = strategy.precondition(context, signals) & context.eligible
        s = signals["S"]
        x1 = design(context, [s])
        train = pre & rows_between(shape, study_start, fit_end) & np.isfinite(s)
        m1_model = fit_ridge(x1, outcome, train, best_lam)
        m0_model = fit_ridge(x0, outcome, train, best_lam)
        m1 = predict(m1_model, x1)
        m0 = predict(m0_model, x0)
        threshold = float(np.quantile(s[train], best_q)) if train.any() else math.inf
        chosen_train = select(train, s, threshold, m1, m0, cost, 0.0, beat_baseline)
        optimism = (m1 - outcome)[chosen_train & np.isfinite(outcome)]
        allowance = max(0.0, float(np.mean(optimism))) if optimism.size else 0.0
        test = pre & rows_between(shape, fold.start, fold.end - 1) & np.isfinite(s)
        chosen = select(test, s, threshold, m1, m0, cost, allowance, beat_baseline)
        decisions: dict[int, list[Decision]] = {}
        baseline: dict[int, list[Decision]] = {}
        eligible_test = context.eligible & rows_between(shape, fold.start, fold.end - 1)
        for day in range(fold.start, fold.end):
            columns = np.flatnonzero(chosen[day])
            if not columns.size:
                continue
            decisions[day] = [
                Decision(day, int(c), float(m1[day, c] - cost[day, c]), strategy.id)
                for c in columns
            ]
            # Matched baseline: the same number of names ranked by m0 alone.
            pool = np.flatnonzero(eligible_test[day] & np.isfinite(m0[day]))
            ranked = pool[np.argsort(-(m0[day, pool] - cost[day, pool]), kind="stable")]
            baseline[day] = [
                Decision(day, int(c), float(m0[day, c] - cost[day, c]), strategy.id + "-base")
                for c in ranked[: columns.size]
            ]
        with np.errstate(invalid="ignore"):
            qualifies = pre & (m1 - cost > allowance) & context.eligible
        early = strategy.exit_mask(context, signals, qualifies)
        results.append(
            FoldResult(
                fold=fold,
                lam=best_lam,
                quantile=best_q,
                threshold=threshold,
                allowance=allowance,
                train_rows=int(train.sum()),
                decisions=decisions,
                baseline=baseline,
                m0=m0,
                m1=m1,
                signal=s,
                early=early,
                test_rows=test,
                notes={"diagnostics": strategy.diagnostics(context, signals, test)},
            )
        )
    return results


def incremental_value(
    context: Context,
    results: list[FoldResult],
    outcome: np.ndarray,
    draws: int = 500,
    block: int = 20,
    seed: int = 7,
) -> dict[str, Any]:
    """Out-of-sample comparison of m1 (with S) against m0 (controls only)."""
    m0 = np.full(context.shape, np.nan)
    m1 = np.full(context.shape, np.nan)
    s = np.full(context.shape, np.nan)
    rows = np.zeros(context.shape, dtype=bool)
    s_resid = np.full(context.shape, np.nan)
    for result in results:
        mask = result.test_rows
        m0[mask], m1[mask], s[mask] = result.m0[mask], result.m1[mask], result.signal[mask]
        rows |= mask
        # Residualize S on Z with a model fitted on training rows only.
        train = np.zeros(context.shape, dtype=bool)
        train[: max(result.fold.start - 1, 0)] = True
        train &= np.isfinite(result.signal) & context.eligible
        x0 = design(context, [])
        model = fit_ridge(x0, result.signal, train, 1e-2)
        fitted = predict(model, x0)
        s_resid[mask] = (result.signal - fitted)[mask]
    use = rows & np.isfinite(outcome) & np.isfinite(m0) & np.isfinite(m1)
    if use.sum() < 50:
        return {"rows": int(use.sum())}
    y = outcome[use]
    e0 = y - m0[use]
    e1 = y - m1[use]
    r2 = 1 - float(np.mean(e1**2)) / float(np.mean(e0**2))
    ic = spearman(m1[use] - m0[use], y - m0[use])
    s_tilde = s_resid[use]
    y_tilde = e0
    good = np.isfinite(s_tilde)
    theta = (
        float((s_tilde[good] * y_tilde[good]).sum() / (s_tilde[good] ** 2).sum())
        if good.sum() > 10 and (s_tilde[good] ** 2).sum() > 0
        else None
    )
    # Date-block bootstrap with the whole cross-section of each day kept together.
    days = np.flatnonzero(use.any(axis=1))
    resid = np.where(use, outcome - m0, np.nan)
    both = np.isfinite(s_resid) & np.isfinite(resid) & use
    numerators = np.where(both, s_resid * resid, 0.0).sum(axis=1)[days]
    denominators = np.where(both, s_resid**2, 0.0).sum(axis=1)[days]
    rng = np.random.default_rng(seed)
    thetas: list[float] = []
    if theta is not None and days.size > block:
        num_blocks = np.convolve(numerators, np.ones(block), mode="valid")
        den_blocks = np.convolve(denominators, np.ones(block), mode="valid")
        count = math.ceil(days.size / block)
        picks = rng.integers(0, num_blocks.size, size=(draws, count))
        num = num_blocks[picks].sum(axis=1)
        den = den_blocks[picks].sum(axis=1)
        thetas = [float(v) for v in (num[den > 0] / den[den > 0])]
    interval = (
        [float(np.quantile(thetas, 0.025)), float(np.quantile(thetas, 0.975))] if thetas else None
    )
    return {
        "rows": int(use.sum()),
        "days": int(days.size),
        "r2_incremental": r2,
        "ic_incremental": ic,
        "theta": theta,
        "theta_95": interval,
        "note": "Out-of-sample test folds only. R2 compares squared errors of the model "
        "with the signal against the controls-only model; theta is the slope of the "
        "outcome residual on the signal residual (both after removing the controls), "
        "with a 20-day block bootstrap interval that keeps each day's cross-section "
        "together.",
    }
