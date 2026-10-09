"""Handbook tests 1, 2, 8 and 10 on full replays of every strategy over synthetic data."""

import math
from collections import Counter
from datetime import timedelta

import numpy as np
import pytest
from lab_helpers import DAY, bar, book, instrument, universe

from xasset.lab.bars import FlowBar
from xasset.lab.execution import BarBroker
from xasset.lab.runtime import RunSettings, Runtime
from xasset.lab.strategies import REGISTRY
from xasset.lab.universe import CostClass, Pair

SYMBOLS = ["BTC", "ETH", "A1", "A2", "A3", "A4"]
STRATEGIES = ["h01", "h02", "h09t", "h03", "h04", "h06", "h07", "h08", "h10"]


def synthetic(days: int, seed: int) -> dict[int, list[FlowBar]]:
    rng = np.random.default_rng(seed)
    prices = {s: 100.0 for s in [*SYMBOLS, "PERP"]}
    output: dict[int, list[FlowBar]] = {}
    for minute in range(days * 1440):
        end = DAY + timedelta(minutes=minute + 1)
        common = rng.normal(0, 0.0012)
        bars = []
        for symbol in prices:
            source = "ETH" if symbol == "PERP" else symbol
            beta = 1.0 if source == "BTC" else 1.3
            move = beta * common + rng.normal(0, 0.0015)
            if symbol == "PERP":
                move = math.log(prices["ETH"] / prices["PERP"]) + rng.normal(0, 0.0003)
            o = prices[symbol]
            c = o * math.exp(move)
            hi = max(o, c) * math.exp(abs(rng.normal(0, 0.0007)))
            lo = min(o, c) * math.exp(-abs(rng.normal(0, 0.0007)))
            notional = float(rng.lognormal(8, 0.6))
            buy = notional * float(np.clip(0.5 + 40 * move + rng.normal(0, 0.1), 0.02, 0.98))
            bars.append(bar(symbol, end, o, hi, lo, c, notional=notional, buy=buy))
            prices[symbol] = c
        output[minute] = bars
    return output


def make_runtime() -> Runtime:
    perp = instrument("PERP", kind="perp", shortable=True, breadth_member=False)
    uni = universe(
        SYMBOLS,
        minimum_peers=2,
        extra=[perp],
        pairs=[Pair(leader="BTC", laggards=["ETH", "A1"])],
        handoff_calendar="XNYS",
    )
    costs = CostClass(half_spread_bps=1, impact_bps=1, fee_bps=5, evidence="synthetic test costs")
    bk = book(STRATEGIES, costs={"test": costs}, calibration_sessions=2, minimum_reference=20)
    runtime = Runtime(bk, uni, [REGISTRY[s] for s in STRATEGIES], RunSettings("trade"))
    broker = BarBroker(runtime.accounting, "trade", lambda: None)
    broker.on_cancel = runtime.cancelled
    runtime.broker = broker
    return runtime


def replay(data: dict[int, list[FlowBar]]) -> Runtime:
    runtime = make_runtime()
    for minute in sorted(data):
        runtime.step(DAY + timedelta(minutes=minute + 1), data[minute])
    return runtime


@pytest.fixture(scope="module")
def baseline() -> tuple[dict[int, list[FlowBar]], Runtime]:
    data = synthetic(4, seed=11)
    return data, replay(data)


def test_strategies_produce_nontrivial_activity(baseline: tuple[dict, Runtime]) -> None:
    _, runtime = baseline
    armed = Counter(e["strategy"] for e in runtime.ledger.events if e["event"] == "armed")
    assert sum(armed.values()) > 100
    assert len(armed) >= 5
    assert runtime.ledger.orders and runtime.ledger.trades


def test_future_mutation_leaves_earlier_decisions_identical(baseline: tuple[dict, Runtime]) -> None:
    data, original = baseline
    cutoff = 3 * 1440 + 600
    rng = np.random.default_rng(99)
    mutated = dict(data)
    for minute in range(cutoff + 1, max(data) + 1):
        changed = []
        for item in data[minute]:
            scale = math.exp(rng.normal(0, 0.02))
            changed.append(
                bar(
                    item.symbol,
                    item.end,
                    item.open * scale,
                    item.high * scale * 1.01,
                    item.low * scale * 0.99,
                    item.close * scale,
                    notional=item.notional * 3,
                    buy=item.notional * 0.1,
                )
            )
        mutated[minute] = changed
    rerun = replay(mutated)
    limit = DAY + timedelta(minutes=cutoff + 1, seconds=30)

    def before(records: list[dict], key: str = "at") -> list[dict]:
        return [r for r in records if r[key] is not None and r[key] <= limit.isoformat()]

    assert before(original.ledger.events)
    assert before(rerun.ledger.events) == before(original.ledger.events)
    assert before(rerun.ledger.orders) == before(original.ledger.orders)
    fills_limit = (DAY + timedelta(minutes=cutoff + 1)).isoformat()
    assert [f for f in rerun.ledger.fills if f["at"] < fills_limit] == [
        f for f in original.ledger.fills if f["at"] < fills_limit
    ]


def test_decisions_wait_for_the_latest_arrival(baseline: tuple[dict, Runtime]) -> None:
    data, _ = baseline
    late = dict(data)
    minute = 2 * 1440 + 100
    late[minute] = [
        FlowBar(
            **{
                **{f: getattr(b, f) for f in b.__slots__},
                "available_at": b.end + timedelta(seconds=40),
            }
        )
        if b.symbol == "BTC"
        else b
        for b in data[minute]
    ]
    runtime = replay(late)
    end = DAY + timedelta(minutes=minute + 1)
    window = [
        e
        for e in runtime.ledger.events
        if end.isoformat() <= e["at"] < (end + timedelta(minutes=1)).isoformat()
    ]
    assert all(e["at"] >= (end + timedelta(seconds=40)).isoformat() for e in window)


def test_no_duplicate_orders_and_portfolio_reconciles(baseline: tuple[dict, Runtime]) -> None:
    _, runtime = baseline
    submitted = [o for o in runtime.ledger.orders if o["status"] == "submitted"]
    setups = Counter(o["setup"] for o in submitted)
    assert all(count == 1 for count in setups.values())
    # One open position per asset at any time: entries and exits alternate per symbol.
    by_symbol: dict[str, list[tuple[str, str]]] = {}
    for fill in runtime.ledger.fills:
        if fill["kind"] in {"entry", "exit"}:
            by_symbol.setdefault(fill["symbol"], []).append((fill["at"], fill["kind"]))
    for fills in by_symbol.values():
        kinds = [kind for _, kind in fills]
        assert all(a != b for a, b in zip(kinds, kinds[1:], strict=False))
    open_value = sum(runtime.portfolio.value(p) for p in runtime.portfolio.positions.values())
    realized = sum(t.net for t in runtime.ledger.trades)
    entry_costs = sum(
        p.entry_fee + (p.entry_notional if p.is_cash else 0.0) - p.funding
        for p in runtime.portfolio.positions.values()
    )
    expected_cash = runtime.book.limits.initial_nav + realized - entry_costs
    assert runtime.portfolio.cash == pytest.approx(expected_cash)
    assert runtime.portfolio.nav() == pytest.approx(expected_cash + open_value)
