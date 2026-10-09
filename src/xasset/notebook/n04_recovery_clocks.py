"""Notebook strategy 4: Directional Recovery Clocks (minute data, daily decisions).

Anchors follow a rule fixed in advance: the five-minute close at every full UTC hour.
An excursion starts at the first five-minute close within the next hour that is at
least k scales away from the anchor (k = 1 and k = 2; the scale is the robust
dispersion of 60-minute log returns over the previous seven days). Its clock runs
until price returns to the anchor (recovery of a drop, erasure of a rise), observed
for at most 24 hours.

At each daily decision, events that have not finished are kept as censored at the
decision time, so restricted mean times (area under the Kaplan-Meier survival curve
up to 24 hours) use every event without hindsight. For each excursion size

    S_k = log(RMT_erase / RMT_recover)_recent - log(RMT_erase / RMT_recover)_reference

with the recent window the last 14 days and the reference the 76 days before it (the
coin's usual asymmetry). A candidate needs S_1 > 0 and S_2 > 0; S is their mean.
Positions are held for one day.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from xasset.lab.bars import read_lab_bars
from xasset.notebook.engine import Rule
from xasset.notebook.features import Context
from xasset.notebook.models import kaplan_meier_rmt, spearman

STEP = 5  # minutes
HOUR = 60 // STEP
DAY = 24 * HOUR
LIMIT = 24 * 60  # minutes observed per event
SIZES = (1.0, 2.0)
RECENT_DAYS, REFERENCE_DAYS = 14, 76
MIN_RECENT, MIN_REFERENCE = 20, 60
HORIZON = 1


@dataclass
class Events:
    """Excursion events of one coin: start, direction, size, end (or NaN)."""

    start: np.ndarray  # minutes since the grid origin
    up: np.ndarray  # True for a rise (time to erase), False for a drop (time to recover)
    size: np.ndarray
    finish: np.ndarray  # minutes since origin when the anchor was reached, NaN if not
    horizon: np.ndarray  # last minute observable (start + LIMIT or the end of data)


def five_minute_closes(
    root: Path, source: str, symbol: str, origin: datetime, end: datetime
) -> np.ndarray:
    frame = read_lab_bars(root, source, symbol, origin, end)
    count = int((end - origin).total_seconds() // (STEP * 60))
    closes = np.full(count, np.nan)
    if not frame.height:
        return closes
    frame = frame.filter((pl.col("ts_end").dt.minute() % STEP) == 0)
    offsets = ((frame["ts_end"] - origin).dt.total_minutes() // STEP - 1).to_numpy()
    values = frame["close"].to_numpy()
    keep = (offsets >= 0) & (offsets < count) & np.isfinite(values) & (values > 0)
    closes[offsets[keep].astype(int)] = values[keep]
    return closes


def daily_scale(log_close: np.ndarray, days: int) -> np.ndarray:
    """Prior-seven-day robust scale of 60-minute returns, one value per UTC day."""
    r60 = np.full_like(log_close, np.nan)
    r60[HOUR:] = log_close[HOUR:] - log_close[:-HOUR]
    scale = np.full(days, np.nan)
    for day in range(7, days):
        window = r60[(day - 7) * DAY : day * DAY]
        window = window[np.isfinite(window)]
        if window.size >= 500:
            mad = float(np.median(np.abs(window - np.median(window))))
            scale[day] = 1.4826 * mad
    return scale


def minute_of(index: int) -> float:
    """Grid index -> minutes since the origin at which that five-minute bar closed."""
    return float((index + 1) * STEP)


def extract(log_close: np.ndarray, scale: np.ndarray) -> Events:
    starts, ups, sizes, finishes, horizons = [], [], [], [], []
    total = log_close.size
    # Index j closes at minute (j + 1) * 5; full hours are j = 11, 23, ...; skip day one.
    for anchor in range(DAY + HOUR - 1, total - 1, HOUR):
        base = log_close[anchor]
        day = int(minute_of(anchor) // 1440)
        sigma = scale[day] if day < scale.size else np.nan
        if not (math.isfinite(base) and math.isfinite(sigma) and sigma > 0):
            continue
        window = log_close[anchor + 1 : anchor + 1 + HOUR]
        for size in SIZES:
            distance = size * sigma
            for up in (False, True):
                crossed = window >= base + distance if up else window <= base - distance
                hits = np.flatnonzero(crossed)
                if not hits.size:
                    continue
                begin = anchor + 1 + int(hits[0])
                path = log_close[begin + 1 : begin + 1 + LIMIT // STEP]
                back = np.flatnonzero(path <= base if up else path >= base)
                starts.append(minute_of(begin))
                ups.append(up)
                sizes.append(size)
                finishes.append(minute_of(begin + 1 + int(back[0])) if back.size else math.nan)
                horizons.append(minute_of(begin + path.size))
    return Events(
        np.asarray(starts, dtype=float),
        np.asarray(ups, dtype=bool),
        np.asarray(sizes, dtype=float),
        np.asarray(finishes, dtype=float),
        np.asarray(horizons, dtype=float),
    )


def restricted_mean(events: Events, mask: np.ndarray, now: float, drop_open: bool) -> float | None:
    """RMT up to 24 h for the selected events as observed at minute ``now``."""
    start = events.start[mask]
    if start.size == 0:
        return None
    finish = events.finish[mask]
    seen_until = np.minimum(events.horizon[mask], now)
    done = np.isfinite(finish) & (finish <= seen_until)
    duration = np.where(done, finish - start, seen_until - start)
    if drop_open:  # the rejected shortcut: discard unfinished excursions
        duration, done = duration[done], done[done]
        if duration.size == 0:
            return None
    return kaplan_meier_rmt(duration, done.astype(float), float(LIMIT))


class RecoveryClocks:
    id = "n04"
    title = "Directional recovery clocks"
    horizon = HORIZON
    rule = Rule(horizon=HORIZON)

    def __init__(self, root: Path | None = None, source: str = "binance-spot"):
        self.root = root or Path("data")
        self.source = source

    def prepare(self, context: Context) -> None:
        panel = context.panel
        origin = datetime.combine(panel.dates[0], datetime.min.time(), tzinfo=UTC)
        end = datetime.combine(panel.dates[-1] + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        t_count, n_count = context.shape
        self.score = np.full((t_count, n_count), np.nan)
        self.score_dropped = np.full((t_count, n_count), np.nan)
        self.parts = np.full((t_count, n_count, len(SIZES)), np.nan)
        self.event_counts: dict[str, int] = {}
        candidates = np.flatnonzero(context.eligible.any(axis=0))
        for column in candidates:
            symbol = panel.symbols[column]
            closes = five_minute_closes(self.root, self.source, symbol, origin, end)
            if not np.isfinite(closes).any():
                continue
            with np.errstate(divide="ignore", invalid="ignore"):
                log_close = np.log(closes)
            scale = daily_scale(log_close, t_count)
            events = extract(log_close, scale)
            self.event_counts[symbol] = int(events.start.size)
            for day in range(t_count):
                if not context.eligible[day, column]:
                    continue
                now = float((day + 1) * 24 * 60)  # decision after day `day` completes
                recent = (events.start >= now - RECENT_DAYS * 1440) & (events.start < now)
                reference = (events.start >= now - (RECENT_DAYS + REFERENCE_DAYS) * 1440) & (
                    events.start < now - RECENT_DAYS * 1440
                )
                values: list[float] = []
                dropped: list[float] = []
                for k, size in enumerate(SIZES):
                    same = events.size == size
                    pieces: dict[bool, float | None] = {}
                    for drop in (False, True):
                        stats: list[float | None] = []
                        for window, minimum in ((recent, MIN_RECENT), (reference, MIN_REFERENCE)):
                            erase_mask = window & same & events.up
                            recover_mask = window & same & ~events.up
                            if erase_mask.sum() < minimum or recover_mask.sum() < minimum:
                                stats.append(None)
                                continue
                            erase = restricted_mean(events, erase_mask, now, drop)
                            recover = restricted_mean(events, recover_mask, now, drop)
                            stats.append(math.log(erase / recover) if erase and recover else None)
                        recent_stat, reference_stat = stats
                        pieces[drop] = (
                            recent_stat - reference_stat
                            if recent_stat is not None and reference_stat is not None
                            else None
                        )
                    kept, without = pieces[False], pieces[True]
                    if kept is not None:
                        self.parts[day, column, k] = kept
                        values.append(kept)
                    if without is not None:
                        dropped.append(without)
                if len(values) == len(SIZES):
                    self.score[day, column] = float(np.mean(values))
                if len(dropped) == len(SIZES):
                    self.score_dropped[day, column] = float(np.mean(dropped))
        self.forward = context.forward(HORIZON)

    def outcome(self, context: Context) -> np.ndarray:
        return self.forward

    def signals(self, context: Context, fit_until: int) -> dict[str, np.ndarray]:
        # Prior-only Kaplan-Meier statistics; nothing is fitted.
        return {"S": self.score, "small": self.parts[..., 0], "large": self.parts[..., 1]}

    def precondition(self, context: Context, signals: dict[str, np.ndarray]) -> np.ndarray:
        with np.errstate(invalid="ignore"):
            return (signals["small"] > 0) & (signals["large"] > 0)

    def exit_mask(
        self, context: Context, signals: dict[str, np.ndarray], qualifies: np.ndarray
    ) -> np.ndarray | None:
        return None  # one-day holding period

    def diagnostics(
        self, context: Context, signals: dict[str, np.ndarray], rows: np.ndarray
    ) -> dict[str, Any]:
        output: dict[str, Any] = {"events": self.event_counts}
        window = np.zeros(context.shape, dtype=bool)
        days = np.flatnonzero(rows.any(axis=1))
        if days.size:
            window[days.min() : days.max() + 1] = True
        use = window & context.eligible
        y = self.forward
        output["rank_correlation_with_next_day"] = spearman(self.score[use], y[use])
        output["rank_correlation_dropping_unfinished"] = spearman(self.score_dropped[use], y[use])
        trend = context.controls["r5"]
        output["signal_trend_correlation"] = spearman(self.score[use], trend[use])
        return output
