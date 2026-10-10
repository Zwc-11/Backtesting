"""Append-only records: every state transition, order, fill, trade and NAV mark."""

from __future__ import annotations

import json
import random
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass
class TradeRecord:
    id: str
    setup: str
    strategy: str
    symbol: str
    direction: int
    qty: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    stop: float
    target: float | None
    risk_distance: float  # realized directional log distance d at entry
    gross: float
    fees: float
    funding: float
    net: float
    entry_notional: float
    signal_time: datetime
    decided_at: datetime
    ambiguous_bar: bool = False
    alternative_exit_price: float | None = None
    execution_basis: str = "bar_open"

    @property
    def net_return(self) -> float:
        return self.net / self.entry_notional


@dataclass
class Ledger:
    events: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    nav: list[tuple[datetime, float]] = field(default_factory=list)
    sink: Callable[[str, dict[str, Any]], None] | None = None

    def emit(self, stream: str, record: dict[str, Any]) -> None:
        if self.sink is not None:
            self.sink(stream, record)

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
        self.events.append(record)
        self.emit("events", record)

    def order(self, record: dict[str, Any]) -> None:
        self.orders.append(record)
        self.emit("orders", record)

    def fill(self, record: dict[str, Any]) -> None:
        self.fills.append(record)
        self.emit("fills", record)

    def trade(self, record: TradeRecord) -> None:
        self.trades.append(record)
        self.emit("trades", trade_json(record))

    def mark(self, at: datetime, value: float) -> None:
        self.nav.append((at, value))

    def event_counts(self, strategy: str) -> Counter[str]:
        return Counter(e["event"] for e in self.events if e["strategy"] == strategy)

    def expiry_reasons(self, strategy: str) -> Counter[str]:
        return Counter(
            str(e["detail"].get("reason"))
            for e in self.events
            if e["strategy"] == strategy and e["event"] == "expired"
        )


TERMINAL_EVENTS = frozenset({"expired", "cancelled", "rejected", "exited"})


class CompactLedger(Ledger):
    """Replay ledger that keeps counts for every event but full records selectively.

    A setup's events are buffered while it is alive. When it ends, its full timeline
    is kept if it was confirmed (it produced an order: these are the trades and
    rejected or cancelled orders), otherwise only counters are updated, plus a small
    seeded random sample of expired setups per (strategy, reason) as examples of
    "why no entry". Memory stays proportional to confirmed setups, not to every arm.
    """

    def __init__(self, samples: int = 3, seed: int = 20260101):
        super().__init__()
        self.counts: Counter[tuple[str, str]] = Counter()
        self.reasons: Counter[tuple[str, str]] = Counter()
        self.alive: dict[str, list[dict[str, Any]]] = {}
        self.timelines: dict[str, list[dict[str, Any]]] = {}
        self.samples: dict[tuple[str, str], list[list[dict[str, Any]]]] = {}
        self._seen: Counter[tuple[str, str]] = Counter()
        self._capacity = samples
        self._random = random.Random(seed)

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
        self.counts[(strategy, event)] += 1
        trail = self.alive.setdefault(setup, [])
        trail.append(record)
        self.emit("events", record)
        if event not in TERMINAL_EVENTS:
            return
        del self.alive[setup]
        reason = str(detail.get("reason"))
        if event == "expired":
            self.reasons[(strategy, reason)] += 1
        if any(item["event"] == "confirmed" for item in trail):
            self.timelines[setup] = trail
        elif event == "expired":
            key = (strategy, reason)
            self._seen[key] += 1
            bucket = self.samples.setdefault(key, [])
            if len(bucket) < self._capacity:
                bucket.append(trail)
            else:  # reservoir sampling keeps a uniform sample over the whole run
                slot = self._random.randrange(self._seen[key])
                if slot < self._capacity:
                    bucket[slot] = trail

    def event_counts(self, strategy: str) -> Counter[str]:
        return Counter({e: n for (s, e), n in self.counts.items() if s == strategy})

    def expiry_reasons(self, strategy: str) -> Counter[str]:
        return Counter({r: n for (s, r), n in self.reasons.items() if s == strategy})


def trade_json(trade: TradeRecord) -> dict[str, Any]:
    data = asdict(trade)
    for key in ("entry_time", "exit_time", "signal_time", "decided_at"):
        data[key] = iso(data[key])
    data["net_return"] = trade.net_return
    return data


class JsonlSink:
    """Durable append-only JSON lines per stream for the live paper desk."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def __call__(self, stream: str, record: dict[str, Any]) -> None:
        with (self.root / f"{stream}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
