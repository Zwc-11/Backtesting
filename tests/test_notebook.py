"""Notebook studies: daily engine, labels, estimators, signals and leakage."""

import math
from datetime import date, timedelta

import numpy as np
import pytest

from xasset.notebook.daily import Panel, classify, pegged, segments, tokenized_equity
from xasset.notebook.engine import Decision, Rule, simulate
from xasset.notebook.features import UniverseRules, build_context
from xasset.notebook.models import CompetingRisk, kaplan_meier_rmt
from xasset.notebook.n03_tail_dependence import tails
from xasset.notebook.n04_recovery_clocks import (
    DAY,
    HOUR,
    Events,
    extract,
    minute_of,
    restricted_mean,
)
from xasset.notebook.n08_upper_tail import UpperTailEscape
from xasset.notebook.walkforward import Fold, run_folds

START = date(2021, 1, 1)


def synthetic_panel(days: int = 700, coins: int = 30, seed: int = 3) -> Panel:
    rng = np.random.default_rng(seed)
    market = rng.normal(0, 0.03, days)
    closes = np.empty((days, coins + 1))
    opens = np.empty_like(closes)
    for j in range(coins + 1):
        beta = 1.0 if j == 0 else rng.uniform(0.5, 1.5)
        noise = 0 if j == 0 else rng.normal(0, 0.03, days)
        r = beta * market + noise
        closes[:, j] = 10 * np.exp(np.cumsum(r))
        opens[1:, j] = closes[:-1, j]
        opens[0, j] = closes[0, j]
    wiggle = np.exp(np.abs(rng.normal(0, 0.02, closes.shape)))
    highs = np.maximum(opens, closes) * wiggle
    lows = np.minimum(opens, closes) / wiggle
    notional = rng.lognormal(18, 0.5, closes.shape)
    symbols = ["BTCUSDT"] + [f"C{j:02d}USDT" for j in range(coins)]
    return Panel(
        dates=[START + timedelta(days=i) for i in range(days)],
        symbols=symbols,
        open=opens,
        high=highs,
        low=lows,
        close=closes,
        notional=notional,
        buy_notional=notional * 0.5,
        first=np.zeros(coins + 1, dtype=int),
        last=np.full(coins + 1, days - 1),
        status=dict.fromkeys(symbols, "TRADING"),
    )


def flat_panel(prices: dict[int, tuple[float, float, float, float]], days: int = 20) -> Panel:
    """One coin (plus a flat benchmark) with explicit OHLC on chosen days, flat otherwise."""
    shape = (days, 2)
    o = np.full(shape, 100.0)
    h = np.full(shape, 100.0)
    lo = np.full(shape, 100.0)
    c = np.full(shape, 100.0)
    for day, (a, b, x, y) in prices.items():
        o[day, 1], h[day, 1], lo[day, 1], c[day, 1] = a, b, x, y
    symbols = ["BTCUSDT", "XUSDT"]
    return Panel(
        dates=[START + timedelta(days=i) for i in range(days)],
        symbols=symbols,
        open=o,
        high=h,
        low=lo,
        close=c,
        notional=np.full(shape, 1e9),
        buy_notional=np.full(shape, 5e8),
        first=np.zeros(2, dtype=int),
        last=np.full(2, days - 1),
        status=dict.fromkeys(symbols, "TRADING"),
    )


def run_one(panel: Panel, rule: Rule, day: int = 2, delay: int = 0, spread: float = 10.0):
    context = build_context(panel, UniverseRules(min_age_days=0, min_liquidity=0))
    costs = np.full(context.shape, spread)
    decisions = {day: [Decision(day, 1, 1.0, "t")]}
    return simulate(context, decisions, rule, costs, 10.0, 0, len(panel.dates), delay=delay)


def test_entry_next_open_and_stop_inside_day() -> None:
    panel = flat_panel({4: (100, 101, 95, 96)})
    result = run_one(panel, Rule(horizon=5, target=0.10, stop=0.04))
    [trade] = result.trades
    assert trade.entry_time.date() == START + timedelta(days=3)  # decided day 2 -> day 3 open
    assert trade.entry_price == pytest.approx(100 * math.exp(10 / 10_000))
    assert trade.exit_reason == "stop"
    assert trade.exit_price == pytest.approx(96 * math.exp(-10 / 10_000))
    fees = trade.entry_notional * 0.001 + trade.qty * trade.exit_price * 0.001
    assert trade.fees == pytest.approx(fees)
    assert trade.net == pytest.approx(trade.qty * (trade.exit_price - trade.entry_price) - fees)
    assert result.nav[-1][1] == pytest.approx(100_000 + trade.net)


def test_adverse_ordering_gaps_and_time_exit() -> None:
    both = run_one(flat_panel({4: (100, 111, 95, 100)}), Rule(horizon=5, target=0.10, stop=0.04))
    [trade] = both.trades
    assert trade.exit_reason == "stop" and trade.ambiguous_bar
    assert trade.alternative_exit_price == pytest.approx(110.0)
    gap = run_one(flat_panel({5: (112, 113, 111, 112)}), Rule(horizon=5, target=0.10, stop=0.04))
    assert gap.trades[0].exit_reason == "target_gap"
    assert gap.trades[0].exit_price == pytest.approx(112 * math.exp(-10 / 10_000))
    timed = run_one(flat_panel({8: (103, 103, 103, 103)}), Rule(horizon=5))
    [trade] = timed.trades
    assert trade.exit_reason == "time" and trade.exit_time.date() == START + timedelta(days=8)
    assert trade.exit_price == pytest.approx(103 * math.exp(-10 / 10_000))
    later = run_one(flat_panel({4: (100, 101, 95, 96)}), Rule(horizon=5, stop=0.04), delay=1)
    assert later.trades[0].entry_time.date() == START + timedelta(days=4)


def test_n08_labels_follow_the_trade_rule() -> None:
    days = 20
    strategy = UpperTailEscape()
    for path, expected in (
        ({4: (100, 111, 99, 108)}, (1, 2, math.log(1.10))),  # target on the 2nd day
        ({5: (95, 96, 94, 95)}, (2, 3, math.log(0.95))),  # gap below the stop on day 3
        ({3: (100, 111, 95, 100)}, (2, 1, math.log(0.96))),  # both on day 1: adverse
        ({8: (103, 103, 103, 103)}, (0, 5, math.log(1.03))),  # time exit at the open
    ):
        panel = flat_panel(path, days)
        context = build_context(panel, UniverseRules(min_age_days=0, min_liquidity=0))
        strategy.prepare(context)
        outcome, interval, realized = expected
        assert strategy.competing[2, 1] == outcome
        assert strategy.interval[2, 1] == interval
        assert strategy.realized[2, 1] == pytest.approx(realized)
    panel = flat_panel({4: (100, 106, 99, 100)}, days)
    context = build_context(panel, UniverseRules(min_age_days=0, min_liquidity=0))
    strategy.prepare(context)
    assert strategy.touch["up5"][2, 1] == 1 and strategy.touch["up10"][2, 1] == 0


def test_kaplan_meier_restricted_mean() -> None:
    durations = np.array([10.0, 20.0, 30.0])
    assert kaplan_meier_rmt(durations, np.ones(3), 100.0) == pytest.approx(20.0)
    # Capped at the horizon: events beyond it count as surviving to the horizon.
    assert kaplan_meier_rmt(np.array([10.0, 200.0]), np.ones(2), 100.0) == pytest.approx(55.0)
    # A censored observation keeps contributing until it leaves the risk set.
    value = kaplan_meier_rmt(np.array([10.0, 40.0]), np.array([1.0, 0.0]), 100.0)
    assert value == pytest.approx(10 + 0.5 * 90)


def test_competing_risk_probabilities_are_coherent() -> None:
    rng = np.random.default_rng(0)
    n = 4000
    x = rng.normal(size=(n, 1))
    up = rng.random(n) < 1 / (1 + np.exp(-(x[:, 0] - 1)))
    down = (~up) & (rng.random(n) < 0.3)
    outcome = np.where(up, 1, np.where(down, 2, 0))
    interval = np.where(outcome > 0, rng.integers(1, 6, n), 5)
    model = CompetingRisk.fit(x, outcome, interval, 5)
    assert model is not None
    probabilities = model.outcome_probabilities(np.array([[-2.0], [0.0], [2.0]]))
    assert np.allclose(probabilities.sum(axis=1), 1.0)
    assert probabilities[0, 1] < probabilities[1, 1] < probabilities[2, 1]


def test_tail_indicators_are_prior_only() -> None:
    values = np.concatenate([np.linspace(-1, 1, 300), [5.0, -5.0]])[:, None]
    low, high = tails(values)
    assert high[300, 0] == 1 and low[301, 0] == 1
    changed = values.copy()
    changed[301, 0] = 100.0  # the future never moves an earlier cut
    assert tails(changed)[1][300, 0] == high[300, 0]


def test_recovery_clock_events_and_censoring() -> None:
    total = DAY * 3
    log_close = np.zeros(total)
    anchor = DAY + HOUR - 1  # the first full-hour anchor after day one
    log_close[anchor + 3 : anchor + 10] = -0.05  # drop 15 minutes after the anchor
    log_close[anchor + 10 :] = 0.0  # back at the anchor 50 minutes after it
    scale = np.full(3, 0.02)
    events = extract(log_close, scale)
    drops = (~events.up) & (events.size == 1.0) & (events.start == minute_of(anchor + 3))
    assert drops.sum() == 1
    finish = events.finish[drops][0]
    assert finish == minute_of(anchor + 10)
    # Observed before it finished, the event is censored at the decision time; with no
    # finished event the survival curve stays at one up to the 24-hour horizon.
    now = minute_of(anchor + 5)
    assert restricted_mean(events, drops, now, drop_open=False) == pytest.approx(1440.0)
    assert restricted_mean(events, drops, now, drop_open=True) is None
    # Once observed past its finish, the event counts with its true duration.
    later = minute_of(anchor + 20)
    assert restricted_mean(events, drops, later, drop_open=False) == pytest.approx(35.0)
    single = Events(
        np.array([0.0]), np.array([True]), np.array([1.0]), np.array([np.nan]), np.array([1440.0])
    )
    assert restricted_mean(single, np.array([True]), 5000.0, False) == pytest.approx(1440.0)


def test_symbol_classification() -> None:
    entries = [
        {"symbol": "BTCUSDT", "baseAsset": "BTC"},
        {"symbol": "BTCUPUSDT", "baseAsset": "BTCUP"},
        {"symbol": "JUPUSDT", "baseAsset": "JUP"},
        {"symbol": "USDCUSDT", "baseAsset": "USDC"},
        {"symbol": "WBTCUSDT", "baseAsset": "WBTC"},
    ]
    reasons = classify(entries)
    assert reasons["BTCUSDT"] is None and reasons["JUPUSDT"] is None
    assert reasons["BTCUPUSDT"] == "leveraged token"
    assert reasons["USDCUSDT"] == "stablecoin or fiat"
    assert reasons["WBTCUSDT"] is not None
    bases = {"BTC", "SHIB", "NVDAB"}
    assert tokenized_equity("NVDAB", bases, date(2025, 6, 1))
    assert not tokenized_equity("SHIB", bases | {"SHI"}, date(2021, 5, 1))
    import polars as pl

    assert pegged(pl.DataFrame({"close": [1.0 + 0.0001 * (i % 3) for i in range(100)]}))
    assert not pegged(pl.DataFrame({"close": [1.0 + 0.05 * (i % 2) for i in range(100)]}))


def test_future_prices_never_change_earlier_decisions() -> None:
    panel = synthetic_panel()
    rules = UniverseRules(min_age_days=30, min_liquidity=0, top=20)
    fold = Fold(560, 640)

    def decisions(p: Panel) -> dict[int, list[Decision]]:
        context = build_context(p, rules)
        strategy = UpperTailEscape()
        strategy.prepare(context)
        costs = np.full(context.shape, 5.0)
        [result] = run_folds(context, strategy, [fold], 200, costs, 10.0)
        return result.decisions

    original = decisions(panel)
    mutated = Panel(**{**panel.__dict__})
    for name in ("open", "high", "low", "close"):
        array = getattr(panel, name).copy()
        array[fold.start + 1 :] *= 1.7
        setattr(mutated, name, array)
    changed = decisions(mutated)
    assert original.get(fold.start) == changed.get(fold.start)
    assert sum(len(v) for v in original.values()) > 0


def test_long_halts_split_a_reused_ticker() -> None:
    import polars as pl

    days = [date(2022, 5, d) for d in (10, 11, 12, 13)] + [date(2022, 5, 31), date(2022, 6, 1)]
    frame = pl.DataFrame({"date": days, "close": [80.0, 5.0, 0.01, 0.0003, 1.2, 1.1]})
    parts = segments(frame)
    assert [p.height for p in parts] == [4, 2]
    assert parts[1]["close"].to_list() == [1.2, 1.1]
    short = pl.DataFrame({"date": [date(2022, 1, 1), date(2022, 1, 5)], "close": [1.0, 2.0]})
    assert len(segments(short)) == 1  # a four-day pause stays one instrument
