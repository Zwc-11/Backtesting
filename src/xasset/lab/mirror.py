"""Mirror-image strategies: every rule applied to the inverted market.

The mirror of a bar is the same minute seen from the other side of the pair: the
price is 1/P (so the high becomes 1/low and the low 1/high), and buyer- and
seller-initiated flow change places, because a taker buying the base asset is a taker
selling the quote asset. Notional stays in quote currency; base volume is replaced by
notional^2 / volume so the volume-weighted price of the mirror bar is exactly
1/VWAP. Log returns change sign, so every calibrated quantile, robust scale,
residual model and breadth count of the mirrored market is the exact mirror of the
original.

A mirror strategy runs the original rule, unchanged, on the mirrored market. A
candidate it produces (stop K' and guards on the 1/P scale) is converted back: the
order takes the opposite side on the real instrument, with stop 1/K', and "below"
guards become "above" guards at the inverted level. Mirrors of long rules are shorts
and need shortable instruments (perpetuals); the mirror of a short rule is a long.
Mirrors are separately registered variants, not handbook strategies.
"""

from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

from xasset.lab.bars import FlowBar
from xasset.lab.strategy import Candidate, Guard, Kind, Requirements, Strategy


def _inverse(value: float | None) -> float | None:
    return None if value is None or value <= 0 else 1.0 / value


def mirror_bar(bar: FlowBar) -> FlowBar:
    """The bar of the inverted pair (1/P), with aggressor sides exchanged."""
    volume = bar.notional * bar.notional / bar.volume if bar.volume > 0 else 0.0
    return replace(
        bar,
        open=_inverse(bar.open),
        high=_inverse(bar.low),
        low=_inverse(bar.high),
        close=_inverse(bar.close),
        volume=volume,
        buy_notional=bar.sell_notional,
        sell_notional=bar.buy_notional,
        mid_open=_inverse(bar.mid_open),
        mid_high=_inverse(bar.mid_low),
        mid_low=_inverse(bar.mid_high),
        mid_close=_inverse(bar.mid_close),
        bid_close=_inverse(bar.ask_close),
        ask_close=_inverse(bar.bid_close),
        # Relative spread (ask - bid) / mid is unchanged to second order in the spread.
    )


def real_candidate(candidate: Candidate) -> Candidate:
    """Convert a candidate found on the mirrored market into the real order."""
    guards = [
        Guard(g.symbol, "above" if g.side == "below" else "below", 1.0 / g.level)
        for g in candidate.guards
    ]
    return replace(
        candidate,
        direction=-candidate.direction,
        stop=1.0 / candidate.stop,
        guards=guards,
    )


class MirrorStrategy(Strategy):
    """Marker base: instances read a mirrored ``Market``; see ``mirror_of``."""

    mirror_of: ClassVar[type[Strategy]]
    real_direction: ClassVar[int]


def mirror(original: type[Strategy]) -> type[MirrorStrategy]:
    """The mirror-image variant of a strategy class (ID suffix ``m``)."""
    real_direction = -original.direction
    side = "short" if real_direction < 0 else "long"
    kinds: tuple[Kind, ...] = ("perp",) if real_direction < 0 else original.kinds
    requires = Requirements(
        **{
            **original.requires.__dict__,
            "shorting": real_direction < 0,
            "notes": f"Mirror of {original.id} ({side}). {original.requires.notes}",
        }
    )
    namespace = {
        "id": f"{original.id}m",
        "title": f"Mirror of {original.id}: {original.title.lower()}, inverted ({side})",
        "mirror_of": original,
        "real_direction": real_direction,
        "kinds": kinds,
        "requires": requires,
        "__doc__": f"{original.id} applied to 1/P with buyer and seller flow exchanged.",
        "__module__": __name__,
    }
    return type(f"{original.__name__}Mirror", (original, MirrorStrategy), namespace)
