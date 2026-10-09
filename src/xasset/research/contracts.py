"""Typed requests and auditable results for the native xasset engine."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

from xasset.config import Instrument
from xasset.normalize.timebase import utc
from xasset.research.costs import Costs
from xasset.research.experiment import Experiment

Parameter = str | int | float | bool


class StrictResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class DailyResult(StrictResult):
    day: date
    net_pnl: float
    portfolio_return: float


class Fold(StrictResult):
    id: str
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    parameters: dict[str, Parameter]

    @field_validator("train_start", "train_end", "test_start", "test_end")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return utc(value)


class Trade(StrictResult):
    id: str
    symbol: str
    fold: str
    signal_at: datetime
    entry_at: datetime
    exit_at: datetime
    entry_bar_end: datetime
    exit_bar_end: datetime
    entry_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    exit_reason: Literal["stop", "target", "time", "fold_end", "session_close"]
    units: float = Field(gt=0)
    entry_notional: float = Field(gt=0)
    exit_notional: float = Field(gt=0)
    gross_pnl: float
    cost: float = Field(ge=0)

    @field_validator("signal_at", "entry_at", "exit_at", "entry_bar_end", "exit_bar_end")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return utc(value)

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.cost


class CheckEvidence(StrictResult):
    passed: bool | None = None
    reference: str = Field(min_length=1, description="Engine test/audit artifact identifier")


class Scenario(StrictResult):
    multiplier: Literal[1, 2]
    daily: list[DailyResult]
    trades: list[Trade]
    folds: list[Fold]
    dsr_probability: float | None = Field(default=None, ge=0, le=1)
    pbo_probability: float | None = Field(default=None, ge=0, le=1)
    trial_count_used: int = Field(ge=1)
    checks: dict[str, CheckEvidence] = Field(default_factory=dict)


class ValidationResult(StrictResult):
    engine: Literal["xasset"]
    engine_version: str = Field(min_length=1)
    strategy_revision: str = Field(min_length=7)
    currency: str
    selected_parameters: dict[str, Parameter]
    scenarios: list[Scenario]
    diagnostics: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class Request:
    contract_version: int
    experiment: Experiment
    instruments: list[Instrument]
    costs: Costs
    bars: pl.DataFrame
    phase: Literal["discovery", "vault"]
    trial_count: int
    data_start: datetime
    data_end: datetime
    frozen_parameters: dict[str, Parameter] | None
    # A vault request contains only sealed test data and frozen parameters.
    discovery_result: dict[str, Any] | None
