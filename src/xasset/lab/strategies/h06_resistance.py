"""Handbook strategy 6: resistance approached with less buying (long).

At t0, U is the maximum high of the preceding 60 minutes (excluding t0). Arm (visit 1)
when U exp(-0.10 sigma_5) <= m_t0 <= U. Buying effort A_j = buyer-initiated notional
over the five minutes ending at visit j divided by the summed prior expected notional
of those time-of-session slots. After a visit, a retreat of at least 0.25 sigma_5
below U must be observed before the next visit; a visit is the first close back in the
band at least five minutes after the previous visit, and a return before then expires
the setup. D_j = log(U / lowest low between visits j and j+1). After visit 3 require
A3 < A2 < A1 (all positive) and D2 < D1, and no close below U exp(-2 sigma_5). Confirm
on the first close above U + tick within five minutes of visit 3. Stop: lowest low
between visits 2 and 3 minus one tick. Expire 45 minutes after arming. A close above
U + tick before visit 3 terminates the setup (plain-breakout control territory).
"""

from __future__ import annotations

import math

from xasset.lab.strategy import Candidate, Requirements, Setup, Strategy


class LessBuyingAtResistance(Strategy):
    id = "h06"
    title = "Resistance approached with less buying"
    levels = {"U": ("asset", "resistance U")}
    requires = Requirements(
        quotes=True,
        flow=True,
        notes="Trade-direction data, quote paths and seasonal expected notional.",
    )

    def effort(self, symbol: str, i: int) -> float | None:
        flow = self.market.tapes[symbol].flow(i - 4, i)
        expected = [self.market.expected_notional(symbol, u) for u in range(i - 4, i + 1)]
        if flow is None or any(not e for e in expected):
            return None
        return flow[0] / sum(float(e) for e in expected if e)

    def arm(self, symbol: str) -> Setup | None:
        i, market = self.minute, self.market
        tape = market.tapes[symbol]
        close, u = tape.close(i), tape.max_high(i - 60, i - 1)
        if close is None or u is None or close > u:  # the visit rule requires close <= U
            return None
        sigma5 = market.sigma(symbol, 5, i, close)
        sigma10 = market.sigma(symbol, 10, i, close)
        if sigma5 is None or sigma10 is None:
            return None
        if not u * math.exp(-0.10 * sigma5) <= close <= u:
            return None
        a1 = self.effort(symbol, i)
        if a1 is None or a1 <= 0:
            return None
        setup = self.new_setup(symbol, "VISIT_1", U=u, sigma5=sigma5, sigma10=sigma10, A1=a1)
        setup.scratch.update(
            visits=[{"t": i, "A": a1}], retreated=False, running_low=None, depths=[]
        )
        return setup

    def step(self, setup: Setup) -> Candidate | None:
        i, tape = self.minute, self.market.tapes[setup.symbol]
        a, s = setup.anchors, setup.scratch
        u, sigma5 = a["U"], a["sigma5"]
        close, low = tape.close(i), tape.low(i)
        if i - setup.armed_minute > 45:
            self.expire(setup, "45 minutes after arming")
            return None
        if close is None or low is None:
            self.expire(setup, "data quality: missing bar")
            return None
        if close < u * math.exp(-2 * sigma5):
            self.expire(setup, "close below U exp(-2 sigma_5)")
            return None
        visits = s["visits"]
        tick = self.tick(setup.symbol)
        if len(visits) == 3:
            if close > u + tick:
                stop = s["low_23"] - tick
                return self.candidate(setup, stop=stop, sigma10=a["sigma10"])
            if i - visits[-1]["t"] >= 5:
                self.expire(setup, "no breakout within five minutes of visit 3")
            return None
        if close > u + tick:
            self.expire(setup, "breakout before three visits (plain-breakout control)")
            return None
        in_band = u * math.exp(-0.10 * sigma5) <= close <= u
        if s["retreated"] and in_band:
            if i - visits[-1]["t"] < 5:
                self.expire(setup, "returned to the band before the five-minute separation")
                return None
        else:
            # The running low covers minutes strictly between visits.
            s["running_low"] = low if s["running_low"] is None else min(s["running_low"], low)
            if not s["retreated"] and s["running_low"] <= u * math.exp(-0.25 * sigma5):
                s["retreated"] = True
            return None
        effort = self.effort(setup.symbol, i)
        if effort is None:
            self.expire(setup, "data quality: buying effort unavailable")
            return None
        depth = math.log(u / s["running_low"])
        s["depths"].append(depth)
        visits.append({"t": i, "A": effort})
        if len(visits) == 3:
            s["low_23"] = s["running_low"]
        s["retreated"], s["running_low"] = False, None
        self.transition(setup, f"VISIT_{len(visits)}", "visit", A=effort, D=depth)
        if len(visits) == 3:
            efforts = [v["A"] for v in visits]
            d = s["depths"]
            if not (all(e > 0 for e in efforts) and efforts[2] < efforts[1] < efforts[0]):
                self.expire(setup, "buying effort not strictly declining", A=efforts)
            elif not d[1] < d[0]:
                self.expire(setup, "retreat depth not shrinking", D=d)
        return None
