"""Compare independent execution records without creating or approving trades."""

import csv
import hashlib
import math
from datetime import date
from pathlib import Path
from typing import Any

from xasset.research.contracts import Trade


def reconcile(
    native: list[Trade], external: list[Trade], tolerance: float = 1e-6
) -> dict[str, Any]:
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("Tolerance must be positive")
    if len({trade.id for trade in native}) != len(native) or len(
        {trade.id for trade in external}
    ) != len(external):
        raise ValueError("Trade IDs must be unique")
    left, right = {t.id: t for t in native}, {t.id: t for t in external}
    differences = []
    numeric = (
        "entry_price",
        "exit_price",
        "units",
        "entry_notional",
        "exit_notional",
        "gross_pnl",
        "cost",
    )
    exact = (
        "symbol",
        "fold",
        "signal_at",
        "entry_at",
        "exit_at",
        "entry_bar_end",
        "exit_bar_end",
        "exit_reason",
    )
    for identity in sorted(left.keys() | right.keys()):
        if identity not in left or identity not in right:
            differences.append(
                {
                    "trade": identity,
                    "field": "presence",
                    "native": identity in left,
                    "external": identity in right,
                }
            )
            continue
        for field in (*exact, *numeric):
            a, b = getattr(left[identity], field), getattr(right[identity], field)
            different = a != b if field in exact else abs(a - b) > tolerance
            if different:
                differences.append(
                    {"trade": identity, "field": field, "native": str(a), "external": str(b)}
                )
    return {
        "ok": bool(native) and not differences,
        "native_trades": len(native),
        "external_trades": len(external),
        "differences": differences,
        "native_net_pnl": sum(t.net_pnl for t in native),
        "external_net_pnl": sum(t.net_pnl for t in external),
        "accepted": False,
        "note": "Reconciliation alone cannot approve a strategy",
    }


def paper_statement(
    path: Path, allowed_symbols: set[str], minimum_sessions: int = 30
) -> dict[str, Any]:
    """Audit a normalized paper fill export, never connect to or place broker orders."""
    if minimum_sessions < 30:
        raise ValueError("Paper observation requires at least 30 sessions")
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    required = {
        "fill_id",
        "account",
        "symbol",
        "session",
        "side",
        "units",
        "reference_price",
        "fill_price",
        "commission",
    }
    if not rows or not required <= rows[0].keys():
        raise ValueError("Paper fill export is empty or lacks required fields")
    if len({row["fill_id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate paper fill IDs")
    if any(not row["fill_id"].strip() for row in rows):
        raise ValueError("Paper fill IDs cannot be blank")
    if len({row["account"] for row in rows}) != 1:
        raise ValueError("Paper fills must belong to one account")
    sessions, slippage = set(), []
    for row in rows:
        if (
            not row["account"].startswith("DU")
            or not row["account"][2:].isdigit()
            or row["symbol"] not in allowed_symbols
        ):
            raise ValueError("Only declared symbols from an IBKR paper account are permitted")
        if row["side"] not in {"BUY", "SELL"}:
            raise ValueError("Unknown paper fill side")
        session = date.fromisoformat(row["session"])
        from xasset.normalize.calendars import calendar

        if not calendar("XNYS").is_session(str(session)):
            raise ValueError("Paper fill session is not a scheduled equity session")
        sessions.add(session)
        units, reference, price, commission = [
            float(row[k]) for k in ("units", "reference_price", "fill_price", "commission")
        ]
        if (
            not all(math.isfinite(v) for v in (units, reference, price, commission))
            or min(units, reference, price) <= 0
            or commission < 0
        ):
            raise ValueError("Invalid paper fill prices, units or commission")
        sign = 1 if row["side"] == "BUY" else -1
        slippage.append(
            sign * (price / reference - 1) * 10000 + commission / (units * reference) * 10000
        )
    return {
        "status": "enough_sessions_for_review"
        if len(sessions) >= minimum_sessions
        else "collecting",
        "sessions": len(sessions),
        "required_sessions": minimum_sessions,
        "fills": len(rows),
        "mean_cost_bps": sum(slippage) / len(slippage),
        "maximum_cost_bps": max(slippage),
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "accepted": False,
    }
