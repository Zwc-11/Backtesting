from datetime import datetime
from pathlib import Path
from typing import Any

from xasset.config import Instrument
from xasset.normalize.calendars import completed_sessions
from xasset.qc.checks import check_bars
from xasset.store.writer import load_bars


def health(
    root: Path, instruments: list[Instrument], as_of: datetime, sessions: int = 5
) -> dict[str, Any]:
    """Require every expected bar in the last N completed regular sessions.

    Missing minutes are observations, not evidence of a provider outage: illiquid
    instruments and halts can have no trades. Never synthesize bars to pass.
    Futures proxies need explicit session/contract rules before coverage is known.
    """
    if sessions < 1:
        raise ValueError("session count must be positive")
    reports: list[dict[str, Any]] = []
    for instrument in instruments:
        bars = load_bars(root, instrument)
        qc = check_bars(bars)
        report: dict[str, Any] = {"symbol": instrument.id, "quality": qc.to_dict()}
        if instrument.calendar is None or instrument.session != "regular":
            report.update(
                status="unknown", reason="verified session rules not configured", sessions=[]
            )
        else:
            observed = set(bars["ts_end"].to_list())
            coverage = []
            for label, expected in completed_sessions(instrument.calendar, as_of, sessions):
                missing = expected - observed
                coverage.append(
                    {
                        "session": label,
                        "expected": len(expected),
                        "observed": len(expected & observed),
                        "missing": len(missing),
                        "first_missing": min(missing).isoformat() if missing else None,
                    }
                )
            ok = (
                qc.ok and len(coverage) == sessions and all(row["missing"] == 0 for row in coverage)
            )
            report.update(status="healthy" if ok else "incomplete", sessions=coverage)
        reports.append(report)
    return {
        "as_of": as_of.isoformat(),
        "required_sessions": sessions,
        "ok": bool(reports) and all(report["status"] == "healthy" for report in reports),
        "instruments": reports,
    }
