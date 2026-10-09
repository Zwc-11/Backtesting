"""Public websocket market-data feeds (no keys, no orders).

* Binance spot via ``data-stream.binance.vision`` (market-data-only endpoint):
  ``aggTrade`` (``m`` true means the buyer was the maker, so the seller initiated)
  and ``bookTicker`` (best bid/ask, pushed on every price or size change).
* Hyperliquid perpetuals via ``api.hyperliquid.xyz/ws``: ``trades`` (``side`` B is a
  buyer-initiated print at the ask, A a seller-initiated print at the bid) and
  ``bbo``. Hyperliquid closes idle sockets, so an application ping is sent.

Each feed reconnects with exponential backoff. The desk is told when coverage starts
and stops so that minutes spanning a gap are never emitted as complete bars.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import websockets

from xasset.lab.live.events import Quote, Trade

BINANCE_URL = "wss://data-stream.binance.vision/stream?streams="
HYPERLIQUID_URL = "wss://api.hyperliquid.xyz/ws"


class Sink(Protocol):
    def feed_connected(self, feed: str, symbols: list[str]) -> None: ...

    def feed_disconnected(self, feed: str, symbols: list[str], error: str | None) -> None: ...

    def on_trade(self, feed: str, trade: Trade) -> None: ...

    def on_quote(self, feed: str, quote: Quote) -> None: ...


def millis(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


class Feed:
    name: str

    def __init__(self, symbols: dict[str, str], clock: Callable[[], datetime]):
        """``symbols`` maps the venue's symbol to the universe instrument id."""
        self.symbols = symbols
        self.clock = clock

    def url(self) -> str:
        raise NotImplementedError

    async def on_open(self, socket: Any, sink: Sink) -> None:
        sink.feed_connected(self.name, list(self.symbols.values()))

    def parse(self, raw: str | bytes, received: datetime, sink: Sink) -> None:
        raise NotImplementedError

    async def keepalive(self, socket: Any) -> None:
        await asyncio.Event().wait()

    async def run(self, sink: Sink, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            opened_at = None
            error: str | None = None
            try:
                async with websockets.connect(
                    self.url(),
                    open_timeout=15,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=2**23,
                ) as socket:
                    opened_at = self.clock()
                    await self.on_open(socket, sink)
                    pinger = asyncio.create_task(self.keepalive(socket))
                    stopper = asyncio.create_task(stop.wait())
                    try:
                        while not stop.is_set():
                            receive = asyncio.ensure_future(socket.recv())
                            done, _ = await asyncio.wait(
                                {receive, stopper}, return_when=asyncio.FIRST_COMPLETED
                            )
                            if receive not in done:
                                receive.cancel()
                                break
                            self.parse(receive.result(), self.clock(), sink)
                    finally:
                        pinger.cancel()
                        stopper.cancel()
            except (TimeoutError, OSError, websockets.WebSocketException) as exc:
                error = f"{type(exc).__name__}: {exc}"[:200]
            except ValueError as exc:  # malformed payload: reconnect rather than guess
                error = f"parse error: {exc}"[:200]
            sink.feed_disconnected(self.name, list(self.symbols.values()), error)
            if stop.is_set():
                return
            if opened_at is not None and (self.clock() - opened_at).total_seconds() > 60:
                delay = 1.0
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
            delay = min(delay * 2, 30.0)


class BinanceSpotFeed(Feed):
    name = "binance"

    def __init__(self, symbols: dict[str, str], clock: Callable[[], datetime]):
        super().__init__(symbols, clock)
        self.last_trade: dict[str, int] = {}
        self.last_update: dict[str, int] = {}

    def url(self) -> str:
        streams = []
        for symbol in sorted(self.symbols):
            streams += [f"{symbol.lower()}@aggTrade", f"{symbol.lower()}@bookTicker"]
        return BINANCE_URL + "/".join(streams)

    def parse(self, raw: str | bytes, received: datetime, sink: Sink) -> None:
        message = json.loads(raw)
        data = message.get("data")
        if not isinstance(data, dict):
            return
        venue = data.get("s")
        if not isinstance(venue, str) or venue not in self.symbols:
            return
        symbol = self.symbols[venue]
        if data.get("e") == "aggTrade":
            trade_id = int(data["a"])
            if trade_id <= self.last_trade.get(venue, -1):
                return
            self.last_trade[venue] = trade_id
            sink.on_trade(
                self.name,
                Trade(
                    symbol=symbol,
                    price=float(data["p"]),
                    qty=float(data["q"]),
                    side=-1 if data["m"] else 1,
                    exchange_time=millis(data["T"]),
                    received=received,
                ),
            )
        elif "b" in data and "a" in data and "u" in data:
            update = int(data["u"])
            if update <= self.last_update.get(venue, -1):
                return
            self.last_update[venue] = update
            sink.on_quote(
                self.name,
                Quote(
                    symbol=symbol,
                    bid=float(data["b"]),
                    ask=float(data["a"]),
                    bid_size=float(data["B"]),
                    ask_size=float(data["A"]),
                    received=received,
                ),
            )


class HyperliquidFeed(Feed):
    name = "hyperliquid"

    def __init__(self, symbols: dict[str, str], clock: Callable[[], datetime]):
        super().__init__(symbols, clock)
        self.seen: dict[str, deque[int]] = {}
        self.seen_set: dict[str, set[int]] = {}
        self.ready: dict[str, set[str]] = {}

    def url(self) -> str:
        return HYPERLIQUID_URL

    async def on_open(self, socket: Any, sink: Sink) -> None:
        self.ready = {coin: set() for coin in self.symbols}
        for coin in sorted(self.symbols):
            for kind in ("trades", "bbo"):
                await socket.send(
                    json.dumps(
                        {"method": "subscribe", "subscription": {"type": kind, "coin": coin}}
                    )
                )

    async def keepalive(self, socket: Any) -> None:
        while True:
            await asyncio.sleep(30)
            await socket.send(json.dumps({"method": "ping"}))

    def _new_trade(self, coin: str, tid: int) -> bool:
        seen = self.seen.setdefault(coin, deque(maxlen=5000))
        members = self.seen_set.setdefault(coin, set())
        if tid in members:
            return False
        if len(seen) == seen.maxlen:
            members.discard(seen[0])
        seen.append(tid)
        members.add(tid)
        return True

    def parse(self, raw: str | bytes, received: datetime, sink: Sink) -> None:
        message = json.loads(raw)
        channel = message.get("channel")
        data = message.get("data")
        if channel == "subscriptionResponse":
            subscription = (data or {}).get("subscription", {})
            coin = subscription.get("coin")
            if coin in self.ready:
                self.ready[coin].add(subscription.get("type"))
                if self.ready[coin] >= {"trades", "bbo"}:
                    sink.feed_connected(self.name, [self.symbols[coin]])
                    self.ready[coin] = {"trades", "bbo", "announced"}
            return
        if channel == "trades" and isinstance(data, list):
            for item in data:
                coin = item.get("coin")
                symbol = self.symbols.get(coin)
                if symbol is None or not self._new_trade(coin, int(item["tid"])):
                    continue
                side = {"B": 1, "A": -1}.get(item.get("side"), 0)
                sink.on_trade(
                    self.name,
                    Trade(
                        symbol=symbol,
                        price=float(item["px"]),
                        qty=float(item["sz"]),
                        side=side,
                        exchange_time=millis(item["time"]),
                        received=received,
                    ),
                )
        elif channel == "bbo" and isinstance(data, dict):
            coin = data.get("coin")
            if not isinstance(coin, str) or coin not in self.symbols:
                return
            symbol = self.symbols[coin]
            bid, ask = (data.get("bbo") or [None, None])[:2]
            sink.on_quote(
                self.name,
                Quote(
                    symbol=symbol,
                    bid=float(bid["px"]) if bid else 0.0,
                    ask=float(ask["px"]) if ask else 0.0,
                    bid_size=float(bid["sz"]) if bid else None,
                    ask_size=float(ask["sz"]) if ask else None,
                    received=received,
                    exchange_time=millis(data["time"]) if "time" in data else None,
                ),
            )
