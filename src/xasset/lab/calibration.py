"""Prior-only reference distributions by time of session, frozen before each session.

The handbook's normalization: the preceding ``sessions`` complete sessions, restricted
to a time-of-session neighbourhood (``half_window`` minutes either side, a 30-minute
neighbourhood by default), frozen before the next session, and at least ``minimum``
valid observations; otherwise the affected signal is skipped. Values recorded during
the current session are invisible until that session closes.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

MAD_TO_SIGMA = 1.4826


def median(values: np.ndarray) -> float:
    """Median of finite values, identical to ``np.median`` but without its overhead.

    Order statistics come from a partial sort; an even count averages the two middle
    values as (a + b) / 2, exactly as ``np.median`` does.
    """
    n = values.size
    if n == 0:
        raise ValueError("Median of an empty array")
    k = n // 2
    if n % 2:
        return float(np.partition(values, k)[k])
    part = np.partition(values, (k - 1, k))
    return float((part[k - 1] + part[k]) / 2.0)


class Calibration:
    def __init__(
        self,
        width: int,
        sessions: int = 20,
        half_window: int = 15,
        minimum: int = 200,
        keep: int | None = None,
    ):
        if keep is not None and keep < sessions:
            raise ValueError("Keep at least the sessions used for reference")
        self.width = width
        self.sessions = sessions
        self.half_window = half_window
        self.minimum = minimum
        self.history: deque[np.ndarray] = deque(maxlen=keep or sessions)
        self.current_key: str | None = None
        self.current = np.full(width, np.nan)
        self._stack: np.ndarray | None = None
        self._cache: dict[tuple[int, str, float], float | None] = {}

    def begin(self, key: str) -> None:
        """Start a session; the previous session (if any) becomes reference history."""
        if key == self.current_key:
            return
        if self.current_key is not None:
            self.history.append(self.current)
        self.current_key = key
        self.current = np.full(self.width, np.nan)
        self._stack = None
        self._cache.clear()

    def record(self, minute: int, value: float | None) -> None:
        if value is not None and math.isfinite(value) and 0 <= minute < self.width:
            self.current[minute] = value

    def record_array(self, values: np.ndarray) -> None:
        if values.shape != (self.width,):
            raise ValueError("Calibration array must match the session width")
        self.current = np.where(np.isfinite(values), values, np.nan)

    @property
    def ready(self) -> bool:
        return len(self.history) >= self.sessions

    def recent(self, count: int | None = None) -> list[np.ndarray]:
        items = list(self.history)
        return items[-(count or self.sessions) :]

    def reference(self, minute: int) -> np.ndarray | None:
        if not self.ready:
            return None
        if self._stack is None:
            self._stack = np.vstack(self.recent())
        low = max(0, minute - self.half_window)
        high = min(self.width, minute + self.half_window + 1)
        values = self._stack[:, low:high].ravel()
        values = values[np.isfinite(values)]
        return values if values.size >= self.minimum else None

    def _stat(self, minute: int, kind: str, p: float = 0.0) -> float | None:
        key = (minute, kind, p)
        if key in self._cache:
            return self._cache[key]
        values = self.reference(minute)
        result: float | None
        if values is None:
            result = None
        elif kind == "quantile":
            result = float(np.quantile(values, p))
        elif kind == "median":
            result = median(values)
        else:
            center = median(values)
            result = float(MAD_TO_SIGMA * median(np.abs(values - center)))
        self._cache[key] = result
        return result

    def quantile(self, minute: int, p: float) -> float | None:
        if not 0 <= p <= 1:
            raise ValueError("Quantile probability must lie in [0, 1]")
        return self._stat(minute, "quantile", p)

    def median(self, minute: int) -> float | None:
        return self._stat(minute, "median")

    def scale(self, minute: int) -> float | None:
        """Robust (MAD-based) scale without a floor; callers apply their own floor."""
        return self._stat(minute, "scale")


class SessionStatistic:
    """One value per completed session (e.g. a fixed-window total), prior-only."""

    def __init__(self, sessions: int = 20):
        self.sessions = sessions
        self.values: deque[float] = deque(maxlen=sessions)
        self.pending: dict[str, float] = {}

    def record(self, key: str, value: float | None) -> None:
        if value is not None and math.isfinite(value):
            self.pending[key] = value

    def close(self, key: str) -> None:
        value = self.pending.pop(key, None)
        if value is not None:
            self.values.append(value)

    @property
    def ready(self) -> bool:
        return len(self.values) >= self.sessions

    def median(self) -> float | None:
        return float(np.median(list(self.values))) if self.ready else None

    def quantile(self, p: float) -> float | None:
        return float(np.quantile(list(self.values), p)) if self.ready else None
