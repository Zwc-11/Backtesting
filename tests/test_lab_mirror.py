"""Mirror strategies, the compact replay ledger, membership and the fast tape."""

from __future__ import annotations

import math
import random
from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest
from lab_helpers import DAY, bar, book, flat, instrument, universe

from xasset.lab.bars import FlowBar
from xasset.lab.ledger import CompactLedger, Ledger
from xasset.lab.market import Market, Tape
from xasset.lab.mirror import mirror, mirror_bar, real_candidate
from xasset.lab.strategies import REGISTRY
from xasset.lab.strategies.h01_resilience import Resilience
from xasset.lab.strategies.h04_failed_recovery import FailedRecovery

SCALE = 10_000.0  # real prices are SCALE / P so they sit near 100 like the h01 path


def h01_bars(m: int, end: object) -> list[FlowBar]:
    """The valid h01 path from test_lab_strategies (benchmark falls, ETH holds, breaks)."""
    btc = 100.0 - 0.1 * min(m, 10)
    eth_bar = flat("ETH", end, 100.0)  # type: ignore[arg-type]
    if m == 15:
        eth_bar = bar("ETH", end, 100.0, 100.2, 100.0, 100.2)  # type: ignore[arg-type]
    bars = [bar("BTC", end, btc, btc, btc, btc), eth_bar]  # type: ignore[arg-type]
    bars += [flat(p, end, 50.0 - 0.05 * min(m, 10)) for p in ("P1", "P2", "P3")]  # type: ignore[arg-type]
    return bars


def inverted(item: FlowBar) -> FlowBar:
    """The real bar whose mirror is ``item`` scaled: real price = SCALE / P."""
    flipped = mirror_bar(item)
    return replace(
        flipped,
        open=SCALE * flipped.open,  # type: ignore[operator]
        high=SCALE * flipped.high,  # type: ignore[operator]
        low=SCALE * flipped.low,  # type: ignore[operator]
        close=SCALE * flipped.close,  # type: ignore[operator]
        volume=item.volume,
    )


def test_mirror_bar_inverts_prices_swaps_flow_and_inverts_vwap() -> None:
    end = DAY + timedelta(minutes=1)
    original = bar("X", end, 100.0, 102.0, 99.0, 101.0, notional=5000.0, buy=3000.0)
    flipped = mirror_bar(original)
    assert flipped.high == pytest.approx(1 / 99.0) and flipped.low == pytest.approx(1 / 102.0)
    assert flipped.close == pytest.approx(1 / 101.0)
    assert flipped.buy_notional == 2000.0 and flipped.sell_notional == 3000.0
    assert flipped.notional == original.notional
    assert flipped.notional / flipped.volume == pytest.approx(original.volume / original.notional)
    twice = mirror_bar(flipped)
    assert twice.close == pytest.approx(original.close) and twice.high == pytest.approx(102.0)
    assert twice.buy_notional == original.buy_notional
    assert twice.volume == pytest.approx(original.volume)


def test_mirror_of_h01_shorts_weakness_during_market_strength() -> None:
    """Real path = SCALE / (h01 path): the market rallies, ETH lags, then breaks down."""
    perps = [instrument(s, kind="perp", shortable=True) for s in ("BTC", "ETH", "P1", "P2", "P3")]
    uni = universe([], minimum_peers=2, extra=perps)  # shorts need shortable perpetuals
    bk = book(["h01m"])
    ledger = Ledger()
    market = Market(uni, bk, "trade", mirrored=True)
    cls = REGISTRY["h01m"]
    strategy = cls(market, ledger, bk, uni)
    for name, value in {
        "quantile": lambda s, n, i, p: -0.005,
        "sigma": lambda s, hz, i, price: 0.01,
        "scale": lambda s, n, i: 0.01,
        "residual_sum": lambda s, i, hz: 0.02,
    }.items():
        setattr(market, name, value)
    candidates = []
    for m in range(40):
        end = DAY + timedelta(minutes=m + 1)
        real = [inverted(b) for b in h01_bars(m, end)]
        assert market.advance(end)
        for item in real:
            market.add(mirror_bar(item))  # what the runtime feeds the mirrored market
        candidates += strategy.evaluate(end + timedelta(seconds=2))
    assert len(candidates) == 1
    found = candidates[0]
    assert found.signal_end == DAY + timedelta(minutes=16)
    order = real_candidate(found)
    assert order.direction == -1
    # Real stress-window high is SCALE/100 = 100; the stop sits one real tick above it.
    assert order.stop == pytest.approx(100.0 + 0.01, rel=1e-6)
    guard = order.guards[0]
    assert guard.symbol == "BTC" and guard.side == "above"
    assert guard.level == pytest.approx(SCALE / 99.0)


def test_mirror_registry_ids_sides_and_kinds() -> None:
    short = REGISTRY["h01m"]
    assert short.real_direction == -1 and short.kinds == ("perp",)  # type: ignore[attr-defined]
    long = mirror(FailedRecovery)
    assert long.real_direction == 1 and long.id == "h04m"  # type: ignore[attr-defined]
    assert issubclass(short, Resilience)


def test_compact_ledger_counts_match_full_ledger_and_keeps_confirmed_timelines() -> None:
    full, compact = Ledger(), CompactLedger(samples=2)
    at = DAY
    sequence = []
    for n in range(50):
        setup = f"s{n}"
        sequence.append((setup, "armed", {}))
        if n % 5 == 0:
            sequence += [
                (setup, "confirmed", {}),
                (setup, "filled", {}),
                (setup, "exited", {"reason": "stop"}),
            ]
        else:
            sequence.append((setup, "expired", {"reason": "deadline" if n % 2 else "invalid"}))
    for setup, event, detail in sequence:
        full.event(at, "h01", "ETH", setup, event, "X", **detail)
        compact.event(at, "h01", "ETH", setup, event, "X", **detail)
    assert compact.event_counts("h01") == full.event_counts("h01")
    assert compact.expiry_reasons("h01") == full.expiry_reasons("h01")
    assert set(compact.timelines) == {f"s{n}" for n in range(0, 50, 5)}
    assert [e["event"] for e in compact.timelines["s0"]] == [
        "armed",
        "confirmed",
        "filled",
        "exited",
    ]
    assert all(len(v) <= 2 for v in compact.samples.values())
    assert not compact.alive and not compact.events


def test_membership_limits_targets_breadth_and_loading() -> None:
    uni = universe(
        ["BTC", "ETH", "P1", "P2", "P3"],
        minimum_peers=1,
        membership={"2026-03": ["BTC", "ETH", "P1"], "2026-04": ["BTC", "P2", "P3"]},
    )
    assert uni.members("2026-03") == frozenset({"BTC", "ETH", "P1"})
    assert uni.members("2026-05") == frozenset()
    assert uni.loaded("2026-03") == frozenset({"BTC", "ETH", "P1", "P2", "P3"})
    market = Market(uni, book(["h01"]), "trade")
    strategy = Resilience(market, Ledger(), book(["h01"]), uni)
    end = DAY + timedelta(minutes=1)  # 2026-03-02
    assert market.advance(end)
    assert strategy.targets() == ["ETH", "P1"]
    for m in range(12):
        end = DAY + timedelta(minutes=m + 1)
        market.advance(end)
        for s in ("BTC", "ETH", "P1", "P2", "P3"):
            market.add(flat(s, end, 100.0 - (m if s != "P1" else -m)))
    share, count = market.breadth("ETH", 11, 10, 1)  # type: ignore[misc]
    assert count == 1 and share == 1.0  # only P1 counts: P2 and P3 are not members
    with pytest.raises(ValueError):
        universe(["BTC", "ETH"], membership={"2026-3": ["BTC"]})
    with pytest.raises(ValueError):
        universe(["BTC", "ETH"], membership={"2026-03": ["ETH"]})  # benchmark missing


class NaiveTape:
    """Reference implementation of the scalar accessors (numpy windows)."""

    def __init__(self, width: int):
        self.h = np.full(width, np.nan)
        self.l = np.full(width, np.nan)
        self.lc = np.full(width, np.nan)
        self.n = np.full(width, np.nan)
        self.b = np.full(width, np.nan)
        self.s = np.full(width, np.nan)
        self.width = width

    def ok(self, first: int, last: int) -> bool:
        return 0 <= first <= last < self.width

    def ret(self, i: int, h: int) -> float | None:
        if not self.ok(i - h, i):
            return None
        w = self.lc[i - h : i + 1]
        return float(w[-1] - w[0]) if np.isfinite(w).all() else None

    def max_high(self, a: int, b: int) -> float | None:
        if not self.ok(a, b):
            return None
        w = self.h[a : b + 1]
        return float(w.max()) if np.isfinite(w).all() else None

    def flow(self, a: int, b: int) -> tuple[float, float, float] | None:
        if not self.ok(a, b):
            return None
        bb, ss, nn = self.b[a : b + 1], self.s[a : b + 1], self.n[a : b + 1]
        if not (np.isfinite(bb).all() and np.isfinite(ss).all() and np.isfinite(nn).all()):
            return None
        x, y, v = float(bb.sum()), float(ss.sum()), float(nn.sum())
        return None if v <= 0 or x + y < 0.95 * v else (x, y, v)


def test_fast_tape_matches_reference_on_gappy_paths() -> None:
    rng = random.Random(3)
    for _ in range(20):
        tape, naive = Tape(120), NaiveTape(120)
        price = 100.0
        for i in range(120):
            if rng.random() < 0.08:
                continue  # missing minute
            price *= math.exp(rng.gauss(0, 0.002))
            labelled = rng.random() > 0.05
            item = bar(
                "X",
                DAY + timedelta(minutes=i + 1),
                price,
                price * 1.001,
                price * 0.999,
                price,
                notional=1000.0 + i,
                buy=400.0 + i if labelled else None,
            )
            tape.put(i, item, "trade")
            naive.h[i], naive.l[i], naive.lc[i] = price * 1.001, price * 0.999, math.log(price)
            naive.n[i] = item.notional
            if labelled:
                naive.b[i], naive.s[i] = item.buy_notional, item.sell_notional
        for _ in range(300):
            i, h = rng.randrange(-5, 125), rng.randrange(1, 15)
            assert tape.ret(i, h) == naive.ret(i, h)
            a = rng.randrange(-3, 122)
            b = a + rng.randrange(0, 12)
            assert tape.max_high(a, b) == naive.max_high(a, b)
            got, want = tape.flow(a, b), naive.flow(a, b)
            assert (got is None) == (want is None)
            if got is not None and want is not None:
                assert got == pytest.approx(want, rel=1e-12)
    with pytest.raises(ValueError):
        tape.put(0, bar("X", DAY, 1, 1, 1, 1), "trade")  # out of order


def test_universe_membership_requires_known_instruments() -> None:
    with pytest.raises(ValueError):
        universe(["BTC", "ETH"], membership={"2026-03": ["BTC", "DOGE"]})
    assert instrument("BTC").id == "BTC"
