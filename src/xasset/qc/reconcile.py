"""Compare matched provider bars; missing overlap can never count as agreement."""

import math
from typing import Any

import polars as pl

from xasset.qc.checks import check_bars


def reconcile(
    left: pl.DataFrame,
    right: pl.DataFrame,
    *,
    relative_tolerance: float = 0.005,
    absolute_tolerance: float = 0.0,
    minimum_overlap: float = 0.95,
) -> dict[str, Any]:
    if (
        not all(
            math.isfinite(value)
            for value in (relative_tolerance, absolute_tolerance, minimum_overlap)
        )
        or relative_tolerance < 0
        or absolute_tolerance < 0
        or not 0 < minimum_overlap <= 1
    ):
        raise ValueError("Invalid reconciliation tolerances")
    for name, bars in (("left", left), ("right", right)):
        qc = check_bars(bars)
        if not qc.ok:
            return {"ok": False, "reason": f"{name} failed structural QC", "quality": qc.to_dict()}
    if set(left["symbol"].to_list()) != set(right["symbol"].to_list()):
        raise ValueError("Reconcile the same internal instrument IDs")
    left_sources, right_sources = set(left["source"].to_list()), set(right["source"].to_list())
    if left_sources & right_sources:
        raise ValueError("Independent reconciliation needs distinct sources")
    keys = ["symbol", "ts_end"]
    joined = left.join(right, on=keys, how="inner", suffix="_reference")
    union_count = left.height + right.height - joined.height
    overlap = joined.height / union_count if union_count else 0.0
    bad = joined.filter(
        pl.any_horizontal(
            (pl.col(name) - pl.col(f"{name}_reference")).abs()
            > absolute_tolerance + relative_tolerance * pl.col(f"{name}_reference").abs()
            for name in ("open", "high", "low", "close")
        )
    )
    return {
        "ok": overlap >= minimum_overlap and joined.height > 0 and bad.is_empty(),
        "matched_rows": joined.height,
        "left_only": left.height - joined.height,
        "right_only": right.height - joined.height,
        "overlap_fraction": overlap,
        "minimum_overlap": minimum_overlap,
        "relative_tolerance": relative_tolerance,
        "absolute_tolerance": absolute_tolerance,
        "disagreements": bad.height,
        "first_disagreement": bad["ts_end"][0].isoformat() if bad.height else None,
        "left_sources": sorted(left_sources),
        "right_sources": sorted(right_sources),
        "note": "Price comparison only; feed/venue and adjustment semantics must also be reviewed.",
    }
