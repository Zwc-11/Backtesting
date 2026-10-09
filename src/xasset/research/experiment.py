"""Immutable preregistration specifications. Loading this file reads no market data."""

import hashlib
import itertools
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from xasset.config import Source
from xasset.normalize.timebase import utc


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    minimum_days: int = Field(default=20, ge=2)
    minimum_daily_t: float = Field(default=2.0, ge=0)
    minimum_dsr: float = Field(default=0.95, gt=0, le=1)
    maximum_pbo: float = Field(default=0.10, ge=0, lt=1)


class ScheduledEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    at: datetime
    known_at: datetime
    reference: str = Field(min_length=10)

    @field_validator("at", "known_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        value = utc(value)
        if value.second or value.microsecond:
            raise ValueError("Event times must be whole aware minutes")
        return value

    @model_validator(mode="after")
    def announced(self) -> "ScheduledEvent":
        if self.known_at > self.at:
            raise ValueError("Scheduled events must have been known before their release")
        return self


class Experiment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    family: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    hypothesis: str = Field(min_length=20)
    strategy: str = Field(min_length=1)
    strategy_revision: str = Field(min_length=7)
    purpose: Literal["research", "smoke"] = "research"
    initial_capital: float = Field(default=100000, gt=0)
    allocation_fraction: float = Field(default=0.1, gt=0, le=1)
    symbols: list[str] = Field(min_length=1)
    start: datetime
    end: datetime
    frequency: Literal["1m", "5m", "1h", "1d"] = "5m"
    parameter_grid: dict[str, list[str | int | float | bool]]
    parameter_sets: list[dict[str, str | int | float | bool]] = Field(default_factory=list)
    max_holding_minutes: int = Field(gt=0)
    embargo_minutes: int = Field(gt=0)
    training_days: int = Field(gt=0)
    test_days: int = Field(gt=0)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    events: list[ScheduledEvent] = Field(default_factory=list)
    # Exact source requirements are frozen; canonical Yahoo fallbacks cannot
    # silently become Tier A experiment inputs.
    required_sources: dict[str, Source] = Field(default_factory=dict)
    minimum_coverage: float = Field(default=0, ge=0, le=1)
    prerequisites: list[str] = Field(default_factory=list)

    @field_validator("start", "end")
    @classmethod
    def aware_minutes(cls, value: datetime) -> datetime:
        value = utc(value)
        if value.second or value.microsecond:
            raise ValueError("Experiment bounds must be whole timezone-aware minutes")
        return value

    @model_validator(mode="after")
    def validate_design(self) -> "Experiment":
        if self.end - self.start < timedelta(minutes=10):
            raise ValueError("Experiment range must span at least ten minutes")
        if len(self.symbols) != len(set(self.symbols)):
            raise ValueError("Experiment symbols must be unique")
        if set(self.required_sources) - set(self.symbols):
            raise ValueError("Source requirements must name registered symbols")
        if len({event.id for event in self.events}) != len(self.events):
            raise ValueError("Event IDs must be unique")
        if self.embargo_minutes < self.max_holding_minutes:
            raise ValueError("Embargo must cover the longest holding period")
        if self.discovery_end <= self.start:
            raise ValueError("Embargo leaves no discovery interval")
        count = 1
        if self.parameter_sets and self.parameter_grid:
            raise ValueError("Use an explicit candidate list or a Cartesian grid, not both")
        if self.parameter_sets:
            if any(not candidate for candidate in self.parameter_sets):
                raise ValueError("Explicit candidates cannot be empty")
            if len({canonical_json(candidate) for candidate in self.parameter_sets}) != len(
                self.parameter_sets
            ):
                raise ValueError("Explicit candidates must be unique")
            count = len(self.parameter_sets)
        for key, values in self.parameter_grid.items():
            if (
                not key
                or not values
                or len({canonical_json(value) for value in values}) != len(values)
            ):
                raise ValueError("Grid names and values must be nonempty and unique")
            if any(isinstance(value, float) and not math.isfinite(value) for value in values):
                raise ValueError("Grid values must be finite")
            count *= len(values)
        if count > 10000:
            raise ValueError("Register at most 10,000 parameter combinations per family")
        return self

    @property
    def vault_start(self) -> datetime:
        # Reserve at least the last 20% of the declared elapsed-time range.
        minutes = int((self.end - self.start).total_seconds() // 60)
        return self.start + timedelta(minutes=minutes * 4 // 5)

    @property
    def discovery_end(self) -> datetime:
        return self.vault_start - timedelta(minutes=self.embargo_minutes)

    def parameters(self) -> list[dict[str, str | int | float | bool]]:
        if self.parameter_sets:
            return [dict(candidate) for candidate in self.parameter_sets]
        names = sorted(self.parameter_grid)
        return [
            dict(zip(names, values, strict=True))
            for values in itertools.product(*(self.parameter_grid[name] for name in names))
        ]


def load_experiment(path: Path) -> Experiment:
    return Experiment.model_validate(yaml.safe_load(path.read_text()))
