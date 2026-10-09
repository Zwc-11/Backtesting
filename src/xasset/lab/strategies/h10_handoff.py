"""Handbook strategy 10: thin-session break survives the volume handoff (long).

O is the scheduled main opening. U and L are the high and low over [O-120, O-30). The
first close above U + tick during [O-30, O) is stored provisionally; only at O, with
the whole thin window [O-120, O) observed, is its total notional checked against the
median of the previous 20 sessions' thin-window notional (below it to arm). At O+5 the
first five main-session minutes' notional must exceed its prior 60th percentile and
every close since the breakout must be at or above U. From O+5 to O+20 take the first
bar whose low lies within [U exp(-0.25 sigma_5), U exp(0.25 sigma_5)] while it closes
above U; confirm on a strictly later bar closing above that bar's high with no
intervening close below U. Stop: pullback-bar low minus one tick; 30-minute time exit.

Continuous markets use a declared handoff calendar (the NYSE open, an analytical
convention registered separately, never optimised on returns). Equity books need
pre-market bars, which the regular-session store does not contain.
"""

from __future__ import annotations

import math
from datetime import datetime

from xasset.lab.calibration import SessionStatistic
from xasset.lab.sessions import exchange_session
from xasset.lab.strategy import Candidate, Requirements, Setup, Strategy


class ThinSessionHandoff(Strategy):
    id = "h10"
    title = "Thin session break survives volume handoff"
    requires = Requirements(
        quotes=True,
        flow=False,
        handoff=True,
        notes="Thin-session quotes and trades, exchange calendar and main-session volume.",
    )
    time_exit_minutes = 30

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        sessions = self.book.calibration_sessions
        self.thin = {s: SessionStatistic(sessions) for s in self.targets()}
        self.opening = {s: SessionStatistic(sessions) for s in self.targets()}
        self._open_index: tuple[str, int | None] | None = None

    def open_index(self) -> int | None:
        """Minute index of the main opening O within the current (UTC-day) session."""
        session = self.market.session
        if session is None:
            return None
        if self._open_index is not None and self._open_index[0] == session.key:
            return self._open_index[1]
        index = None
        calendar = self.universe.handoff_calendar
        if calendar is not None and self.universe.calendar is None:
            main = exchange_session(calendar, session.open.date())
            if main is not None:
                index = int((main.open - session.open).total_seconds() // 60)
        self._open_index = (session.key, index)
        return index

    def on_session(self) -> None:
        previous = self._open_index[0] if self._open_index else None
        if previous is not None:
            for statistic in (*self.thin.values(), *self.opening.values()):
                statistic.close(previous)
        super().on_session()

    def evaluate(self, now: datetime) -> list[Candidate]:
        o = self.open_index()
        session = self.market.session
        if o is not None and session is not None and o >= 120:
            i = self.minute
            for symbol in self.targets():
                tape = self.market.tapes[symbol]
                if i == o - 1:
                    self.thin[symbol].record(session.key, tape.sum_notional(o - 120, o - 1))
                if i == o + 4:
                    self.opening[symbol].record(session.key, tape.sum_notional(o, o + 4))
        return super().evaluate(now)

    def arm(self, symbol: str) -> Setup | None:
        o = self.open_index()
        i = self.minute
        if o is None or o < 120 or i != o - 1:
            return None
        tape = self.market.tapes[symbol]
        u, l_ = tape.max_high(o - 120, o - 31), tape.min_low(o - 120, o - 31)
        thin_notional = tape.sum_notional(o - 120, o - 1)
        median = self.thin[symbol].median()
        if u is None or l_ is None or thin_notional is None or median is None:
            return None
        tick = self.tick(symbol)
        breakout = next(
            (
                b
                for b in range(o - 30, o)
                if (close := tape.close(b)) is not None and close > u + tick
            ),
            None,
        )
        if breakout is None or thin_notional >= median:
            return None
        closes = [tape.close(b) for b in range(breakout, o)]
        if any(c is None or c < u for c in closes):
            return None
        price = tape.close(i)
        sigma5 = self.market.sigma(symbol, 5, i, price) if price else None
        sigma10 = self.market.sigma(symbol, 10, i, price) if price else None
        if sigma5 is None or sigma10 is None:
            return None
        return self.new_setup(
            symbol,
            "ARMED",
            U=u,
            L=l_,
            breakout_minute=breakout,
            thin_notional=thin_notional,
            thin_median=median,
            sigma5=sigma5,
            sigma10=sigma10,
            open_minute=o,
        )

    def step(self, setup: Setup) -> Candidate | None:
        i, tape = self.minute, self.market.tapes[setup.symbol]
        a, s = setup.anchors, setup.scratch
        o, u = a["open_minute"], a["U"]
        close, low, high = tape.close(i), tape.low(i), tape.high(i)
        if close is None or low is None or high is None:
            self.expire(setup, "data quality: missing bar")
            return None
        if setup.state == "ARMED":
            if close < u:
                self.expire(setup, "close below U before the handoff check")
                return None
            if i < o + 4:
                return None
            main = tape.sum_notional(o, o + 4)
            q60 = self.opening[setup.symbol].quantile(0.60)
            if main is None or q60 is None or main <= q60:
                self.expire(setup, "main-session volume handoff failed", notional=main, q60=q60)
                return None
            self.transition(setup, "HANDOFF", "handoff", notional=main, q60=q60)
            return None
        if i > o + 19:
            self.expire(setup, "no confirmation by O+20")
            return None
        if close < u:
            self.expire(setup, "close below U after the handoff")
            return None
        if setup.state == "HANDOFF":
            band_low = u * math.exp(-0.25 * a["sigma5"])
            band_high = u * math.exp(0.25 * a["sigma5"])
            if band_low <= low <= band_high and close > u:
                s["pullback"] = {"low": low, "high": high, "at": i}
                self.transition(setup, "PULLBACK", "pullback", low=low, high=high)
            return None
        pullback = s["pullback"]
        if i > pullback["at"] and close > pullback["high"]:
            return self.candidate(
                setup, stop=pullback["low"] - self.tick(setup.symbol), sigma10=a["sigma10"]
            )
        return None
