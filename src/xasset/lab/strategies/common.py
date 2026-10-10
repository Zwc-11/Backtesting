"""Fixed two-minute blocks anchored to the session open, shared by strategies 2 and 9."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from xasset.lab.market import Market, Tape


@dataclass(frozen=True)
class Block:
    end: int  # minute index of the block's last bar
    start_mid: float  # reference price cached at the block start
    end_mid: float
    low: float
    high: float
    signed_flow: float
    notional: float
    spread: float | None  # median relative quoted spread inside the block

    @property
    def start(self) -> int:
        return self.end - 1

    @property
    def log_return(self) -> float:
        return math.log(self.end_mid / self.start_mid)


def block_at(tape: Tape, end: int) -> Block | None:
    """The completed block ending at ``end`` (odd minute index), or None if unobserved.

    The start reference is the preceding minute's close when it lies in the same
    session; the first block of a session uses its first bar's open instead of
    bridging the closure.
    """
    if end % 2 != 1 or end < 1:
        return None
    key = ("block", end)
    if key in tape.memo:
        return tape.memo[key]  # type: ignore[return-value]
    block = _block(tape, end)
    if end <= tape.last:  # a block over completed minutes can no longer change
        tape.memo[key] = block
    return block


def _block(tape: Tape, end: int) -> Block | None:
    start = end - 1
    if start == 0:
        reference = None if math.isnan(tape.o[0]) else float(tape.o[0])
    else:
        reference = tape.close(start - 1)
    end_mid, low, high = tape.close(end), tape.min_low(start, end), tape.max_high(start, end)
    flow = tape.flow(start, end)
    if reference is None or end_mid is None or low is None or high is None or flow is None:
        return None
    return Block(
        end=end,
        start_mid=reference,
        end_mid=end_mid,
        low=low,
        high=high,
        signed_flow=flow[0] - flow[1],
        notional=flow[2],
        spread=block_spread(tape, start, end),
    )


def block_spread(tape: Tape, start: int, end: int) -> float | None:
    """Median relative quoted spread over the block's quote samples.

    Live bars carry per-second samples; recorded bars without samples fall back to
    the median of per-minute medians (declared approximation). None without quotes.
    """
    samples: list[float] = []
    medians: list[float] = []
    for index in range(start, end + 1):
        bar = tape.bars[index]
        if bar is None or bar.spread_rel is None:
            return None
        medians.append(bar.spread_rel)
        samples.extend(bar.spread_samples or ())
    values = samples or medians
    return float(statistics.median(values)) if values else None


def local_volatility(tape: Tape, block_start: int, count: int = 60) -> float | None:
    """sqrt(mean squared one-minute return) over the last ``count`` valid minutes
    strictly before the block start, within the session."""
    values: list[float] = []
    index = block_start - 1
    while index >= 1 and len(values) < count:
        r = tape.ret(index, 1)
        if r is not None:
            values.append(r)
        index -= 1
    if len(values) < count:
        return None
    return math.sqrt(sum(v * v for v in values) / count)


def ratio_ok(values: list[float], limit: float = 1.25) -> bool:
    return all(v > 0 for v in values) and max(values) / min(values) <= limit


def sigma(market: Market, symbol: str, h: int, i: int, price: float) -> float | None:
    return market.sigma(symbol, h, i, price)
