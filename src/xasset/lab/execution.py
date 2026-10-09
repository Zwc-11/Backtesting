"""Order handling and fills.

``BarBroker`` approximates executable prices from one-minute bars (historical replay):
entries fill at the next bar's open moved adversely by half spread plus impact; stops
trigger on the bar's adverse extreme and fill at the stop or a worse gap open; when a
stop and a target both fall inside one bar the stop is taken (adverse ordering) and
the favourable alternative is recorded for a bound. ``QuoteBroker`` (live paper) uses
actual bid/ask updates. Both share the position and trade accounting below.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from xasset.lab.bars import MINUTE, Basis, FlowBar
from xasset.lab.ledger import Ledger, TradeRecord, iso
from xasset.lab.portfolio import Portfolio, Position, cost_side, fee
from xasset.lab.strategy import Candidate
from xasset.lab.universe import Book, Universe


@dataclass
class Order:
    id: str
    candidate: Candidate
    qty: float
    p_ref: float
    submitted_at: datetime
    fill_bar_end: datetime
    strategy_rank: int
    guards_checked_until: datetime


@dataclass
class Accounting:
    """Opens and closes positions on a shared portfolio and writes the ledger."""

    book: Book
    universe: Universe
    portfolio: Portfolio
    ledger: Ledger
    multiplier: int
    on_open: Callable[[Candidate, Position], None]
    on_close: Callable[[Position, str, datetime], None]
    trade_ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    prefix: str = ""

    def cost(self, symbol: str) -> Any:
        return self.book.costs[self.universe.get(symbol).cost_class]

    def open(self, order: Order, price: float, at: datetime, basis: str) -> Position | None:
        candidate = order.candidate
        item = self.universe.get(candidate.symbol)
        notional = order.qty * price
        paid = fee(self.cost(candidate.symbol), notional, order.qty, self.multiplier)
        direction = candidate.direction
        if item.kind != "perp" and direction < 0:
            raise ValueError("Cash instruments cannot be sold short in the lab")
        if item.kind == "perp":
            self.portfolio.cash -= paid
        else:
            self.portfolio.cash -= notional + paid
        d = direction * math.log(price / candidate.stop) if candidate.stop > 0 else -math.inf
        target = price * math.exp(direction * candidate.target_multiple * d) if d > 0 else None
        position = Position(
            symbol=candidate.symbol,
            strategy=candidate.setup.strategy,
            setup=candidate.setup.id,
            direction=direction,
            qty=order.qty,
            kind=item.kind,
            entry_time=at,
            entry_price=price,
            entry_fee=paid,
            entry_notional=notional,
            stop=candidate.stop,
            target=target,
            deadline=at + timedelta(minutes=candidate.time_exit_minutes),
            initial_risk=order.qty * abs(order.p_ref - candidate.stop),
            risk_distance=d,
            signal_time=candidate.signal_end,
            decided_at=order.submitted_at,
            exit_next_open="wrong-side stop at fill" if d <= 0 else None,
        )
        self.portfolio.positions[candidate.symbol] = position
        self.portfolio.marks.setdefault(candidate.symbol, price)
        self.ledger.fill(
            {
                "order": order.id,
                "setup": candidate.setup.id,
                "strategy": candidate.setup.strategy,
                "symbol": candidate.symbol,
                "side": "buy" if direction > 0 else "sell",
                "qty": order.qty,
                "price": price,
                "fee": paid,
                "at": iso(at),
                "kind": "entry",
                "basis": basis,
            }
        )
        self.on_open(candidate, position)
        return position

    def close(
        self,
        position: Position,
        price: float,
        at: datetime,
        reason: str,
        basis: str,
        ambiguous: bool = False,
        alternative: float | None = None,
    ) -> TradeRecord:
        notional = position.qty * price
        paid = fee(self.cost(position.symbol), notional, position.qty, self.multiplier)
        gross = position.direction * position.qty * (price - position.entry_price)
        if position.is_cash:
            self.portfolio.cash += notional - paid
        else:
            self.portfolio.cash += gross - paid
        del self.portfolio.positions[position.symbol]
        fees = position.entry_fee + paid
        record = TradeRecord(
            id=f"{self.prefix}T{next(self.trade_ids):07d}",
            setup=position.setup,
            strategy=position.strategy,
            symbol=position.symbol,
            direction=position.direction,
            qty=position.qty,
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            exit_time=at,
            exit_price=price,
            exit_reason=reason,
            stop=position.stop,
            target=position.target,
            risk_distance=position.risk_distance,
            gross=gross,
            fees=fees,
            funding=position.funding,
            net=gross - fees + position.funding,
            entry_notional=position.entry_notional,
            signal_time=position.signal_time,
            decided_at=position.decided_at,
            ambiguous_bar=ambiguous,
            alternative_exit_price=alternative,
            execution_basis=basis,
        )
        self.ledger.fill(
            {
                "order": f"exit:{record.id}",
                "setup": position.setup,
                "strategy": position.strategy,
                "symbol": position.symbol,
                "side": "sell" if position.direction > 0 else "buy",
                "qty": position.qty,
                "price": price,
                "fee": paid,
                "at": iso(at),
                "kind": "exit",
                "reason": reason,
                "basis": basis,
            }
        )
        self.ledger.trade(record)
        self.on_close(position, reason, at)
        return record

    def fund(self, symbol: str, rate: float, mark: float, at: datetime) -> None:
        position = self.portfolio.positions.get(symbol)
        if position is None or position.is_cash:
            return
        payment = -position.direction * position.qty * mark * rate
        position.funding += payment
        self.portfolio.cash += payment
        self.ledger.fill(
            {
                "order": f"funding:{symbol}:{iso(at)}",
                "setup": position.setup,
                "strategy": position.strategy,
                "symbol": symbol,
                "side": "funding",
                "qty": position.qty,
                "price": mark,
                "fee": 0.0,
                "cash": payment,
                "rate": rate,
                "at": iso(at),
                "kind": "funding",
                "basis": "funding_event",
            }
        )


class BarBroker:
    """Historical execution on one-minute bars (an approximation of quote fills)."""

    def __init__(
        self,
        accounting: Accounting,
        basis: Basis,
        session_bounds: Callable[[], tuple[datetime, datetime] | None],
        funding: dict[str, list[tuple[datetime, float]]] | None = None,
    ):
        self.accounting = accounting
        self.basis = basis
        self.session_bounds = session_bounds
        self.pending: list[Order] = []
        self.funding = {k: sorted(v) for k, v in (funding or {}).items()}
        self._funding_index = dict.fromkeys(self.funding, 0)
        self.recent: dict[str, list[FlowBar]] = {}
        self.on_cancel: Callable[[Order, str, datetime], None] = lambda *_: None

    @property
    def portfolio(self) -> Portfolio:
        return self.accounting.portfolio

    def submit(self, order: Order) -> None:
        self.pending.append(order)

    def _side(self, symbol: str) -> float:
        return cost_side(self.accounting.cost(symbol), self.accounting.multiplier)

    def on_bars(self, end: datetime, bars: dict[str, FlowBar]) -> None:
        start = end - MINUTE
        flatten_at = self._flatten_time()
        self._apply_funding(end, bars)
        # Guards observe every bar between the decision and the fill.
        for order in list(self.pending):
            if order.fill_bar_end > end:
                for guard in order.candidate.guards:
                    bar = bars.get(guard.symbol)
                    price = bar.price(self.basis) if bar is not None else None
                    if price is not None and crossed(guard.side, guard.level, price[2], price[1]):
                        self._cancel(order, f"invalidated before entry: {guard.symbol}", end)
                        break
        fresh: set[str] = set()
        for order in sorted(
            [o for o in self.pending if o.fill_bar_end == end],
            key=lambda o: (o.strategy_rank, o.candidate.signal_end, o.candidate.symbol),
        ):
            self.pending.remove(order)
            symbol = order.candidate.symbol
            bar = bars.get(symbol)
            price = bar.price(self.basis) if bar is not None else None
            if price is None:
                self._cancel(order, "no executable bar at the scheduled entry minute", end)
                continue
            if flatten_at is not None and start >= flatten_at:
                self._cancel(order, "entry would fall in the session-flattening minute", end)
                continue
            reason = None
            for guard in order.candidate.guards:
                guard_bar = bars.get(guard.symbol)
                guard_price = guard_bar.price(self.basis) if guard_bar is not None else None
                if guard_price is not None and crossed(
                    guard.side, guard.level, guard_price[0], guard_price[0]
                ):
                    reason = f"invalidated before entry: {guard.symbol}"
                    break
            if reason is not None:
                self._cancel(order, reason, end)
                continue
            if symbol in self.portfolio.positions:
                self._cancel(order, "one open position per asset", end)
                continue
            fill = price[0] * math.exp(order.candidate.direction * self._side(symbol))
            self.accounting.open(order, fill, start, "bar_open")
            fresh.add(symbol)
        for order in [o for o in self.pending if o.fill_bar_end < end]:
            self.pending.remove(order)
            self._cancel(order, "scheduled entry minute passed", end)
        for symbol, position in list(self.portfolio.positions.items()):
            bar = bars.get(symbol)
            price = bar.price(self.basis) if bar is not None else None
            if price is None:
                continue
            self._exits(position, price, start, end, symbol in fresh, flatten_at)
        for symbol, bar in bars.items():
            price = bar.price(self.basis)
            if price is not None:
                self.portfolio.marks[symbol] = price[3]
        self.accounting.ledger.mark(end, self.portfolio.nav())

    def _flatten_time(self) -> datetime | None:
        bounds = self.session_bounds()
        return None if bounds is None else bounds[1] - MINUTE

    def _session_open(self) -> datetime | None:
        bounds = self.session_bounds()
        return None if bounds is None else bounds[0]

    def _exits(
        self,
        position: Position,
        price: tuple[float, float, float, float],
        start: datetime,
        end: datetime,
        fresh: bool,
        flatten_at: datetime | None,
    ) -> None:
        o, h, lo, c = price
        side = self._side(position.symbol)
        sign = position.direction

        def executable(level: float) -> float:
            return level * math.exp(-sign * side)

        close = self.accounting.close
        if not fresh:
            if position.exit_next_open is not None:
                close(position, executable(o), start, position.exit_next_open, "bar_open")
                return
            if (sign > 0 and o <= position.stop) or (sign < 0 and o >= position.stop):
                close(position, executable(o), start, "stop_gap", "bar_open")
                return
            if position.target is not None and (
                (sign > 0 and o >= position.target) or (sign < 0 and o <= position.target)
            ):
                close(position, executable(o), start, "target_gap", "bar_open")
                return
            if start >= position.deadline:
                close(position, executable(o), start, "time", "bar_open")
                return
            if flatten_at is not None and start >= flatten_at:
                close(position, executable(o), start, "session_flatten", "bar_open")
                return
            opened = self._session_open()
            if opened is not None and position.entry_time < opened:
                # The previous session's flattening bar was missing; exit at the first
                # executable open instead of silently holding overnight.
                close(position, executable(o), start, "session_flatten_missed", "bar_open")
                return
        elif position.exit_next_open is not None:
            return  # Exit at the next bar's open.
        hit_stop = lo <= position.stop if sign > 0 else h >= position.stop
        hit_target = position.target is not None and (
            h >= position.target if sign > 0 else lo <= position.target
        )
        inside = end - timedelta(microseconds=1)
        if hit_stop:
            alternative = executable(position.target) if hit_target and position.target else None
            close(
                position,
                executable(position.stop),
                inside,
                "stop",
                "bar_range",
                ambiguous=hit_target,
                alternative=alternative,
            )
        elif hit_target and position.target is not None:
            close(position, executable(position.target), inside, "target", "bar_range")

    def _apply_funding(self, end: datetime, bars: dict[str, FlowBar]) -> None:
        for symbol, events in self.funding.items():
            index = self._funding_index[symbol]
            while index < len(events) and events[index][0] <= end - MINUTE:
                at, rate = events[index]
                mark = self.portfolio.marks.get(symbol)
                if mark is not None:
                    self.accounting.fund(symbol, rate, mark, at)
                index += 1
            self._funding_index[symbol] = index

    def _cancel(self, order: Order, reason: str, at: datetime) -> None:
        if order in self.pending:
            self.pending.remove(order)
        self.accounting.ledger.order(
            {
                "order": order.id,
                "setup": order.candidate.setup.id,
                "strategy": order.candidate.setup.strategy,
                "symbol": order.candidate.symbol,
                "status": "cancelled",
                "reason": reason,
                "at": iso(at),
            }
        )
        self.on_cancel(order, reason, at)


def crossed(side: str, level: float, low: float, high: float) -> bool:
    return low < level if side == "below" else high > level
