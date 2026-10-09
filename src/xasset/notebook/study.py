"""Registered notebook studies: configuration, registration, discovery and holdout.

A study freezes its configuration, the daily symbol manifest and the notebook code
when registered. Discovery runs purged walk-forward folds from ``first_test`` to the
holdout start (the last 20% of the registered period); the holdout is opened once,
with a written reason, and then run once. Every run is written to
``data/lab/runs/`` in the same shape as replay runs, so the dashboard lists both.
"""

from __future__ import annotations

import hashlib
import traceback
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field

from xasset.lab.evaluate import calendar_days, daily_pnl, episodes, max_t_test, trade_summary
from xasset.lab.ledger import trade_json
from xasset.notebook.daily import Panel, daily_dir, load_panel
from xasset.notebook.engine import Decision, simulate
from xasset.notebook.features import Context, UniverseRules, build_context, spread_bps
from xasset.notebook.n01_missing_breakdown import MissingBreakdown
from xasset.notebook.n03_tail_dependence import DownsideSeparation
from xasset.notebook.n04_recovery_clocks import RecoveryClocks
from xasset.notebook.n08_upper_tail import UpperTailEscape
from xasset.notebook.walkforward import Fold, FoldResult, incremental_value, run_folds
from xasset.store.writer import write_json

Phase = Literal["discovery", "holdout"]
STRATEGIES: dict[str, Any] = {
    "n01": MissingBreakdown,
    "n03": DownsideSeparation,
    "n04": RecoveryClocks,
    "n08": UpperTailEscape,
}


class Costs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    fee_bps: float = Field(ge=0)
    tiers: list[tuple[int, float]] = Field(min_length=1)
    evidence: str = Field(min_length=10)


class PortfolioSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    initial_nav: float = Field(default=100_000, gt=0)
    max_positions: int = Field(default=10, ge=1)
    participation: float = Field(default=0.02, gt=0, le=1)


class StudyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    description: str = Field(min_length=10)
    strategies: list[str] = Field(min_length=1)
    history_start: date
    start: date
    end: date
    first_test: date
    fold_months: int = Field(default=12, ge=1, le=24)
    universe: UniverseRules
    costs: Costs
    portfolio: PortfolioSettings = Field(default_factory=PortfolioSettings)


def load_study(path: Path) -> StudyConfig:
    data = yaml.safe_load(path.read_text())
    rules = dict(data.get("universe", {}))
    if rules.get("symbols") is not None:
        rules["symbols"] = tuple(rules["symbols"])
    data["universe"] = UniverseRules(**rules)
    return StudyConfig.model_validate(data)


def plain(value: Any) -> Any:
    """JSON-safe copy: numpy scalars to Python, non-finite floats to None."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return [plain(v) for v in value.tolist()]
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if np.isfinite(number) else None
    return value


def code_revision() -> str:
    root = Path(__file__).resolve().parent
    shared = root.parent / "lab"
    digest = hashlib.sha256()
    for path in [*sorted(root.glob("*.py")), shared / "evaluate.py", shared / "ledger.py"]:
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def data_fingerprint(root: Path, config: StudyConfig) -> str:
    """Hash of every price and notional the study can read (its window only)."""
    panel = load_panel(root, config.history_start, config.end)
    digest = hashlib.sha256()
    digest.update("\n".join(panel.symbols).encode())
    for array in (
        panel.open,
        panel.high,
        panel.low,
        panel.close,
        panel.notional,
        panel.buy_notional,
        panel.first,
        panel.last,
    ):
        digest.update(np.ascontiguousarray(array).tobytes())
    return digest.hexdigest()


def registry_path(root: Path, study: str) -> Path:
    return root / "lab" / "registry" / f"{study}.json"


def read_registry(root: Path, study: str) -> dict[str, Any] | None:
    import json

    path = registry_path(root, study)
    return json.loads(path.read_text()) if path.exists() else None


def holdout_start(config: StudyConfig) -> date:
    span = (config.end - config.start).days
    return config.start + timedelta(days=int(span * 0.8))


def register(root: Path, path: Path) -> dict[str, Any]:
    config = load_study(path)
    manifest = daily_dir(root) / "_symbols.json"
    if not manifest.exists():
        raise ValueError("Download the daily panel first: xasset study ingest-daily")
    unknown = set(config.strategies) - STRATEGIES.keys()
    if unknown:
        raise ValueError(f"Unknown notebook strategies: {', '.join(sorted(unknown))}")
    if config.first_test >= holdout_start(config):
        raise ValueError("The first test fold must start before the holdout")
    registration = {
        "book": config.id,
        "kind": "notebook-study",
        "book_path": str(path),
        "book_sha256": sha256(path),
        "universe_sha256": data_fingerprint(root, config),
        "code": code_revision(),
        "start": datetime.combine(config.start, datetime.min.time(), tzinfo=UTC).isoformat(),
        "end": datetime.combine(config.end, datetime.min.time(), tzinfo=UTC).isoformat(),
        "holdout_start": datetime.combine(
            holdout_start(config), datetime.min.time(), tzinfo=UTC
        ).isoformat(),
        "registered_at": datetime.now(UTC).isoformat(),
    }
    existing = read_registry(root, config.id)
    if existing is not None:
        same = all(
            existing["registration"].get(k) == registration[k]
            for k in ("book_sha256", "universe_sha256", "start", "end")
        )
        if not same:
            raise ValueError(
                "Study already registered with a different configuration or data; "
                "register a new study ID"
            )
        return existing
    record: dict[str, Any] = {"registration": registration, "runs": [], "holdout": None}
    write_json(registry_path(root, config.id), record)
    return record


def open_holdout(root: Path, study: str, reason: str) -> dict[str, Any]:
    record = read_registry(root, study)
    if record is None:
        raise ValueError("Unknown study; register it first")
    if record["holdout"] is not None:
        raise ValueError("The holdout has already been opened for this study")
    if len(reason.strip()) < 20:
        raise ValueError("Record a substantive reason (20+ characters) for opening the holdout")
    if not any(r["phase"] == "discovery" and r["status"] == "completed" for r in record["runs"]):
        raise ValueError("Complete a discovery run before opening the holdout")
    record["holdout"] = {
        "opened_at": datetime.now(UTC).isoformat(),
        "reason": reason.strip(),
        "run": None,
    }
    write_json(registry_path(root, study), record)
    return record


def folds_for(panel: Panel, config: StudyConfig, phase: Phase) -> list[Fold]:
    index = {d: i for i, d in enumerate(panel.dates)}
    cutoff = holdout_start(config)
    if phase == "holdout":
        return [Fold(index[cutoff], index[config.end - timedelta(days=1)] + 1)]
    folds = []
    begin = config.first_test
    while begin < cutoff:
        months = begin.year * 12 + begin.month - 1 + config.fold_months
        finish = min(date(months // 12, months % 12 + 1, 1), cutoff)
        folds.append(Fold(index[begin], index[finish - timedelta(days=1)] + 1))
        begin = finish
    return folds


def restrict(context: Context, start: int, end: int) -> Context:
    """Keep only coins eligible at some point in the study window (plus the benchmark)."""
    keep = context.eligible[start:end].any(axis=0)
    keep[context.market] = True
    columns = np.flatnonzero(keep)
    panel = context.panel
    subset = Panel(
        dates=panel.dates,
        symbols=[panel.symbols[c] for c in columns],
        open=panel.open[:, columns],
        high=panel.high[:, columns],
        low=panel.low[:, columns],
        close=panel.close[:, columns],
        notional=panel.notional[:, columns],
        buy_notional=panel.buy_notional[:, columns],
        first=panel.first[columns],
        last=panel.last[columns],
        status={panel.symbols[c]: panel.status[panel.symbols[c]] for c in columns},
    )
    return build_context(subset, context.rules)


def merge(results: list[FoldResult], attribute: str) -> dict[int, list[Decision]]:
    merged: dict[int, list[Decision]] = {}
    for result in results:
        for day, items in getattr(result, attribute).items():
            merged.setdefault(day, []).extend(items)
    return merged


def sleeve(
    context: Context,
    strategy: Any,
    decisions: dict[int, list[Decision]],
    early: np.ndarray | None,
    spread: np.ndarray,
    config: StudyConfig,
    start: int,
    end: int,
    multiplier: float,
    delay: int,
    sid: str,
    title: str,
) -> dict[str, Any]:
    exit_check = None
    if early is not None:
        flags = early

        def exit_check(day: int, column: int) -> bool:
            return bool(flags[day, column])

    simulation = simulate(
        context,
        decisions,
        strategy.rule,
        spread,
        config.costs.fee_bps,
        start,
        end,
        initial_nav=config.portfolio.initial_nav,
        multiplier=multiplier,
        delay=delay,
        early_exit=exit_check,
        prefix=f"{sid}-",
    )
    first = datetime.combine(context.panel.dates[start], datetime.min.time(), tzinfo=UTC)
    last = datetime.combine(context.panel.dates[end - 1], datetime.min.time(), tzinfo=UTC)
    days = calendar_days(first, last + timedelta(days=1))
    nav = config.portfolio.initial_nav
    marks = [(at, value) for at, value in simulation.ledger.nav]
    return {
        "title": title,
        "summary": trade_summary(simulation.trades, nav, days),
        "events": {
            "decisions": sum(len(v) for v in decisions.values()),
            "filled": len(simulation.trades),
            "skipped": simulation.skipped,
        },
        "sessions5": episodes(marks, nav, first, length=5),
        "episodes14": episodes(marks, nav, first),
        "nav": [[d.isoformat(), v] for d, v in simulation.nav],
        "daily": [v / nav for v in daily_pnl(simulation.trades, days)],
        "trades": [trade_json(t) for t in simulation.trades],
    }


def run(root: Path, path: Path, phase: Phase = "discovery") -> dict[str, Any]:
    config = load_study(path)
    record = read_registry(root, config.id)
    if record is None:
        raise ValueError("Register the study before running it")
    registration = record["registration"]
    if sha256(path) != registration["book_sha256"]:
        raise ValueError("Study file changed since registration; register a new study ID")
    if data_fingerprint(root, config) != registration["universe_sha256"]:
        raise ValueError("The study's price data changed since registration")
    if phase == "holdout":
        if record["holdout"] is None:
            raise ValueError("The holdout is sealed; open it explicitly first")
        if record["holdout"]["run"] is not None:
            raise ValueError("The holdout has already been consumed")
    run_id = f"{config.id}-{phase}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    output: dict[str, Any] = {
        "id": run_id,
        "book": config.id,
        "kind": "notebook-study",
        "phase": phase,
        "started_at": datetime.now(UTC).isoformat(),
        "code": code_revision(),
        "registered_code": registration["code"],
        "amended_code": code_revision() != registration["code"],
        "basis": "daily",
        "variant": "daily-model",
        "strategies": {sid: {"title": STRATEGIES[sid].title} for sid in config.strategies},
        "universe": {
            "id": "binance-usdt-daily",
            "benchmark": config.universe.benchmark,
            "selection_note": (
                "Point-in-time: Binance USDT spot pairs including delisted ones; each day the "
                f"top {config.universe.top} by trailing 30-day median notional among coins listed "
                f"at least {config.universe.min_age_days} days with median notional of at least "
                f"{config.universe.min_liquidity:,.0f} USDT. Stablecoins, fiat, leveraged and "
                "wrapped tokens and tokenized equities are excluded by type."
            ),
        },
    }
    try:
        output["scenarios"] = scenarios(root, config, phase, output)
        output["status"] = "completed"
    except Exception as exc:  # failed attempts are recorded too
        output["status"] = "failed"
        output["error"] = f"{type(exc).__name__}: {exc}"
        output["traceback"] = traceback.format_exc()
    output["finished_at"] = datetime.now(UTC).isoformat()
    write_json(root / "lab" / "runs" / f"{run_id}.json", plain(output))
    record = read_registry(root, config.id) or record
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
    write_json(registry_path(root, config.id), record)
    return output


def scenarios(
    root: Path, config: StudyConfig, phase: Phase, output: dict[str, Any]
) -> dict[str, Any]:
    panel = load_panel(root, config.history_start, config.end)
    full = build_context(panel, config.universe)
    index = {d: i for i, d in enumerate(panel.dates)}
    study_start = index[config.start]
    folds = folds_for(panel, config, phase)
    context = restrict(full, study_start, folds[-1].end)
    spread = spread_bps(context, config.costs.tiers)
    report_start, report_end = folds[0].start, folds[-1].end
    output["period"] = {
        "replay_start": datetime.combine(config.start, datetime.min.time(), tzinfo=UTC).isoformat(),
        "report_from": datetime.combine(
            panel.dates[report_start], datetime.min.time(), tzinfo=UTC
        ).isoformat(),
        "end": datetime.combine(
            panel.dates[report_end - 1] + timedelta(days=1), datetime.min.time(), tzinfo=UTC
        ).isoformat(),
    }
    output["folds"] = [f.label(panel.dates) for f in folds]
    prepared = []
    diagnostics: dict[str, Any] = {}
    for sid in config.strategies:
        strategy = STRATEGIES[sid](root)
        strategy.prepare(context)
        results = run_folds(context, strategy, folds, study_start, spread, config.costs.fee_bps)
        early = None
        if any(result.early is not None for result in results):
            early = np.zeros(context.shape, dtype=bool)
            for result in results:
                if result.early is not None:
                    window = slice(result.fold.start, result.fold.end)
                    early[window] = result.early[window]
        diagnostics[sid] = {
            "incremental": incremental_value(context, results, strategy.outcome(context)),
            "folds": [
                {
                    "test": r.fold.label(panel.dates),
                    "ridge_penalty": r.lam,
                    "signal_quantile": r.quantile,
                    "threshold": r.threshold,
                    "allowance": r.allowance,
                    "training_rows": r.train_rows,
                    "decisions": sum(len(v) for v in r.decisions.values()),
                    "checks": r.notes.get("diagnostics", {}),
                }
                for r in results
            ],
        }
        prepared.append((sid, strategy, results, early))
    settings = {
        "base": (1.0, 0),
        "costs_2x": (2.0, 0),
        "delay_1_bar": (1.0, 1),
    }
    output_scenarios: dict[str, Any] = {}
    nav = config.portfolio.initial_nav
    for name, (multiplier, delay) in settings.items():
        strategies: dict[str, Any] = {}
        trades: list[dict[str, Any]] = []
        sleeves = []
        for sid, strategy, results, early in prepared:
            main = sleeve(
                context,
                strategy,
                merge(results, "decisions"),
                early,
                spread,
                config,
                report_start,
                report_end,
                multiplier,
                delay,
                sid,
                strategy.title,
            )
            base = sleeve(
                context,
                strategy,
                merge(results, "baseline"),
                None,
                spread,
                config,
                report_start,
                report_end,
                multiplier,
                delay,
                sid + "-base",
                f"{strategy.title}: matched baseline",
            )
            main["diagnostics"] = diagnostics[sid]
            base["baseline_of"] = sid
            for key, item in ((sid, main), (sid + "-base", base)):
                trades.extend(item.pop("trades"))
                strategies[key] = item
            sleeves.append(main["nav"])
        series = {sid: item["daily"] for sid, item in strategies.items()}
        combined = []
        for rows in zip(*sleeves, strict=True):
            combined.append([rows[0][0], float(np.mean([v for _, v in rows]))])
        first = datetime.combine(panel.dates[report_start], datetime.min.time(), tzinfo=UTC)
        marks = [
            (datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(days=1), value)
            for day, value in combined
        ]
        for item in strategies.values():
            item.pop("daily")
        output_scenarios[name] = {
            "settings": {"basis": "daily", "cost_multiplier": multiplier, "delay_bars": delay},
            "minutes": None,
            "bars": int(np.isfinite(context.panel.close[report_start:report_end]).sum()),
            "strategies": strategies,
            "multiplicity": max_t_test(series),
            "portfolio": {
                "start_nav": nav,
                "end_nav": combined[-1][1] if combined else nav,
                "nav_daily": combined,
                "episodes14": episodes(marks, nav, first),
                "sessions5": episodes(marks, nav, first, length=5),
                "note": "Equal capital in each strategy's sleeve; baselines are excluded.",
            },
            "trades": trades,
        }
    return output_scenarios
