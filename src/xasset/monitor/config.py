from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from xasset.config import Instrument
from xasset.research.experiment import ScheduledEvent


class MonitorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    instruments: list[Instrument] = Field(min_length=1)
    interval_seconds: int = Field(default=60, ge=30)
    history_minutes: int = Field(default=180, ge=120, le=600)
    settlement_seconds: int = Field(default=60, ge=30)
    stale_seconds: int = Field(default=240, ge=60)
    volatility_window: int = Field(default=60, ge=20)
    move_z: float = Field(default=4.0, ge=2)
    residual_z: float = Field(default=4.0, ge=2)
    regime_change: float = Field(default=0.5, gt=0, le=2)
    event_window_minutes: int = Field(default=60, ge=1, le=1440)
    cooldown_seconds: int = Field(default=1800, ge=60)
    alert_mode: Literal["log", "telegram", "discord"] = "log"
    # Sending is deliberately opt-in, independently of credential presence.
    delivery_enabled: bool = False
    events: list[ScheduledEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid(self) -> "MonitorConfig":
        if self.history_minutes < 2 * self.volatility_window + 2:
            raise ValueError("History must cover two volatility windows plus return warmup")
        if len({item.id for item in self.instruments}) != len(self.instruments):
            raise ValueError("Monitor instrument IDs must be unique")
        if any(
            len(item.sources) != 1 or item.sources[0] not in {"alpaca", "kraken", "hyperliquid"}
            for item in self.instruments
        ):
            raise ValueError("Monitor requires one explicit supported feed per instrument")
        if self.delivery_enabled and self.alert_mode == "log":
            raise ValueError("Choose an external alert channel before enabling delivery")
        for item in self.instruments:
            source = item.sources[0]
            if source == "kraken" and not item.kraken_symbol:
                raise ValueError("Kraken instruments need an explicit native symbol")
            if source == "hyperliquid" and not item.hyperliquid_symbol:
                raise ValueError("Hyperliquid instruments need an explicit native symbol")
            if item.session not in {"regular", "all"} or (
                item.session == "regular" and not item.calendar
            ):
                raise ValueError("Monitor needs continuous or verified regular-session hours")
        return self


def load(path: Path) -> MonitorConfig:
    return MonitorConfig.model_validate(yaml.safe_load(path.read_text()))
