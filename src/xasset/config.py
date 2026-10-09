"""Validated pilot universe; internal IDs are also safe storage path segments."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Source = Literal["yahoo", "binance", "dukascopy", "alpaca", "kraken", "hyperliquid"]


class Instrument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_.-]{0,63}$")
    asset_class: Literal["equity", "etf", "futures", "fx", "crypto", "index", "cfd"]
    tier: Literal["A", "B", "C"]
    venue: str
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3,5}$")
    lot_size: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    calendar: str | None = None
    session: Literal["regular", "all"]
    yahoo_symbol: str | None = Field(default=None, min_length=1, max_length=64)
    binance_symbol: str | None = Field(default=None, pattern=r"^[A-Z0-9_]+$")
    binance_market: Literal["spot", "um"] = "spot"
    dukascopy_symbol: str | None = Field(default=None, pattern=r"^[A-Z0-9]+$")
    price_scale: int | None = Field(default=None, gt=0)
    alpaca_symbol: str | None = Field(default=None, pattern=r"^[A-Z0-9.-]+$")
    alpaca_feed: Literal["iex", "sip"] = "iex"
    kraken_symbol: str | None = Field(default=None, pattern=r"^[A-Z0-9/]+$")
    hyperliquid_symbol: str | None = Field(default=None, pattern=r"^[A-Z0-9]+$")
    sources: list[Source] = Field(default=["yahoo"])
    proxy: bool = False

    @model_validator(mode="after")
    def require_calendar(self) -> "Instrument":
        if self.session == "regular" and self.calendar is None:
            raise ValueError("regular-session instruments require a calendar")
        if not self.sources or len(set(self.sources)) != len(self.sources):
            raise ValueError("sources must be nonempty and unique, in priority order")
        for source in self.sources:
            if getattr(self, f"{source}_symbol") is None:
                raise ValueError(f"{source} requires its provider symbol mapping")
        if "dukascopy" in self.sources and self.price_scale is None:
            raise ValueError("Dukascopy requires an explicit instrument price_scale")
        return self


class Universe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruments: list[Instrument] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> "Universe":
        ids = [item.id for item in self.instruments]
        if len(ids) != len(set(ids)):
            raise ValueError("instrument IDs must be unique")
        return self


def load_universe(path: Path, symbols: list[str] | None = None) -> list[Instrument]:
    universe = Universe.model_validate(yaml.safe_load(path.read_text()))
    if symbols is None:
        return universe.instruments
    unknown = set(symbols) - {item.id for item in universe.instruments}
    if unknown:
        raise ValueError(f"Unknown instrument IDs: {', '.join(sorted(unknown))}")
    return [item for item in universe.instruments if item.id in symbols]
