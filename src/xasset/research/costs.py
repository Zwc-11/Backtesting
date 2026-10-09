"""Frozen execution-cost assumptions for base and stressed native runs."""

import math
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class CostProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    commission_bps: float = Field(ge=0)
    commission_per_unit: float = Field(default=0, ge=0)
    minimum_commission: float = Field(default=0, ge=0)
    spread_bps: float = Field(ge=0, description="Full quoted spread, half charged per side")
    slippage_bps: float = Field(ge=0, description="Adverse slippage per side")
    calibrated: bool = False
    evidence: str = Field(min_length=10)

    def per_fill(self, notional: float, units: float, multiplier: int = 1) -> float:
        if (
            not math.isfinite(notional)
            or not math.isfinite(units)
            or notional <= 0
            or units <= 0
            or multiplier not in {1, 2}
        ):
            raise ValueError("Costs require positive finite notional/units and a 1x or 2x scenario")
        commission = max(
            self.minimum_commission,
            notional * self.commission_bps / 10000 + units * self.commission_per_unit,
        )
        return multiplier * (
            commission + notional * (self.spread_bps / 2 + self.slippage_bps) / 10000
        )


class Costs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    currency: str = Field(pattern=r"^[A-Z]{3,5}$")
    profiles: dict[str, CostProfile]


def load_costs(path: Path) -> Costs:
    return Costs.model_validate(yaml.safe_load(path.read_text()))
