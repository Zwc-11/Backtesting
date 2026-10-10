"""Handbook strategy 1: strength during market weakness (long).

Arm at t0 when the benchmark's 10-minute return is negative and at or below its prior
10th percentile, at least 70% of eligible peers fell over the same window, the asset's
own return is at least -0.5 sigma_10 and its cumulative residual is at least
+1.0 sigma_e,10. Freeze the stress lows K0 (asset) and KM (benchmark). Stabilize at the
first minute t >= t0+3 with a non-negative 3-minute benchmark return and the last
three benchmark lows at or above KM. Confirm strictly after stabilization, at least
five minutes after arming, on a positive one-minute return closing above the previous
five minutes' high. Expire on an asset close below K0, a benchmark print below KM, or
30 minutes after t0. Stop K = K0 - one tick.
"""

from __future__ import annotations

from xasset.lab.strategy import Candidate, Guard, Requirements, Setup, Strategy


class Resilience(Strategy):
    id = "h01"
    title = "Strength during market weakness"
    requires = Requirements(
        quotes=True,
        flow=False,
        peers=True,
        notes="Benchmark, at least 20 synchronized peers and a frozen residual model.",
    )
    window = 30
    levels = {
        "K0": ("asset", "stress-window low K0"),
        "KM": ("benchmark", "market stress low KM"),
    }

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._stress: tuple[int, dict[str, float] | None] | None = None

    def candidates(self) -> list[str]:
        return [s for s in super().candidates() if s != self.universe.benchmark]

    def market_stress(self) -> dict[str, float] | None:
        """Benchmark conditions shared by every asset at this minute (memoized)."""
        i = self.minute
        if self._stress is not None and self._stress[0] == i:
            return self._stress[1]
        result = None
        benchmark = self.universe.benchmark
        r_market = self.market.tapes[benchmark].ret(i, 10)
        if r_market is not None and r_market < 0:
            q10 = self.market.quantile(benchmark, "R10", i, 0.10)
            if q10 is not None and r_market <= q10:
                result = {"r_market": r_market, "q10": q10}
        self._stress = (i, result)
        return result

    def arm(self, symbol: str) -> Setup | None:
        stress = self.market_stress()
        if stress is None:
            return None
        i, market = self.minute, self.market
        breadth = market.breadth(symbol, i, 10, -1)
        if breadth is None or breadth[0] < 0.70:
            return None
        tape = market.tapes[symbol]
        r_asset, price = tape.ret(i, 10), tape.close(i)
        if r_asset is None or price is None:
            return None
        sigma10 = market.sigma(symbol, 10, i, price)
        if sigma10 is None or r_asset < -0.50 * sigma10:
            return None
        residual = market.residual_sum(symbol, i, 10)
        scale = market.scale(symbol, "E10", i)
        if residual is None or scale is None:
            return None
        sigma_e = max(scale, self.tick(symbol) / price)
        if residual < 1.00 * sigma_e:
            return None
        k0 = tape.min_low(i - 9, i)
        km = market.tapes[self.universe.benchmark].min_low(i - 9, i)
        if k0 is None or km is None:
            return None
        return self.new_setup(
            symbol,
            "ARMED",
            K0=k0,
            KM=km,
            sigma10=sigma10,
            sigma_e10=sigma_e,
            r_asset10=r_asset,
            residual10=residual,
            r_market10=stress["r_market"],
            q10_market=stress["q10"],
            peers_down=breadth[0],
            peers=breadth[1],
        )

    def step(self, setup: Setup) -> Candidate | None:
        i, t0 = self.minute, setup.armed_minute
        a = setup.anchors
        benchmark = self.market.tapes[self.universe.benchmark]
        tape = self.market.tapes[setup.symbol]
        if i - t0 > self.window:
            self.expire(setup, "30 minutes elapsed after arming")
            return None
        close, market_low = tape.close(i), benchmark.low(i)
        if close is None or market_low is None:
            self.expire(setup, "data quality: missing asset or benchmark bar")
            return None
        if close < a["K0"]:
            self.expire(setup, "asset closed below its stress low K0", close=close)
            return None
        if market_low < a["KM"]:
            self.expire(setup, "benchmark printed below its stress low KM", low=market_low)
            return None
        if setup.state == "ARMED":
            lows = [benchmark.low(u) for u in (i - 2, i - 1, i)]
            r3 = benchmark.ret(i, 3)
            if (
                i >= t0 + 3
                and r3 is not None
                and r3 >= 0
                and all(low is not None and low >= a["KM"] for low in lows)
            ):
                setup.scratch["stabilized"] = i
                self.transition(setup, "STABILIZED", "stabilized", r_market3=r3)
            return None
        if i <= setup.scratch["stabilized"] or i < t0 + 5:
            return None
        r1 = tape.ret(i, 1)
        prior_high = tape.max_high(i - 5, i - 1)
        if r1 is None or prior_high is None or r1 <= 0 or close <= prior_high:
            return None
        stop = a["K0"] - self.tick(setup.symbol)
        return self.candidate(
            setup,
            stop=stop,
            sigma10=a["sigma10"],
            guards=[Guard(self.universe.benchmark, "below", a["KM"])],
        )
