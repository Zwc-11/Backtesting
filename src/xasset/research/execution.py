"""Minute-bar cash ledger with next-open fills and conservative intrabar exits."""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

import polars as pl

from xasset.config import Instrument
from xasset.normalize.calendars import calendar, expected_bar_ends
from xasset.research.contracts import DailyResult, Fold, Trade
from xasset.research.costs import CostProfile
from xasset.research.strategy import Parameters

MINUTE = timedelta(minutes=1)


@dataclass
class Simulation:
    trades: list[Trade]
    daily: list[DailyResult]
    ending_cash: float


def simulate(
    bars: pl.DataFrame,
    instrument: Instrument,
    fold: Fold,
    signals: dict[datetime, datetime],
    profile: CostProfile,
    multiplier: Literal[1, 2],
    capital: float,
    allocation: float,
) -> Simulation:
    params = Parameters.model_validate(fold.parameters)
    frame = bars.filter(
        (pl.col("symbol") == instrument.id)
        & (pl.col("ts_end") > fold.test_start)
        & (pl.col("ts_end") <= fold.test_end)
    ).sort("ts_end")
    closes: dict[date, datetime] = {}
    if instrument.session == "regular" and instrument.calendar:
        cal = calendar(instrument.calendar)
        closes = {
            session.date(): cal.session_close(session).to_pydatetime()
            for session in cal.sessions_in_range(fold.test_start.date(), fold.test_end.date())
        }
        allowed = expected_bar_ends(instrument.calendar, fold.test_start, fold.test_end)
        frame = frame.filter(pl.col("ts_end").is_in(sorted(allowed)))
    cash = capital
    position: dict[str, Any] | None = None
    trades: list[Trade] = []
    equity_by_day: dict[date, float] = {}
    for row in frame.iter_rows(named=True):
        end = row["ts_end"]
        opening = end - MINUTE
        fillable = row["volume"] is not None and row["volume"] > 0
        session_close = closes.get(opening.date())
        if position is not None and position.get("session_close") is not None:
            if opening >= position["session_close"]:
                raise ValueError("Missing executable session close; cannot silently hold overnight")
        if min(row[name] for name in ("open", "high", "low", "close")) <= 0:
            raise ValueError("Cash spot execution requires positive prices")
        # Missing the exact next opening bar cancels an entry; signals never wait.
        if (
            position is None
            and fillable
            and opening in signals
            and fold.test_start <= signals[opening] <= opening
            and min(
                opening + timedelta(minutes=params.hold_minutes + 1),
                session_close or fold.test_end + MINUTE,
            )
            <= fold.test_end
        ):
            price = row["open"]
            budget = max(0.0, cash * allocation)
            # Find the largest whole lot whose notional AND fees fit the allocation.
            low, high = 0, math.floor(budget / (price * instrument.lot_size))
            while low < high:
                middle = (low + high + 1) // 2
                units = middle * instrument.lot_size
                if units * price + profile.per_fill(units * price, units, multiplier) <= budget:
                    low = middle
                else:
                    high = middle - 1
            units = low * instrument.lot_size
            if units > 0:
                fee = profile.per_fill(units * price, units, multiplier)
                cash -= units * price + fee
                position = dict(
                    entry_at=opening,
                    entry_bar_end=end,
                    signal_at=signals[opening],
                    entry_price=price,
                    units=units,
                    entry_cost=fee,
                    deadline=opening + timedelta(minutes=params.hold_minutes),
                    session_close=session_close,
                )
        if position is not None and fillable:
            price = position["entry_price"]
            stop, target = price * (1 - params.stop_pct), price * (1 + params.target_pct)
            exit_price: float | None = None
            reason: Literal["stop", "target", "time", "fold_end", "session_close"] = "time"
            exit_at = opening
            if row["open"] <= stop:
                exit_price, reason = row["open"], "stop"
            elif row["open"] >= target:
                exit_price, reason = row["open"], "target"
            elif opening >= position["deadline"]:
                exit_price = row["open"]
            elif row["low"] <= stop:
                exit_price, reason = stop, "stop"
                exit_at = end - timedelta(microseconds=1)
            elif row["high"] >= target:
                exit_price, reason = target, "target"
                exit_at = end - timedelta(microseconds=1)
            elif session_close is not None and end == session_close:
                exit_price, reason, exit_at = row["close"], "session_close", end
            elif end == fold.test_end:
                exit_price, reason, exit_at = row["close"], "fold_end", end
            if exit_price is not None:
                units = position["units"]
                fee = profile.per_fill(units * exit_price, units, multiplier)
                cash += units * exit_price - fee
                trades.append(
                    Trade(
                        id=f"{fold.id}:{len(trades)}",
                        symbol=instrument.id,
                        fold=fold.id,
                        signal_at=position["signal_at"],
                        entry_at=position["entry_at"],
                        entry_bar_end=position["entry_bar_end"],
                        exit_at=exit_at,
                        exit_bar_end=end,
                        entry_price=price,
                        exit_price=exit_price,
                        exit_reason=reason,
                        units=units,
                        entry_notional=units * price,
                        exit_notional=units * exit_price,
                        gross_pnl=(exit_price - price) * units,
                        cost=position["entry_cost"] + fee,
                    )
                )
                position = None
        # Marking a position is separate from permission to execute a fill.
        equity_by_day[(end - timedelta(microseconds=1)).date()] = cash + (
            position["units"] * row["close"] if position else 0
        )
    if position is not None:
        raise ValueError("Unclosed position: no executable exit by the declared fold boundary")
    daily: list[DailyResult] = []
    day = fold.test_start.date()
    last_day = (fold.test_end - timedelta(microseconds=1)).date()
    previous_equity = capital
    while day <= last_day:
        equity = equity_by_day.get(day, previous_equity)
        pnl = equity - previous_equity
        daily.append(DailyResult(day=day, net_pnl=pnl, portfolio_return=pnl / previous_equity))
        previous_equity = equity
        day += timedelta(days=1)
    return Simulation(trades, daily, cash)
