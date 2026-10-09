"""Registration, discovery runs and the one-time sealed holdout for strategy books.

A book is registered once with fingerprints of its configuration and the lab source.
Discovery replays cover the first 80% of the registered interval; the final 20% stays
sealed until ``open_holdout`` is called with a reason, after which it can be run
exactly once. Every run (including failures and zero-trade runs) is persisted.
"""

from __future__ import annotations

import hashlib
import json
import traceback
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from xasset.lab.backtest import basis_for, replay
from xasset.lab.evaluate import (
    calendar_days,
    daily_pnl,
    episodes,
    event_summary,
    max_t_test,
    nav_daily,
    trade_summary,
)
from xasset.lab.ledger import trade_json
from xasset.lab.runtime import RunSettings
from xasset.lab.strategies import REGISTRY
from xasset.lab.universe import Book, Universe, load_book
from xasset.store.writer import write_json

Phase = Literal["discovery", "holdout"]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_revision() -> str:
    """Fingerprint of the replay code path (the live paper package is excluded)."""
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if path.relative_to(root).parts[0] in {"live", "catalog.py", "cli.py"}:
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class Registration:
    book: str
    book_path: str
    book_sha256: str
    universe_sha256: str
    code: str
    start: datetime
    end: datetime
    holdout_start: datetime
    registered_at: datetime

    def json(self) -> dict[str, Any]:
        return {
            "book": self.book,
            "book_path": self.book_path,
            "book_sha256": self.book_sha256,
            "universe_sha256": self.universe_sha256,
            "code": self.code,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "holdout_start": self.holdout_start.isoformat(),
            "registered_at": self.registered_at.isoformat(),
        }


def registry_path(root: Path, book: str) -> Path:
    return root / "lab" / "registry" / f"{book}.json"


def load_registry(root: Path, book: str) -> dict[str, Any] | None:
    path = registry_path(root, book)
    return json.loads(path.read_text()) if path.exists() else None


def register(root: Path, book_path: Path, start: datetime, end: datetime) -> dict[str, Any]:
    book, _ = load_book(book_path)
    universe_path = (
        book.universe if book.universe.is_absolute() else book_path.parent / book.universe
    )
    if end <= start + timedelta(days=10):
        raise ValueError("Register at least ten days")
    span = end - start
    holdout = start + timedelta(seconds=int(span.total_seconds() * 0.8))
    holdout = holdout.replace(hour=0, minute=0, second=0, microsecond=0)
    registration = Registration(
        book=book.id,
        book_path=str(book_path),
        book_sha256=file_hash(book_path),
        universe_sha256=file_hash(universe_path),
        code=code_revision(),
        start=start,
        end=end,
        holdout_start=holdout,
        registered_at=datetime.now(UTC),
    )
    existing = load_registry(root, book.id)
    if existing is not None:
        same = all(
            existing["registration"][k] == registration.json()[k]
            for k in ("book_sha256", "universe_sha256", "start", "end")
        )
        if not same:
            raise ValueError(
                "Book already registered with a different configuration or interval; "
                "register a new book ID (amended designs are new variants)"
            )
        return existing
    record: dict[str, Any] = {"registration": registration.json(), "runs": [], "holdout": None}
    write_json(registry_path(root, book.id), record)
    return record


def open_holdout(root: Path, book: str, reason: str) -> dict[str, Any]:
    record = load_registry(root, book)
    if record is None:
        raise ValueError("Unknown book; register it first")
    if record["holdout"] is not None:
        raise ValueError("The holdout has already been opened for this book")
    if len(reason.strip()) < 20:
        raise ValueError("Record a substantive reason (20+ characters) for opening the holdout")
    if not any(
        run["phase"] == "discovery" and run["status"] == "completed" for run in record["runs"]
    ):
        raise ValueError("Complete a discovery run before opening the holdout")
    record["holdout"] = {
        "opened_at": datetime.now(UTC).isoformat(),
        "reason": reason.strip(),
        "run": None,
    }
    write_json(registry_path(root, book), record)
    return record


def scenario(
    root: Path,
    book: Book,
    universe: Universe,
    start: datetime,
    end: datetime,
    settings: RunSettings,
    report_from: datetime,
) -> dict[str, Any]:
    result = replay(root, book, universe, start, end, settings)
    runtime = result.runtime
    ledger = runtime.ledger
    report_trades = [t for t in ledger.trades if t.entry_time >= report_from]
    days = calendar_days(report_from, end)
    strategies: dict[str, Any] = {}
    daily_series: dict[str, list[float]] = {}
    for strategy in runtime.strategies:
        trades = [t for t in report_trades if t.strategy == strategy.id]
        strategies[strategy.id] = {
            "title": strategy.title,
            "events": event_summary(ledger, strategy.id),
            "summary": trade_summary(trades, book.limits.initial_nav, days),
        }
        daily_series[strategy.id] = [v / book.limits.initial_nav for v in daily_pnl(trades, days)]
    marks = [(at, value) for at, value in ledger.nav if at > report_from]
    base = next((v for at, v in reversed(ledger.nav) if at <= report_from), book.limits.initial_nav)
    return {
        "settings": {
            "basis": settings.basis,
            "cost_multiplier": settings.multiplier,
            "delay_bars": settings.delay_bars,
        },
        "minutes": result.minutes,
        "bars": result.bars,
        "strategies": strategies,
        "multiplicity": max_t_test(daily_series),
        "portfolio": {
            "start_nav": base,
            "end_nav": marks[-1][1] if marks else base,
            "nav_daily": [
                [d.isoformat(), v] for d, v in nav_daily(ledger) if d >= report_from.date()
            ],
            "episodes14": episodes(marks, base, report_from),
        },
        "trades": [trade_json(t) for t in report_trades],
    }


def run(root: Path, book_path: Path, phase: Phase = "discovery") -> dict[str, Any]:
    book, universe = load_book(book_path)
    record = load_registry(root, book.id)
    if record is None:
        raise ValueError("Register the book before running it")
    registration = record["registration"]
    if file_hash(book_path) != registration["book_sha256"]:
        raise ValueError("Book file changed since registration; register a new book ID")
    universe_path = (
        book.universe if book.universe.is_absolute() else book_path.parent / book.universe
    )
    if file_hash(universe_path) != registration["universe_sha256"]:
        raise ValueError("Universe file changed since registration; register a new book ID")
    start = datetime.fromisoformat(registration["start"])
    end = datetime.fromisoformat(registration["end"])
    holdout_start = datetime.fromisoformat(registration["holdout_start"])
    if phase == "holdout":
        holdout = record["holdout"]
        if holdout is None:
            raise ValueError("The holdout is sealed; open it explicitly first")
        if holdout["run"] is not None:
            raise ValueError("The holdout has already been consumed")
        # Calibration warm-up may read earlier data; only holdout trades are reported.
        replay_start, report_from, replay_end = start, holdout_start, end
    else:
        replay_start, report_from, replay_end = start, start, holdout_start
    run_id = f"{book.id}-{phase}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    basis = basis_for(universe)
    output: dict[str, Any] = {
        "id": run_id,
        "book": book.id,
        "phase": phase,
        "started_at": datetime.now(UTC).isoformat(),
        "code": code_revision(),
        "registered_code": registration["code"],
        "amended_code": code_revision() != registration["code"],
        "period": {
            "replay_start": replay_start.isoformat(),
            "report_from": report_from.isoformat(),
            "end": replay_end.isoformat(),
        },
        "basis": basis,
        "variant": "trade-bar" if basis == "trade" else "quote",
        "strategies": {
            sid: {"title": REGISTRY[sid].title, "requires": REGISTRY[sid].requires.__dict__}
            for sid in book.strategies
        },
        "universe": {
            "id": universe.id,
            "instruments": len(universe.instruments),
            "benchmark": universe.benchmark,
            "selection_note": universe.selection_note,
        },
    }
    try:
        output["scenarios"] = {
            "base": scenario(
                root, book, universe, replay_start, replay_end, RunSettings(basis), report_from
            ),
            "costs_2x": scenario(
                root, book, universe, replay_start, replay_end, RunSettings(basis, 2), report_from
            ),
            "delay_1_bar": scenario(
                root,
                book,
                universe,
                replay_start,
                replay_end,
                RunSettings(basis, 1, 1),
                report_from,
            ),
        }
        output["status"] = "completed"
    except Exception as exc:  # Persist failed attempts; they count as attempts.
        output["status"] = "failed"
        output["error"] = f"{type(exc).__name__}: {exc}"
        output["traceback"] = traceback.format_exc()
    output["finished_at"] = datetime.now(UTC).isoformat()
    write_json(root / "lab" / "runs" / f"{run_id}.json", output)
    record = load_registry(root, book.id) or record
    record["runs"].append(
        {
            "id": run_id,
            "phase": phase,
            "status": output["status"],
            "finished_at": output["finished_at"],
            "amended_code": output["amended_code"],
        }
    )
    if phase == "holdout" and record["holdout"] is not None:
        record["holdout"]["run"] = run_id
    write_json(registry_path(root, book.id), record)
    return output
