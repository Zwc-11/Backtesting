"""Append-only records: every state transition, order, fill, trade and NAV mark."""

from __future__ import annotations

import json
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
