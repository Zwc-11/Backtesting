"""Normalized trade and quote events and a short in-memory history per instrument.

Every event carries a receipt time on the desk's corrected clock (``received``).
Trades also carry the exchange's trade time, which assigns them to minute bars the
same way the exchange's own one-minute klines do. Quotes are assigned by receipt.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(slots=True)
class Trade:
    symbol: str  # universe instrument id
    price: float
    qty: float
    side: int  # +1 buyer-initiated, -1 seller-initiated, 0 unknown
    exchange_time: datetime
    received: datetime

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass(slots=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    bid_size: float | None
    ask_size: float | None
    received: datetime
    exchange_time: datetime | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def valid(self) -> bool:
        """Positive and ordered; a single venue's book is never locked or crossed."""
        return 0 < self.bid < self.ask

    @property
    def spread_rel(self) -> float:
        return (self.ask - self.bid) / self.mid


class MarketCache:
    """Recent quotes and trades per instrument, in receipt order."""

    def __init__(self, keep: timedelta = timedelta(seconds=180)):
        self.keep = keep
        self.quotes: dict[str, deque[Quote]] = {}
        self.trades: dict[str, deque[Trade]] = {}

    def add_quote(self, quote: Quote) -> None:
        items = self.quotes.setdefault(quote.symbol, deque())
        items.append(quote)
        horizon = quote.received - self.keep
        while items and items[0].received < horizon:
            items.popleft()

    def add_trade(self, trade: Trade) -> None:
        items = self.trades.setdefault(trade.symbol, deque())
        items.append(trade)
        horizon = trade.received - self.keep
        while items and items[0].received < horizon:
            items.popleft()

    def reset(self, symbols: list[str]) -> None:
        """Forget everything received before a disconnect; it cannot be trusted after."""
        for symbol in symbols:
            self.quotes.pop(symbol, None)
            self.trades.pop(symbol, None)

    def latest(self, symbol: str) -> Quote | None:
        items = self.quotes.get(symbol)
        return items[-1] if items else None

    def quote_at(self, symbol: str, at: datetime, max_age: timedelta) -> Quote | None:
        """The quote prevailing at ``at`` (last received at or before it), if fresh."""
        for quote in reversed(self.quotes.get(symbol, ())):
            if quote.received <= at:
                return quote if at - quote.received <= max_age else None
        return None

    def first_after(self, symbol: str, at: datetime) -> Quote | None:
        found = None
        for quote in reversed(self.quotes.get(symbol, ())):
            if quote.received <= at:
                break
            found = quote
        return found

    def executable(self, symbol: str, at: datetime, max_age: timedelta) -> Quote | None:
        """First eligible quote at or after ``at``.

        The prevailing quote when it is fresh and valid; otherwise the first valid
        quote received after ``at`` (None until one arrives).
        """
        quote = self.quote_at(symbol, at, max_age)
        if quote is not None and quote.valid:
            return quote
        for candidate in self.quotes.get(symbol, ()):
            if candidate.received > at and candidate.valid:
                return candidate
        return None

    def quotes_between(self, symbol: str, start: datetime, end: datetime) -> Iterator[Quote]:
        """Quotes received in ``[start, end)``."""
        for quote in self.quotes.get(symbol, ()):
            if quote.received >= end:
                break
            if quote.received >= start:
                yield quote

    def trades_between(self, symbol: str, start: datetime, end: datetime) -> Iterator[Trade]:
        """Trades with exchange time in ``[start, end)`` (receipt order is preserved)."""
        for trade in self.trades.get(symbol, ()):
            if start <= trade.exchange_time < end:
                yield trade

    def last_trade_before(self, symbol: str, before: datetime) -> Trade | None:
        found = None
        for trade in self.trades.get(symbol, ()):
            if trade.exchange_time < before and (
                found is None or trade.exchange_time >= found.exchange_time
            ):
                found = trade
        return found
