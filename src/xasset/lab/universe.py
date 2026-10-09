"""Point-in-time instrument metadata and book configuration for the strategy lab."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

Kind = Literal["spot", "perp", "equity", "etf"]


class LabInstrument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    id: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_.-]{0,63}$")
    kind: Kind
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3,5}$")
    tick: float = Field(gt=0, description="Price tick in quote units")
    lot: float = Field(gt=0, description="Quantity step in base units")
    calendar: str | None = Field(
        default=None, description="Exchange calendar; None means UTC-day sessions"
    )
    cluster: str = Field(min_length=1, description="Economic cluster for exposure limits")
    sector: str | None = Field(
        default=None, description="Instrument whose returns enter the residual model"
    )
    cost_class: str = Field(min_length=1)
    shortable: bool = False
    breadth_member: bool = Field(
        default=True, description="Counts in peer and breadth sets (one entry per underlying)"
    )
    # Historical source: lab bar store ("binance-spot", "binance-um") or the canonical
    # store ("store") identified by ``store_id``.
    history: str = Field(min_length=1)
    store_id: str | None = None
    # Symbol under which the lab bar store files this instrument's history (for example
    # the Binance USD-M symbol of a perpetual). Defaults to ``id``.
    history_symbol: str | None = None
    # Live feed symbols; one live feed per instrument.
    live_feed: Literal["binance", "hyperliquid", "kraken", "coinbase", "alpaca"] | None = None
    live_symbol: str | None = None

    @property
    def archive_symbol(self) -> str:
        return self.history_symbol or self.id

    @model_validator(mode="after")
    def valid(self) -> LabInstrument:
        if self.history == "store" and not self.store_id:
            raise ValueError("Canonical-store history requires store_id")
        if self.history == "store" and self.history_symbol:
            raise ValueError("Canonical-store history is located by store_id")
        if self.kind == "perp" and not self.shortable:
            raise ValueError("Perpetual contracts are shortable by construction")
        if (self.live_feed is None) != (self.live_symbol is None):
            raise ValueError("Live feed and live symbol must be configured together")
        return self


class Pair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    leader: str
    laggards: list[str] = Field(min_length=1)


class Universe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    description: str = Field(min_length=10)
    benchmark: str
    instruments: list[LabInstrument] = Field(min_length=2)
    minimum_peers: int = Field(default=20, ge=1)
    pairs: list[Pair] = Field(default_factory=list)
    index_constituents: dict[str, list[str]] = Field(default_factory=dict)
    handoff_calendar: str | None = Field(
        default=None,
        description="Calendar whose scheduled open defines the main-session handoff (strategy 10)",
    )
    selection_note: str = Field(min_length=10)
    # Point-in-time membership: month ("YYYY-MM") -> instruments eligible to trade and
    # to count in breadth that month. Empty means every instrument, always.
    membership: dict[str, list[str]] = Field(default_factory=dict)
    _index: dict[str, LabInstrument] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def consistent(self) -> Universe:
        ids = [item.id for item in self.instruments]
        if len(ids) != len(set(ids)):
            raise ValueError("Instrument IDs must be unique")
        known = set(ids)
        if self.benchmark not in known:
            raise ValueError("Benchmark must be a universe member")
        for item in self.instruments:
            if item.sector is not None and item.sector not in known:
                raise ValueError(f"Unknown sector factor {item.sector}")
        for pair in self.pairs:
            if pair.leader not in known or set(pair.laggards) - known:
                raise ValueError("Pairs must reference universe members")
            if pair.leader in pair.laggards:
                raise ValueError("A leader cannot be its own laggard")
        for index, members in self.index_constituents.items():
            if index not in known or set(members) - known or index in members:
                raise ValueError("Index constituents must be universe members")
        calendars = {item.calendar for item in self.instruments}
        if len(calendars) != 1:
            raise ValueError("One book uses one session definition; split mixed calendars")
        for month, members in self.membership.items():
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
                raise ValueError(f"Membership month must be YYYY-MM: {month}")
            if set(members) - known:
                raise ValueError(f"Membership for {month} names unknown instruments")
            if self.benchmark not in members:
                raise ValueError(f"The benchmark must be a member in {month}")
        return self

    def get(self, symbol: str) -> LabInstrument:
        if not self._index:
            self._index.update({item.id: item for item in self.instruments})
        return self._index[symbol]

    def members(self, month: str) -> frozenset[str] | None:
        """Instruments eligible in ``month`` (YYYY-MM); None when membership is not used.

        A month outside the declared schedule has no members, so nothing trades there.
        """
        if not self.membership:
            return None
        return frozenset(self.membership.get(month, ()))

    def loaded(self, month: str) -> frozenset[str] | None:
        """Instruments whose bars a replay must load in ``month``.

        Members of the month itself, of the previous month (positions opened just
        before the month boundary must still exit) and of the next two months
        (calibrations need 20 prior sessions; pair models need 60). None means all.
        """
        if not self.membership:
            return None
        year, number = (int(part) for part in month.split("-"))
        names: set[str] = {self.benchmark}
        for offset in (-1, 0, 1, 2):
            index = year * 12 + number - 1 + offset
            names |= set(self.membership.get(f"{index // 12:04d}-{index % 12 + 1:02d}", ()))
        return frozenset(names)

    @property
    def calendar(self) -> str | None:
        return self.instruments[0].calendar


class CostClass(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    half_spread_bps: float = Field(ge=0, description="Half the quoted spread, charged per side")
    impact_bps: float = Field(ge=0, description="Adverse impact per side")
    fee_bps: float = Field(ge=0)
    fee_per_unit: float = Field(default=0, ge=0)
    minimum_fee: float = Field(default=0, ge=0)
    evidence: str = Field(min_length=10)


class PortfolioLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    initial_nav: float = Field(default=100_000, gt=0)
    risk_per_trade: float = Field(default=0.001, gt=0, le=0.05)
    max_single_notional: float = Field(default=0.10, gt=0, le=1)
    max_participation: float = Field(default=0.01, gt=0, le=1)
    max_gross: float = Field(default=1.0, gt=0, le=10)
    max_aggregate_risk: float = Field(default=0.01, gt=0, le=1)
    max_cluster_gross: float = Field(default=0.25, gt=0, le=10)


class ExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    decision_latency_ms: int = Field(default=250, ge=0)
    transport_latency_ms: int = Field(default=150, ge=0)
    bar_settlement_ms: int = Field(
        default=1500, ge=0, description="Bar completion to availability in replay"
    )
    cooldown_minutes: int = Field(default=15, ge=0)
    max_quote_age_seconds: float = Field(default=5.0, gt=0)


class Book(BaseModel):
    """One shared-capital book: a universe, its strategies and frozen assumptions."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    universe: Path
    strategies: list[str] = Field(min_length=1, description="Strategy IDs in priority order")
    costs: dict[str, CostClass]
    limits: PortfolioLimits = Field(default_factory=PortfolioLimits)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    calibration_sessions: int = Field(default=20, ge=2)
    calibration_half_window: int = Field(default=15, ge=1)
    minimum_reference: int = Field(default=200, ge=20)


def load_universe(path: Path) -> Universe:
    return Universe.model_validate(yaml.safe_load(path.read_text()))


def load_book(path: Path) -> tuple[Book, Universe]:
    book = Book.model_validate(yaml.safe_load(path.read_text()))
    universe_path = book.universe if book.universe.is_absolute() else path.parent / book.universe
    universe = load_universe(universe_path)
    missing = {item.cost_class for item in universe.instruments} - book.costs.keys()
    if missing:
        raise ValueError(f"Book lacks cost classes: {', '.join(sorted(missing))}")
    return book, universe
