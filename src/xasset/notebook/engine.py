"""Daily long-only portfolio simulation for notebook strategies.

Decisions made after day ``t`` completes enter at the open of ``t + 1 + delay``,
moved adversely by the per-side cost. Exits:

* price barriers (when a rule sets them) are checked each day on the open first
  (gaps fill at the open), then on the day's range; when both barriers fall in one
  daily bar the adverse one is taken and the favourable alternative is recorded;
* an early exit flagged after a day's close leaves at the next open;
* the time exit leaves at the open ``horizon`` days after entry;
* a coin with no further bars leaves at its last close (halted or delisted).

Positions are equally weighted (``1 / max_positions`` of NAV at entry), capped at a
share of the coin's trailing median daily notional.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

import numpy as np

from xasset.lab.ledger import Ledger, TradeRecord
from xasset.notebook.features import Context


@dataclass(frozen=True)
class Decision:
    day: int
    column: int
    score: float
    strategy: str


@dataclass(frozen=True)
class Rule:
    horizon: int
    target: float | None = None  # e.g. 0.10 for +10%
    stop: float | None = None  # e.g. 0.04 for -4%
    max_positions: int = 10
    participation: float = 0.02


@dataclass
class Holding:
    column: int
    strategy: str
    qty: float
    entry_day: int
    reference: float  # the entry day's open, the barrier reference
    entry_price: float  # executed: open moved by half spread and impact
    entry_fee: float
    entry_notional: float
    exit_day: int
    signal_day: int
    target: float | None
    stop: float | None
    risk_distance: float
    early_exit: bool = False


@dataclass
class Simulation:
    ledger: Ledger
    trades: list[TradeRecord] = field(default_factory=list)
    nav: list[tuple[date, float]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)


def stamp(day: date, hour: int = 0) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=UTC)


def simulate(
    context: Context,
    decisions: dict[int, list[Decision]],
    rule: Rule,
    spread_bps: np.ndarray,
    fee_bps: float,
    start: int,
    end: int,
    initial_nav: float = 100_000.0,
    multiplier: float = 1.0,
    delay: int = 0,
    early_exit: Callable[[int, int], bool] | None = None,
    prefix: str = "",
) -> Simulation:
    """Run days ``start <= d < end``; decisions keyed by their decision day."""
    panel = context.panel
    ledger = Ledger()
    output = Simulation(ledger)
    cash = initial_nav
    holdings: dict[int, Holding] = {}
    pending: list[Decision] = []
    counter = 0

    fee_rate = multiplier * fee_bps / 10_000
    widest = float(np.nanmax(spread_bps)) if np.isfinite(spread_bps).any() else 25.0

    def side_cost(day: int, column: int) -> float:
        """Half spread plus impact as a log fraction (the widest tier if unranked)."""
        value = spread_bps[day, column]
        return multiplier * (float(value) if math.isfinite(value) else widest) / 10_000

    def nav_at(day: int) -> float:
        total = cash
        for h in holdings.values():
            price = panel.close[day, h.column]
            if not math.isfinite(price):
                price = last_price(h.column, day)
            total += h.qty * price
        return total

    def last_price(column: int, day: int) -> float:
        values = panel.close[: day + 1, column]
        finite = np.flatnonzero(np.isfinite(values))
        return float(values[finite[-1]]) if finite.size else 0.0

    def close_position(
        h: Holding,
        day: int,
        price: float,
        reason: str,
        hour: int,
        ambiguous: bool = False,
        alternative: float | None = None,
    ) -> None:
        nonlocal cash, counter
        cost = side_cost(min(day, len(panel.dates) - 1), h.column)
        executed = price * math.exp(-cost)
        notional = h.qty * executed
        fee = notional * fee_rate
        cash += notional - fee
        gross = h.qty * (executed - h.entry_price)
        counter += 1
        exit_day = panel.dates[min(day, len(panel.dates) - 1)]
        record = TradeRecord(
            id=f"{prefix}T{counter:07d}",
            setup=f"{h.strategy}-{panel.dates[h.signal_day]}-{panel.symbols[h.column]}",
            strategy=h.strategy,
            symbol=panel.symbols[h.column],
            direction=1,
            qty=h.qty,
            entry_time=stamp(panel.dates[h.entry_day]),
            entry_price=h.entry_price,
            exit_time=stamp(exit_day, hour),
            exit_price=executed,
            exit_reason=reason,
            stop=h.reference * (1 - h.stop) if h.stop else 0.0,
            target=h.reference * (1 + h.target) if h.target else None,
            risk_distance=h.risk_distance,
            gross=gross,
            fees=h.entry_fee + fee,
            funding=0.0,
            net=gross - h.entry_fee - fee,
            entry_notional=h.entry_notional,
            signal_time=stamp(panel.dates[h.signal_day]) + timedelta(days=1),
            decided_at=stamp(panel.dates[h.signal_day]) + timedelta(days=1, seconds=1),
            ambiguous_bar=ambiguous,
            alternative_exit_price=alternative,
            execution_basis="daily_bar",
        )
        output.trades.append(record)
        ledger.trade(record)
        del holdings[h.column]

    for day in range(start, end):
        # 1. Scheduled exits at the open: time exits and early exits flagged yesterday.
        for h in list(holdings.values()):
            o = panel.open[day, h.column]
            if not math.isfinite(o):
                if day > context.panel.last[h.column]:
                    close_position(h, day - 1, last_price(h.column, day), "delisted", 23)
                continue
            if day >= h.exit_day:
                close_position(h, day, o, "time", 0)
            elif h.early_exit:
                close_position(h, day, o, "signal_exit", 0)
        # 2. Entries decided after an earlier close.
        due = [p for p in pending if p.day + 1 + delay == day]
        pending = [p for p in pending if p.day + 1 + delay > day]
        due.sort(key=lambda p: (-p.score, p.column))
        nav = nav_at(max(day - 1, 0))
        for decision in due:
            if len(holdings) >= rule.max_positions:
                output.skipped["portfolio full"] = output.skipped.get("portfolio full", 0) + 1
                continue
            column = decision.column
            if column in holdings:
                output.skipped["already held"] = output.skipped.get("already held", 0) + 1
                continue
            o = panel.open[day, column]
            if not math.isfinite(o) or o <= 0:
                output.skipped["no open"] = output.skipped.get("no open", 0) + 1
                continue
            liquidity = context.liquidity[decision.day, column]
            budget = nav / rule.max_positions
            if math.isfinite(liquidity):
                budget = min(budget, rule.participation * liquidity)
            budget = min(budget, cash / (1 + fee_rate))
            if budget <= 0:
                continue
            price = o * math.exp(side_cost(decision.day, column))
            qty = budget / price
            fee = budget * fee_rate
            cash -= budget + fee
            sigma = context.sigma20[decision.day, column]
            risk = (
                -math.log(1 - rule.stop)
                if rule.stop
                else (sigma * math.sqrt(rule.horizon) if math.isfinite(sigma) else 0.0)
            )
            holdings[column] = Holding(
                column=column,
                strategy=decision.strategy,
                qty=qty,
                entry_day=day,
                reference=o,
                entry_price=price,
                entry_fee=fee,
                entry_notional=budget,
                exit_day=day + rule.horizon,
                signal_day=decision.day,
                target=rule.target,
                stop=rule.stop,
                risk_distance=risk,
            )
        # 3. Barriers inside the day (adverse ordering when both are touched).
        for h in list(holdings.values()):
            o, hi, lo = (
                panel.open[day, h.column],
                panel.high[day, h.column],
                panel.low[day, h.column],
            )
            if not (math.isfinite(o) and math.isfinite(hi) and math.isfinite(lo)):
                continue
            reference = h.reference
            down = reference * (1 - h.stop) if h.stop else None
            up = reference * (1 + h.target) if h.target else None
            if day > h.entry_day:
                if down is not None and o <= down:
                    close_position(h, day, o, "stop_gap", 0)
                    continue
                if up is not None and o >= up:
                    close_position(h, day, o, "target_gap", 0)
                    continue
            hit_down = down is not None and lo <= down
            hit_up = up is not None and hi >= up
            if hit_down and down is not None:
                close_position(
                    h,
                    day,
                    down,
                    "stop",
                    12,
                    ambiguous=hit_up,
                    alternative=up if hit_up else None,
                )
            elif hit_up and up is not None:
                close_position(h, day, up, "target", 12)
        # 4. Early-exit checks after the close, then new decisions.
        if early_exit is not None:
            for h in holdings.values():
                if day >= h.entry_day and early_exit(day, h.column):
                    h.early_exit = True
        pending.extend(decisions.get(day, []))
        nav_today = nav_at(day)
        output.nav.append((panel.dates[day], nav_today))
        ledger.mark(stamp(panel.dates[day]) + timedelta(days=1), nav_today)
    # Close anything still open at the final close for reporting.
    final = end - 1
    for h in list(holdings.values()):
        close_position(h, final, last_price(h.column, final), "end_of_test", 23)
    return output
