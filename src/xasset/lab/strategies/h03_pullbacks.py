"""Handbook strategy 3: improving pullbacks after a broad breakout (long).

Arm when the asset closes above the maximum high of the previous 60 minutes with a
positive one-minute return, the benchmark's 10-minute return is positive and at least
60% of eligible peers rose over 10 minutes. Freeze the breakout level U and sigma_5.
Track a running high of observed closes. A pullback starts at the first close at least
0.5 sigma_5 below the running high (freeze A_j and s_j); it recovers at the first later
close >= A_j exp(-0.10 sigma_5) within ten minutes, else the setup is invalid. Record
depth D_j, duration T_j = t_j - s_j + 1 and seller-initiated notional W_j in units of
the prior expected one-minute notional at s_j. After recovery 1 reset the running high
and allow pullback 2 to start only after one more completed minute. Require
D2 < D1, T2 < T1 and W2 < W1, and the second recovery bar must close above the high of
the previous three minutes; otherwise expire. Stop: pullback 2's low minus one tick.
Expire after 45 minutes or on any close below U exp(-0.5 sigma_5).
"""

from __future__ import annotations

import math

from xasset.lab.strategy import Candidate, Requirements, Setup, Strategy


class ImprovingPullbacks(Strategy):
    id = "h03"
    title = "Improving pullbacks after broad breakout"
    levels = {
        "U": ("asset", "breakout level U"),
        "A": ("asset", "pullback peak"),
        "low": ("asset", "pullback low"),
    }
    requires = Requirements(
        quotes=True,
        flow=True,
        peers=True,
        notes="Aggressor-labelled seller notional, quotes and contemporaneous breadth.",
    )

    def candidates(self) -> list[str]:
        return [s for s in super().candidates() if s != self.universe.benchmark]

    def arm(self, symbol: str) -> Setup | None:
        i, market = self.minute, self.market
        tape = market.tapes[symbol]
        close, prior = tape.close(i), tape.max_high(i - 60, i - 1)
        r1 = tape.ret(i, 1)
        if close is None or prior is None or r1 is None or close <= prior or r1 <= 0:
            return None
        r_market = market.tapes[self.universe.benchmark].ret(i, 10)
        if r_market is None or r_market <= 0:
            return None
        breadth = market.breadth(symbol, i, 10, 1)
        if breadth is None or breadth[0] < 0.60:
            return None
        sigma5 = market.sigma(symbol, 5, i, close)
        sigma10 = market.sigma(symbol, 10, i, close)
        if sigma5 is None or sigma10 is None:
            return None
        setup = self.new_setup(
            symbol,
            "BROKEOUT",
            U=prior,
            sigma5=sigma5,
            sigma10=sigma10,
            breakout_close=close,
            peers_up=breadth[0],
        )
        setup.scratch.update(running_high=close, pullbacks=[], current=None, earliest_start=i + 1)
        return setup

    def step(self, setup: Setup) -> Candidate | None:
        i, tape = self.minute, self.market.tapes[setup.symbol]
        s, a = setup.scratch, setup.anchors
        if i - setup.armed_minute > 45:
            self.expire(setup, "45 minutes after the breakout")
            return None
        close = tape.close(i)
        if close is None:
            self.expire(setup, "data quality: missing bar")
            return None
        if close < a["U"] * math.exp(-0.50 * a["sigma5"]):
            self.expire(setup, "close below U exp(-0.5 sigma_5)")
            return None
        current = s["current"]
        if current is None:
            if i >= s["earliest_start"] and close <= s["running_high"] * math.exp(
                -0.50 * a["sigma5"]
            ):
                s["current"] = {"A": s["running_high"], "s": i}
                self.transition(
                    setup, f"PULLBACK_{len(s['pullbacks']) + 1}", "pullback", A=s["running_high"]
                )
            else:
                s["running_high"] = max(s["running_high"], close)
            return None
        if i - current["s"] > 10:
            self.expire(setup, "pullback did not recover within ten minutes")
            return None
        if close < current["A"] * math.exp(-0.10 * a["sigma5"]):
            return None
        start = current["s"]
        low = tape.min_low(start, i)
        flow = tape.flow(start, i)
        expected = self.market.expected_notional(setup.symbol, start)
        if low is None or flow is None or not expected:
            self.expire(setup, "data quality: pullback flow or expected notional unavailable")
            return None
        pullback = {
            "D": math.log(current["A"] / low),
            "T": i - start + 1,
            "W": flow[1] / expected,
            "low": low,
            "recovered": i,
        }
        s["pullbacks"].append(pullback)
        s["current"] = None
        if len(s["pullbacks"]) == 1:
            s["running_high"] = close
            s["earliest_start"] = i + 2  # one additional completed minute first
            self.transition(setup, "RECOVERED_1", "recovered", **pullback)
            return None
        first, second = s["pullbacks"]
        if not (second["D"] < first["D"] and second["T"] < first["T"] and second["W"] < first["W"]):
            self.expire(setup, "pullbacks not improving on depth, duration and selling", **second)
            return None
        prior_high = tape.max_high(i - 3, i - 1)
        if prior_high is None or close <= prior_high:
            self.expire(setup, "second recovery bar did not close above the prior 3-minute high")
            return None
        return self.candidate(
            setup, stop=second["low"] - self.tick(setup.symbol), sigma10=a["sigma10"]
        )
