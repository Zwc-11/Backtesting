"""Causal session-open and scheduled-event signals shared by all experiments."""

from datetime import datetime, timedelta
from typing import Any

import polars as pl

from xasset.config import Instrument
from xasset.normalize.calendars import calendar, expected_bar_ends
from xasset.research.contracts import Parameter, Request
from xasset.research.strategy import Parameters, observable, signals

MINUTE = timedelta(minutes=1)


def _change(
    lookup: dict[datetime, dict[str, Any]],
    expected: list[datetime],
    instrument: Instrument,
    opening_price: bool = False,
) -> float | None:
    if not expected or any(stamp not in lookup for stamp in expected):
        return None
    rows = [lookup[stamp] for stamp in expected]
    quoted = instrument.asset_class in {"fx", "cfd", "index", "futures"}
    if len({row["source"] for row in rows}) != 1 or any(
        not observable(row, quoted) or "roll_boundary" in row["flags"] for row in rows
    ):
        return None
    baseline = rows[0]["open" if opening_price else "close"]
    if baseline <= 0 or rows[-1]["close"] <= 0:
        return None
    return float(rows[-1]["close"] / baseline - 1)


def generate(
    request: Request,
    candidate: dict[str, Parameter],
    cutoff: datetime | None = None,
) -> dict[datetime, datetime]:
    spec = request.experiment
    params = Parameters.model_validate(candidate)
    universe = {item.id: item for item in request.instruments}
    driver, follower = universe[params.driver], universe[params.follower]
    end = min(cutoff, request.data_end) if cutoff else request.data_end
    bars = request.bars.filter(pl.col("ts_end") <= end)
    if spec.strategy == "cross_asset_leadlag":
        return signals(bars, driver, spec.frequency, candidate)
    lookup = {
        row["ts_end"]: row
        for row in bars.filter(pl.col("symbol") == params.driver).iter_rows(named=True)
    }
    output: dict[datetime, datetime] = {}
    if spec.strategy == "event_response":
        for event in spec.events:
            instant = event.at + timedelta(minutes=params.observation_minutes)
            if (
                event.kind != params.event_kind
                or event.known_at > event.at
                or event.at <= request.data_start
                or instant > end
            ):
                continue
            expected = [
                event.at + timedelta(minutes=i) for i in range(params.observation_minutes + 1)
            ]
            change = _change(lookup, expected, driver)
            if change is not None and change * params.direction >= params.threshold:
                output[instant] = instant
        return output
    if spec.strategy != "session_open":
        raise ValueError("Unknown native signal strategy")
    if follower.calendar is None:
        raise ValueError("Session-open signals require a verified follower exchange calendar")
    cal = calendar(follower.calendar)
    sessions = cal.sessions_in_range(request.data_start.date(), end.date())
    for session in sessions:
        instant = cal.session_open(session).to_pydatetime()
        if not request.data_start <= instant <= end:
            continue
        if params.anchor == "previous_close":
            previous = cal.session_close(cal.previous_session(session)).to_pydatetime()
            # Every minute is required: weekend crypto works, unknown quote-market
            # closures/gaps cancel the signal rather than carrying stale prices.
            minutes = int((instant - previous).total_seconds() // 60)
            expected = [previous + timedelta(minutes=i) for i in range(minutes + 1)]
            change = _change(lookup, expected, driver)
        else:
            if driver.calendar is None:
                raise ValueError("Regional-session signals require a driver exchange calendar")
            region = calendar(driver.calendar)
            region_sessions = region.sessions_in_range(
                instant.date() - timedelta(days=10), instant.date()
            )
            available = [
                item
                for item in region_sessions
                if region.session_open(item).to_pydatetime() < instant
            ]
            if not available:
                continue
            latest = available[-1]
            opened = region.session_open(latest).to_pydatetime()
            closed = min(instant, region.session_close(latest).to_pydatetime())
            if instant - closed > timedelta(days=1):
                continue  # Do not carry a regional holiday's stale session forward.
            expected = sorted(expected_bar_ends(driver.calendar, opened, closed))
            change = _change(lookup, expected, driver, opening_price=True)
        if change is not None and change * params.direction >= params.threshold:
            output[instant] = instant
    return output
