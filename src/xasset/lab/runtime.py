"""The causal event loop shared by replay and live paper trading.

Per completed minute: advance the market (rolling sessions, calibrations and models),
add the bars, let the broker process fills and exits from earlier decisions, then let
strategies evaluate in the frozen priority order and submit sized, limit-checked
orders. Nothing evaluated at minute t can fill before the next executable price.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from xasset.lab.bars import MINUTE, Basis, FlowBar
from xasset.lab.execution import Accounting, Order
from xasset.lab.ledger import Ledger, iso
from xasset.lab.market import Market
from xasset.lab.mirror import MirrorStrategy, mirror_bar, real_candidate
from xasset.lab.portfolio import Portfolio, Position, cost_side
from xasset.lab.strategy import Candidate, Guard, Strategy
from xasset.lab.universe import Book, Universe


class Broker(Protocol):
    def submit(self, order: Order) -> None: ...


@dataclass(frozen=True)
class RunSettings:
    basis: Basis
    multiplier: int = 1
    delay_bars: int = 0  # extra bars between decision and entry (delay stress)


class Runtime:
    def __init__(
        self,
        book: Book,
        universe: Universe,
        strategy_types: Sequence[type[Strategy]],
        settings: RunSettings,
        ledger: Ledger | None = None,
        prefix: str = "",
    ):
        self.book = book
        self.universe = universe
        self.settings = settings
        self.ledger = ledger or Ledger()
        self.market = Market(universe, book, settings.basis)
        # Mirror strategies read the inverted market (xasset.lab.mirror).
        self.mirror_market: Market | None = None
        if any(issubclass(cls, MirrorStrategy) for cls in strategy_types):
            self.mirror_market = Market(universe, book, settings.basis, mirrored=True)
        order = {sid: rank for rank, sid in enumerate(book.strategies)}

        def build(cls: type[Strategy]) -> Strategy:
            market = self.market
            if issubclass(cls, MirrorStrategy):
                assert self.mirror_market is not None
                market = self.mirror_market
            return cls(market, self.ledger, book, universe)

        self.strategies = sorted(
            (build(cls) for cls in strategy_types),
            key=lambda s: order.get(s.id, len(order)),
        )
        self.rank = {s.id: order.get(s.id, len(order)) for s in self.strategies}
        for strategy in self.strategies:
            strategy.prefix = prefix
        self.prefix = prefix
        self._orders = itertools.count(1)
        self.by_id = {s.id: s for s in self.strategies}
        self.portfolio = Portfolio(book.limits, {item.id: item for item in universe.instruments})
        self.accounting = Accounting(
            book,
            universe,
            self.portfolio,
            self.ledger,
            settings.multiplier,
            on_open=self._opened,
            on_close=self._closed,
            prefix=prefix,
        )
        self.broker: Broker | None = None
        self.decisions = 0

    # --- callbacks from the broker ------------------------------------------------
    def _opened(self, candidate: Candidate, position: Position) -> None:
        strategy = self.by_id[candidate.setup.strategy]
        if strategy.setups.get(candidate.symbol) is candidate.setup:
            strategy.now = position.entry_time
            strategy.transition(
                candidate.setup,
                "OPEN",
                "filled",
                price=position.entry_price,
                qty=position.qty,
                target=position.target,
                risk_distance=position.risk_distance,
            )

    def _closed(self, position: Position, reason: str, at: datetime) -> None:
        strategy = self.by_id[position.strategy]
        setup = strategy.setups.get(position.symbol)
        if setup is not None and setup.id == position.setup:
            strategy.now = at
            strategy.release(setup, "exited", reason)

    def cancelled(self, order: Order, reason: str, at: datetime) -> None:
        strategy = self.by_id[order.candidate.setup.strategy]
        setup = order.candidate.setup
        if strategy.setups.get(setup.symbol) is setup:
            strategy.now = at
            strategy.release(setup, "cancelled", reason)

    # --- main loop ----------------------------------------------------------------
    def advance(self, end: datetime) -> bool:
        """Move every market to the minute ending at ``end``; roll strategy sessions."""
        previous = self.market.session
        if not self.market.advance(end):
            return False
        if self.mirror_market is not None:
            self.mirror_market.advance(end)
        if previous is not None and self.market.session is not previous:
            for strategy in self.strategies:
                strategy.now = end
                strategy.on_session()
        return True

    def add_bars(self, end: datetime, bars: Sequence[FlowBar]) -> dict[str, FlowBar]:
        """Add this minute's bars (one per instrument) to every market."""
        current = {bar.symbol: bar for bar in bars if bar.end == end}
        for bar in current.values():
            self.market.add(bar)
        if self.mirror_market is not None:
            for bar in current.values():
                self.mirror_market.add(mirror_bar(bar))
        return current

    def step(self, end: datetime, bars: Sequence[FlowBar], process: bool = True) -> list[Order]:
        """Process one completed minute. Returns the orders submitted at this minute."""
        if not self.advance(end):
            return []
        current = self.add_bars(end, bars)
        if process and hasattr(self.broker, "on_bars"):
            self.broker.on_bars(end, current)  # type: ignore[union-attr]
        if not current:
            return []
        available = max(bar.available_at for bar in current.values())
        decided = available + timedelta(milliseconds=self.book.execution.decision_latency_ms)
        candidates: list[tuple[int, Candidate]] = []
        context: dict[str, float | None] | None = None
        for strategy in self.strategies:
            for candidate in strategy.evaluate(decided):
                if context is None:
                    context = self.market.context(self.market.minute)
                # The wider market at the decision, on the real scale, for every signal.
                self.ledger.event(
                    decided,
                    strategy.id,
                    candidate.symbol,
                    candidate.setup.id,
                    "context",
                    candidate.setup.state,
                    **context,
                )
                if isinstance(strategy, MirrorStrategy):
                    candidate = real_candidate(candidate)
                candidates.append((self.rank[strategy.id], candidate))
        candidates.sort(key=lambda item: (item[0], item[1].signal_end, item[1].symbol))
        return [o for o in (self.submit(rank, c, decided) for rank, c in candidates) if o]

    def adopt_ledger(self, ledger: Ledger) -> None:
        """Route all further records to ``ledger`` (used after a live warm-up)."""
        self.ledger = ledger
        self.accounting.ledger = ledger
        for strategy in self.strategies:
            strategy.ledger = ledger

    def reset_setups(self) -> None:
        """Forget open setups and cooldowns without records (warm-up hand-over)."""
        for strategy in self.strategies:
            strategy.setups.clear()
            strategy.cooldown_until.clear()

    def reference_price(self, symbol: str, direction: int) -> float | None:
        """Conservative executable estimate at decision time.

        A quote broker supplies the current executable side plus impact; on bars it is
        the last close moved adversely by half spread plus impact.
        """
        quoted = getattr(self.broker, "reference", None)
        if quoted is not None:
            value: float | None = quoted(symbol, direction)
            return value
        tape = self.market.tapes[symbol]
        last = tape.close(self.market.minute)
        if last is None:
            return None
        cost = self.book.costs[self.universe.get(symbol).cost_class]
        return last * math.exp(direction * cost_side(cost, self.settings.multiplier))

    def submit(self, rank: int, candidate: Candidate, decided: datetime) -> Order | None:
        strategy = self.by_id[candidate.setup.strategy]
        strategy.now = decided
        reason = None
        p_ref = self.reference_price(candidate.symbol, candidate.direction)
        qty = 0.0
        if p_ref is None:
            reason = "no reference price"
        else:
            d_ref = candidate.direction * math.log(p_ref / candidate.stop)
            if not (d_ref > 0 and 0.25 * candidate.sigma10 <= d_ref <= 3 * candidate.sigma10):
                reason = "stop distance outside [0.25, 3] x sigma10"
            else:
                item = self.universe.get(candidate.symbol)
                if candidate.direction < 0 and not item.shortable:
                    reason = "instrument not shortable"
                else:
                    expected = self.market.expected_notional(
                        candidate.symbol, self.market.minute + 1
                    )
                    qty = self.portfolio.size(p_ref, candidate.stop, expected, item.lot)
                    pending = [
                        (o.candidate.symbol, o.qty, o.p_ref, o.candidate.stop)
                        for o in getattr(self.broker, "pending", [])
                    ]
                    reason = self.portfolio.check(
                        candidate.symbol, qty, p_ref, candidate.stop, pending
                    )
        if reason is not None or p_ref is None:
            self.ledger.order(
                {
                    "order": None,
                    "setup": candidate.setup.id,
                    "strategy": candidate.setup.strategy,
                    "symbol": candidate.symbol,
                    "status": "rejected",
                    "reason": reason,
                    "at": iso(decided),
                }
            )
            strategy.release(candidate.setup, "rejected", reason or "rejected")
            return None
        # The structural invalidation level (the stop) is always a pre-entry guard.
        guards = [
            *candidate.guards,
            Guard(
                candidate.symbol, "below" if candidate.direction > 0 else "above", candidate.stop
            ),
        ]
        candidate.guards = guards
        order = Order(
            id=f"{self.prefix}O{next(self._orders):07d}",
            candidate=candidate,
            qty=qty,
            p_ref=p_ref,
            submitted_at=decided,
            fill_bar_end=candidate.signal_end + MINUTE * (1 + self.settings.delay_bars),
            strategy_rank=rank,
            guards_checked_until=candidate.signal_end,
        )
        self.ledger.order(
            {
                "order": order.id,
                "setup": candidate.setup.id,
                "strategy": candidate.setup.strategy,
                "symbol": candidate.symbol,
                "status": "submitted",
                "side": "buy" if candidate.direction > 0 else "sell",
                "qty": qty,
                "p_ref": p_ref,
                "stop": candidate.stop,
                "at": iso(decided),
            }
        )
        assert self.broker is not None
        self.broker.submit(order)
        self.decisions += 1
        return order
