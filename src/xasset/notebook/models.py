"""Small, transparent estimators used by the notebook studies.

All fitting uses training rows only; standardization parameters are part of the
fitted model, so nothing about the evaluation period leaks into a prediction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import LogisticRegression


@dataclass
class Standardizer:
    mean: np.ndarray
    scale: np.ndarray

    @classmethod
    def fit(cls, x: np.ndarray) -> Standardizer:
        mean = np.nanmean(x, axis=0)
        scale = np.nanstd(x, axis=0)
        scale = np.where(np.isfinite(scale) & (scale > 1e-12), scale, 1.0)
        return cls(np.nan_to_num(mean), scale)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        scaled: np.ndarray = (x - self.mean) / self.scale
        return scaled


def complete(*arrays: np.ndarray) -> np.ndarray:
    """Rows where every array is finite."""
    mask = np.ones(arrays[0].shape[0], dtype=bool)
    for array in arrays:
        values = array.reshape(array.shape[0], -1)
        mask &= np.isfinite(values).all(axis=1)
    return mask


@dataclass
class Ridge:
    standardizer: Standardizer
    intercept: float
    coefficients: np.ndarray
    lam: float

    @classmethod
    def fit(cls, x: np.ndarray, y: np.ndarray, lam: float) -> Ridge:
        """Ridge on standardized columns (penalty ``lam * n``); the intercept is unpenalized."""
        standardizer = Standardizer.fit(x)
        z = standardizer(x)
        n, k = z.shape
        y_mean = float(y.mean())
        gram = z.T @ z + lam * n * np.eye(k)
        coefficients = np.linalg.solve(gram, z.T @ (y - y_mean))
        # Training columns are centred by the standardizer, so the intercept is the mean.
        return cls(standardizer, y_mean, coefficients, lam)

    def predict(self, x: np.ndarray) -> np.ndarray:
        prediction: np.ndarray = self.intercept + self.standardizer(x) @ self.coefficients
        return prediction


@dataclass
class Logistic:
    standardizer: Standardizer
    model: LogisticRegression

    @classmethod
    def fit(cls, x: np.ndarray, y: np.ndarray, c: float = 0.1) -> Logistic | None:
        if len(np.unique(y)) < 2:
            return None
        standardizer = Standardizer.fit(x)
        model = LogisticRegression(C=c, max_iter=2000)
        model.fit(standardizer(x), y)
        return cls(standardizer, model)

    def probability(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(self.model.predict_proba(self.standardizer(x))[:, 1], dtype=float)


@dataclass
class CompetingRisk:
    """Discrete-time competing risks: softmax over {survive, up, down} per interval.

    Each candidate contributes one row per interval until its first event; the
    interval index enters as one-hot columns, so hazards can change with time.
    """

    standardizer: Standardizer
    model: LogisticRegression
    intervals: int
    classes: tuple[int, ...]

    @classmethod
    def fit(
        cls,
        x: np.ndarray,
        outcome: np.ndarray,
        event_interval: np.ndarray,
        intervals: int,
        c: float = 0.1,
    ) -> CompetingRisk | None:
        """``outcome``: 0 none by the horizon, 1 up first, 2 down first; ``event_interval``
        is the 1-based interval of the event (``intervals`` when none occurred)."""
        rows, labels, steps = [], [], []
        for i in range(x.shape[0]):
            last = int(event_interval[i])
            for k in range(1, last + 1):
                rows.append(i)
                steps.append(k)
                labels.append(int(outcome[i]) if k == last else 0)
        if not rows:
            return None
        labels_array = np.asarray(labels)
        if len(np.unique(labels_array)) < 2:
            return None
        standardizer = Standardizer.fit(x)
        design = np.hstack([standardizer(x)[rows], one_hot(np.asarray(steps), intervals)])
        model = LogisticRegression(C=c, max_iter=3000)
        model.fit(design, labels_array)
        return cls(standardizer, model, intervals, tuple(int(v) for v in model.classes_))

    def hazards(self, x: np.ndarray) -> np.ndarray:
        """Array (n, intervals, 3) of per-interval probabilities for survive/up/down."""
        z = self.standardizer(x)
        n = z.shape[0]
        output = np.zeros((n, self.intervals, 3))
        for k in range(1, self.intervals + 1):
            design = np.hstack([z, one_hot(np.full(n, k), self.intervals)])
            probabilities = self.model.predict_proba(design)
            for column, label in enumerate(self.classes):
                output[:, k - 1, label] = probabilities[:, column]
        return output

    def outcome_probabilities(self, x: np.ndarray) -> np.ndarray:
        """(n, 3): probability of none, up first, down first within the horizon."""
        hazard = self.hazards(x)
        survive = np.ones(x.shape[0])
        result = np.zeros((x.shape[0], 3))
        for k in range(self.intervals):
            result[:, 1] += survive * hazard[:, k, 1]
            result[:, 2] += survive * hazard[:, k, 2]
            survive = survive * hazard[:, k, 0]
        result[:, 0] = survive
        return result


def one_hot(steps: np.ndarray, intervals: int) -> np.ndarray:
    output = np.zeros((steps.size, intervals))
    output[np.arange(steps.size), steps - 1] = 1.0
    return output


def kaplan_meier_rmt(durations: np.ndarray, observed: np.ndarray, horizon: float) -> float | None:
    """Restricted mean time to event up to ``horizon`` with right censoring.

    ``observed`` is 1 when the event happened at ``duration`` and 0 when observation
    ended first. The area under the Kaplan-Meier survival curve up to ``horizon``
    stays finite when some events never finish.
    """
    if durations.size == 0:
        return None
    order = np.argsort(durations, kind="stable")
    times = np.minimum(durations[order], horizon)
    events = observed[order].astype(bool) & (durations[order] <= horizon)
    at_risk = times.size
    survival = 1.0
    area = 0.0
    previous = 0.0
    index = 0
    while index < times.size:
        t = times[index]
        deaths = 0
        leaving = 0
        while index < times.size and times[index] == t:
            deaths += int(events[index])
            leaving += 1
            index += 1
        area += survival * (t - previous)
        previous = t
        if at_risk > 0 and deaths:
            survival *= 1 - deaths / at_risk
        at_risk -= leaving
    area += survival * (horizon - previous)
    return float(area)


def spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    mask = np.isfinite(a) & np.isfinite(b)
    if mask.sum() < 10:
        return None
    ra = np.argsort(np.argsort(a[mask])).astype(float)
    rb = np.argsort(np.argsort(b[mask])).astype(float)
    ra -= ra.mean()
    rb -= rb.mean()
    denominator = math.sqrt(float(ra @ ra) * float(rb @ rb))
    return float(ra @ rb / denominator) if denominator > 0 else None
