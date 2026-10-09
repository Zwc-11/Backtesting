"""Declared sessions: exchange calendars for equities, UTC days for continuous markets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

from xasset.normalize.calendars import calendar as exchange_calendar


@dataclass(frozen=True, slots=True)
class Session:
    key: str
    open: datetime
    close: datetime

    @property
    def minutes(self) -> int:
        return int((self.close - self.open).total_seconds() // 60)


class Sessions:
    """Map bar starts to sessions. ``None`` declares UTC-day sessions (continuous markets)."""

    def __init__(self, calendar: str | None):
        self.calendar = calendar
        self._cache: dict[date, Session | None] = {}

    @property
    def max_minutes(self) -> int:
        return 1440 if self.calendar is None else 390

    def for_date(self, day: date) -> Session | None:
        if day in self._cache:
            return self._cache[day]
        if self.calendar is None:
            opened = datetime(day.year, day.month, day.day, tzinfo=UTC)
            session: Session | None = Session(day.isoformat(), opened, opened + timedelta(days=1))
        else:
            session = exchange_session(self.calendar, day)
        self._cache[day] = session
        return session

    def locate(self, bar_start: datetime) -> tuple[Session, int] | None:
        """Session containing the bar starting at ``bar_start`` and its minute index."""
        session = self.for_date(bar_start.astimezone(UTC).date())
        if session is None or not session.open <= bar_start < session.close:
            return None
        return session, int((bar_start - session.open).total_seconds() // 60)


@lru_cache(maxsize=4096)
def exchange_session(name: str, day: date) -> Session | None:
    cal = exchange_calendar(name)
    stamp = day.isoformat()
    if not cal.is_session(stamp):
        return None
    opened = cal.session_open(stamp).to_pydatetime().astimezone(UTC)
    closed = cal.session_close(stamp).to_pydatetime().astimezone(UTC)
    if opened.date() != day or (closed - timedelta(microseconds=1)).date() != day:
        raise ValueError(f"{name} session {stamp} crosses a UTC date; unsupported by the lab")
    if cal.session_has_break(stamp):
        raise ValueError(f"{name} session {stamp} has a midday break; unsupported by the lab")
    return Session(stamp, opened, closed)
