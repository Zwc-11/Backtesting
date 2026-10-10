"""Handbook strategy 4: failed recovery into a prior high-volume area (short).

Arm at t0 when R(10) <= -2 sigma_10 and ten-minute notional exceeds its prior 90th
percentile. Volume profile: the 120 minutes ending at t0-10, fixed bins of width
max(tick, 0.10 m sigma_10) rounded up to a tick multiple and anchored at price zero;
among bins in the upper 30% of that window's price range, the one with most notional
(ties to the lower bin) defines [A, B]. Freeze H0 (max high over (t0-20, t0-10]) and
L0 (min low over the drop window); require H0 > L0 and A > L0. AT_AREA at the first
close inside [A, B] that has recovered at least half of H0 - L0 (within 60 minutes).
Then exactly ten minutes (v, v+10] must keep every close in [A, B], buyer-initiated
notional above its prior 90th percentile, I(10) >= 0.20 and |R(10)| <= 0.25 sigma_10,
else expire. After c = v+10, confirm short on a close below the previous five minutes'
low. Stop K = (high of the ten-minute window) + one tick. Cancel on a close above
B + 0.5 m(t0) sigma_10 or after 60 minutes. Shorts require a shortable instrument.

Trade prices and notionals are approximated at minute VWAPs when building the profile
(a declared approximation; one-minute bars do not carry individual trades).
"""

from __future__ import annotations

import math

from xasset.lab.strategy import Candidate, Requirements, Setup, Strategy


class FailedRecovery(Strategy):
    id = "h04"
    title = "Failed recovery into prior high-volume area"
    direction = -1
    kinds = ("perp",)
    levels = {
        "A": ("asset", "volume area bottom A"),
        "B": ("asset", "volume area top B"),
        "H0": ("asset", "pre-drop high H0"),
        "L0": ("asset", "drop low L0"),
        "Hc": ("asset", "absorption high Hc"),
    }
    requires = Requirements(
        quotes=True,
        flow=True,
        trade_vwap=True,
        shorting=True,
        notes="Trade-price volume profile, aggressor labels, quotes and a shortable contract.",
    )

    def arm(self, symbol: str) -> Setup | None:
        i, market = self.minute, self.market
        tape = market.tapes[symbol]
        r10, close = tape.ret(i, 10), tape.close(i)
        if r10 is None or close is None or i < 129:
            return None
        sigma10 = market.sigma(symbol, 10, i, close)
        if sigma10 is None or r10 > -2 * sigma10:
            return None
        # The profile grid uses the price and volatility scale at the pre-drop endpoint.
        anchor_price = tape.close(i - 10)
        sigma_pre = market.sigma(symbol, 10, i - 10, anchor_price) if anchor_price else None
        if anchor_price is None or sigma_pre is None:
            return None
        n10 = tape.sum_notional(i - 9, i)
        q90 = market.quantile(symbol, "N10", i, 0.90)
        if n10 is None or q90 is None or n10 <= q90:
            return None
        area = self.profile(symbol, i - 10, anchor_price, sigma_pre)
        h0 = tape.max_high(i - 19, i - 10)
        l0 = tape.min_low(i - 9, i)
        if area is None or h0 is None or l0 is None or not (h0 > l0 and area[0] > l0):
            return None
        return self.new_setup(
            symbol,
            "REBOUNDING",
            A=area[0],
            B=area[1],
            H0=h0,
            L0=l0,
            m0=close,
            sigma10=sigma10,
            bin_width=area[2],
        )

    def profile(
        self, symbol: str, last: int, price: float, sigma10: float
    ) -> tuple[float, float, float] | None:
        tape = self.market.tapes[symbol]
        tick = self.tick(symbol)
        raw = max(tick, 0.10 * price * sigma10)
        width = math.ceil(raw / tick - 1e-9) * tick
        first = last - 119
        high, low = tape.max_high(first, last), tape.min_low(first, last)
        if high is None or low is None or high <= low:
            return None
        threshold = high - 0.30 * (high - low)
        totals: dict[int, float] = {}
        for index in range(first, last + 1):
            bar = tape.bars[index]
            if bar is None or bar.vwap is None:
                return None  # Every profile minute must be observed.
            k = math.floor(bar.vwap / width)
            totals[k] = totals.get(k, 0.0) + bar.notional
        eligible = [(k, v) for k, v in totals.items() if (k + 0.5) * width >= threshold]
        if not eligible:
            return None
        best = max(eligible, key=lambda item: (item[1], -item[0]))[0]
        return best * width, (best + 1) * width, width

    def step(self, setup: Setup) -> Candidate | None:
        i, tape = self.minute, self.market.tapes[setup.symbol]
        a, s = setup.anchors, setup.scratch
        if i - setup.armed_minute > 60:
            self.expire(setup, "60-minute rebound clock expired")
            return None
        close = tape.close(i)
        if close is None:
            self.expire(setup, "data quality: missing bar")
            return None
        if close > a["B"] + 0.50 * a["m0"] * a["sigma10"]:
            self.expire(setup, "close above B + 0.5 m sigma_10")
            return None
        inside = a["A"] <= close <= a["B"]
        if setup.state == "REBOUNDING":
            recovered = (close - a["L0"]) / (a["H0"] - a["L0"])
            if inside and recovered >= 0.5:
                s["v"] = i
                self.transition(setup, "AT_AREA", "at_area", recovery_fraction=recovered)
            return None
        if setup.state == "AT_AREA":
            if not inside:
                self.expire(setup, "close left [A, B] during the ten-minute window")
                return None
            if i < s["v"] + 10:
                return None
            return self.evaluate_window(setup)
        prior_low = tape.min_low(i - 5, i - 1)
        if prior_low is not None and close < prior_low:
            return self.candidate(
                setup, stop=s["Hc"] + self.tick(setup.symbol), sigma10=a["sigma10"]
            )
        return None

    def evaluate_window(self, setup: Setup) -> Candidate | None:
        i, market = self.minute, self.market
        tape = market.tapes[setup.symbol]
        a, s = setup.anchors, setup.scratch
        flow = tape.flow(i - 9, i)
        b90 = market.quantile(setup.symbol, "B10", i, 0.90)
        r10 = tape.ret(i, 10)
        hc = tape.max_high(i - 9, i)
        if flow is None or b90 is None or r10 is None or hc is None:
            self.expire(setup, "data quality: absorption window unobserved")
            return None
        imbalance = (flow[0] - flow[1]) / flow[2]
        if flow[0] <= b90:
            self.expire(setup, "buyer-initiated notional not above its prior 90th percentile")
        elif imbalance < 0.20:
            self.expire(setup, "net imbalance I(10) below 0.20")
        elif abs(r10) > 0.25 * a["sigma10"]:
            self.expire(setup, "price progressed more than 0.25 sigma_10")
        else:
            s["Hc"] = hc
            self.transition(setup, "ABSORBED", "absorbed", Hc=hc, imbalance=imbalance)
        return None
