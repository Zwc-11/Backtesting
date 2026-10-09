"""The paper desk: live feeds -> minute bars -> shared runtime -> quote fills.

One desk process owns a set of public feeds and runs one or more books over the same
bars. Each book has its own capital, ledger and state:

* a midpoint book runs the handbook's primary (quote) versions. Its calibrations
  come only from bars this desk recorded, so it trades after enough recorded sessions;
* a trade-bar book runs the archive variants, primed from Binance kline archives, so
  it trades immediately and measures how the archive results survive real quotes.

Durable outputs under ``data/lab/paper/<desk>/``: ``desk.json`` (live snapshot),
and per book ``events/orders/fills/trades/nav.jsonl`` plus ``state.json`` (cash,
open positions and pending orders, used to resume after a restart).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import asdict, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import yaml
from pydantic import BaseModel, ConfigDict, Field

from xasset.lab.backtest import minutes
from xasset.lab.bars import MINUTE, Basis, FlowBar
from xasset.lab.ledger import JsonlSink, Ledger, TradeRecord, iso, trade_json
from xasset.lab.live.aggregator import Aggregator, FeedHealth, floor_minute
from xasset.lab.live.broker import DiscardBroker, FundingExposure, QuoteBroker
from xasset.lab.live.clock import Clock
from xasset.lab.live.events import MarketCache, Quote, Trade
from xasset.lab.live.feeds import BinanceSpotFeed, Feed, HyperliquidFeed
from xasset.lab.live.recorder import BarRecorder
from xasset.lab.live.warmup import Warmup, prepare_archive, warmup_frame
from xasset.lab.portfolio import Position
from xasset.lab.runtime import RunSettings, Runtime
from xasset.lab.strategies import resolve
from xasset.lab.strategy import Strategy
from xasset.lab.universe import Book, Universe, load_book, load_universe
from xasset.store.writer import atomic_path, writer_lock

HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
USER_AGENT = {"User-Agent": "xasset/0.1 paper-desk"}
STALL = timedelta(seconds=20)
FEEDS: dict[str, type[Feed]] = {"binance": BinanceSpotFeed, "hyperliquid": HyperliquidFeed}


class BookSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    book: Path
    basis: Basis
    warmup: Warmup


class DeskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    universe: Path
    books: list[BookSpec] = Field(min_length=1)
    warmup_days: int = Field(default=62, ge=1, le=400)
    snapshot_seconds: float = Field(default=2.0, gt=0)
    flush_minutes: int = Field(default=5, ge=1)


def resolve_path(base: Path, path: Path) -> Path:
    return path if path.is_absolute() else base / path


def load_desk(path: Path) -> tuple[DeskConfig, Universe, list[tuple[BookSpec, Book]]]:
    config = DeskConfig.model_validate(yaml.safe_load(path.read_text()))
    universe_path = resolve_path(path.parent, config.universe).resolve()
    universe = load_universe(universe_path)
    for item in universe.instruments:
        if item.live_feed not in FEEDS or item.live_symbol is None:
            raise ValueError(
                f"{item.id}: the paper desk supports Binance spot and Hyperliquid feeds"
            )
    books = []
    for spec in config.books:
        book_path = resolve_path(path.parent, spec.book)
        book, _ = load_book(book_path)
        if resolve_path(book_path.parent, book.universe).resolve() != universe_path:
            raise ValueError(f"{book.id} must use the desk universe {universe_path.name}")
        books.append((spec, book))
    executions = {
        (b.execution.bar_settlement_ms, b.execution.max_quote_age_seconds) for _, b in books
    }
    if len(executions) != 1:
        raise ValueError("Books on one desk share bar settlement and maximum quote age")
    if len({book.id for _, book in books}) != len(books):
        raise ValueError("Book IDs on a desk must be unique")
    return config, universe, books


def write_atomic(path: Path, value: Any, attempts: int = 6) -> None:
    """Atomic JSON write; retries while a Windows reader briefly holds the file."""
    payload = json.dumps(value, separators=(",", ":"), default=str)
    for attempt in range(attempts):
        try:
            with atomic_path(path) as temporary:
                temporary.write_text(payload, encoding="utf-8")
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.03 * (attempt + 1))


class LiveLedger(Ledger):
    """Writes every record durably; keeps recent records and counters in memory."""

    def __init__(self, sink: JsonlSink, recent: int = 400):
        super().__init__(sink=sink)
        self.recent_events: deque[dict[str, Any]] = deque(maxlen=recent)
        self.recent_orders: deque[dict[str, Any]] = deque(maxlen=recent)
        self.recent_fills: deque[dict[str, Any]] = deque(maxlen=recent)
        self.counts: Counter[tuple[str, str]] = Counter()
        self.reasons: Counter[tuple[str, str]] = Counter()
        self.last_mark: tuple[datetime, float] | None = None

    def event(
        self,
        at: datetime,
        strategy: str,
        symbol: str,
        setup: str,
        event: str,
        state: str,
        **detail: Any,
    ) -> None:
        record = {
            "at": iso(at),
            "strategy": strategy,
            "symbol": symbol,
            "setup": setup,
            "event": event,
            "state": state,
            "detail": detail,
        }
        self.recent_events.append(record)
        self.counts[(strategy, event)] += 1
        if event == "expired":
            self.reasons[(strategy, str(detail.get("reason")))] += 1
        self.emit("events", record)

    def event_counts(self, strategy: str) -> Counter[str]:
        return Counter({e: n for (s, e), n in self.counts.items() if s == strategy})

    def expiry_reasons(self, strategy: str) -> Counter[str]:
        return Counter({r: n for (s, r), n in self.reasons.items() if s == strategy})

    def order(self, record: dict[str, Any]) -> None:
        self.recent_orders.append(record)
        self.emit("orders", record)

    def fill(self, record: dict[str, Any]) -> None:
        self.recent_fills.append(record)
        self.emit("fills", record)

    def mark(self, at: datetime, value: float) -> None:
        self.last_mark = (at, value)


def position_json(position: Position) -> dict[str, Any]:
    data = asdict(position)
    for key in ("entry_time", "deadline", "signal_time", "decided_at"):
        data[key] = iso(data[key])
    return data


def position_from(data: dict[str, Any]) -> Position:
    names = {f.name for f in fields(Position)}
    values = {k: v for k, v in data.items() if k in names}
    for key in ("entry_time", "deadline", "signal_time", "decided_at"):
        values[key] = datetime.fromisoformat(values[key])
    return Position(**values)


class BookDesk:
    """One book on the desk: warm-up runtime, then live runtime with a quote broker."""

    def __init__(
        self,
        spec: BookSpec,
        book: Book,
        universe: Universe,
        directory: Path,
        cache: MarketCache,
        clock: Callable[[], datetime],
        prefix: str,
    ):
        self.spec = spec
        self.book = book
        self.universe = universe
        self.dir = directory
        self.cache = cache
        self.clock = clock
        self.runtime = Runtime(
            book, universe, resolve(book.strategies), RunSettings(spec.basis), Ledger(), prefix
        )
        self.runtime.broker = DiscardBroker(self.runtime.cancelled)
        self.ledger = LiveLedger(JsonlSink(directory))
        self.broker: QuoteBroker | None = None
        self.status = "waiting"  # waiting -> warming -> ready -> live | failed
        self.error: str | None = None
        self.queue: list[tuple[datetime, list[FlowBar]]] = []
        self.warm: dict[str, Any] = {"minutes": 0, "bars": 0, "from": None, "to": None}
        self.last_end: datetime | None = None
        self.live_since: datetime | None = None
        self.restored: dict[str, Any] | None = None

    # --- warm-up (worker thread) ---------------------------------------------------
    def warm_step(self, end: datetime, bars: list[FlowBar]) -> None:
        """Advance calibrations, session statistics and models without trading.

        Only strategies that override ``evaluate`` keep cross-session statistics there
        (strategy 10's thin-session and opening volumes); the rest hold nothing but
        intraday setups, which are discarded at hand-over, so they are not evaluated.
        """
        runtime = self.runtime
        if not runtime.advance(end):
            return
        runtime.add_bars(end, bars)
        decided = max(bar.available_at for bar in bars) if bars else end
        for strategy in runtime.strategies:
            if type(strategy).evaluate is not Strategy.evaluate:
                for candidate in strategy.evaluate(decided):
                    strategy.release(candidate.setup, "cancelled", "warm-up")

    def warm_up(self, frame: pl.DataFrame, halt: threading.Event | None = None) -> None:
        if frame.height:
            self.warm["from"] = iso(frame["ts_end"].min())  # type: ignore[arg-type]
        for end, batch in minutes(frame):
            if halt is not None and halt.is_set():
                return  # the desk is shutting down
            self.warm_step(end, batch)
            self.last_end = end
            self.warm["minutes"] += 1
            self.warm["bars"] += len(batch)
        self.warm["to"] = iso(self.last_end)

    # --- loop thread ------------------------------------------------------------------
    def bounds(self) -> tuple[datetime, datetime] | None:
        session = self.runtime.market.session
        if session is None or self.universe.calendar is None:
            return None
        return session.open, session.close

    def _advance(self, end: datetime, bars: list[FlowBar]) -> None:
        if self.last_end is not None and end <= self.last_end:
            return  # Already covered by warm-up history.
        self.runtime.step(end, bars)
        self.last_end = end

    def go_live(self, now: datetime) -> None:
        for end, bars in self.queue:
            if self.last_end is None or end > self.last_end:
                self.warm_step(end, bars)  # Still warm-up: nothing trades.
                self.last_end = end
        self.queue.clear()
        self.runtime.adopt_ledger(self.ledger)
        self.runtime.reset_setups()
        broker = QuoteBroker(
            self.runtime.accounting, self.cache, self.spec.basis, self.clock, self.bounds
        )
        broker.on_cancel = self.runtime.cancelled
        self.runtime.broker = broker
        self.broker = broker
        self.restore(now)
        self.status = "live"
        self.live_since = now
        self.ledger.event(now, "desk", "*", "-", "live", "LIVE", restored=self.restored)

    def step(self, end: datetime, bars: list[FlowBar]) -> None:
        if self.status in {"waiting", "warming", "ready"}:
            self.queue.append((end, bars))
            return
        if self.status != "live":
            return
        self._advance(end, bars)
        mark = self.ledger.last_mark
        portfolio = self.runtime.portfolio
        if mark is not None and mark[0] == end and self.ledger.sink is not None:
            self.ledger.sink(
                "nav",
                {
                    "at": iso(end),
                    "nav": mark[1],
                    "cash": portfolio.cash,
                    "gross": portfolio.gross(),
                    "positions": len(portfolio.positions),
                },
            )
        self.save_state(now=end)

    # --- persistence ---------------------------------------------------------------------
    def save_state(self, now: datetime) -> None:
        if self.broker is None:
            return
        portfolio = self.runtime.portfolio
        write_atomic(
            self.dir / "state.json",
            {
                "book": self.book.id,
                "saved_at": iso(now),
                "cash": portfolio.cash,
                "positions": [position_json(p) for p in portfolio.positions.values()],
                "marks": dict(portfolio.marks),
                "pending": [
                    {
                        "order": e.order.id,
                        "setup": e.order.candidate.setup.id,
                        "strategy": e.order.candidate.setup.strategy,
                        "symbol": e.order.candidate.symbol,
                    }
                    for e in self.broker.entries
                ],
            },
        )

    def restore(self, now: datetime) -> None:
        path = self.dir / "state.json"
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        portfolio = self.runtime.portfolio
        positions = [position_from(item) for item in data.get("positions", [])]
        known = {item.id for item in self.universe.instruments}
        for position in positions:
            if position.symbol not in known or position.strategy not in self.runtime.by_id:
                raise ValueError(
                    f"Saved position {position.symbol}/{position.strategy} does not belong to "
                    f"this book; move {self.dir} aside to start the book fresh"
                )
        portfolio.cash = float(data["cash"])
        for position in positions:
            portfolio.positions[position.symbol] = position
            portfolio.marks[position.symbol] = float(
                data.get("marks", {}).get(position.symbol, position.entry_price)
            )
        for pending in data.get("pending", []):
            self.ledger.order(
                {
                    **pending,
                    "status": "cancelled",
                    "reason": "desk restarted before the order filled",
                    "at": iso(now),
                }
            )
        self.restored = {
            "saved_at": data.get("saved_at"),
            "positions": len(positions),
            "cancelled_orders": len(data.get("pending", [])),
            "cash": portfolio.cash,
        }

    # --- reporting --------------------------------------------------------------------------
    def calibration(self) -> dict[str, Any]:
        market = self.runtime.market
        benchmark = self.universe.benchmark
        reference = market.cal[(benchmark, "R10")]
        lag = market.cal[(benchmark, "R1")]
        sessions = len(reference.history)
        return {
            "sessions": sessions,
            "required": self.book.calibration_sessions,
            "lag_sessions": len(lag.history),
            "lag_required": 3 * self.book.calibration_sessions,
            "ready": sessions >= self.book.calibration_sessions,
            "session": market.session.key if market.session else None,
        }

    def snapshot(self, now: datetime) -> dict[str, Any]:
        portfolio = self.runtime.portfolio
        positions = []
        for position in portfolio.positions.values():
            mark = portfolio.marks.get(position.symbol, position.entry_price)
            positions.append(
                {
                    **position_json(position),
                    "mark": mark,
                    "unrealized": position.direction * position.qty * (mark - position.entry_price),
                    "exiting": self.broker.exits[position.symbol].reason
                    if self.broker and position.symbol in self.broker.exits
                    else None,
                }
            )
        setups = []
        if self.status == "live":
            for strategy in self.runtime.strategies:
                for setup in strategy.setups.values():
                    setups.append(
                        {
                            "id": setup.id,
                            "strategy": setup.strategy,
                            "symbol": setup.symbol,
                            "state": setup.state,
                            "armed_at": iso(setup.armed_at),
                        }
                    )
        counts: dict[str, dict[str, int]] = {}
        for (sid, event), count in self.ledger.counts.items():
            counts.setdefault(sid, {})[event] = count
        trades = self.ledger.trades
        last = self.ledger.last_mark
        return {
            "id": self.book.id,
            "basis": self.spec.basis,
            "warmup": self.spec.warmup,
            "strategies": [
                {
                    "id": s.id,
                    "title": s.title,
                    # A mirror's own direction is that of the rule it inverts.
                    "direction": "short"
                    if getattr(s, "real_direction", s.direction) < 0
                    else "long",
                    "targets": s.targets(),
                    "cooling": sorted(
                        symbol
                        for symbol, until in s.cooldown_until.items()
                        if self.status == "live" and until > now
                    ),
                }
                for s in self.runtime.strategies
            ],
            "status": self.status,
            "error": self.error,
            "warm": self.warm,
            "queued": len(self.queue),
            "live_since": iso(self.live_since),
            "restored": self.restored,
            "calibration": self.calibration(),
            "initial_nav": self.book.limits.initial_nav,
            "nav": last[1] if last else portfolio.nav(),
            "cash": portfolio.cash,
            "gross": portfolio.gross(),
            "positions": positions,
            "pending": [
                {
                    "order": e.order.id,
                    "strategy": e.order.candidate.setup.strategy,
                    "symbol": e.order.candidate.symbol,
                    "side": "buy" if e.order.candidate.direction > 0 else "sell",
                    "qty": e.order.qty,
                    "eligible": iso(e.eligible),
                }
                for e in (self.broker.entries if self.broker else [])
            ],
            "setups": setups,
            "counts": counts,
            "session_trades": len(trades),
            "session_net": sum(t.net for t in trades),
            "recent_events": list(self.ledger.recent_events)[-60:],
        }


class PaperDesk:
    def __init__(self, root: Path, config_path: Path):
        self.root = root
        self.config, self.universe, specs = load_desk(config_path)
        self.dir = root / "lab" / "paper" / self.config.id
        self.clock = Clock()
        self.cache = MarketCache()
        self.started = datetime.now(UTC)
        prefix = f"{self.started:%y%m%d%H%M%S}-"
        execution = specs[0][1].execution
        self.settlement = timedelta(milliseconds=execution.bar_settlement_ms)
        self.max_age = timedelta(seconds=execution.max_quote_age_seconds)
        self.books = [
            BookDesk(
                spec, book, self.universe, self.dir / book.id, self.cache, self.clock.now, prefix
            )
            for spec, book in specs
        ]
        items = self.universe.instruments
        self.aggregator = Aggregator(
            {item.id: f"paper-{item.live_feed}" for item in items},
            self.cache,
            self.max_age,
            self.started,
        )
        self.recorder = BarRecorder(root, {item.id: str(item.live_symbol) for item in items})
        self.feeds: list[Feed] = []
        for name, feed_type in FEEDS.items():
            symbols = {str(i.live_symbol): i.id for i in items if i.live_feed == name}
            if symbols:
                self.feeds.append(feed_type(symbols, self.clock.now))
        self.feed_health = {feed.name: FeedHealth(feed.name) for feed in self.feeds}
        self.coin = {i.id: str(i.live_symbol) for i in items if i.live_feed == "hyperliquid"}
        self.notes: deque[dict[str, Any]] = deque(maxlen=50)
        self.last_bar: datetime | None = None
        self.stop = asyncio.Event()
        self.halt = threading.Event()  # tells the warm-up thread to stop early
        self.warm_message = ""

    def note(self, level: str, message: str) -> None:
        self.notes.append({"at": iso(self.clock.now()), "level": level, "message": message})

    def live_books(self) -> list[BookDesk]:
        return [b for b in self.books if b.status == "live" and b.broker is not None]

    # --- feed sink ------------------------------------------------------------------
    def feed_connected(self, feed: str, symbols: list[str]) -> None:
        now = self.clock.now()
        health = self.feed_health[feed]
        if not health.connected:
            health.connects += 1
        health.connected = True
        health.last_message = now
        self.aggregator.connected(symbols, now)

    def feed_disconnected(self, feed: str, symbols: list[str], error: str | None) -> None:
        health = self.feed_health[feed]
        health.connected = False
        health.last_error = error
        self.aggregator.disconnected(symbols, self.clock.now())
        if error and not self.stop.is_set():
            self.note("warning", f"{feed} disconnected: {error}")

    def on_trade(self, feed: str, trade: Trade) -> None:
        health = self.feed_health[feed]
        health.last_message = trade.received
        since = self.aggregator.health[trade.symbol].covered_since
        if since is not None and trade.exchange_time >= since:
            health.lags.append((trade.received - trade.exchange_time).total_seconds() * 1000)
        if self.aggregator.on_trade(trade):
            for book in self.live_books():
                assert book.broker is not None
                book.broker.on_trade(trade)

    def on_quote(self, feed: str, quote: Quote) -> None:
        self.feed_health[feed].last_message = quote.received
        if self.aggregator.health[quote.symbol].covered_since is None:
            return
        self.aggregator.on_quote(quote)
        for book in self.live_books():
            assert book.broker is not None
            book.broker.on_quote(quote)

    # --- warm-up ----------------------------------------------------------------------
    def _warm_all(self) -> None:
        now = self.clock.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=self.config.warmup_days
        )
        tails: dict[str, pl.DataFrame] = {}
        if any(b.spec.warmup == "archive" for b in self.books):
            with httpx.Client(timeout=60, headers=USER_AGENT, follow_redirects=True) as client:
                for item in self.universe.instruments:
                    if item.history != "binance-spot" or self.halt.is_set():
                        continue

                    def progress(message: str) -> None:
                        self.warm_message = f"downloading {message}"

                    tails[item.id] = prepare_archive(
                        client, self.root, item.archive_symbol, start, now, progress
                    )
        for book in self.books:
            if self.halt.is_set():
                return
            book.status = "warming"
            self.warm_message = f"replaying history for {book.book.id}"
            frame = warmup_frame(
                self.root, self.universe, book.spec.warmup, start, now, tails, self.settlement
            )
            book.warm_up(frame, self.halt)
            if self.halt.is_set():
                return
            book.status = "ready"
        self.warm_message = ""

    async def warm_all(self) -> None:
        try:
            await asyncio.to_thread(self._warm_all)
        except Exception as exc:  # A failed warm-up must be visible, never silent.
            for book in self.books:
                if book.status != "live":
                    book.status = "failed"
                    book.error = f"warm-up failed: {type(exc).__name__}: {exc}"[:300]
            self.note("error", f"warm-up failed: {type(exc).__name__}: {exc}"[:300])

    # --- funding -------------------------------------------------------------------------
    def funding_exposures(self, at: datetime) -> list[tuple[BookDesk, FundingExposure]]:
        output = []
        for book in self.live_books():
            assert book.broker is not None
            for exposure in book.broker.funding_snapshot(at):
                output.append((book, exposure))
            # Positions that closed in the instant since the funding time still owe it.
            for trade in reversed(book.ledger.trades):
                if trade.exit_time < at:
                    break
                if trade.entry_time < at and trade.symbol in self.coin:
                    output.append((book, exposure_from(trade, self.cache, at, self.max_age)))
        return output

    async def funding(self, client: httpx.AsyncClient, at: datetime) -> None:
        exposures = self.funding_exposures(at)
        if not exposures:
            return
        coins = {self.coin[e.symbol] for _, e in exposures if e.symbol in self.coin}
        rates: dict[str, float] = {}
        for attempt in range(60):
            await asyncio.sleep(5 if attempt == 0 else 10)
            for coin in sorted(coins - rates.keys()):
                with contextlib.suppress(httpx.HTTPError, ValueError, KeyError, TypeError):
                    response = await client.post(
                        HYPERLIQUID_INFO,
                        json={
                            "type": "fundingHistory",
                            "coin": coin,
                            "startTime": int((at - timedelta(minutes=5)).timestamp() * 1000),
                        },
                        timeout=10,
                    )
                    response.raise_for_status()
                    for row in response.json():
                        stamp = datetime.fromtimestamp(int(row["time"]) / 1000, tz=UTC)
                        if abs((stamp - at).total_seconds()) <= 60:
                            rates[coin] = float(row["fundingRate"])
            if rates.keys() >= coins:
                break
        for book, exposure in exposures:
            name = self.coin.get(exposure.symbol)
            if name is not None and name in rates and book.broker is not None:
                book.broker.apply_funding(exposure, rates[name], at)
            else:
                self.note("error", f"funding rate for {name} at {iso(at)} unavailable; not applied")

    # --- snapshot ---------------------------------------------------------------------------
    def snapshot(self, now: datetime) -> dict[str, Any]:
        symbols = []
        for item in self.universe.instruments:
            health = self.aggregator.health[item.id]
            quote = self.cache.latest(item.id)
            symbols.append(
                {
                    "id": item.id,
                    "feed": item.live_feed,
                    "kind": item.kind,
                    "venue_symbol": item.live_symbol,
                    "covered": health.covered_since is not None,
                    "bid": quote.bid if quote else None,
                    "ask": quote.ask if quote else None,
                    "spread_bps": round(quote.spread_rel * 10_000, 4)
                    if quote and quote.valid
                    else None,
                    "quote_age": round((now - quote.received).total_seconds(), 2)
                    if quote
                    else None,
                    "trade_age": round((now - health.last_trade).total_seconds(), 2)
                    if health.last_trade
                    else None,
                    "bars": health.bars,
                    "skipped": health.skipped,
                    "late_prints": health.late_prints,
                    "invalid_quotes": health.invalid_quotes,
                }
            )
        return {
            "id": self.config.id,
            "universe": self.universe.id,
            "pid": os.getpid(),
            "started_at": iso(self.started),
            "updated_at": iso(now),
            "last_bar": iso(self.last_bar),
            "clock": self.clock.status(),
            "warm_message": self.warm_message,
            "feeds": [
                {
                    "name": h.name,
                    "connected": h.connected,
                    "connects": h.connects,
                    "last_message_age": round((now - h.last_message).total_seconds(), 2)
                    if h.last_message
                    else None,
                    "last_error": h.last_error,
                    "lag": h.lag(),
                }
                for h in self.feed_health.values()
            ],
            "symbols": symbols,
            "books": [book.snapshot(now) for book in self.books],
            "recorder": {"written": self.recorder.written, "error": self.recorder.error},
            "notes": list(self.notes),
            "orders_enabled": False,
        }

    def write_snapshot(self, now: datetime, final: bool = False) -> None:
        data = self.snapshot(now)
        data["running"] = not final
        with contextlib.suppress(OSError):
            write_atomic(self.dir / "desk.json", data)

    # --- main loop -----------------------------------------------------------------------
    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        """Ctrl+C (and SIGTERM, or Ctrl+Break on Windows) stop the desk gracefully.

        Handlers are installed explicitly because a process started in the background
        can inherit an ignored SIGINT. A second Ctrl+C interrupts immediately.
        """

        def request_stop(number: int, frame: object) -> None:
            loop.call_soon_threadsafe(self.stop.set)
            if number == signal.SIGINT:
                signal.signal(signal.SIGINT, signal.default_int_handler)

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            number = getattr(signal, name, None)
            if number is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(number, request_stop)

    def tick(self, now: datetime) -> None:
        for book in self.books:
            if book.status == "ready":
                book.go_live(now)
                self.note("info", f"{book.book.id} is live")
        for book in self.live_books():
            assert book.broker is not None
            book.broker.process(now)
        due = self.aggregator.emitted_until + MINUTE + self.settlement
        if now - due > STALL:
            # The process or machine stalled: never emit minutes that were not observed.
            self.aggregator.skip_to(floor_minute(now))
            for health in self.aggregator.health.values():
                if health.covered_since is not None:
                    health.covered_since = max(health.covered_since, now)
            self.note("warning", "event loop stalled; unobserved minutes were skipped")
        while (end := self.aggregator.due(now, self.settlement)) is not None:
            bars = self.aggregator.close(end, now)
            self.recorder.add(bars)
            for book in self.books:
                book.step(end, bars)
            self.last_bar = end

    async def run(self, duration: timedelta | None = None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with writer_lock(self.dir):
            self.stop = asyncio.Event()
            self.install_signal_handlers(asyncio.get_running_loop())
            async with httpx.AsyncClient(headers=USER_AGENT) as client:
                await self.clock.measure(client)
                if self.clock.error:
                    self.note("warning", self.clock.error + "; using the local clock")
                self.started = self.clock.now()
                self.aggregator.skip_to(floor_minute(self.started))
                tasks = [asyncio.create_task(feed.run(self, self.stop)) for feed in self.feeds]
                tasks.append(asyncio.create_task(self.warm_all()))
                background: set[asyncio.Task[None]] = set()
                try:
                    await self._loop(client, duration, background)
                finally:
                    self.stop.set()
                    self.halt.set()
                    for task in background:
                        task.cancel()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(
                            asyncio.gather(*tasks, return_exceptions=True), timeout=10
                        )
                    now = self.clock.now()
                    for book in self.live_books():
                        book.save_state(now)
                    if all(b.status != "warming" for b in self.books):
                        self.recorder.flush()
                    self.write_snapshot(now, final=True)

    async def _loop(
        self,
        client: httpx.AsyncClient,
        duration: timedelta | None,
        background: set[asyncio.Task[None]],
    ) -> None:
        started = self.clock.now()
        next_snapshot = started
        next_flush = started + timedelta(minutes=self.config.flush_minutes)
        next_clock = started + timedelta(minutes=30)
        next_funding = started.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

        def keep(task: asyncio.Task[None]) -> None:
            background.add(task)
            task.add_done_callback(background.discard)

        while not self.stop.is_set():
            now = self.clock.now()
            self.tick(now)
            if now >= next_funding:
                keep(asyncio.create_task(self.funding(client, next_funding)))
                next_funding += timedelta(hours=1)
            if now >= next_clock:
                keep(asyncio.create_task(self.clock.measure(client)))
                next_clock += timedelta(minutes=30)
            if now >= next_flush and all(b.status != "warming" for b in self.books):
                self.recorder.flush()
                next_flush = now + timedelta(minutes=self.config.flush_minutes)
            if now >= next_snapshot:
                self.write_snapshot(now)
                next_snapshot = now + timedelta(seconds=self.config.snapshot_seconds)
            if duration is not None and now - started >= duration:
                return
            await asyncio.sleep(0.1)


def exposure_from(
    trade: TradeRecord, cache: MarketCache, at: datetime, max_age: timedelta
) -> FundingExposure:
    quote = cache.quote_at(trade.symbol, at, max_age)
    mark = quote.mid if quote is not None and quote.valid else trade.exit_price
    return FundingExposure(trade.symbol, trade.setup, trade.direction, trade.qty, mark)


def run_desk(root: Path, config: Path, minutes_to_run: float | None = None) -> None:
    desk = PaperDesk(root, config)
    duration = None if minutes_to_run is None else timedelta(minutes=minutes_to_run)
    try:
        asyncio.run(desk.run(duration))
    except KeyboardInterrupt:
        pass


__all__ = ["PaperDesk", "load_desk", "run_desk", "trade_json"]
