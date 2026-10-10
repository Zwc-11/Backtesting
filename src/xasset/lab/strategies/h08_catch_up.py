"""Handbook strategy 8: confirmed catch-up after a common shock (long the laggard).

Pairs are declared before the session. For each leader L and laggard i a past-only
distributed-lag model r_i,u = alpha + sum_{k=0..3} beta_k r_L,u-k is fitted on the
previous 20 sessions; the pair is eligible only if beta_sum > 0 in each of the
previous three 20-session subwindows. Gap_t = beta_sum R_L(5) - R_i(5). Arm when the
leader's five-minute return is positive and above its prior 95th percentile and
Gap >= 1.0 sigma_gap (prior robust scale of five-minute gaps). Freeze the laggard's
five-minute low K0 and the pre-shock leader price. Within ten minutes confirm when the
laggard's two-minute return is positive, it closes above its previous three minutes'
high, and the leader retains at least 70% of its observed five-minute log gain.
Expire if leader retention fails, Gap becomes non-positive, or the laggard trades
below K0. Stop K0 - one tick; 20-minute time exit.
"""

from __future__ import annotations

import math

from xasset.lab.strategy import Candidate, Requirements, Setup, Strategy


class CatchUp(Strategy):
    id = "h08"
    title = "Confirmed catch-up after common shock"
    kinds = ("spot", "equity", "etf", "perp")
    requires = Requirements(
        quotes=True,
        flow=False,
        pairs=True,
        notes="Synchronized quotes for predeclared economically related pairs.",
    )
    time_exit_minutes = 20
    levels = {
        "K0": ("asset", "laggard five-minute low K0"),
        "leader_pre_shock": ("leader", "leader price before the shock"),
    }

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.leaders: dict[str, list[str]] = {}
        for leader, laggard in self.market.pairs:
            self.leaders.setdefault(laggard, []).append(leader)

    def candidates(self) -> list[str]:
        return sorted(self.leaders)

    def arm(self, symbol: str) -> Setup | None:
        i, market = self.minute, self.market
        for leader in self.leaders[symbol]:
            if not market.is_member(leader):
                continue
            model = market.lag.get((leader, symbol))
            if model is None or not model.stable or model.beta_sum <= 0:
                continue
            r_leader = market.tapes[leader].ret(i, 5)
            q95 = market.quantile(leader, "R5", i, 0.95)
            if r_leader is None or q95 is None or r_leader <= 0 or r_leader <= q95:
                continue
            gap = market.gap(leader, symbol, i)
            scale = market.scale(f"{leader}>{symbol}", "GAP5", i)
            tape = market.tapes[symbol]
            close = tape.close(i)
            if gap is None or scale is None or close is None:
                continue
            sigma_gap = max(scale, self.tick(symbol) / close)
            if gap < 1.0 * sigma_gap:
                continue
            k0 = tape.min_low(i - 4, i)
            pre_shock = market.tapes[leader].close(i - 5)
            sigma10 = market.sigma(symbol, 10, i, close)
            if k0 is None or pre_shock is None or sigma10 is None:
                continue
            return self.new_setup(
                symbol,
                "ARMED",
                leader=leader,
                K0=k0,
                leader_pre_shock=pre_shock,
                leader_gain=r_leader,
                gap=gap,
                sigma_gap=sigma_gap,
                beta_sum=model.beta_sum,
                sigma10=sigma10,
            )
        return None

    def step(self, setup: Setup) -> Candidate | None:
        i, market = self.minute, self.market
        a = setup.anchors
        leader = market.tapes[a["leader"]]
        tape = market.tapes[setup.symbol]
        if i - setup.armed_minute > 10:
            self.expire(setup, "no confirmation within ten minutes")
            return None
        leader_close, close, low = leader.close(i), tape.close(i), tape.low(i)
        gap = market.gap(a["leader"], setup.symbol, i)
        if leader_close is None or close is None or low is None or gap is None:
            self.expire(setup, "data quality: pair not actively quoting")
            return None
        retained = math.log(leader_close / a["leader_pre_shock"])
        if retained < 0.70 * a["leader_gain"]:
            self.expire(setup, "leader retained less than 70% of its gain")
            return None
        if gap <= 0:
            self.expire(setup, "gap closed before confirmation")
            return None
        if low < a["K0"]:
            self.expire(setup, "laggard broke K0")
            return None
        r2, prior = tape.ret(i, 2), tape.max_high(i - 3, i - 1)
        if r2 is not None and prior is not None and r2 > 0 and close > prior:
            return self.candidate(
                setup, stop=a["K0"] - self.tick(setup.symbol), sigma10=a["sigma10"]
            )
        return None
