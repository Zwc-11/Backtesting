"""Completed one-minute flow bars built from live trades and quotes.

A bar ending at ``end`` covers ``[end - 1 minute, end)``. Trades are assigned by the
exchange's trade time (matching the exchange's own klines); quotes by receipt time.
A bar is emitted for an instrument only when its feed was connected for the whole
minute; minutes overlapping a disconnect are left missing, never filled in.

Trade fields follow the kline convention (a minute without trades repeats the
previous close with zero volume). Midpoint fields use bid/ask quotes that are valid,
ordered and no older than the maximum quote age: open is the mid prevailing at the
minute start, high and low span every valid mid in the minute, close is the mid
prevailing at the end. Relative spreads are sampled once per second for strategies
that compare spreads (strategy 9). Trades arriving after their minute was emitted
are counted as late prints and never rewrite an emitted bar.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from xasset.lab.bars import MINUTE, FlowBar
from xasset.lab.live.events import MarketCache, Quote, Trade

SECOND = timedelta(seconds=1)


def floor_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


@dataclass
class SymbolHealth:
    covered_since: datetime | None = None
    last_trade: datetime | None = None
    last_quote: datetime | None = None
    bars: int = 0
    skipped: int = 0  # minutes not emitted because the feed was not connected throughout
    late_prints: int = 0
    late_notional: float = 0.0
    invalid_quotes: int = 0
    last_close: tuple[float, datetime] | None = None  # (trade close, exchange time)


@dataclass
class FeedHealth:
    name: str
    connected: bool = False
    connects: int = 0
    last_message: datetime | None = None
    last_error: str | None = None
    lags: deque[float] = field(default_factory=lambda: deque(maxlen=500))

    def lag(self) -> dict[str, float | None]:
        if not self.lags:
            return {"median_ms": None, "p95_ms": None}
        ordered = sorted(self.lags)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
        return {"median_ms": round(statistics.median(ordered), 1), "p95_ms": round(p95, 1)}


class Aggregator:
    def __init__(
        self,
        sources: dict[str, str],
        cache: MarketCache,
        max_quote_age: timedelta,
        start: datetime,
    ):
        """``sources`` maps instrument id to the recorded source label (``paper-<feed>``)."""
        self.sources = sources
        self.cache = cache
        self.max_age = max_quote_age
        self.health = {symbol: SymbolHealth() for symbol in sources}
        self.emitted_until = floor_minute(start)

    # --- connection state ----------------------------------------------------------
    def connected(self, symbols: list[str], at: datetime) -> None:
        for symbol in symbols:
            self.health[symbol].covered_since = at

    def disconnected(self, symbols: list[str], at: datetime) -> None:
        self.cache.reset(symbols)
        for symbol in symbols:
            state = self.health[symbol]
            state.covered_since = None
            state.last_close = None

    # --- events ----------------------------------------------------------------------
    def on_trade(self, trade: Trade) -> bool:
        """Store a trade; False when it is late or predates the current connection."""
        state = self.health[trade.symbol]
        if state.covered_since is None or trade.exchange_time < state.covered_since:
            return False  # Replayed snapshot or pre-connection print.
        state.last_trade = trade.received
        if trade.exchange_time < self.emitted_until:
            state.late_prints += 1
            state.late_notional += trade.notional
            return False
        self.cache.add_trade(trade)
        return True

    def on_quote(self, quote: Quote) -> None:
        state = self.health[quote.symbol]
        if state.covered_since is None:
            return
        state.last_quote = quote.received
        if not quote.valid:
            state.invalid_quotes += 1
        self.cache.add_quote(quote)

    # --- bar completion ----------------------------------------------------------------
    def due(self, now: datetime, settlement: timedelta) -> datetime | None:
        end = self.emitted_until + MINUTE
        return end if now >= end + settlement else None

    def close(self, end: datetime, now: datetime) -> list[FlowBar]:
        """Emit bars for the minute ending at ``end`` (the next one due)."""
        if end != self.emitted_until + MINUTE:
            raise ValueError("Minutes must be closed in order")
        start = end - MINUTE
        bars = []
        for symbol, source in self.sources.items():
            state = self.health[symbol]
            if state.covered_since is None or state.covered_since > start:
                state.skipped += 1
                continue
            bar = self._bar(symbol, source, start, end, now, state)
            state.bars += 1
            bars.append(bar)
        self.emitted_until = end
        return bars

    def skip_to(self, end: datetime) -> None:
        """Jump the emission cursor (after a stall); skipped minutes stay missing."""
        if end > self.emitted_until:
            self.emitted_until = end

    def _bar(
        self,
        symbol: str,
        source: str,
        start: datetime,
        end: datetime,
        now: datetime,
        state: SymbolHealth,
    ) -> FlowBar:
        bar = FlowBar(symbol=symbol, end=end, available_at=now, source=source)
        trades = sorted(
            self.cache.trades_between(symbol, start, end), key=lambda t: t.exchange_time
        )
        if trades:
            prices = [t.price for t in trades]
            bar.open, bar.close = prices[0], prices[-1]
            bar.high, bar.low = max(prices), min(prices)
            bar.volume = sum(t.qty for t in trades)
            bar.notional = sum(t.notional for t in trades)
            bar.trades = len(trades)
            bar.buy_notional = sum(t.notional for t in trades if t.side > 0)
            bar.sell_notional = sum(t.notional for t in trades if t.side < 0)
            bar.unclassified_notional = sum(t.notional for t in trades if t.side == 0)
            state.last_close = (prices[-1], trades[-1].exchange_time)
        else:
            previous = state.last_close
            if previous is None:
                prior = self.cache.last_trade_before(symbol, start)
                previous = None if prior is None else (prior.price, prior.exchange_time)
            if previous is not None and state.covered_since is not None:
                price = previous[0]
                bar.open = bar.high = bar.low = bar.close = price
                state.last_close = previous
            bar.trades = 0
            bar.buy_notional = bar.sell_notional = bar.unclassified_notional = 0.0
        self._quotes(bar, symbol, start, end)
        return bar

    def _quotes(self, bar: FlowBar, symbol: str, start: datetime, end: datetime) -> None:
        closing = self.cache.quote_at(symbol, end - timedelta(microseconds=1), self.max_age)
        if closing is None or not closing.valid:
            return
        opening = self.cache.quote_at(symbol, start, self.max_age)
        inside = list(self.cache.quotes_between(symbol, start, end))
        mids = [q.mid for q in inside if q.valid]
        if opening is not None and opening.valid:
            first = opening.mid
        elif mids:
            first = mids[0]
        else:
            first = closing.mid
        bar.mid_open = first
        bar.mid_high = max([first, *mids])
        bar.mid_low = min([first, *mids])
        bar.mid_close = closing.mid
        bar.bid_close, bar.ask_close = closing.bid, closing.ask
        bar.spread_rel = closing.spread_rel
        bar.quote_age = (end - closing.received).total_seconds()
        samples = []
        prevailing = opening
        index = 0
        for k in range(60):
            at = start + k * SECOND
            while index < len(inside) and inside[index].received <= at:
                prevailing = inside[index]
                index += 1
            if (
                prevailing is not None
                and prevailing.valid
                and at - prevailing.received <= self.max_age
            ):
                samples.append(prevailing.spread_rel)
        bar.spread_samples = tuple(samples)
