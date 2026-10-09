"""Independent accounting and validation checks on native engine results."""

import math
import statistics
from datetime import datetime, timedelta
from typing import Any

from xasset.normalize.timebase import minute_floor
from xasset.research.contracts import Request, Scenario, ValidationResult
from xasset.research.experiment import canonical_json

REQUIRED_ENGINE_CHECKS = {
    "purged_walk_forward",
    "training_only_selection",
    "stop_first_fills",
    "feature_truncation",
    "signal_delay_degrades",
    "data_quality",
}


def scenario_checks(scenario: Scenario, request: Request) -> dict[str, Any]:
    failures: list[str] = []
    missing: list[str] = []
    spec = request.experiment
    if not scenario.trades or not scenario.daily or not scenario.folds:
        return {
            "failures": ["no completed trades, daily results, or folds"],
            "missing": [],
            "metrics": {},
        }
    if scenario.trial_count_used != request.trial_count:
        failures.append("selection statistics used a different registry trial count")
    for name in sorted(REQUIRED_ENGINE_CHECKS):
        evidence = scenario.checks.get(name)
        if evidence is None or evidence.passed is None:
            missing.append(name)
        elif not evidence.passed:
            failures.append(name)

    folds = {fold.id: fold for fold in scenario.folds}
    if len(folds) != len(scenario.folds):
        failures.append("duplicate fold IDs")
    if len({trade.id for trade in scenario.trades}) != len(scenario.trades):
        failures.append("duplicate trade IDs")
    grid = {canonical_json(params) for params in spec.parameters()}
    previous_end: datetime | None = None
    for fold in sorted(scenario.folds, key=lambda item: item.test_start):
        if not (spec.start <= fold.train_start < fold.train_end <= fold.test_start < fold.test_end):
            failures.append("invalid train/test chronology")
        if fold.train_end > fold.test_start - timedelta(minutes=spec.embargo_minutes):
            failures.append("train/test embargo too short")
        if fold.train_end - fold.train_start < timedelta(days=spec.training_days):
            failures.append("training window shorter than preregistered")
        if fold.test_end - fold.test_start > timedelta(days=spec.test_days):
            failures.append("test window longer than preregistered")
        if fold.test_start < request.data_start or fold.test_end > request.data_end:
            failures.append("test fold outside permitted data interval")
        if previous_end is not None and fold.test_start < previous_end:
            failures.append("overlapping out-of-sample folds")
        previous_end = fold.test_end
        if canonical_json(fold.parameters) not in grid:
            failures.append("fold parameters outside preregistered grid")
        if request.phase == "vault":
            if fold.train_end > spec.discovery_end or fold.parameters != request.frozen_parameters:
                failures.append("vault refitting or parameter change")

    instruments = {item.id: item for item in request.instruments}
    rows = {(row["symbol"], row["ts_end"]): row for row in request.bars.iter_rows(named=True)}
    pnl_by_fold = dict.fromkeys(folds, 0.0)
    for trade in scenario.trades:
        trade_fold = folds.get(trade.fold)
        instrument = instruments.get(trade.symbol)
        if trade_fold is None or instrument is None:
            failures.append("trade references an unknown fold or instrument")
            continue
        if (
            not trade_fold.test_start
            <= trade.signal_at
            <= trade.entry_at
            <= trade.exit_at
            <= trade_fold.test_end
        ):
            failures.append("trade outside test fold or fills before signal")
        if trade.exit_at - trade.entry_at > timedelta(minutes=spec.max_holding_minutes):
            failures.append("holding period exceeds preregistration")
        # Entry at a bar-close instant is the NEXT minute's opening instant.
        # Reject intrabar entries that cannot represent a next-open fill.
        if trade.entry_at != minute_floor(trade.entry_at):
            failures.append("entry must occur at a subsequent minute open")
        if trade.entry_at != trade.entry_bar_end - timedelta(minutes=1):
            failures.append("entry is not the next bar's open")
        if trade.entry_bar_end <= trade.signal_at:
            failures.append("same-bar signal and fill")
        entry_bar = rows.get((trade.symbol, trade.entry_bar_end))
        exit_bar = rows.get((trade.symbol, trade.exit_bar_end))
        if entry_bar is not None and not math.isclose(trade.entry_price, entry_bar["open"]):
            failures.append("entry price differs from the observed next open")
        if exit_bar is not None and not exit_bar["low"] <= trade.exit_price <= exit_bar["high"]:
            failures.append("exit price is outside its observed bar")
        if not (
            math.isclose(trade.entry_notional, trade.entry_price * trade.units)
            and math.isclose(trade.exit_notional, trade.exit_price * trade.units)
        ):
            failures.append("trade notionals do not reconcile to fills")
        if not math.isclose(
            trade.gross_pnl,
            (trade.exit_price - trade.entry_price) * trade.units,
            rel_tol=1e-7,
            abs_tol=1e-7,
        ):
            failures.append("gross PnL does not reconcile to long-position fills")
        for stamp in (trade.entry_bar_end, trade.exit_bar_end):
            bar = rows.get((trade.symbol, stamp))
            if bar is None:
                failures.append("fill has no observed market bar")
            elif (
                instrument.asset_class in {"fx", "cfd"}
                and bar["bid_open"] is not None
                and bar["ask_open"] is not None
            ):
                pass  # Quote instruments use actual two-sided quotes, not invented exchange volume.
            elif bar["volume"] is None or bar["volume"] <= 0:
                failures.append("fill on a bar without positive traded volume")
        profile = request.costs.profiles[instrument.asset_class]
        expected_cost = profile.per_fill(
            trade.entry_notional, trade.units, scenario.multiplier
        ) + profile.per_fill(trade.exit_notional, trade.units, scenario.multiplier)
        if not math.isclose(trade.cost, expected_cost, rel_tol=1e-7, abs_tol=1e-7):
            failures.append("trade costs do not match the frozen cost scenario")
        pnl_by_fold[trade.fold] += trade.net_pnl

    total = sum(trade.net_pnl for trade in scenario.trades)
    reported = sum(day.net_pnl for day in scenario.daily)
    if not math.isclose(total, reported, rel_tol=1e-7, abs_tol=1e-7):
        failures.append("daily and trade PnL do not reconcile")
    if len({day.day for day in scenario.daily}) != len(scenario.daily):
        failures.append("duplicate portfolio return days")
    equity = spec.initial_capital
    for day in sorted(scenario.daily, key=lambda item: item.day):
        if equity <= 0 or not math.isclose(
            day.portfolio_return, day.net_pnl / equity, rel_tol=1e-7, abs_tol=1e-10
        ):
            failures.append("daily returns do not reconcile to portfolio equity")
        equity += day.net_pnl
    for day in scenario.daily:
        if not any(
            fold.test_start.date() <= day.day <= (fold.test_end - timedelta(microseconds=1)).date()
            for fold in scenario.folds
        ):
            failures.append("daily return outside out-of-sample folds")
    daily_returns = [day.portfolio_return for day in scenario.daily]
    daily_t = None
    if len(daily_returns) >= 2 and statistics.stdev(daily_returns) > 0:
        daily_t = (
            statistics.mean(daily_returns)
            / statistics.stdev(daily_returns)
            * math.sqrt(len(daily_returns))
        )
    if len(daily_returns) < spec.thresholds.minimum_days or daily_t is None:
        missing.append("enough nonconstant daily portfolio returns")
    elif daily_t < spec.thresholds.minimum_daily_t:
        failures.append("daily out-of-sample t-statistic below threshold")
    if total <= 0:
        failures.append("nonpositive net PnL")
    removed = max(1, math.ceil(len(scenario.trades) * 0.05))
    trimmed = sum(sorted((trade.net_pnl for trade in scenario.trades), reverse=True)[removed:])
    if trimmed <= 0:
        failures.append("nonpositive PnL after removing the best 5% of trades")
    share = max(pnl_by_fold.values()) / total if total > 0 else None
    if share is not None and share > 0.5:
        failures.append("one fold supplies more than half the net profit")
    for value, label, passed in (
        (
            scenario.dsr_probability,
            "deflated Sharpe probability",
            scenario.dsr_probability is not None
            and scenario.dsr_probability >= spec.thresholds.minimum_dsr,
        ),
        (
            scenario.pbo_probability,
            "probability of backtest overfitting",
            scenario.pbo_probability is not None
            and scenario.pbo_probability <= spec.thresholds.maximum_pbo,
        ),
    ):
        if value is None:
            missing.append(label)
        elif not passed:
            failures.append(label + " outside preregistered threshold")
    return {
        "failures": sorted(set(failures)),
        "missing": sorted(set(missing)),
        "metrics": {
            "net_pnl": total,
            "trade_count": len(scenario.trades),
            "trimmed_net_pnl": trimmed,
            "trades_removed": removed,
            "maximum_fold_profit_share": share,
            "daily_t": daily_t,
            "daily_observations": len(daily_returns),
            "dsr_probability": scenario.dsr_probability,
            "pbo_probability": scenario.pbo_probability,
        },
    }


def evaluate(result: ValidationResult, request: Request) -> dict[str, Any]:
    failures: list[str] = []
    missing: list[str] = []
    if result.strategy_revision != request.experiment.strategy_revision:
        failures.append("strategy revision differs from preregistration")
    if result.currency != request.costs.currency:
        failures.append("PnL/cost currencies differ")
    if request.experiment.purpose == "smoke":
        missing.append("smoke runs cannot qualify as research candidates")
    if result.selected_parameters not in request.experiment.parameters():
        failures.append("selected parameters outside registered grid")
    if request.phase == "vault" and result.selected_parameters != request.frozen_parameters:
        failures.append("holdout parameter selection changed")
    followers = {str(params["follower"]) for params in request.experiment.parameters()}
    if any(
        not request.costs.profiles[item.asset_class].calibrated
        for item in request.instruments
        if item.id in followers
    ):
        missing.append("calibrated cost evidence for every asset class")
    if sorted(scenario.multiplier for scenario in result.scenarios) != [1, 2]:
        failures.append("exactly one 1x and one 2x cost run are mandatory")
    scenarios = {
        str(scenario.multiplier): scenario_checks(scenario, request)
        for scenario in result.scenarios
    }
    for multiplier, checks in scenarios.items():
        failures.extend(f"{multiplier}x: {item}" for item in checks["failures"])
        missing.extend(f"{multiplier}x: {item}" for item in checks["missing"])
    if len(result.scenarios) == 2:
        schedules = [
            [fold.model_dump(mode="json") for fold in scenario.folds]
            for scenario in result.scenarios
        ]
        if schedules[0] != schedules[1]:
            failures.append("cost stress changed the selected fold/parameter schedule")
    return {
        "status": "rejected"
        if failures
        else "incomplete"
        if missing
        else "candidate"
        if request.phase == "discovery"
        else "awaiting_review",
        "failures": sorted(set(failures)),
        "missing": sorted(set(missing)),
        "scenarios": scenarios,
        "accepted": False,
        "remaining_acceptance": [
            "independent look-ahead audit",
            "Nautilus cross-engine reconciliation",
        ]
        + (["one-time held-out evaluation"] if request.phase == "discovery" else []),
        "note": (
            "Candidate/review states are not accepted verdicts. Engine evidence requires audit."
        ),
    }
