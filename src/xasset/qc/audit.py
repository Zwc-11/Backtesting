"""Anomaly evidence for investigation; flags never silently repair prices."""

import math
from typing import Any

import polars as pl

from xasset.qc.checks import check_bars


def audit_bars(
    bars: pl.DataFrame, *, jump_threshold: float = 0.15, zero_run_length: int = 5
) -> dict[str, Any]:
    if not 0 < jump_threshold < 1 or zero_run_length < 1:
        raise ValueError("Invalid quality audit thresholds")
    structural = check_bars(bars)
    if not structural.ok:
        return {"ok": False, "structural": structural.to_dict(), "anomalies": []}
    events: list[dict[str, Any]] = []
    for _, group in bars.sort("symbol", "ts_end").partition_by("symbol", as_dict=True).items():
        previous: dict[str, Any] | None = None
        zero_count = 0
        for row in group.iter_rows(named=True):
            elapsed = (row["ts_end"] - previous["ts_end"]).total_seconds() if previous else None
            if elapsed != 60:
                zero_count = 0
            zero_count = zero_count + 1 if row["volume"] == 0 else 0
            base = {"symbol": row["symbol"], "ts_end": row["ts_end"].isoformat()}
            if zero_count == zero_run_length:
                events.append({**base, "kind": "zero_volume_run", "bars": zero_count})
            if previous and previous["close"] != 0:
                change = row["close"] / previous["close"] - 1
                if abs(change) > jump_threshold:
                    ratio = previous["close"] / row["close"] if row["close"] else math.inf
                    possible_split = any(
                        abs(ratio / factor - 1) < 0.02
                        for factor in (0.1, 0.2, 0.25, 0.5, 2, 3, 4, 5, 10, 20)
                    )
                    events.append(
                        {
                            **base,
                            "kind": "price_jump",
                            "return": change,
                            "across_gap": elapsed != 60,
                            "possible_split": possible_split,
                            "requires": "corporate-action/source evidence; no automatic adjustment",
                        }
                    )
            previous = row
    return {
        "ok": not events,
        "structural": structural.to_dict(),
        "anomalies": events,
        "jump_threshold": jump_threshold,
        "zero_run_length": zero_run_length,
        "cross_source_verified": False,
    }
