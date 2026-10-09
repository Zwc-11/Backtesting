"""Synthetic markets for strategy-lab tests: exact paths, controlled calibrations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from xasset.lab.bars import FlowBar
from xasset.lab.ledger import Ledger
from xasset.lab.market import Market
from xasset.lab.strategy import Strategy
from xasset.lab.universe import Book, CostClass, LabInstrument, Pair, Universe

DAY = datetime(2026, 3, 2, tzinfo=UTC)


def instrument(symbol: str, **overrides: Any) -> LabInstrument:
    base: dict[str, Any] = {
        "id": symbol,
        "kind": "spot",
        "currency": "USDT",
        "tick": 0.01,
        "lot": 0.001,
        "cluster": f"cluster-{symbol}",
        "cost_class": "test",
        "history": "binance-spot",
    }
    base.update(overrides)
    return LabInstrument(**base)


def universe(
    symbols: list[str],
    benchmark: str = "BTC",
    minimum_peers: int = 2,
    pairs: list[Pair] | None = None,
    extra: list[LabInstrument] | None = None,
    **overrides: Any,
) -> Universe:
    instruments = [instrument(s) for s in symbols] + list(extra or [])
    return Universe(
        id="test",
        description="Synthetic test universe for the lab",
        benchmark=benchmark,
        instruments=instruments,
        minimum_peers=minimum_peers,
        pairs=pairs or [],
        selection_note="Synthetic instruments for unit tests.",
        **overrides,
    )


def book(strategies: list[str], **overrides: Any) -> Book:
    data: dict[str, Any] = {
        "id": "test",
        "universe": "unused.yaml",
        "strategies": strategies,
        "costs": {
            "test": CostClass(
                half_spread_bps=0, impact_bps=0, fee_bps=0, evidence="zero-cost test profile"
            )
        },
        "calibration_sessions": 5,
        "minimum_reference": 20,
    }
    data.update(overrides)
    return Book(**data)


def bar(
    symbol: str,
    end: datetime,
    o: float,
    h: float,
    lo: float,
    c: float,
    notional: float = 1000.0,
    buy: float | None = None,
    sell: float | None = None,
) -> FlowBar:
    if buy is not None and sell is None:
        sell = notional - buy
    return FlowBar(
        symbol=symbol,
        end=end,
        available_at=end + timedelta(seconds=1),
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=notional / c,
        notional=notional,
        buy_notional=buy,
        sell_notional=sell,
        unclassified_notional=0.0 if buy is not None else None,
        source="test",
    )


def flat(symbol: str, end: datetime, price: float, **kwargs: Any) -> FlowBar:
    return bar(symbol, end, price, price, price, price, **kwargs)


class Harness:
    """Drive one strategy over a synthetic session, minute by minute."""

    def __init__(self, strategy_type: type[Strategy], uni: Universe, bk: Book | None = None):
        self.universe = uni
        self.book = bk or book([strategy_type.id])
        self.ledger = Ledger()
        self.market = Market(uni, self.book, "trade")
        self.strategy = strategy_type(self.market, self.ledger, self.book, uni)
        self.candidates: list[Any] = []

    def end(self, minute: int, day: datetime = DAY) -> datetime:
        return day + timedelta(minutes=minute + 1)

    def minute(self, minute: int, bars: list[FlowBar], evaluate: bool = True) -> list[Any]:
        end = self.end(minute)
        previous = self.market.session
        assert self.market.advance(end)
        if previous is not None and self.market.session is not previous:
            self.strategy.now = end
            self.strategy.on_session()
        for item in bars:
            assert item.end == end
            self.market.add(item)
        if not evaluate:
            return []
        produced = self.strategy.evaluate(end + timedelta(seconds=2))
        self.candidates.extend(produced)
        return produced

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.ledger.events if kind is None or e["event"] == kind]

    def patch(self, **functions: Any) -> None:
        for name, function in functions.items():
            setattr(self.market, name, function)
