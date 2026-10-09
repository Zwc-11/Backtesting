"""Registered strategy implementations, keyed by stable strategy ID."""

from __future__ import annotations

from xasset.lab.strategies.h01_resilience import Resilience
from xasset.lab.strategies.h02_buying_bursts import BuyingBursts
from xasset.lab.strategies.h03_pullbacks import ImprovingPullbacks
from xasset.lab.strategies.h04_failed_recovery import FailedRecovery
from xasset.lab.strategies.h05_residual_breadth import ResidualBreadth
from xasset.lab.strategies.h06_resistance import LessBuyingAtResistance
from xasset.lab.strategies.h07_rising_center import RisingCenter
from xasset.lab.strategies.h08_catch_up import CatchUp
from xasset.lab.strategies.h09_selling_bursts import SellingBursts, SellingBurstsTradeBars
from xasset.lab.strategies.h10_handoff import ThinSessionHandoff
from xasset.lab.strategy import Strategy

REGISTRY: dict[str, type[Strategy]] = {
    cls.id: cls
    for cls in (
        Resilience,
        BuyingBursts,
        ImprovingPullbacks,
        FailedRecovery,
        ResidualBreadth,
        LessBuyingAtResistance,
        RisingCenter,
        CatchUp,
        SellingBursts,
        SellingBurstsTradeBars,
        ThinSessionHandoff,
    )
}


def resolve(ids: list[str]) -> list[type[Strategy]]:
    unknown = [sid for sid in ids if sid not in REGISTRY]
    if unknown:
        raise ValueError(f"Unknown strategy IDs: {', '.join(unknown)}")
    return [REGISTRY[sid] for sid in ids]
