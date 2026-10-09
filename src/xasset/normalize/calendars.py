"""Exchange minutes use [open, close); canonical bars are labeled by their end."""

from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

import exchange_calendars as xcals

from xasset.normalize.timebase import utc


@lru_cache(maxsize=32)
def calendar(name: str) -> Any:
    # Explicit side excludes the closing instant and includes the opening minute.
    return xcals.get_calendar(name, side="left")


def expected_bar_ends(name: str, start: datetime, end: datetime) -> set[datetime]:
    """Bar ends in (start, end], with holidays, breaks and half-days respected."""
    start, end = utc(start), utc(end)
    if end <= start:
        return set()
    minutes = calendar(name).minutes_in_range(start, end - timedelta(minutes=1))
    return {minute.to_pydatetime() + timedelta(minutes=1) for minute in minutes}


def completed_sessions(name: str, as_of: datetime, count: int) -> list[tuple[str, set[datetime]]]:
    if count < 1:
        raise ValueError("session count must be positive")
    as_of = utc(as_of)
    cal = calendar(name)
    sessions = cal.sessions_in_range(as_of.date() - timedelta(days=count * 4 + 30), as_of.date())
    completed = [
        session for session in sessions if cal.session_close(session).to_pydatetime() <= as_of
    ]
    result = []
    for session in completed[-count:]:
        ends = {
            minute.to_pydatetime() + timedelta(minutes=1) for minute in cal.session_minutes(session)
        }
        result.append((str(session.date()), ends))
    return result
