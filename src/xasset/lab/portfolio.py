"""Shared-capital cash ledger, the handbook's sizing rule and hard exposure limits."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from xasset.lab.universe import CostClass, LabInstrument, PortfolioLimits


@dataclass
class Position:
    symbol: str
    strategy: str
    setup: str
    direction: int
    qty: float
    kind: str
    entry_time: datetime
    entry_price: float
    entry_fee: float
    entry_notional: float
    stop: float
    target: float | None
    deadline: datetime
    initial_risk: float
    risk_distance: float
    signal_time: datetime
    decided_at: datetime
    funding: float = 0.0
    exit_next_open: str | None = None  # forced exit reason (e.g. wrong-side stop)

    @property
    def is_cash(self) -> bool:
        return self.kind != "perp"


@dataclass
class Portfolio:
    limits: PortfolioLimits
    instruments: dict[str, LabInstrument]
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    marks: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.cash == 0.0:
            self.cash = self.limits.initial_nav

    def value(self, position: Position) -> float:
        mark = self.marks.get(position.symbol, position.entry_price)
        if position.is_cash:
            return position.qty * mark
        return position.direction * position.qty * (mark - position.entry_price)

    def nav(self) -> float:
        return self.cash + sum(self.value(p) for p in self.positions.values())

    def gross(self) -> float:
        return sum(p.qty * self.marks.get(p.symbol, p.entry_price) for p in self.positions.values())

    def cluster_gross(self, cluster: str) -> float:
        return sum(
            p.qty * self.marks.get(p.symbol, p.entry_price)
            for p in self.positions.values()
            if self.instruments[p.symbol].cluster == cluster
        )

    def aggregate_risk(self) -> float:
        return sum(p.initial_risk for p in self.positions.values())

    def size(self, p_ref: float, stop: float, expected_notional: float | None, lot: float) -> float:
        """abs(q) = min(risk*NAV/|P-K|, max_single*NAV/P, participation*V_next/P), lot-rounded."""
        nav = self.nav()
        distance = abs(p_ref - stop)
        if nav <= 0 or distance <= 0 or p_ref <= 0 or not expected_notional:
            return 0.0
        limit = self.limits
        q = min(
            limit.risk_per_trade * nav / distance,
            limit.max_single_notional * nav / p_ref,
            limit.max_participation * expected_notional / p_ref,
        )
        steps = math.floor(q / lot + 1e-9)
        return max(0.0, steps * lot)

    def check(
        self,
        symbol: str,
        qty: float,
        p_ref: float,
        stop: float,
        pending: list[tuple[str, float, float, float]] | None = None,
    ) -> str | None:
        """Reason the order breaches a hard limit after conservative fill estimation.

        ``pending`` holds (symbol, qty, p_ref, stop) of submitted, unfilled orders,
        which already consume exposure and risk budget.
        """
        pending = pending or []
        if symbol in self.positions:
            return "one open position per asset"
        if any(item[0] == symbol for item in pending):
            return "one pending order per asset"
        if qty <= 0:
            return "size rounds to zero"
        nav = self.nav()
        notional = qty * p_ref
        pending_gross = sum(q * p for _, q, p, _ in pending)
        if self.gross() + pending_gross + notional > self.limits.max_gross * nav:
            return "aggregate gross exposure limit"
        pending_risk = sum(q * abs(p - k) for _, q, p, k in pending)
        risk = self.aggregate_risk() + pending_risk + qty * abs(p_ref - stop)
        if risk > self.limits.max_aggregate_risk * nav:
            return "aggregate initial stop-risk limit"
        cluster = self.instruments[symbol].cluster
        pending_cluster = sum(
            q * p for s, q, p, _ in pending if self.instruments[s].cluster == cluster
        )
        if (
            self.cluster_gross(cluster) + pending_cluster + notional
            > self.limits.max_cluster_gross * nav
        ):
            return "cluster gross exposure limit"
        return None


def cost_side(cost: CostClass, multiplier: int) -> float:
    """Adverse executable-side adjustment (half spread plus impact) as a log fraction."""
    return multiplier * (cost.half_spread_bps + cost.impact_bps) / 10_000


def fee(cost: CostClass, notional: float, qty: float, multiplier: int) -> float:
    if notional <= 0 or qty <= 0:
        raise ValueError("Fees require positive notional and quantity")
    return multiplier * max(
        cost.minimum_fee, notional * cost.fee_bps / 10_000 + qty * cost.fee_per_unit
    )
