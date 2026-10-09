"""Window-local estimators with explicit missingness and dependence-aware inference."""

import math
from typing import Any

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy.stats import rankdata
from sklearn.covariance import graphical_lasso
from statsmodels.tsa.stattools import coint


def adjusted_pvalues(values: list[float | None], dependent: bool = True) -> list[float]:
    """Benjamini-Yekutieli by default; unestimable registered cells remain p=1."""
    p = [1.0 if v is None or not math.isfinite(v) else min(1.0, max(0.0, v)) for v in values]
    n = len(p)
    factor = sum(1 / i for i in range(1, n + 1)) if dependent else 1.0
    result = [1.0] * n
    running = 1.0
    for rank, index in reversed(list(enumerate(sorted(range(n), key=p.__getitem__), 1))):
        running = min(running, p[index] * n * factor / rank)
        result[index] = running
    return result


def regression(
    frame: pd.DataFrame,
    source: str,
    target: str,
    *,
    lag: int = 0,
    factor: str | None = None,
    ranked: bool = False,
    daily: bool = False,
    minimum: int = 60,
    minimum_clusters: int = 20,
) -> dict[str, Any]:
    """Lag before dropping missing rows. Controls use strictly prior returns."""
    columns = {"y": frame[target], "x": frame[source].shift(lag)}
    if lag:
        columns["own"] = frame[target].shift(1)
        if factor and factor not in {source, target} and factor in frame:
            columns["market"] = frame[factor].shift(1)
    pair = pd.DataFrame(columns).dropna()
    groups = [stamp.date() for stamp in pair.index]
    n, clusters = len(pair), len(set(groups))
    base: dict[str, Any] = {"n": n, "clusters": clusters, "strength": None, "p": None}
    if n < minimum or clusters < minimum_clusters:
        return {**base, "reason": "insufficient paired observations or date clusters"}
    if ranked:
        pair = pair.apply(rankdata)
    if any(float(pair[column].std()) <= 1e-12 for column in pair):
        return {**base, "reason": "constant returns or controls"}
    matrix = sm.add_constant(pair.drop(columns="y"), has_constant="add")
    if np.linalg.matrix_rank(matrix.to_numpy()) < matrix.shape[1]:
        return {**base, "reason": "rank-deficient regression"}
    try:
        model = sm.OLS(pair.y, matrix)
        fit = (
            model.fit(cov_type="HAC", cov_kwds={"maxlags": 3})
            if daily
            else model.fit(cov_type="cluster", cov_kwds={"groups": groups, "use_correction": True})
        )
        correlation = float(pair.y.corr(pair.x))
        coefficient, pvalue = float(fit.params["x"]), float(fit.pvalues["x"])
        if not all(math.isfinite(x) for x in (coefficient, pvalue, correlation)):
            return {**base, "reason": "nonfinite regression result"}
        return {
            **base,
            "strength": coefficient if lag else correlation,
            "beta": coefficient,
            "p": pvalue,
            "residual_std": float(np.std(fit.resid, ddof=matrix.shape[1])),
            "inference": "HAC(3)" if daily else "date-cluster robust",
            "reason": None,
        }
    except (ValueError, np.linalg.LinAlgError) as exc:
        return {**base, "reason": f"regression unavailable: {type(exc).__name__}"}


def cointegration(x: pd.Series, y: pd.Series, minimum: int = 60) -> dict[str, Any]:
    pair = pd.concat([x, y], axis=1).dropna()
    base: dict[str, Any] = {"n": len(pair), "strength": None, "p": None}
    if len(pair) < minimum or (pair <= 0).any().any():
        return {**base, "reason": "cointegration requires at least 60 positive paired closes"}
    values = np.log(pair.to_numpy())
    if np.any(np.std(values, axis=0) < 1e-12):
        return {**base, "reason": "constant log price"}
    try:
        stat, pvalue, _ = coint(values[:, 1], values[:, 0], trend="c", maxlag=1, autolag=None)
        beta = float(np.linalg.lstsq(sm.add_constant(values[:, 0]), values[:, 1], rcond=None)[0][1])
        if not all(math.isfinite(v) for v in (stat, pvalue, beta)):
            return {**base, "reason": "nonfinite cointegration result"}
        return {
            **base,
            "strength": beta,
            "p": float(pvalue),
            "statistic": float(stat),
            "reason": None,
        }
    except (ValueError, np.linalg.LinAlgError):
        return {**base, "reason": "cointegration could not be estimated"}


def partial_correlations(frame: pd.DataFrame, alpha: float = 0.02) -> dict[tuple[str, str], float]:
    """Fixed-penalty graphical lasso; no significance claim for its selected links."""
    clean = frame.dropna()
    if len(clean) < max(60, 3 * len(frame.columns)):
        return {}
    scaled = (clean - clean.mean()) / clean.std()
    if not np.isfinite(scaled.to_numpy()).all():
        return {}
    _, precision = graphical_lasso(np.cov(scaled.to_numpy(), rowvar=False), alpha=alpha)
    return {
        (str(a), str(b)): float(-precision[i, j] / math.sqrt(precision[i, i] * precision[j, j]))
        for i, a in enumerate(frame.columns)
        for j, b in enumerate(frame.columns)
        if i < j
    }


def hayashi_yoshida(
    left: list[tuple[float, float, float]],
    right: list[tuple[float, float, float]],
) -> float | None:
    """Overlap covariance normalized by realized variances; touching ends do not overlap."""
    for series in (left, right):
        if any(not all(math.isfinite(v) for v in row) or row[0] >= row[1] for row in series):
            raise ValueError("Intervals must be finite and have positive length")
        if any(a[1] > b[0] for a, b in zip(series, series[1:], strict=False)):
            raise ValueError("Intervals within a series must be sorted and disjoint")
    covariance = 0.0
    first = 0
    for start, end, change in left:
        while first < len(right) and right[first][1] <= start:
            first += 1
        index = first
        while index < len(right) and right[index][0] < end:
            covariance += change * right[index][2]
            index += 1
    denominator = math.sqrt(sum(v * v for _, _, v in left) * sum(v * v for _, _, v in right))
    return covariance / denominator if denominator else None
