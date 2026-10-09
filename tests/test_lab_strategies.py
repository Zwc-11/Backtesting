"""Synthetic state paths per handbook strategy: valid, expired and boundary cases."""

from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from lab_helpers import DAY, Harness, bar, flat, universe

from xasset.lab.market import LagModel
from xasset.lab.strategies import h09_selling_bursts
from xasset.lab.strategies.h01_resilience import Resilience
from xasset.lab.strategies.h02_buying_bursts import BuyingBursts
from xasset.lab.strategies.h03_pullbacks import ImprovingPullbacks
from xasset.lab.strategies.h04_failed_recovery import FailedRecovery
from xasset.lab.strategies.h05_residual_breadth import ResidualBreadth
from xasset.lab.strategies.h06_resistance import LessBuyingAtResistance
from xasset.lab.strategies.h07_rising_center import RisingCenter
from xasset.lab.strategies.h08_catch_up import CatchUp
from xasset.lab.strategies.h09_selling_bursts import SellingBursts, SellingBurstsTradeBars
from xasset.lab.strategies.h10_handoff import ThinSessionHandoff
from xasset.lab.universe import Pair

# ---------------------------------------------------------------------------- h01


def h01_harness(km_break: int | None = None, confirm_close: float = 100.2) -> Harness:
    uni = universe(["BTC", "ETH", "P1", "P2", "P3"], minimum_peers=2)
    h = Harness(Resilience, uni)
    h.patch(
        quantile=lambda s, n, i, p: -0.005,
        sigma=lambda s, hz, i, price: 0.01,
        scale=lambda s, n, i: 0.01,
        residual_sum=lambda s, i, hz: 0.02,
    )
    for m in range(0, 40):
        end = h.end(m)
        btc = 100.0 - 0.1 * min(m, 10)  # falls 1% over minutes 0..10, then flat at 99
        btc_low = btc
        if km_break is not None and m == km_break:
            btc_low = 98.9
        eth = 100.0
        eth_bar = flat("ETH", end, eth)
        if m == 15:
            eth_bar = bar("ETH", end, 100.0, confirm_close, 100.0, confirm_close)
        bars = [bar("BTC", end, btc, btc, btc_low, btc), eth_bar]
        bars += [flat(p, end, 50.0 - 0.05 * min(m, 10)) for p in ("P1", "P2", "P3")]
        h.minute(m, bars)
    return h


def test_h01_valid_path_arms_stabilizes_and_confirms() -> None:
    h = h01_harness()
    armed = h.events("armed")[0]
    assert armed["detail"]["KM"] == pytest.approx(99.0)
    assert armed["detail"]["K0"] == pytest.approx(100.0)
    stabilized = h.events("stabilized")
    assert stabilized and h.candidates
    candidate = h.candidates[0]
    assert candidate.signal_end == h.end(15)
    assert candidate.stop == pytest.approx(100.0 - 0.01)
    assert candidate.guards[0].symbol == "BTC" and candidate.guards[0].level == pytest.approx(99.0)


def test_h01_market_print_below_km_expires() -> None:
    h = h01_harness(km_break=12)
    assert not h.candidates
    assert any("KM" in e["detail"]["reason"] for e in h.events("expired"))


def test_h01_boundary_close_equal_to_prior_high_does_not_confirm() -> None:
    h = h01_harness(confirm_close=100.0)  # equals the prior five-minute high: not above
    assert not h.candidates


# ---------------------------------------------------------------------------- h02


def burst_bar(
    symbol: str, end: datetime, o: float, c: float, lo: float | None = None, buy: float = 800.0
) -> Any:
    return bar(
        symbol, end, o, max(o, c), lo if lo is not None else min(o, c), c, notional=1000.0, buy=buy
    )


def h02_harness(pause_low: float = 100.30) -> Harness:
    h = Harness(BuyingBursts, universe(["BTC", "ETH"]))
    h.patch(quantile=lambda s, n, i, p: 50.0, sigma=lambda s, hz, i, price: 0.002)
    path = {
        0: (100.0, 100.2, None, 800),
        1: (100.2, 100.4, None, 800),  # burst 1
        2: (100.4, 100.35, pause_low, 500),
        3: (100.35, 100.32, None, 500),  # pause
        4: (100.32, 100.5, None, 800),
        5: (100.5, 100.65, None, 800),  # burst 2
        6: (100.65, 100.6, 100.57, 500),
        7: (100.6, 100.6, None, 500),  # pause
        8: (100.6, 100.7, None, 800),
        9: (100.7, 100.82, None, 800),  # burst 3
    }
    for m, (o, c, lo, buy) in path.items():
        end = h.end(m)
        h.minute(m, [flat("BTC", end, 50.0), burst_bar("ETH", end, o, c, lo, buy)])
    return h


def test_h02_three_bursts_with_retention_confirm() -> None:
    h = h02_harness()
    assert [e["event"] for e in h.events() if e["event"] != "burst_ignored"][:2] == [
        "armed",
        "burst",
    ]
    (candidate,) = h.candidates
    assert candidate.signal_end == h.end(9)
    assert candidate.stop == pytest.approx(100.57 - 0.01)


def test_h02_failed_retention_expires() -> None:
    h = h02_harness(pause_low=100.25)  # retention 0.625 < 0.70
    assert not h.candidates
    assert "retention" in h.events("expired")[0]["detail"]["reason"]


def test_h02_consecutive_burst_blocks_are_not_separate_bursts() -> None:
    h = Harness(BuyingBursts, universe(["BTC", "ETH"]))
    h.patch(quantile=lambda s, n, i, p: 50.0, sigma=lambda s, hz, i, price: 0.002)
    prices = [100.0, 100.2, 100.4, 100.6, 100.8, 101.0, 101.2]
    for m in range(6):
        end = h.end(m)
        h.minute(m, [flat("BTC", end, 50.0), burst_bar("ETH", end, prices[m], prices[m + 1])])
    assert len(h.events("burst_ignored")) == 2
    assert not h.events("burst") and not h.candidates


# ---------------------------------------------------------------------------- h09


def selling_harness(cls: type, monkeypatch: pytest.MonkeyPatch, d3_low: float = 99.9) -> Harness:
    monkeypatch.setattr(h09_selling_bursts, "local_volatility", lambda tape, start, count=60: 0.001)
    h = Harness(cls, universe(["BTC", "ETH"]))
    h.patch(quantile=lambda s, n, i, p: -50.0, sigma=lambda s, hz, i, price: 0.002)
    path: dict[int, tuple[float, float, float, float]] = {}  # m -> (o, c, low, buy)
    for m in range(0, 70):
        path[m] = (100.0, 100.0, 100.0, 500)
    # Burst 1 [70,71]: A=100, low 99.4 (D1 0.0060); recovered at the block ending 77 (T1=6).
    path.update(
        {
            70: (100.0, 99.6, 99.4, 100),
            71: (99.6, 99.5, 99.45, 100),
            72: (99.5, 99.6, 99.5, 500),
            73: (99.6, 99.7, 99.6, 500),
            74: (99.7, 99.8, 99.7, 500),
            75: (99.8, 99.9, 99.8, 500),
            76: (99.9, 99.95, 99.9, 500),
            77: (99.95, 100.0, 99.95, 500),
            78: (100.0, 100.0, 100.0, 500),
            79: (100.0, 100.0, 100.0, 500),
        }
    )
    # Burst 2 [80,81]: A=100, low 99.6 (D2 0.0040); recovered at 85 (T2=4).
    path.update(
        {
            80: (100.0, 99.7, 99.6, 100),
            81: (99.7, 99.7, 99.65, 100),
            82: (99.7, 99.85, 99.7, 500),
            83: (99.85, 99.9, 99.85, 500),
            84: (99.9, 99.95, 99.9, 500),
            85: (99.95, 100.0, 99.95, 500),
            86: (100.0, 100.0, 100.0, 500),
            87: (100.0, 100.0, 100.0, 500),
        }
    )
    # Burst 3 [88,89]: A=100, low d3_low (D3 0.0010); recovered at 91 (T3=2).
    path.update(
        {
            88: (100.0, 99.9, d3_low, 120),
            89: (99.9, 99.92, 99.9, 100),
            90: (99.92, 99.97, 99.92, 500),
            91: (99.97, 100.05, 99.97, 500),
        }
    )
    for m in sorted(path):
        o, c, lo, buy = path[m]
        end = h.end(m)
        eth = bar("ETH", end, o, max(o, c), lo, c, notional=1000.0, buy=buy)
        h.minute(m, [flat("BTC", end, 50.0), eth])
    return h


def test_h09_trade_bar_variant_confirms_shrinking_damage(monkeypatch: pytest.MonkeyPatch) -> None:
    h = selling_harness(SellingBurstsTradeBars, monkeypatch)
    recovered = [e["detail"]["T"] for e in h.events("recovered")]
    assert recovered == [6, 4, 2]
    (candidate,) = h.candidates
    assert candidate.signal_end == h.end(91)
    assert candidate.stop == pytest.approx(99.9 - 0.01)


def test_h09_primary_requires_quoted_spreads(monkeypatch: pytest.MonkeyPatch) -> None:
    h = selling_harness(SellingBursts, monkeypatch)
    assert not h.candidates
    assert any("spread" in e["detail"]["reason"] for e in h.events("expired"))


def test_h09_non_shrinking_damage_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    h = selling_harness(SellingBurstsTradeBars, monkeypatch, d3_low=99.5)  # D3 > D2
    assert not h.candidates
    assert any("damage" in e["detail"]["reason"] for e in h.events("expired"))


# ---------------------------------------------------------------------------- h03


def test_h03_two_improving_pullbacks_confirm() -> None:
    uni = universe(["BTC", "ETH", "P1", "P2"], minimum_peers=2)
    h = Harness(ImprovingPullbacks, uni)
    h.patch(sigma=lambda s, hz, i, price: 0.004, expected_notional=lambda s, i: 1000.0)
    eth: dict[int, tuple[float, float, float, float]] = {
        m: (100.0, 100.0, 100.0, 500) for m in range(60)
    }
    eth.update(
        {
            60: (100.0, 100.5, 100.0, 500),
            61: (100.5, 100.6, 100.5, 500),
            62: (100.6, 100.3, 100.25, 400),
            63: (100.3, 100.4, 100.3, 400),
            64: (100.4, 100.58, 100.4, 400),
            65: (100.58, 100.7, 100.58, 500),
            66: (100.7, 100.45, 100.45, 500),
            67: (100.45, 100.75, 100.45, 500),
        }
    )
    for m in sorted(eth):
        o, c, lo, buy = eth[m]
        end = h.end(m)
        btc = 100.0 + 0.01 * m
        bars = [
            flat("BTC", end, btc),
            bar("ETH", end, o, max(o, c), lo, c, notional=1000.0, buy=buy),
        ]
        bars += [flat(p, end, 10.0 + 0.001 * m) for p in ("P1", "P2")]
        h.minute(m, bars)
    pullbacks = h.events("recovered")
    assert pullbacks and pullbacks[0]["detail"]["T"] == 3
    (candidate,) = h.candidates
    assert candidate.signal_end == h.end(67)
    assert candidate.stop == pytest.approx(100.45 - 0.01)


# ---------------------------------------------------------------------------- h04


def test_h04_absorption_then_breakdown_confirms_short() -> None:
    perp = (
        universe(["BTC", "XX", "YY"])
        .instruments[0]
        .model_copy(
            update={"id": "PERP", "kind": "perp", "shortable": True, "breadth_member": False}
        )
    )
    h = Harness(FailedRecovery, universe(["BTC", "XX"], extra=[perp]))
    h.patch(
        sigma=lambda s, hz, i, price: 0.005,
        quantile=lambda s, n, i, p: {"N10": 5000.0, "B10": 3000.0}[n],
    )
    for m in range(0, 160):
        end = h.end(m)
        if m < 121:
            # Profile window: heavy notional at 103.1, light elsewhere in a 95..105 range.
            price = 103.1 if m % 2 == 0 else (95.0 if m % 4 == 1 else 105.0)
            notional = 5000.0 if price == 103.1 else 200.0
            p = bar("PERP", end, price, price, price, price, notional=notional, buy=notional / 2)
        elif m < 140:
            p = bar("PERP", end, 105.0, 105.0, 104.9, 105.0, notional=500.0, buy=250.0)
        elif m == 140:
            p = bar("PERP", end, 105.0, 105.0, 100.0, 100.0, notional=6000.0, buy=1000.0)
        elif m <= 145:
            price = [101.0, 102.0, 102.5, 102.9, 103.1][m - 141]
            p = bar("PERP", end, price, price, price, price, notional=1000.0, buy=500.0)
        elif m <= 155:
            p = bar("PERP", end, 103.1, 103.13, 103.09, 103.1, notional=1000.0, buy=800.0)
        else:
            p = bar("PERP", end, 103.1, 103.1, 103.0, 103.0, notional=1000.0, buy=300.0)
        h.minute(m, [flat("BTC", end, 50.0), flat("XX", end, 10.0), p])
    armed = h.events("armed")[0]["detail"]
    assert armed["A"] == pytest.approx(103.08) and armed["B"] == pytest.approx(103.14)
    assert [e["event"] for e in h.events() if e["event"] in {"at_area", "absorbed"}] == [
        "at_area",
        "absorbed",
    ]
    (candidate,) = h.candidates
    assert candidate.direction == -1
    assert candidate.signal_end == h.end(156)
    assert candidate.stop == pytest.approx(103.13 + 0.01)


# ---------------------------------------------------------------------------- h06


def test_h06_three_weaker_visits_then_breakout() -> None:
    h = Harness(LessBuyingAtResistance, universe(["BTC", "ETH"]))
    h.patch(sigma=lambda s, hz, i, price: 0.004, expected_notional=lambda s, i: 1000.0)
    path: dict[int, tuple[float, float, float, float]] = {}
    for m in range(60):
        path[m] = (99.5, 100.0 if m == 30 else 99.5, 99.5, 500)
    path.update(
        {
            56: (99.5, 99.9, 99.5, 800),
            57: (99.9, 99.9, 99.9, 800),
            58: (99.9, 99.9, 99.9, 800),
            59: (99.9, 99.9, 99.9, 800),
            60: (99.9, 99.98, 99.9, 800),  # visit 1, A1 = 0.8
            61: (99.98, 99.88, 99.85, 600),
            62: (99.88, 99.9, 99.88, 600),
            63: (99.9, 99.9, 99.9, 600),
            64: (99.9, 99.92, 99.9, 600),
            65: (99.92, 99.97, 99.92, 600),  # visit 2, A2 = 0.6
            66: (99.97, 99.9, 99.88, 400),
            67: (99.9, 99.9, 99.9, 400),
            68: (99.9, 99.9, 99.9, 400),
            69: (99.9, 99.92, 99.9, 400),
            70: (99.92, 99.97, 99.92, 400),  # visit 3, A3 = 0.4
            71: (99.97, 99.98, 99.95, 500),
            72: (99.98, 100.05, 99.98, 500),
        }
    )
    for m in sorted(path):
        o, c, lo, buy = path[m]
        end = h.end(m)
        hi = 100.0 if m == 30 else max(o, c)
        h.minute(
            m, [flat("BTC", end, 50.0), bar("ETH", end, o, hi, lo, c, notional=1000.0, buy=buy)]
        )
    visits = [e["detail"]["A"] for e in h.events("visit")]
    assert visits == pytest.approx([0.6, 0.4])
    (candidate,) = h.candidates
    assert candidate.signal_end == h.end(72)
    assert candidate.stop == pytest.approx(99.88 - 0.01)


# ---------------------------------------------------------------------------- h07


def test_h07_rising_center_narrowing_range_confirms() -> None:
    h = Harness(RisingCenter, universe(["BTC", "ETH"]))
    h.patch(
        sigma=lambda s, hz, i, price: 0.002,
        quantile=lambda s, n, i, p: {"N10": 5000.0, "RANGE1": 0.001}[n],
    )
    for m in range(32):
        end = h.end(m)
        if m < 10:
            c = 100.0 + (0.4 if m % 2 else -0.4)
            hi, lo = (100.4, 99.6)
        elif m < 20:
            c = 100.2 + (0.3 if m % 2 else -0.3)
            hi, lo = (100.5, 99.9)
        elif m < 30:
            c = 100.45 + (0.1 if m % 2 else -0.1)
            hi, lo = (100.55, 100.15)
        else:
            c, hi, lo = (100.6, 100.65, 100.4)
        eth = bar("ETH", end, c, hi, lo, c, notional=1000.0)
        h.minute(m, [flat("BTC", end, 50.0), eth])
    armed = h.events("armed")[0]["detail"]
    assert armed["U"] == pytest.approx(100.55) and armed["K0"] == pytest.approx(100.15)
    (candidate,) = h.candidates
    assert candidate.signal_end == h.end(30)
    assert candidate.stop == pytest.approx(100.14)


# ---------------------------------------------------------------------------- h08


def test_h08_laggard_catches_up_after_leader_shock() -> None:
    uni = universe(["BTC", "ETH"], pairs=[Pair(leader="BTC", laggards=["ETH"])])
    h = Harness(CatchUp, uni)
    h.patch(
        quantile=lambda s, n, i, p: 0.002,
        scale=lambda s, n, i: 0.001,
        sigma=lambda s, hz, i, price: 0.01,
    )
    h.market._fit_models = lambda: None  # type: ignore[method-assign]
    h.market.lag = {("BTC", "ETH"): LagModel("BTC", "ETH", 0.0, (1.0, 0.0, 0.0, 0.0), True, 500)}
    for m in range(16):
        end = h.end(m)
        btc = 100.5 if m >= 10 else 100.0
        eth = 100.2 if m >= 12 else 100.0
        h.minute(m, [flat("BTC", end, btc), flat("ETH", end, eth)])
    assert h.events("armed")[0]["detail"]["leader"] == "BTC"
    (candidate,) = h.candidates
    assert candidate.signal_end == h.end(12)
    assert candidate.stop == pytest.approx(99.99)
    assert candidate.time_exit_minutes == 20


def test_h08_unstable_relationship_never_arms() -> None:
    uni = universe(["BTC", "ETH"], pairs=[Pair(leader="BTC", laggards=["ETH"])])
    h = Harness(CatchUp, uni)
    h.market._fit_models = lambda: None  # type: ignore[method-assign]
    h.market.lag = {("BTC", "ETH"): LagModel("BTC", "ETH", 0.0, (1.0, 0.0, 0.0, 0.0), False, 500)}
    for m in range(16):
        end = h.end(m)
        h.minute(m, [flat("BTC", end, 100.5 if m >= 10 else 100.0), flat("ETH", end, 100.0)])
    assert not h.events("armed")


# ---------------------------------------------------------------------------- h05


def test_h05_residual_breadth_rise_then_index_breakout() -> None:
    uni = universe(
        ["BTC", "IDX", "A", "B", "C"],
        index_constituents={"IDX": ["A", "B", "C"]},
    )
    h = Harness(ResidualBreadth, uni)
    h.patch(
        sigma=lambda s, hz, i, price: 0.01,
        residual_sum=lambda s, i, hz: 0.01 if i >= 20 else -0.01,
    )
    for m in range(25):
        end = h.end(m)
        idx = 100.1 if m == 22 else 100.0
        bars = [flat("BTC", end, 50.0), flat("IDX", end, idx)]
        bars += [flat(s, end, 10.0) for s in ("A", "B", "C")]
        h.minute(m, bars)
    armed = h.events("armed")[0]["detail"]
    assert armed["RB"] == 1.0 and armed["RB_prior"] == 0.0
    (candidate,) = h.candidates
    assert candidate.signal_end == h.end(22) and candidate.time_exit_minutes == 30


# ---------------------------------------------------------------------------- h10


def test_h10_thin_break_survives_handoff_and_pullback() -> None:
    uni = universe(["BTC", "ETH"], handoff_calendar="XNYS")
    h = Harness(ThinSessionHandoff, uni)
    h.patch(sigma=lambda s, hz, i, price: 0.004)
    strategy = h.strategy
    assert isinstance(strategy, ThinSessionHandoff)
    for stat in strategy.thin.values():
        stat.values = deque([50_000.0] * 5, maxlen=5)
    for stat in strategy.opening.values():
        stat.values = deque([10_000.0] * 5, maxlen=5)
    monday = datetime(2026, 3, 9, tzinfo=UTC)  # NYSE opens 13:30 UTC (minute 810)
    o = 810
    for m in range(o - 125, o + 20):
        end = monday + timedelta(minutes=m + 1)
        assert h.market.advance(end)
        if m < o - 25:
            eth = bar("ETH", end, 100.0, 100.1, 99.9, 100.0, notional=100.0)
        elif m < o:
            eth = bar("ETH", end, 100.2, 100.25, 100.15, 100.2, notional=100.0)  # thin breakout
        elif m < o + 5:
            eth = bar("ETH", end, 100.2, 100.3, 100.15, 100.25, notional=5000.0)
        elif m == o + 6:
            eth = bar("ETH", end, 100.2, 100.22, 100.12, 100.15, notional=3000.0)  # pullback bar
        elif m == o + 7:
            eth = bar("ETH", end, 100.15, 100.21, 100.15, 100.2, notional=3000.0)  # not above
        elif m == o + 8:
            eth = bar("ETH", end, 100.2, 100.3, 100.2, 100.28, notional=3000.0)  # confirm
        else:
            # Above the pullback band (U = 100.1, band [100.0, 100.2]) and above U.
            eth = bar("ETH", end, 100.3, 100.35, 100.25, 100.3, notional=3000.0)
        h.market.add(eth)
        h.market.add(flat("BTC", end, 50.0))
        h.candidates.extend(strategy.evaluate(end + timedelta(seconds=2)))
    events = [e["event"] for e in h.events()]
    assert events[:4] == ["armed", "handoff", "pullback", "confirmed"]
    (candidate,) = h.candidates
    assert candidate.signal_end == monday + timedelta(minutes=o + 9)
    assert candidate.stop == pytest.approx(100.12 - 0.01)
    assert DAY.tzinfo is not None
