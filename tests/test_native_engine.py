from dataclasses import replace
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from xasset.config import Instrument
from xasset.research.contracts import Fold, Request
from xasset.research.costs import CostProfile, Costs
from xasset.research.engine import evaluate, source_revision
from xasset.research.execution import simulate
from xasset.research.experiment import Experiment
from xasset.research.gate import evaluate as gate
from xasset.research.statistics import deflated_sharpe, probability_overfit
from xasset.research.strategy import signals
from xasset.store.schema import BAR_SCHEMA

START = datetime(2026, 1, 1, tzinfo=UTC)
PARAMS = {
    "driver": "DRIVER",
    "follower": "FOLLOWER",
    "lookback": 1,
    "threshold": 0.001,
    "hold_minutes": 2,
    "stop_pct": 0.05,
    "target_pct": 0.05,
}


def instrument(symbol="FOLLOWER"):
    return Instrument(
        id=symbol, asset_class="crypto", tier="C", venue="TEST", session="all", yahoo_symbol=symbol
    )


def profile(bps=0):
    return CostProfile(
        commission_bps=bps,
        spread_bps=0,
        slippage_bps=0,
        evidence="Synthetic known-answer test assumptions",
    )


def frame(prices, symbol="FOLLOWER", start=START, volumes=None):
    return pl.DataFrame(
        [
            dict(
                symbol=symbol,
                asset_class="crypto",
                ts_end=start + timedelta(minutes=i + 1),
                open=o,
                high=h,
                low=low,
                close=c,
                volume=volumes[i] if volumes else 1000.0,
                source="yahoo",
                flags=["synthetic", "single_source"],
            )
            for i, (o, h, low, c) in enumerate(prices)
        ],
        schema=BAR_SCHEMA,
    )


def fold(minutes=10, **params):
    return Fold(
        id="test",
        train_start=START - timedelta(days=2),
        train_end=START - timedelta(days=1),
        test_start=START,
        test_end=START + timedelta(minutes=minutes),
        parameters={**PARAMS, **params},
    )


def execute(bars, *, multiplier=1, costs=None, params=None, signals_at=None, capital=1000):
    return simulate(
        bars,
        instrument(),
        fold(**(params or {})),
        signals_at or {START: START},
        costs or profile(),
        multiplier,
        capital,
        0.5,
    )


def test_known_answer_next_open_time_exit_and_daily_ledger():
    bars = frame([(100, 102, 99, 101), (101, 104, 100, 103), (104, 105, 103, 104)])
    result = execute(bars)
    (trade,) = result.trades
    assert trade.entry_price == 100  # Never the signal or entry bar's close.
    assert trade.entry_at == START
    assert trade.entry_bar_end == START + timedelta(minutes=1)
    assert trade.exit_at == START + timedelta(minutes=2)
    assert trade.exit_reason == "time"
    assert trade.units == 5
    assert trade.gross_pnl == 20
    assert result.ending_cash == 1020
    assert sum(day.net_pnl for day in result.daily) == 20
    assert result.daily[0].portfolio_return == 0.02


def test_stop_first_when_both_levels_hit_in_entry_bar():
    result = execute(frame([(100, 110, 90, 108)]))
    (trade,) = result.trades
    assert trade.exit_reason == "stop"
    assert trade.exit_price == 95
    assert trade.net_pnl == -25


def test_gap_stop_fills_at_worse_open_not_stop_level():
    result = execute(frame([(100, 102, 99, 101), (90, 94, 89, 93)]))
    (trade,) = result.trades
    assert trade.exit_price == 90
    assert trade.exit_at == START + timedelta(minutes=1)
    assert trade.net_pnl == -50


def test_opening_target_precedes_later_intrabar_stop():
    (trade,) = execute(frame([(100, 102, 99, 101), (110, 112, 90, 91)])).trades
    assert trade.exit_price == 110
    assert trade.exit_reason == "target"


def test_missing_or_zero_volume_next_bar_cancels_entry():
    bars = frame([(100, 101, 99, 100)] * 3, volumes=[0, 100, 100])
    assert execute(bars).trades == []
    assert execute(bars.slice(1)).trades == []


def test_zero_volume_exit_waits_for_executable_bar():
    bars = frame([(100, 101, 99, 100)] * 4, volumes=[100, 100, 0, 100])
    (trade,) = execute(bars).trades
    assert trade.exit_bar_end == START + timedelta(minutes=4)
    assert trade.exit_at - trade.entry_at == timedelta(minutes=3)


def test_unclosed_position_fails_instead_of_disappearing():
    with pytest.raises(ValueError, match="Unclosed position"):
        execute(frame([(100, 101, 99, 100)]))


def test_costs_charge_both_sides_and_double_every_component():
    costs = CostProfile(
        commission_bps=1,
        commission_per_unit=0.01,
        minimum_commission=0.2,
        spread_bps=2,
        slippage_bps=1,
        evidence="Exact synthetic cost schedule",
    )
    bars = frame([(100, 101, 99, 100)] * 3)
    base = execute(bars, costs=costs)
    stress = execute(bars, costs=costs, multiplier=2)
    (first,) = base.trades
    (second,) = stress.trades
    assert first.units == second.units == 4
    assert first.cost == pytest.approx(0.56)
    assert second.cost == pytest.approx(1.12)
    assert base.ending_cash == pytest.approx(999.44)
    assert stress.ending_cash == pytest.approx(998.88)


def test_never_spends_cash_needed_for_fees():
    result = simulate(
        frame([(100, 101, 99, 100)] * 3),
        instrument(),
        fold(),
        {START: START},
        profile(100),
        1,
        100,
        1,
    )
    assert not result.trades
    assert result.ending_cash == 100


def test_signal_prefix_is_invariant_to_future_truncation_and_perturbation():
    bars = frame([(100 + i, 101 + i, 99 + i, 100 + i) for i in range(15)], "DRIVER")
    cutoff = START + timedelta(minutes=8)
    truncated = bars.filter(pl.col("ts_end") <= cutoff)
    original = signals(bars, instrument("DRIVER"), "1m", PARAMS)
    expected = {key: value for key, value in original.items() if key <= cutoff}
    assert signals(truncated, instrument("DRIVER"), "1m", PARAMS) == expected
    changed = bars.with_columns(
        [
            pl.when(pl.col("ts_end") > cutoff)
            .then(pl.col(name) * 50)
            .otherwise(pl.col(name))
            .alias(name)
            for name in ("open", "high", "low", "close")
        ]
    )
    actual = signals(changed, instrument("DRIVER"), "1m", PARAMS)
    assert {key: value for key, value in actual.items() if key <= cutoff} == expected


def test_signals_reset_at_gap_and_provider_seam():
    bars = frame([(100 + i, 101 + i, 99 + i, 100 + i) for i in range(6)], "DRIVER")
    bars = bars.filter(pl.col("ts_end") != START + timedelta(minutes=3)).with_columns(
        pl.when(pl.col("ts_end") >= START + timedelta(minutes=5))
        .then(pl.lit("binance"))
        .otherwise(pl.col("source"))
        .alias("source")
    )
    result = signals(bars, instrument("DRIVER"), "1m", PARAMS)
    assert START + timedelta(minutes=4) not in result
    assert START + timedelta(minutes=5) not in result
    assert START + timedelta(minutes=6) in result


def request():
    spec = Experiment(
        family="test-native",
        hypothesis="Synthetic training-only selection test",
        strategy="cross_asset_leadlag",
        strategy_revision=source_revision(),
        symbols=["DRIVER", "FOLLOWER"],
        start=START,
        end=START + timedelta(days=5),
        frequency="1m",
        parameter_grid={key: [value] for key, value in PARAMS.items()},
        max_holding_minutes=2,
        embargo_minutes=2,
        training_days=1,
        test_days=1,
    )
    # Sparse sessions deliberately exercise gap resets, not fabricated continuous data.
    frames = []
    for day in range(4):
        for symbol in spec.symbols:
            frames.append(
                frame(
                    [
                        (
                            100 + step * 0.2,
                            100.5 + step * 0.2,
                            99.8 + step * 0.2,
                            100.1 + step * 0.2,
                        )
                        for i in range(12)
                        for step in [min(i, 5) if symbol == "DRIVER" else i]
                    ],
                    symbol,
                    START + timedelta(days=day, hours=1),
                )
            )
    costs = Costs(currency="USD", profiles={"crypto": profile(1)})
    return Request(
        1,
        spec,
        [instrument(name) for name in spec.symbols],
        costs,
        pl.concat(frames),
        "discovery",
        1,
        spec.start,
        spec.discovery_end,
        None,
        None,
    )


def test_walk_forward_selection_cannot_see_future_test_prices():
    original = request()
    original = replace(
        original,
        trial_count=2,
        experiment=original.experiment.model_copy(
            update={"parameter_grid": {**original.experiment.parameter_grid, "lookback": [1, 2]}}
        ),
    )
    result = evaluate(original)
    cutoff = result.scenarios[0].folds[0].test_start
    changed = original.bars.with_columns(
        [
            pl.when(pl.col("ts_end") > cutoff)
            .then(pl.col(name) * 2)
            .otherwise(pl.col(name))
            .alias(name)
            for name in ("open", "high", "low", "close")
        ]
    )
    updated = evaluate(replace(original, bars=changed))
    assert result.diagnostics["training_scores"][0] == updated.diagnostics["training_scores"][0]
    assert result.scenarios[0].folds[0].parameters == updated.scenarios[0].folds[0].parameters
    assert result.scenarios[0].folds == result.scenarios[1].folds
    for current in result.scenarios[0].folds:
        assert current.test_start - current.train_end >= timedelta(minutes=2)
    report = gate(result, original)
    assert not report["accepted"]
    assert "calibrated cost evidence for every asset class" in report["missing"]


def test_gate_catches_tampered_fills_costs_daily_returns_and_trial_counts():
    from xasset.research.gate import scenario_checks

    original = request()
    scenario = evaluate(original).scenarios[0]
    assert scenario.trades
    trade = scenario.trades[0].model_copy(update={"entry_price": 999, "cost": 0})
    day = scenario.daily[0].model_copy(update={"portfolio_return": 42})
    forged = scenario.model_copy(
        update={
            "trades": [trade, *scenario.trades[1:]],
            "daily": [day, *scenario.daily[1:]],
            "trial_count_used": 999,
        }
    )
    failures = scenario_checks(forged, original)["failures"]
    assert "entry price differs from the observed next open" in failures
    assert "trade costs do not match the frozen cost scenario" in failures
    assert "daily returns do not reconcile to portfolio equity" in failures
    assert "selection statistics used a different registry trial count" in failures


def test_input_vault_rows_and_changed_source_revision_are_rejected():
    original = request()
    future = frame([(100, 101, 99, 100)], start=original.experiment.end)
    with pytest.raises(ValueError, match="authorized research interval"):
        evaluate(replace(original, bars=pl.concat([original.bars, future])))
    with pytest.raises(ValueError, match="source changed"):
        evaluate(
            replace(
                original,
                experiment=original.experiment.model_copy(update={"strategy_revision": "outdated"}),
            )
        )


def test_unsupported_derivatives_and_currency_conversion_fail():
    original = request()
    for update in ({"binance_market": "um"}, {"currency": "JPY"}):
        with pytest.raises(ValueError):
            evaluate(
                replace(
                    original,
                    instruments=[item.model_copy(update=update) for item in original.instruments],
                )
            )


def test_statistics_require_real_variance_and_account_for_search_count():
    returns = [0.01, -0.005, 0.012, -0.007, 0.004, 0.005] * 4
    candidates = [returns, [-value for value in returns]]
    assert deflated_sharpe([0.0] * 30, candidates, 2) is None
    assert deflated_sharpe(returns, [returns], 2) is None
    one = deflated_sharpe(returns, candidates, 1)
    many = deflated_sharpe(returns, candidates, 100)
    assert one is not None and many is not None and many < one
    assert probability_overfit([returns]) is None
    assert probability_overfit([returns, [0.0] * len(returns)]) is None
    # Stable ordering on every partition gives zero overfit probability.
    assert probability_overfit([[0.01, 0.02] * 16, [-0.01, -0.02] * 16]) == 0
    assert probability_overfit([[0.01, 0.02] * 16] * 2) == 1  # Median ties are conservative.
