"""Handbook strategy 2: buying bursts with retained gains (long).

Fixed two-minute blocks anchored to the session open. A buying burst has signed flow
above its prior 90th percentile and positive, imbalance Q/V >= 0.20 and a block return
of at least 0.5 sigma_2. After each accepted burst j, at least one non-burst block must
pass before another burst is accepted; a pause lasts one to three non-burst blocks and
worst retention log(min low since b_j / A_j) / log(C_j / A_j) must stay >= 0.70 at every
completed minute. Confirm on the first qualifying third burst whose ending price
exceeds the maximum high of both earlier bursts and their pauses. Stop: the final
pause's low minus one tick. Expire on failed retention, a fourth consecutive non-burst
block, more than 20 minutes after burst 1, or a third burst that fails the prior-high
break. Consecutive burst blocks before any pause block are not separate bursts.
"""

from __future__ import annotations

import math

from xasset.lab.strategies.common import Block, block_at
from xasset.lab.strategy import Candidate, Requirements, Setup, Strategy


class BuyingBursts(Strategy):
    id = "h02"
    title = "Buying bursts with retained gains"
    requires = Requirements(
        quotes=True,
        flow=True,
        notes="Aggressor-labelled trades (at least 95% classified) and quote paths.",
    )
    retention = 0.70
    max_pause_blocks = 3
    max_minutes = 20

    def burst(self, symbol: str, block: Block, sigma2: float) -> bool:
        q90 = self.market.quantile(symbol, "Q2", block.end, 0.90)
        return (
            q90 is not None
            and block.signed_flow > 0
            and block.signed_flow > q90
            and block.signed_flow / block.notional >= 0.20
            and block.log_return >= 0.50 * sigma2
        )

    def arm(self, symbol: str) -> Setup | None:
        i = self.minute
        if i % 2 != 1:
            return None
        tape = self.market.tapes[symbol]
        block = block_at(tape, i)
        if block is None:
            return None
        sigma2 = self.market.sigma(symbol, 2, i, block.end_mid)
        sigma10 = self.market.sigma(symbol, 10, i, block.end_mid)
        if sigma2 is None or sigma10 is None or not self.burst(symbol, block, sigma2):
            return None
        setup = self.new_setup(
            symbol,
            "BURST_SEEN",
            A1=block.start_mid,
            C1=block.end_mid,
            b1=block.end,
            low1=block.low,
            sigma2=sigma2,
            sigma10=sigma10,
        )
        setup.scratch.update(
            bursts=[(block.start_mid, block.end_mid, block.end)],
            pause_blocks=0,
            first_start=block.start,
        )
        return setup

    def step(self, setup: Setup) -> Candidate | None:
        i, tape = self.minute, self.market.tapes[setup.symbol]
        s = setup.scratch
        a = setup.anchors
        if i - a["b1"] > self.max_minutes:
            self.expire(setup, "more than 20 minutes since burst 1")
            return None
        a_j, c_j, b_j = s["bursts"][-1]
        low = tape.min_low(b_j + 1, i)
        if low is None:
            self.expire(setup, "data quality: missing bar during pause")
            return None
        worst = math.log(low / a_j) / math.log(c_j / a_j)
        if worst < self.retention:
            self.expire(setup, "retention below 0.70 during pause", worst_retention=worst)
            return None
        if i % 2 != 1:
            return None
        block = block_at(tape, i)
        if block is None:
            self.expire(setup, "data quality: block unobserved or flow coverage below 95%")
            return None
        if not self.burst(setup.symbol, block, a["sigma2"]):
            s["pause_blocks"] += 1
            if s["pause_blocks"] > self.max_pause_blocks:
                self.expire(setup, "fourth consecutive non-burst pause block")
            return None
        if s["pause_blocks"] == 0:
            # No completed pause block yet: strong consecutive blocks are not new bursts.
            self.ledger.event(
                self.now or self.end,
                self.id,
                setup.symbol,
                setup.id,
                "burst_ignored",
                setup.state,
                reason="no pause block since previous burst",
            )
            return None
        if len(s["bursts"]) == 1:
            s["bursts"].append((block.start_mid, block.end_mid, block.end))
            s["pause_blocks"] = 0
            self.transition(
                setup, "BURST2_SEEN", "burst", A2=block.start_mid, C2=block.end_mid, b2=block.end
            )
            return None
        prior_high = tape.max_high(s["first_start"], block.start - 1)
        if prior_high is None or block.end_mid <= prior_high:
            self.expire(setup, "third burst did not exceed the prior high", prior_high=prior_high)
            return None
        pause_low = tape.min_low(s["bursts"][-1][2] + 1, block.start - 1)
        if pause_low is None:
            self.expire(setup, "data quality: final pause unobserved")
            return None
        stop = pause_low - self.tick(setup.symbol)
        setup.anchors.update(
            A3=block.start_mid, C3=block.end_mid, b3=block.end, prior_high=prior_high
        )
        return self.candidate(setup, stop=stop, sigma10=a["sigma10"])
