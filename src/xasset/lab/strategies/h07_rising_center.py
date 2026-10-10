"""Handbook strategy 7: rising trading centre inside a narrowing range (long).

Three fixed, consecutive ten-minute blocks anchored to the session open end at t0.
For block j: high h_j and low l_j of observed prices, trade VWAP c_j = sum(p q)/sum(q)
and log range w_j = log(h_j/l_j). Arm if w3 < w2 < w1, w3 <= 0.70 w1, c3 > c2 > c1,
log(c3/c1) >= 0.50 sigma_10, h3 > l3, 0.60 <= (c3 - l3)/(h3 - l3) <= 1.00 and the third
block's notional exceeds its prior 25th percentile. Freeze U = h3 and K0 = l3. Within
ten minutes confirm on a close above U + tick whose one-minute log range exceeds its
prior 75th percentile. Stop K0 - one tick. Expire on a close below K0 or the deadline.

The notional percentile uses rolling ten-minute notional at each minute of the
reference sessions: block-aligned values alone give three observations per
neighbourhood and could never reach the 200-observation minimum.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from xasset.lab.market import Tape
from xasset.lab.strategy import Candidate, Requirements, Setup, Strategy


@dataclass(frozen=True)
class Center:
    high: float
    low: float
    vwap: float
    notional: float

    @property
    def width(self) -> float:
        return math.log(self.high / self.low)


def center(tape: Tape, start: int, end: int) -> Center | None:
    high, low = tape.max_high(start, end), tape.min_low(start, end)
    if high is None or low is None or start < 0:
        return None
    quantity = notional = 0.0
    for index in range(start, end + 1):
        bar = tape.bars[index]
        if bar is None or bar.volume <= 0 or bar.notional <= 0:
            return None
        quantity += bar.volume
        notional += bar.notional
    return Center(high, low, notional / quantity, notional)


class RisingCenter(Strategy):
    id = "h07"
    title = "Rising trading center inside narrowing range"
    levels = {
        "U": ("asset", "block-3 high U"),
        "K0": ("asset", "block-3 low K0"),
    }
    requires = Requirements(
        quotes=True,
        flow=False,
        trade_vwap=True,
        notes="Trade prices and base quantities for VWAP plus validated quotes.",
    )

    def arm(self, symbol: str) -> Setup | None:
        i, market = self.minute, self.market
        if i % 10 != 9 or i < 29:
            return None
        tape = market.tapes[symbol]
        blocks = [center(tape, i - 29 + 10 * k, i - 20 + 10 * k) for k in range(3)]
        if any(b is None for b in blocks):
            return None
        b1, b2, b3 = (b for b in blocks if b is not None)
        if not (b3.width < b2.width < b1.width and b3.width <= 0.70 * b1.width):
            return None
        if not (b3.vwap > b2.vwap > b1.vwap) or b3.high <= b3.low:
            return None
        position = (b3.vwap - b3.low) / (b3.high - b3.low)
        if not 0.60 <= position <= 1.00:
            return None
        close = tape.close(i)
        sigma10 = market.sigma(symbol, 10, i, close) if close else None
        if sigma10 is None or math.log(b3.vwap / b1.vwap) < 0.50 * sigma10:
            return None
        q25 = market.quantile(symbol, "N10", i, 0.25)
        if q25 is None or b3.notional <= q25:
            return None
        return self.new_setup(
            symbol,
            "ARMED",
            U=b3.high,
            K0=b3.low,
            sigma10=sigma10,
            w=[b1.width, b2.width, b3.width],
            c=[b1.vwap, b2.vwap, b3.vwap],
            position=position,
        )

    def step(self, setup: Setup) -> Candidate | None:
        i, tape = self.minute, self.market.tapes[setup.symbol]
        a = setup.anchors
        if i - setup.armed_minute > 10:
            self.expire(setup, "no confirmation within ten minutes")
            return None
        close, high, low = tape.close(i), tape.high(i), tape.low(i)
        if close is None or high is None or low is None:
            self.expire(setup, "data quality: missing bar")
            return None
        if close < a["K0"]:
            self.expire(setup, "close below K0")
            return None
        tick = self.tick(setup.symbol)
        q75 = self.market.quantile(setup.symbol, "RANGE1", i, 0.75)
        if close > a["U"] + tick and q75 is not None and math.log(high / low) > q75:
            return self.candidate(setup, stop=a["K0"] - tick, sigma10=a["sigma10"])
        return None
