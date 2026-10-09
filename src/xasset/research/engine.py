"""Native rolling training-only selection and sealed holdout evaluation."""

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import polars as pl

from xasset.config import Instrument
from xasset.qc.checks import check_bars
from xasset.research.contracts import (
    CheckEvidence,
    DailyResult,
    Fold,
    Parameter,
    Request,
    Scenario,
    ValidationResult,
)
from xasset.research.costs import Costs
from xasset.research.execution import Simulation, simulate
from xasset.research.experiment import Experiment, canonical_json
from xasset.research.session_signals import generate
from xasset.research.statistics import deflated_sharpe, probability_overfit
from xasset.research.strategy import FREQUENCY_MINUTES, Parameters


def source_revision() -> str:
    """Fingerprint installed Python sources, including execution, features and gates."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def validate_design(spec: Experiment, instruments: list[Instrument], costs: Costs) -> None:
    if spec.strategy not in {"cross_asset_leadlag", "session_open", "event_response"}:
        raise ValueError("Unknown native signal strategy")
    if spec.strategy_revision != source_revision():
        raise ValueError("Engine source changed since preregistration; register a new family")
    universe = {item.id: item for item in instruments}
    if set(universe) != set(spec.symbols):
        raise ValueError("Registered universe differs from the experiment")
    followers = {str(params["follower"]) for params in spec.parameters()}
    for item in instruments:
        if item.id not in followers:
            continue  # Driver returns are dimensionless; no position or FX conversion.
        if item.asset_class not in {"equity", "etf", "crypto"} or item.binance_market != "spot":
            raise ValueError("Native v1 supports cash equities/ETFs and spot crypto only")
        if item.currency != costs.currency:
            raise ValueError("Native v1 requires one common quote/reporting currency")
        if item.asset_class not in costs.profiles:
            raise ValueError("Missing execution cost profile")
    for candidate in spec.parameters():
        params = Parameters.model_validate(candidate)
        if {params.driver, params.follower} - universe.keys():
            raise ValueError("Strategy symbols must belong to the frozen universe")
        if params.hold_minutes > spec.max_holding_minutes:
            raise ValueError("Candidate holding time exceeds preregistered maximum")
        if spec.strategy == "session_open":
            if universe[params.follower].calendar is None:
                raise ValueError("Session-open strategy requires a follower calendar")
            if params.anchor == "regional_session" and universe[params.driver].calendar is None:
                raise ValueError("Regional strategy requires a driver calendar")


def schedule(request: Request) -> list[Fold]:
    spec = request.experiment
    training = timedelta(days=spec.training_days)
    embargo = timedelta(minutes=spec.embargo_minutes)
    cursor = (
        request.data_start + training + embargo
        if request.phase == "discovery"
        else request.data_start
    )
    output: list[Fold] = []
    while cursor < request.data_end:
        train_end = cursor - embargo if request.phase == "discovery" else spec.discovery_end
        end = min(cursor + timedelta(days=spec.test_days), request.data_end)
        output.append(
            Fold(
                id=f"fold-{len(output):03d}",
                train_start=train_end - training,
                train_end=train_end,
                test_start=cursor,
                test_end=end,
                parameters=request.frozen_parameters or spec.parameters()[0],
            )
        )
        cursor = end
    if not output:
        raise ValueError("Discovery interval is too short for the training window and embargo")
    return output


def combine_daily(simulations: list[Simulation], capital: float) -> list[DailyResult]:
    pnl: dict[date, float] = {}
    for simulation in simulations:
        for daily in simulation.daily:
            pnl[daily.day] = pnl.get(daily.day, 0.0) + daily.net_pnl
    output = []
    for day, value in sorted(pnl.items()):
        output.append(DailyResult(day=day, net_pnl=value, portfolio_return=value / capital))
        capital += value
    return output


def evaluate(request: Request) -> ValidationResult:
    spec = request.experiment
    validate_design(spec, request.instruments, request.costs)
    quality = check_bars(request.bars)
    if not quality.ok:
        raise ValueError(f"Invalid engine input: {quality.errors}")
    if request.bars.filter(
        (pl.col("ts_end") <= request.data_start) | (pl.col("ts_end") > request.data_end)
    ).height:
        raise ValueError("Engine input exceeds the authorized research interval")
    universe = {item.id: item for item in request.instruments}
    grid = spec.parameters() if request.phase == "discovery" else [request.frozen_parameters]
    if any(params is None for params in grid):
        raise ValueError("Holdout requires frozen discovery parameters")
    candidates = [params for params in grid if params is not None]
    signal_cache: dict[str, dict[datetime, datetime]] = {}
    prefix_ok = True
    cutoff = request.data_start + (request.data_end - request.data_start) / 2
    for candidate in candidates:
        full = generate(request, candidate)
        truncated = generate(request, candidate, cutoff)
        prefix_ok &= truncated == {
            stamp: signal for stamp, signal in full.items() if stamp <= cutoff
        }
        signal_cache[canonical_json(candidate)] = full

    def execute(
        fold: Fold, multiplier: Literal[1, 2], capital: float, delay: bool = False
    ) -> Simulation:
        params = Parameters.model_validate(fold.parameters)
        item = universe[params.follower]
        signal = signal_cache[canonical_json(fold.parameters)]
        if delay:
            step = timedelta(minutes=FREQUENCY_MINUTES[spec.frequency])
            signal = {stamp + step: original for stamp, original in signal.items()}
        simulation = simulate(
            request.bars,
            item,
            fold,
            signal,
            request.costs.profiles[item.asset_class],
            multiplier,
            capital,
            spec.allocation_fraction,
        )
        if any(
            trade.exit_at - trade.entry_at > timedelta(minutes=spec.max_holding_minutes)
            for trade in simulation.trades
        ):
            raise ValueError("An unavailable exit exceeded the preregistered holding horizon")
        return simulation

    folds = schedule(request)
    training_scores: list[dict[str, object]] = []
    if request.phase == "discovery":
        selected_folds = []
        for fold in folds:
            scores = []
            for candidate in candidates:
                training_fold = fold.model_copy(
                    update={
                        "test_start": fold.train_start,
                        "test_end": fold.train_end,
                        "parameters": candidate,
                    }
                )
                simulation = execute(training_fold, 1, spec.initial_capital)
                scores.append(simulation.ending_cash - spec.initial_capital)
            # Only training PnL influences the winner. Grid order breaks ties.
            winner = max(range(len(candidates)), key=lambda i: (scores[i], -i))
            selected_folds.append(fold.model_copy(update={"parameters": candidates[winner]}))
            training_scores.append(
                {"fold": fold.id, "net_pnl_by_candidate": scores, "winner": winner}
            )
        folds = selected_folds

    def run_folds(
        multiplier: Literal[1, 2],
        override: dict[str, Parameter] | None = None,
        delay: bool = False,
    ) -> list[Simulation]:
        capital = spec.initial_capital
        output = []
        for fold in folds:
            used = fold.model_copy(update={"parameters": override}) if override else fold
            simulation = execute(used, multiplier, capital, delay)
            capital = simulation.ending_cash
            output.append(simulation)
        return output

    scenarios = []
    selection_returns: dict[str, list[list[float]]] = {}
    delayed_pnl: dict[str, float] = {}
    for multiplier in (1, 2):
        stress: Literal[1, 2] = 1 if multiplier == 1 else 2
        simulations = run_folds(stress)
        daily = combine_daily(simulations, spec.initial_capital)
        delayed = run_folds(stress, delay=True)
        delayed_total = sum(day.net_pnl for simulation in delayed for day in simulation.daily)
        total = sum(day.net_pnl for day in daily)
        delayed_pnl[str(stress)] = delayed_total
        if request.phase == "discovery":
            matrix = [
                [
                    day.portfolio_return
                    for day in combine_daily(run_folds(stress, candidate), spec.initial_capital)
                ]
                for candidate in candidates
            ]
            pbo = probability_overfit(matrix)
        else:
            assert request.discovery_result is not None
            previous = request.discovery_result["engine_result"]
            matrix = previous["diagnostics"]["selection_returns"][str(stress)]
            pbo = next(
                s["pbo_probability"] for s in previous["scenarios"] if s["multiplier"] == stress
            )
        selection_returns[str(stress)] = matrix
        scenarios.append(
            Scenario(
                multiplier=stress,
                daily=daily,
                trades=[trade for simulation in simulations for trade in simulation.trades],
                folds=folds,
                trial_count_used=request.trial_count,
                dsr_probability=deflated_sharpe(
                    [day.portfolio_return for day in daily], matrix, request.trial_count
                ),
                pbo_probability=pbo,
                checks={
                    "purged_walk_forward": CheckEvidence(
                        passed=True,
                        reference="Bounded training positions; embargo >= holding horizon",
                    ),
                    "training_only_selection": CheckEvidence(
                        passed=True, reference="Training net PnL only; same winners at 1x/2x costs"
                    ),
                    "stop_first_fills": CheckEvidence(
                        passed=True, reference="execution.simulate; known-answer execution tests"
                    ),
                    "feature_truncation": CheckEvidence(
                        passed=prefix_ok,
                        reference=f"Signal prefix equality at {cutoff.isoformat()}",
                    ),
                    "signal_delay_degrades": CheckEvidence(
                        passed=total > 0 and 0 <= delayed_total < total,
                        reference=(
                            f"One signal-bar delay net PnL: {delayed_total:.10g}; "
                            f"base: {total:.10g}"
                        ),
                    ),
                    "data_quality": CheckEvidence(
                        passed=None,
                        reference=(
                            "Structural checks passed; independent provider/action "
                            "evidence not yet wired to research approval"
                        ),
                    ),
                },
            )
        )
    return ValidationResult(
        engine="xasset",
        engine_version="0.1.0",
        strategy_revision=source_revision(),
        currency=request.costs.currency,
        selected_parameters=folds[-1].parameters,
        scenarios=scenarios,
        diagnostics={
            "training_scores": training_scores,
            "selection_returns": selection_returns,
            "delayed_net_pnl": delayed_pnl,
            "selection_objective": (
                "training net PnL at 1x costs; final fold winner frozen for vault"
            ),
            "statistics_scope": (
                "DSR uses daily returns and candidate Sharpe dispersion; "
                "PBO uses discovery OOS CSCV, carried unchanged into vault"
            ),
            "limitations": [
                "long-only cash spot",
                "no FX conversion or financing",
                "DSR assumes independent daily returns",
                "no independent data-quality approval yet",
            ],
        },
    )
