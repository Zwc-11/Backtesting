"""Causal diagnostics computed from the same minute feature function as research."""

import itertools
import math
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from xasset.features.returns import trailing_features
from xasset.monitor.config import MonitorConfig
from xasset.research.experiment import digest


def alert(kind: str, symbol: str, at: datetime, severity: str, message: str) -> dict[str, Any]:
    event = {
        "kind": kind,
        "symbol": symbol,
        "at": at.isoformat(),
        "severity": severity,
        "message": message,
        "action": "observe",
    }
    return {"id": digest({k: event[k] for k in ("kind", "symbol", "at")}), **event}


def detect(
    bars: pl.DataFrame,
    config: MonitorConfig,
    as_of: datetime,
    health: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    alerts = []
    for entry in health:
        if entry["status"] in {"failed", "stale", "missing"}:
            alerts.append(
                alert(
                    "data_health",
                    entry["symbol"],
                    as_of.replace(second=0, microsecond=0),
                    "high",
                    f"{entry['symbol']}: {entry['status']} market data",
                )
            )
    features = (
        trailing_features(bars.filter(pl.col("ts_end") <= as_of), config.volatility_window)
        if not bars.is_empty()
        else None
    )
    latest = []
    if features is not None:
        features = features.with_columns(
            pl.col("volatility").shift(1).over("symbol").alias("prior_volatility")
        )
        active = {entry["symbol"] for entry in health if entry["status"] == "healthy"}
        for symbol in features["symbol"].unique().to_list():
            frame = features.filter(pl.col("symbol") == symbol).sort("ts_end")
            row = frame.tail(1).to_dicts()[0]
            change, volatility = row["return_1m"], row["prior_volatility"]
            z = (
                change / volatility
                if change is not None and volatility is not None and volatility > 0
                else None
            )
            latest.append(
                {
                    "symbol": symbol,
                    "at": row["ts_end"].isoformat(),
                    "close": row["close"],
                    "return_1m": change,
                    "volatility": volatility,
                    "move_z": z,
                }
            )
            if symbol in active and z is not None and abs(z) >= config.move_z:
                alerts.append(
                    alert(
                        "move",
                        symbol,
                        row["ts_end"],
                        "medium",
                        f"{symbol} moved {change:+.2%} in one minute "
                        f"({z:+.1f} prior-volatility units)",
                    )
                )
        for source, target in itertools.combinations(sorted(active), 2):
            pair = (
                features.filter(pl.col("symbol") == source)
                .select("ts_end", pl.col("return_1m").alias("x"))
                .join(
                    features.filter(pl.col("symbol") == target).select(
                        "ts_end", pl.col("return_1m").alias("y")
                    ),
                    on="ts_end",
                    how="inner",
                )
                .sort("ts_end")
            )
            # Require a contiguous recent window; do not compress missing minutes.
            if pair.height < config.volatility_window + 1:
                continue
            tail = pair.tail(2 * config.volatility_window + 1)
            if tail["x"].null_count() or tail["y"].null_count():
                continue
            stamps = tail["ts_end"].to_list()
            if any(b - a != timedelta(minutes=1) for a, b in zip(stamps, stamps[1:], strict=False)):
                continue
            x, y = tail["x"].to_numpy(), tail["y"].to_numpy()
            n = config.volatility_window
            train_x, train_y = x[-n - 1 : -1], y[-n - 1 : -1]
            if float(np.std(train_x)) <= 1e-12 or float(np.std(train_y)) <= 1e-12:
                continue
            design = np.column_stack([np.ones(n), train_x])
            beta = np.linalg.lstsq(design, train_y, rcond=None)[0]
            residual_std = float(np.std(train_y - design @ beta, ddof=2))
            if residual_std > 1e-12:
                z = float((y[-1] - np.array([1.0, x[-1]]) @ beta) / residual_std)
                if math.isfinite(z) and abs(z) >= config.residual_z:
                    alerts.append(
                        alert(
                            "break",
                            f"{source}/{target}",
                            stamps[-1],
                            "medium",
                            f"{target} diverged from its prior relationship with {source} "
                            f"({z:+.1f} residual units)",
                        )
                    )
            if len(x) >= 2 * n + 1:
                old, new = (
                    float(np.corrcoef(x[:n], y[:n])[0, 1]),
                    float(np.corrcoef(x[-n - 1 : -1], y[-n - 1 : -1])[0, 1]),
                )
                if (
                    math.isfinite(old)
                    and math.isfinite(new)
                    and abs(new - old) >= config.regime_change
                ):
                    alerts.append(
                        alert(
                            "regime",
                            f"{source}/{target}",
                            stamps[-1],
                            "medium",
                            f"Observed correlation changed from {old:+.2f} to {new:+.2f}",
                        )
                    )
    for event in config.events:
        if (
            event.known_at <= as_of
            and abs((event.at - as_of).total_seconds()) <= config.event_window_minutes * 60
        ):
            alerts.append(
                alert(
                    "event_window",
                    event.id,
                    event.at,
                    "info",
                    f"{event.kind} scheduled at {event.at.isoformat()}",
                )
            )
    # Lead-fired signals require an accepted research strategy. Descriptive stable
    # edges never acquire trading permission, and this version has no accepted family.
    return alerts, latest
