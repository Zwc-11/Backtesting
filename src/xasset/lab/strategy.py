"""State-machine strategy contract shared by historical replay and live paper trading.

Each (strategy, instrument) pair holds at most one setup. States follow
IDLE -> ARMED -> (intermediate) -> CONFIRMED -> ORDERED -> OPEN -> COOLDOWN.
Anchors are frozen when observed and never move to a more attractive past point.
A strategy only sees the market state as of the bar it is evaluating; the runtime
owns sizing, limits, orders and fills.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Literal

if TYPE_CHECKING:
    from xasset.lab.ledger import Ledger
    from xasset.lab.market import Market
    from xasset.lab.universe import Book, Universe

Kind = Literal["spot", "perp", "equity", "etf"]


@dataclass
class Setup:
    id: str
    strategy: str
    symbol: str
    direction: int
    state: str
    armed_at: datetime
    armed_minute: int
    session: str
    anchors: dict[str, Any] = field(default_factory=dict)
    scratch: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Guard:
    """Pre-entry invalidation: cancel if ``symbol`` has traded beyond ``level``."""

    symbol: str
    side: Literal["below", "above"]
    level: float


@dataclass
class Candidate:
    setup: Setup
    symbol: str
    direction: int
    stop: float
    sigma10: float
    time_exit_minutes: int
    signal_end: datetime
    guards: list[Guard] = field(default_factory=list)
    target_multiple: float = 2.0


@dataclass(frozen=True)
class Requirements:
    """What the primary (quote) version needs; the catalog and runtime check it."""

    quotes: bool
    flow: bool
    peers: bool = False
    pairs: bool = False
    constituents: bool = False
    trade_vwap: bool = False
    shorting: bool = False
    handoff: bool = False
    notes: str = ""


class Strategy:
    id: ClassVar[str]
    title: ClassVar[str]
    direction: ClassVar[int] = 1
    kinds: ClassVar[tuple[Kind, ...]] = ("spot", "equity", "etf")
    requires: ClassVar[Requirements]
    time_exit_minutes: ClassVar[int] = 60
    trade_bar_variant: ClassVar[str] = (
        "Trade prices replace quote midpoints (separately registered trade-bar variant)."
    )

    def __init__(self, market: Market, ledger: Ledger, book: Book, universe: Universe):
        self.market = market
        self.ledger = ledger
        self.book = book
        self.universe = universe
        self.setups: dict[str, Setup] = {}
        self.cooldown_until: dict[str, datetime] = {}
        self.cooldown = timedelta(minutes=book.execution.cooldown_minutes)
        self.now: datetime | None = None  # decision time of the current evaluation
        self.prefix = ""  # set by the runtime so IDs stay unique across live restarts
        self._sequence = itertools.count(1)

    # --- universe -------------------------------------------------------------------
    def targets(self) -> list[str]:
        return [item.id for item in self.universe.instruments if item.kind in self.kinds]

    # --- lifecycle hooks used by the runtime -----------------------------------------
    def on_session(self) -> None:
        """A new session began; intraday setups never bridge a session boundary."""
        for setup in list(self.setups.values()):
            if setup.state not in {"ORDERED", "OPEN"}:
                self.expire(setup, "session ended")

    def evaluate(self, now: datetime) -> list[Candidate]:
        self.now = now
        output: list[Candidate] = []
        for symbol in self.targets():
            setup = self.setups.get(symbol)
            if setup is not None and setup.state in {"ORDERED", "OPEN"}:
                continue
            if setup is None:
                if self.cooling(symbol):
                    continue
                setup = self.arm(symbol)
                if setup is None:
                    continue
                # A setup armed on this bar may also progress on later bars only.
                continue
            candidate = self.step(setup)
            if candidate is not None:
                self.transition(setup, "ORDERED", "confirmed", stop=candidate.stop)
                output.append(candidate)
        return output

    def arm(self, symbol: str) -> Setup | None:
        raise NotImplementedError

    def step(self, setup: Setup) -> Candidate | None:
        raise NotImplementedError

    # --- helpers --------------------------------------------------------------------
    @property
    def minute(self) -> int:
        return self.market.minute

    @property
    def end(self) -> datetime:
        assert self.market.end is not None
        return self.market.end

    def cooling(self, symbol: str) -> bool:
        until = self.cooldown_until.get(symbol)
        return until is not None and self.end < until

    def new_setup(self, symbol: str, state: str, **anchors: Any) -> Setup:
        assert self.market.session is not None and self.now is not None
        setup = Setup(
            id=f"{self.prefix}{self.id}-{next(self._sequence):06d}",
            strategy=self.id,
            symbol=symbol,
            direction=self.direction,
            state=state,
            armed_at=self.end,
            armed_minute=self.minute,
            session=self.market.session.key,
            anchors=dict(anchors),
        )
        self.setups[symbol] = setup
        self.ledger.event(self.now, self.id, symbol, setup.id, "armed", state, **anchors)
        return setup

    def transition(self, setup: Setup, state: str, event: str, **detail: Any) -> None:
        assert self.now is not None
        setup.state = state
        self.ledger.event(self.now, self.id, setup.symbol, setup.id, event, state, **detail)

    def expire(self, setup: Setup, reason: str, **detail: Any) -> None:
        self.release(setup, "expired", reason, **detail)

    def release(self, setup: Setup, event: str, reason: str, **detail: Any) -> None:
        """End a setup (expiry, cancellation, rejection or exit) and start the cooldown."""
        at = self.now or self.end
        self.ledger.event(
            at, self.id, setup.symbol, setup.id, event, "COOLDOWN", reason=reason, **detail
        )
        if self.setups.get(setup.symbol) is setup:
            del self.setups[setup.symbol]
        self.cooldown_until[setup.symbol] = self.end + self.cooldown

    def candidate(
        self,
        setup: Setup,
        stop: float,
        sigma10: float,
        guards: list[Guard] | None = None,
        time_exit_minutes: int | None = None,
    ) -> Candidate:
        return Candidate(
            setup=setup,
            symbol=setup.symbol,
            direction=setup.direction,
            stop=stop,
            sigma10=sigma10,
            time_exit_minutes=time_exit_minutes or self.time_exit_minutes,
            signal_end=self.end,
            guards=guards or [],
        )

    def tick(self, symbol: str) -> float:
        return self.universe.get(symbol).tick

    def elapsed(self, setup: Setup) -> int:
        return self.minute - setup.armed_minute
