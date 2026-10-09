"""Handbook strategy 5: residual breadth before index response (long the index).

RB_t is the share of the frozen constituent set with a positive 10-minute cumulative
residual E(10) from each constituent's frozen model. Every constituent must have a
valid E(10) at both measurement times; otherwise the observation is skipped (never
treated as zero). Arm when |R_M(10)| <= 0.30 sigma_M,10, RB_t0 >= 0.65,
RB_t0 - RB_t0-10 >= 0.20 and the median constituent residual is positive. Freeze K0 as
the index instrument's 10-minute low. Expire after 15 minutes or if RB falls below
0.50. Confirm when the index instrument closes above its previous five minutes' high
with a positive one-minute return. Stop K0 - one tick; 30-minute time exit.
"""

from __future__ import annotations

import statistics

from xasset.lab.strategy import Candidate, Requirements, Setup, Strategy


class ResidualBreadth(Strategy):
    id = "h05"
    title = "Residual breadth before index response"
    kinds = ("spot", "equity", "etf", "perp")
    requires = Requirements(
        quotes=True,
        flow=False,
        constituents=True,
        notes="Complete synchronized quotes for a point-in-time constituent set.",
    )
    time_exit_minutes = 30

    def candidates(self) -> list[str]:
        return list(self.universe.index_constituents)

    def breadth(self, index: str, i: int) -> tuple[float, float] | None:
        members = self.universe.index_constituents[index]
        values = [self.market.residual_sum(member, i, 10) for member in members]
        if not members or any(v is None for v in values):
            return None
        valid = [float(v) for v in values if v is not None]
        return sum(1 for v in valid if v > 0) / len(valid), statistics.median(valid)

    def arm(self, symbol: str) -> Setup | None:
        i, market = self.minute, self.market
        tape = market.tapes[symbol]
        r10, close = tape.ret(i, 10), tape.close(i)
        if r10 is None or close is None:
            return None
        sigma10 = market.sigma(symbol, 10, i, close)
        if sigma10 is None or abs(r10) > 0.30 * sigma10:
            return None
        now, before = self.breadth(symbol, i), self.breadth(symbol, i - 10)
        if now is None or before is None:
            return None
        if now[0] < 0.65 or now[0] - before[0] < 0.20 or now[1] <= 0:
            return None
        k0 = tape.min_low(i - 9, i)
        if k0 is None:
            return None
        return self.new_setup(
            symbol, "ARMED", K0=k0, RB=now[0], RB_prior=before[0], sigma10=sigma10
        )

    def step(self, setup: Setup) -> Candidate | None:
        i, tape = self.minute, self.market.tapes[setup.symbol]
        if i - setup.armed_minute > 15:
            self.expire(setup, "15 minutes elapsed")
            return None
        current = self.breadth(setup.symbol, i)
        close, r1, prior = tape.close(i), tape.ret(i, 1), tape.max_high(i - 5, i - 1)
        if current is None or close is None:
            self.expire(setup, "data quality: incomplete constituent or index coverage")
            return None
        if current[0] < 0.50:
            self.expire(setup, "residual breadth fell below 0.50", RB=current[0])
            return None
        if r1 is not None and prior is not None and r1 > 0 and close > prior:
            return self.candidate(
                setup,
                stop=setup.anchors["K0"] - self.tick(setup.symbol),
                sigma10=setup.anchors["sigma10"],
            )
        return None
