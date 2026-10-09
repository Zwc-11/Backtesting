"""Simulated execution against live bid/ask quotes (the handbook's primary model).

Entries are marketable orders: eligible at the decision time plus transport latency,
they fill at the first eligible quote at or after that moment (the prevailing quote
if it is fresh, otherwise the next one). Longs buy the ask and sell the bid; shorts
sell the bid and cover at the ask; a per-side impact charge is added and the book's
fee schedule applies. The quoted half spread is paid for real, so the cost class's
assumed half spread is not charged again.

A signal is cancelled if its structural invalidation level is crossed between the
signal bar's end and the fill (midpoints for midpoint books, trade prices for
trade-bar books). Stops and targets trigger on the executable side (bid for longs,
ask for shorts) and exit at the next eligible quote after transport latency, so gaps
and latency are paid. Time exits and wrong-side stops exit the same way. Perpetual
funding is applied when the venue publishes it, to positions open at the funding time.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from xasset.lab.bars import MINUTE, Basis, FlowBar
from xasset.lab.execution import Accounting, Order, crossed
from xasset.lab.ledger import iso, trade_json
from xasset.lab.live.events import MarketCache, Quote, Trade
from xasset.lab.portfolio import Portfolio, Position

MAX_WAIT = timedelta(seconds=30)


@dataclass
class PendingEntry:
    order: Order
    eligible: datetime


@dataclass(frozen=True)
class FundingExposure:
    symbol: str
    setup: str
    direction: int
    qty: float
    mark: float


@dataclass
class PendingExit:
    reason: str
    eligible: datetime
    triggered_at: datetime
    trigger_price: float | None


class QuoteBroker:
    def __init__(
        self,
        accounting: Accounting,
        cache: MarketCache,
        basis: Basis,
        clock: Callable[[], datetime],
        session_bounds: Callable[[], tuple[datetime, datetime] | None],
    ):
        self.accounting = accounting
        self.cache = cache
        self.basis = basis
        self.clock = clock
        self.session_bounds = session_bounds
        execution = accounting.book.execution
        self.transport = timedelta(milliseconds=execution.transport_latency_ms)
        self.max_age = timedelta(seconds=execution.max_quote_age_seconds)
        self.entries: list[PendingEntry] = []
        self.exits: dict[str, PendingExit] = {}
        self.on_cancel: Callable[[Order, str, datetime], None] = lambda *_: None
        self.events: list[dict[str, Any]] = []  # broker-level notes for the desk snapshot

    # --- interface used by the runtime ------------------------------------------------
    @property
    def portfolio(self) -> Portfolio:
        return self.accounting.portfolio

    @property
    def pending(self) -> list[Order]:
        return [entry.order for entry in self.entries]

    def impact(self, symbol: str) -> float:
        cost = self.accounting.cost(symbol)
        return float(self.accounting.multiplier * cost.impact_bps / 10_000)

    def reference(self, symbol: str, direction: int) -> float | None:
        """Executable price estimate now: the ask (long) or bid (short) plus impact."""
        quote = self.cache.quote_at(symbol, self.clock(), self.max_age)
        if quote is None or not quote.valid:
            return None
        side = quote.ask if direction > 0 else quote.bid
        return side * math.exp(direction * self.impact(symbol))

    def submit(self, order: Order) -> None:
        now = self.clock()
        eligible = max(order.submitted_at, now) + self.transport
        # Invalidation already observed between the signal bar and submission.
        reason = self._guard_breach(order, order.candidate.signal_end, now)
        if reason is not None:
            self._cancel(order, reason, now)
            return
        self.entries.append(PendingEntry(order, eligible))

    def on_bars(self, end: datetime, bars: dict[str, FlowBar]) -> None:
        for symbol in list(self.portfolio.positions):
            quote = self.cache.quote_at(symbol, end, self.max_age)
            if quote is not None and quote.valid:
                self.portfolio.marks[symbol] = quote.mid
            elif symbol in bars:
                price = bars[symbol].price(self.basis)
                if price is not None:
                    self.portfolio.marks[symbol] = price[3]
        self.accounting.ledger.mark(end, self.portfolio.nav())

    # --- market events ----------------------------------------------------------------
    def on_quote(self, quote: Quote) -> None:
        """Call after the quote is in the cache. Fills due before it happen first."""
        self._settle(quote.received)
        if self.basis == "mid" and quote.valid:
            self._guards(quote.symbol, quote.mid, quote.received)
        position = self.portfolio.positions.get(quote.symbol)
        if position is None or quote.symbol in self.exits or not quote.valid:
            return
        executable = quote.bid if position.direction > 0 else quote.ask
        sign = position.direction
        if sign * (executable - position.stop) <= 0:
            self._schedule(position, "stop", quote.received, executable)
        elif position.target is not None and sign * (executable - position.target) >= 0:
            self._schedule(position, "target", quote.received, executable)

    def on_trade(self, trade: Trade) -> None:
        if self.basis == "trade":
            self._settle(trade.received)
            self._guards(trade.symbol, trade.price, trade.received)

    def process(self, now: datetime) -> None:
        """Fill everything whose eligible time has passed and whose quote has arrived."""
        self._settle(now)
        flatten_at, opened = self._session_marks()
        for symbol, position in list(self.portfolio.positions.items()):
            if symbol in self.exits:
                continue
            if position.exit_next_open is not None:
                self._schedule(position, position.exit_next_open, position.entry_time, None)
            elif now >= position.deadline:
                self._schedule(position, "time", position.deadline, None)
            elif flatten_at is not None and now >= flatten_at:
                self._schedule(position, "session_flatten", flatten_at, None)
            elif opened is not None and position.entry_time < opened:
                self._schedule(position, "session_flatten_missed", now, None)
        for symbol, pending in list(self.exits.items()):
            if pending.eligible <= now:
                self._fill_exit(symbol, pending, now)

    def funding_snapshot(self, at: datetime) -> list[FundingExposure]:
        """Perpetual positions open at a funding time, with the mid at that time."""
        output = []
        for symbol, position in self.portfolio.positions.items():
            if position.is_cash or position.entry_time >= at:
                continue
            quote = self.cache.quote_at(symbol, at, self.max_age)
            mark = (
                quote.mid if quote is not None and quote.valid else self.portfolio.marks.get(symbol)
            )
            if mark is not None:
                output.append(
                    FundingExposure(symbol, position.setup, position.direction, position.qty, mark)
                )
        return output

    def apply_funding(self, exposure: FundingExposure, rate: float, at: datetime) -> None:
        """Positive rates mean longs pay. A position closed since the funding time still pays."""
        position = self.portfolio.positions.get(exposure.symbol)
        if position is not None and position.setup == exposure.setup:
            self.accounting.fund(exposure.symbol, rate, exposure.mark, at)
            return
        trade = next(
            (t for t in reversed(self.accounting.ledger.trades) if t.setup == exposure.setup),
            None,
        )
        payment = -exposure.direction * exposure.qty * exposure.mark * rate
        self.portfolio.cash += payment
        self.accounting.ledger.fill(
            {
                "order": f"funding:{exposure.symbol}:{iso(at)}",
                "setup": exposure.setup,
                "strategy": trade.strategy if trade else None,
                "symbol": exposure.symbol,
                "side": "funding",
                "qty": exposure.qty,
                "price": exposure.mark,
                "fee": 0.0,
                "cash": payment,
                "rate": rate,
                "at": iso(at),
                "kind": "funding",
                "basis": "funding_event_after_exit",
            }
        )
        if trade is not None:
            trade.funding += payment
            trade.net += payment
            self.accounting.ledger.emit("trades", trade_json(trade))  # last record wins

    # --- internals ---------------------------------------------------------------------
    def _settle(self, now: datetime) -> None:
        """Exits due first (they free the asset), then entries, each in eligible order."""
        for symbol, pending in sorted(self.exits.items(), key=lambda item: item[1].eligible):
            if pending.eligible <= now:
                self._fill_exit(symbol, pending, now)
        self._fill_entries(now)

    def _fill_entries(self, now: datetime) -> None:
        for entry in sorted(
            [e for e in self.entries if e.eligible <= now],
            key=lambda e: (e.eligible, e.order.strategy_rank, e.order.candidate.symbol),
        ):
            self._fill_entry(entry, now)

    def _session_marks(self) -> tuple[datetime | None, datetime | None]:
        bounds = self.session_bounds()
        if bounds is None:
            return None, None
        return bounds[1] - MINUTE, bounds[0]

    def _guards(self, symbol: str, price: float, at: datetime) -> None:
        for entry in list(self.entries):
            for guard in entry.order.candidate.guards:
                if guard.symbol == symbol and crossed(guard.side, guard.level, price, price):
                    self.entries.remove(entry)
                    self._cancel(entry.order, f"invalidated before entry: {symbol}", at)
                    break

    def _guard_breach(self, order: Order, start: datetime, end: datetime) -> str | None:
        for guard in order.candidate.guards:
            if self.basis == "mid":
                prices = [
                    q.mid for q in self.cache.quotes_between(guard.symbol, start, end) if q.valid
                ]
            else:
                prices = [
                    t.price
                    for t in self.cache.trades.get(guard.symbol, ())
                    if start <= t.exchange_time and t.received < end
                ]
            if any(crossed(guard.side, guard.level, p, p) for p in prices):
                return f"invalidated before entry: {guard.symbol}"
        return None

    def _fill_entry(self, entry: PendingEntry, now: datetime) -> None:
        order = entry.order
        symbol = order.candidate.symbol
        quote = self.cache.executable(symbol, entry.eligible, self.max_age)
        if quote is None or quote.received > now:
            if now - entry.eligible > MAX_WAIT:
                self.entries.remove(entry)
                self._cancel(order, "no executable quote within 30 s", now)
            return
        self.entries.remove(entry)
        at = max(entry.eligible, quote.received)
        flatten_at, _ = self._session_marks()
        if flatten_at is not None and at >= flatten_at:
            self._cancel(order, "entry would fall in the session-flattening minute", at)
            return
        if symbol in self.portfolio.positions:
            self._cancel(order, "one open position per asset", at)
            return
        direction = order.candidate.direction
        side = quote.ask if direction > 0 else quote.bid
        price = side * math.exp(direction * self.impact(symbol))
        self.accounting.open(order, price, at, "quote")

    def _schedule(
        self, position: Position, reason: str, at: datetime, trigger: float | None
    ) -> None:
        if position.symbol not in self.exits:
            self.exits[position.symbol] = PendingExit(reason, at + self.transport, at, trigger)

    def _fill_exit(self, symbol: str, pending: PendingExit, now: datetime) -> None:
        position = self.portfolio.positions.get(symbol)
        if position is None:
            del self.exits[symbol]
            return
        quote = self.cache.executable(symbol, pending.eligible, self.max_age)
        if quote is None or quote.received > now:
            return  # Keep waiting: an open position always exits at the next valid quote.
        del self.exits[symbol]
        side = quote.bid if position.direction > 0 else quote.ask
        price = side * math.exp(-position.direction * self.impact(symbol))
        at = max(pending.eligible, quote.received)
        self.accounting.close(position, price, at, pending.reason, "quote")
        self.portfolio.marks[symbol] = quote.mid

    def _cancel(self, order: Order, reason: str, at: datetime) -> None:
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


class DiscardBroker:
    """Warm-up broker: decisions are recorded in a throwaway ledger and never filled."""

    def __init__(self, on_cancel: Callable[[Order, str, datetime], None]):
        self.on_cancel = on_cancel
        self.pending: list[Order] = []

    def submit(self, order: Order) -> None:
        self.on_cancel(order, "warm-up", order.submitted_at)
