import math
from datetime import datetime, timedelta
from typing import Any, ClassVar

import pytest
from lab_helpers import DAY, bar, book, instrument, universe

from xasset.lab.bars import FlowBar
from xasset.lab.execution import BarBroker
from xasset.lab.runtime import RunSettings, Runtime
from xasset.lab.strategy import Candidate, Guard, Requirements, Setup, Strategy
from xasset.lab.universe import CostClass


class Scripted(Strategy):
    """Arms at minute ``arm_at`` and confirms on the next bar with a fixed stop."""

    id = "zz"
    title = "scripted"
    kinds = ("spot", "perp")
    requires = Requirements(quotes=False, flow=False)
    plan: ClassVar[dict[str, Any]] = {}

    def targets(self) -> list[str]:
        return [self.plan["symbol"]]

    def arm(self, symbol: str) -> Setup | None:
        if self.minute == self.plan["arm_at"]:
            return self.new_setup(symbol, "ARMED")
        return None

    def step(self, setup: Setup) -> Candidate | None:
        return self.candidate(
            setup,
            stop=self.plan["stop"],
            sigma10=self.plan.get("sigma10", 0.02),
            guards=self.plan.get("guards", []),
            time_exit_minutes=self.plan.get("time_exit", 60),
        )

    @property
    def direction(self) -> int:  # type: ignore[override]
        return int(self.plan.get("direction", 1))


def make_runtime(
    plan: dict[str, Any],
    costs: CostClass | None = None,
    perp: bool = False,
    funding: dict[str, list[tuple[datetime, float]]] | None = None,
    multiplier: int = 1,
    delay: int = 0,
) -> Runtime:
    Scripted.plan = plan
    extra = [instrument("PERP", kind="perp", shortable=True, breadth_member=False)] if perp else []
    uni = universe(["BTC", "ETH"], extra=extra)
    bk = book(
        ["zz"],
        costs={
            "test": costs
            or CostClass(half_spread_bps=0, impact_bps=0, fee_bps=0, evidence="zero cost profile")
        },
    )
    runtime = Runtime(bk, uni, [Scripted], RunSettings("trade", multiplier, delay))
    # Sizing needs prior expected notional; give every minute a large constant.
    runtime.market.expected_notional = lambda symbol, i: 1e12  # type: ignore[method-assign]
    broker = BarBroker(runtime.accounting, "trade", lambda: None, funding)
    broker.on_cancel = runtime.cancelled
    runtime.broker = broker
    return runtime


def run(runtime: Runtime, path: dict[int, dict[str, tuple[float, float, float, float]]]) -> None:
    for minute in sorted(path):
        end = DAY + timedelta(minutes=minute + 1)
        bars: list[FlowBar] = [bar(symbol, end, *ohlc) for symbol, ohlc in path[minute].items()]
        runtime.step(end, bars)


def flat_path(start: int, stop: int, symbol: str, price: float) -> dict[int, dict[str, Any]]:
    return {m: {symbol: (price, price, price, price)} for m in range(start, stop)}


def test_entry_fills_next_open_and_target_uses_two_risk_units() -> None:
    rt = make_runtime({"symbol": "ETH", "arm_at": 0, "stop": 99.0})
    path = flat_path(0, 2, "ETH", 100.0)
    path[2] = {"ETH": (100.5, 100.6, 100.4, 100.5)}  # entry bar: fills at its open
    path[3] = {"ETH": (100.5, 104.0, 100.4, 102.0)}  # target (103.56) hit inside the bar
    run(rt, path)
    (trade,) = rt.ledger.trades
    d = math.log(100.5 / 99.0)
    assert trade.entry_price == pytest.approx(100.5)
    assert trade.entry_time == DAY + timedelta(minutes=2)
    assert trade.risk_distance == pytest.approx(d)
    assert trade.exit_reason == "target"
    assert trade.exit_price == pytest.approx(100.5 * math.exp(2 * d))
    # Bar replay prices the entry at the next bar's open (an approximation of the first
    # quote after the decision); the decision itself is recorded for auditing.
    assert trade.signal_time <= trade.entry_time < trade.decided_at
    assert trade.decided_at - trade.signal_time == timedelta(milliseconds=1000 + 250)


def test_same_bar_stop_and_target_take_the_adverse_stop_and_record_bound() -> None:
    rt = make_runtime({"symbol": "ETH", "arm_at": 0, "stop": 99.0})
    path = flat_path(0, 3, "ETH", 100.0)
    path[3] = {"ETH": (100.0, 105.0, 98.0, 100.0)}
    run(rt, path)
    (trade,) = rt.ledger.trades
    assert trade.exit_reason == "stop" and trade.ambiguous_bar
    assert trade.exit_price == pytest.approx(99.0)
    assert trade.alternative_exit_price is not None and trade.alternative_exit_price > 100


def test_gap_through_stop_fills_at_the_worse_open() -> None:
    rt = make_runtime({"symbol": "ETH", "arm_at": 0, "stop": 99.0})
    path = flat_path(0, 3, "ETH", 100.0)
    path[3] = {"ETH": (97.0, 97.5, 96.0, 97.0)}
    run(rt, path)
    (trade,) = rt.ledger.trades
    assert trade.exit_reason == "stop_gap" and trade.exit_price == pytest.approx(97.0)


def test_time_exit_at_open_after_holding_period() -> None:
    rt = make_runtime({"symbol": "ETH", "arm_at": 0, "stop": 99.0, "time_exit": 5})
    run(rt, flat_path(0, 12, "ETH", 100.0))
    (trade,) = rt.ledger.trades
    assert trade.exit_reason == "time"
    assert trade.exit_time - trade.entry_time == timedelta(minutes=5)


def test_costs_move_fills_adversely_and_double_under_stress() -> None:
    costs = CostClass(half_spread_bps=2, impact_bps=3, fee_bps=10, evidence="test profile")
    results = []
    for multiplier in (1, 2):
        rt = make_runtime(
            {"symbol": "ETH", "arm_at": 0, "stop": 98.0}, costs, multiplier=multiplier
        )
        run(rt, flat_path(0, 70, "ETH", 100.0))
        (trade,) = rt.ledger.trades
        side = multiplier * 5 / 10_000
        assert trade.entry_price == pytest.approx(100 * math.exp(side))
        assert trade.exit_price == pytest.approx(100 * math.exp(-side))
        entry_fee = multiplier * trade.entry_notional * 10 / 10_000
        exit_fee = multiplier * trade.qty * trade.exit_price * 10 / 10_000
        assert trade.fees == pytest.approx(entry_fee + exit_fee)
        results.append(trade.net)
    assert results[1] < results[0] < 0


def test_stop_distance_filter_rejects_with_reason() -> None:
    rt = make_runtime({"symbol": "ETH", "arm_at": 0, "stop": 99.99, "sigma10": 0.02})
    run(rt, flat_path(0, 3, "ETH", 100.0))
    rejected = [o for o in rt.ledger.orders if o["status"] == "rejected"]
    assert rejected and "sigma10" in rejected[0]["reason"]
    assert not rt.ledger.trades


def test_guard_cancels_when_invalidated_before_entry() -> None:
    plan = {"symbol": "ETH", "arm_at": 0, "stop": 98.0, "guards": [Guard("BTC", "below", 50.0)]}
    rt = make_runtime(plan)
    path = {m: {"ETH": (100.0,) * 4, "BTC": (60.0,) * 4} for m in range(0, 2)}
    path[2] = {"ETH": (100.0,) * 4, "BTC": (49.0, 49.0, 49.0, 49.0)}  # benchmark opens below KM
    run(rt, path)
    cancelled = [o for o in rt.ledger.orders if o["status"] == "cancelled"]
    assert cancelled and "BTC" in cancelled[0]["reason"]
    assert not rt.ledger.trades
    assert any(e["event"] == "cancelled" for e in rt.ledger.events)


def test_entry_cancelled_when_asset_opens_through_its_stop() -> None:
    rt = make_runtime({"symbol": "ETH", "arm_at": 0, "stop": 99.0})
    path = flat_path(0, 2, "ETH", 100.0)
    path[2] = {"ETH": (98.0, 98.0, 98.0, 98.0)}
    run(rt, path)
    assert not rt.ledger.trades
    assert any(o["status"] == "cancelled" for o in rt.ledger.orders)


def test_missing_entry_bar_cancels_instead_of_waiting() -> None:
    rt = make_runtime({"symbol": "ETH", "arm_at": 0, "stop": 99.0})
    path = flat_path(0, 2, "ETH", 100.0)
    path[2] = {"BTC": (60.0,) * 4}
    path.update(flat_path(3, 5, "ETH", 100.0))
    run(rt, path)
    assert not rt.ledger.trades
    assert [o["reason"] for o in rt.ledger.orders if o["status"] == "cancelled"]


def test_delay_stress_enters_one_bar_later() -> None:
    rt = make_runtime({"symbol": "ETH", "arm_at": 0, "stop": 98.0}, delay=1)
    run(rt, flat_path(0, 70, "ETH", 100.0))
    (trade,) = rt.ledger.trades
    assert trade.entry_time == DAY + timedelta(minutes=3)


def test_short_perpetual_accounting_and_funding_sign() -> None:
    funding_time = DAY + timedelta(minutes=4)
    plan = {"symbol": "PERP", "arm_at": 0, "stop": 102.0, "direction": -1}
    rt = make_runtime(plan, perp=True, funding={"PERP": [(funding_time, 0.001)]})
    path = flat_path(0, 70, "PERP", 100.0)
    run(rt, path)
    (trade,) = rt.ledger.trades
    assert trade.direction == -1 and trade.exit_reason == "time"
    # Positive funding: shorts receive qty * mark * rate.
    assert trade.funding == pytest.approx(trade.qty * 100.0 * 0.001)
    assert trade.net == pytest.approx(trade.gross - trade.fees + trade.funding)
    nav = rt.portfolio.nav()
    assert nav == pytest.approx(rt.book.limits.initial_nav + trade.net)


def test_wrong_side_fill_exits_at_next_open() -> None:
    rt = make_runtime({"symbol": "ETH", "arm_at": 0, "stop": 99.0})
    path = flat_path(0, 2, "ETH", 100.0)
    path[2] = {"ETH": (99.0, 99.5, 99.0, 99.2)}  # opens exactly at the stop: d = 0
    path[3] = {"ETH": (99.3, 99.4, 99.2, 99.3)}
    run(rt, path)
    (trade,) = rt.ledger.trades
    assert trade.risk_distance <= 0 and trade.target is None
    assert trade.exit_reason == "wrong-side stop at fill"
    assert trade.exit_time == DAY + timedelta(minutes=3)
    assert trade.exit_price == pytest.approx(99.3)


def test_nav_conserves_cash_and_positions_through_round_trips() -> None:
    costs = CostClass(half_spread_bps=1, impact_bps=1, fee_bps=7, evidence="test profile")
    rt = make_runtime({"symbol": "ETH", "arm_at": 0, "stop": 98.0}, costs)
    path = flat_path(0, 3, "ETH", 100.0)
    for m in range(3, 70):
        price = 100 + 0.01 * m
        path[m] = {"ETH": (price, price, price, price)}
    run(rt, path)
    total = sum(t.net for t in rt.ledger.trades)
    assert rt.portfolio.nav() == pytest.approx(rt.book.limits.initial_nav + total)
    assert not rt.portfolio.positions


def test_single_position_per_asset_and_limits() -> None:
    rt = make_runtime({"symbol": "ETH", "arm_at": 0, "stop": 98.0})
    portfolio = rt.portfolio
    portfolio.marks["ETH"] = 100.0
    assert portfolio.check("ETH", 0.0, 100.0, 98.0) == "size rounds to zero"
    assert portfolio.check("ETH", 1e9, 100.0, 98.0) == "aggregate gross exposure limit"
    assert portfolio.check("ETH", 1.0, 100.0, 98.0, [("ETH", 1.0, 100.0, 98.0)]) == (
        "one pending order per asset"
    )
    size = portfolio.size(100.0, 98.0, 1e12, 0.001)
    # Risk cap: 0.1% of NAV over a 2.0 stop distance = 50 units; notional cap = 100 units.
    assert size == pytest.approx(50.0)
    assert portfolio.size(100.0, 98.0, 100_000.0, 0.001) == pytest.approx(10.0)  # 1% participation


def test_session_flatten_for_calendar_books() -> None:
    plan = {"symbol": "ETH", "arm_at": 0, "stop": 98.0}
    rt = make_runtime(plan)
    close = DAY + timedelta(minutes=10)

    def bounds() -> tuple[datetime, datetime]:
        return DAY, close

    assert isinstance(rt.broker, BarBroker)
    rt.broker.session_bounds = bounds
    run(rt, flat_path(0, 10, "ETH", 100.0))
    (trade,) = rt.ledger.trades
    assert trade.exit_reason == "session_flatten"
    assert trade.exit_time == close - timedelta(minutes=1)
