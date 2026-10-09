"""Handbook strategy 9: selling bursts with shrinking damage (long).

Same fixed two-minute blocks as strategy 2. A selling burst j has negative signed flow
below its prior 10th percentile, Q/V <= -0.20 and a negative block return; A_j is the
cached price at the block start and l_j the block low; damage D_j = log(A_j / l_j) must
be at least 0.25 sigma_2. Recovery is assessed only at later block ends: another
qualifying selling burst expires the sequence; otherwise the block end must be at or
above A_j exp(-0.10 sigma_2), recognised after T_j = 2, 4 or 6 minutes. After a
recovery the next burst must arrive within 10 minutes with at least one intervening
non-burst block, and all three bursts must lie within 40 minutes. Comparability: net
selling, block median relative spreads and prior local volatilities each have a
max/min ratio <= 1.25 (all strictly positive). Pattern: D3 < D2 < D1 and T3 < T2 < T1.
Confirm at the final recovery if its close exceeds the previous three minutes' high,
otherwise within three further minutes while price holds the recovery threshold.
Stop: the minimum low from burst 3's start through its recovery, minus one tick.

The trade-bar variant (``h09t``) omits the spread-comparability rule because trade
archives carry no quotes; it is a separately registered variant.
"""

from __future__ import annotations

import math

from xasset.lab.strategies.common import Block, block_at, local_volatility, ratio_ok
from xasset.lab.strategy import Candidate, Requirements, Setup, Strategy


class SellingBursts(Strategy):
    id = "h09"
    title = "Selling bursts with shrinking damage"
    requires = Requirements(
        quotes=True,
        flow=True,
        notes="Aggressor-labelled trades, quote paths and quoted spreads for every block.",
    )
    require_spread = True

    def selling_burst(self, symbol: str, block: Block, sigma2: float) -> bool:
        q10 = self.market.quantile(symbol, "Q2", block.end, 0.10)
        return (
            q10 is not None
            and block.signed_flow < 0
            and block.signed_flow < q10
            and block.signed_flow / block.notional <= -0.20
            and block.log_return < 0
            and math.log(block.start_mid / block.low) >= 0.25 * sigma2
        )

    def describe(self, symbol: str, block: Block) -> dict[str, float | None]:
        tape = self.market.tapes[symbol]
        return {
            "A": block.start_mid,
            "low": block.low,
            "D": math.log(block.start_mid / block.low),
            "sell": -block.signed_flow,
            "spread": block.spread,
            "vol": local_volatility(tape, block.start),
            "end": block.end,
            "start": block.start,
        }

    def arm(self, symbol: str) -> Setup | None:
        i = self.minute
        if i % 2 != 1:
            return None
        block = block_at(self.market.tapes[symbol], i)
        if block is None:
            return None
        sigma2 = self.market.sigma(symbol, 2, i, block.start_mid)
        sigma10 = self.market.sigma(symbol, 10, i, block.start_mid)
        if sigma2 is None or sigma10 is None or not self.selling_burst(symbol, block, sigma2):
            return None
        burst = self.describe(symbol, block)
        setup = self.new_setup(
            symbol,
            "RECOVERING_1",
            sigma2=sigma2,
            sigma10=sigma10,
            **{f"{k}1": v for k, v in burst.items()},
        )
        setup.scratch.update(bursts=[burst], recoveries=[], waiting_since=None, final=None)
        return setup

    def step(self, setup: Setup) -> Candidate | None:
        i, tape = self.minute, self.market.tapes[setup.symbol]
        s, a = setup.scratch, setup.anchors
        sigma2 = a["sigma2"]
        first_start = s["bursts"][0]["start"]
        if s["final"] is not None:
            return self.await_breakout(setup)
        if len(s["bursts"]) < 3 and i - first_start > 40:
            self.expire(setup, "three bursts not completed within 40 minutes")
            return None
        if i % 2 != 1:
            return None
        block = block_at(tape, i)
        if block is None:
            self.expire(setup, "data quality: block unobserved or flow coverage below 95%")
            return None
        is_burst = self.selling_burst(setup.symbol, block, sigma2)
        burst = s["bursts"][-1]
        if len(s["recoveries"]) < len(s["bursts"]):
            # Waiting for recovery of the latest burst.
            if is_burst:
                self.expire(setup, "another selling burst before recovery")
                return None
            elapsed = i - burst["end"]
            if block.end_mid >= burst["A"] * math.exp(-0.10 * sigma2):
                s["recoveries"].append({"at": i, "T": elapsed})
                n = len(s["recoveries"])
                if n < 3:
                    s["waiting_since"] = i
                    self.transition(setup, f"WAITING_{n + 1}", "recovered", T=elapsed)
                    return None
                return self.final_recovery(setup)
            if elapsed >= 6:
                self.expire(setup, "no recovery within six minutes")
            return None
        # Waiting for the next burst after a recovery.
        since = i - s["waiting_since"]
        if is_burst:
            if since < 4:
                self.expire(setup, "next burst without an intervening non-burst block")
                return None
            if since > 10:
                self.expire(setup, "next burst later than ten minutes after recovery")
                return None
            s["bursts"].append(self.describe(setup.symbol, block))
            n = len(s["bursts"])
            if n == 3 and block.end - first_start > 40:
                self.expire(setup, "three bursts span more than 40 minutes")
                return None
            self.transition(setup, f"RECOVERING_{n}", "burst", D=s["bursts"][-1]["D"])
            return None
        if since >= 10:
            self.expire(setup, "no further selling burst within ten minutes")
        return None

    def comparable(self, bursts: list[dict[str, float | None]]) -> str | None:
        sells = [float(b["sell"] or 0) for b in bursts]
        if not ratio_ok(sells):
            return "net selling not comparable (max/min > 1.25)"
        if self.require_spread:
            spreads = [b["spread"] for b in bursts]
            if any(v is None for v in spreads):
                return "quoted spread unavailable for a burst block"
            if not ratio_ok([float(v) for v in spreads if v is not None]):
                return "quoted spreads not comparable (max/min > 1.25)"
        vols = [b["vol"] for b in bursts]
        if any(v is None for v in vols):
            return "fewer than 60 prior valid minutes for local volatility"
        if not ratio_ok([float(v) for v in vols if v is not None]):
            return "local volatility not comparable (max/min > 1.25)"
        d = [float(b["D"] or 0) for b in bursts]
        if not d[2] < d[1] < d[0]:
            return "damage not strictly shrinking"
        return None

    def final_recovery(self, setup: Setup) -> Candidate | None:
        s = setup.scratch
        problem = self.comparable(s["bursts"])
        t = [r["T"] for r in s["recoveries"]]
        if problem is None and not t[2] < t[1] < t[0]:
            problem = "recovery times not strictly shrinking"
        if problem is not None:
            self.expire(setup, problem)
            return None
        tape = self.market.tapes[setup.symbol]
        burst3 = s["bursts"][2]
        stop_low = tape.min_low(int(burst3["start"]), self.minute)
        if stop_low is None:
            self.expire(setup, "data quality: burst 3 path unobserved")
            return None
        s["final"] = {
            "at": self.minute,
            "stop_low": stop_low,
            "threshold": float(burst3["A"]) * math.exp(-0.10 * setup.anchors["sigma2"]),
        }
        self.transition(setup, "RECOVERED_3", "recovered", T=t[2], D=[b["D"] for b in s["bursts"]])
        return self.await_breakout(setup)

    def await_breakout(self, setup: Setup) -> Candidate | None:
        i, tape = self.minute, self.market.tapes[setup.symbol]
        final = setup.scratch["final"]
        close, low = tape.close(i), tape.low(i)
        if close is None or low is None:
            self.expire(setup, "data quality: missing bar while awaiting breakout")
            return None
        if i > final["at"]:
            if low < final["stop_low"]:
                self.expire(setup, "burst-3 low broken before entry")
                return None
            if close < final["threshold"]:
                self.expire(setup, "price fell below the final recovery threshold")
                return None
        prior_high = tape.max_high(i - 3, i - 1)
        if prior_high is not None and close > prior_high:
            stop = final["stop_low"] - self.tick(setup.symbol)
            return self.candidate(setup, stop=stop, sigma10=setup.anchors["sigma10"])
        if i - final["at"] >= 3:
            self.expire(setup, "no breakout within three minutes of the final recovery")
        return None


class SellingBurstsTradeBars(SellingBursts):
    id = "h09t"
    title = "Selling bursts with shrinking damage (trade-bar variant, no spread rule)"
    require_spread = False
