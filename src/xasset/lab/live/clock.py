"""Desk clock: the local UTC clock corrected by a measured offset to exchange time.

Windows and laptop clocks can drift by seconds, which would misplace trades in
minute bars and make bars close early. The offset is measured with Cristian's method
against Binance's public server time (accuracy about half the round trip) and
re-measured periodically. Readings never move backwards.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx

TIME_URL = "https://data-api.binance.vision/api/v3/time"


class Clock:
    def __init__(self) -> None:
        self.offset = timedelta(0)
        self.round_trip: timedelta | None = None
        self.measured_at: datetime | None = None
        self.error: str | None = None
        self._last = datetime.min.replace(tzinfo=UTC)

    def now(self) -> datetime:
        value = datetime.now(UTC) + self.offset
        if value < self._last:
            return self._last
        self._last = value
        return value

    def apply(self, server: datetime, sent: datetime, received: datetime) -> None:
        trip = received - sent
        midpoint = sent + trip / 2
        self.offset = server - midpoint
        self.round_trip = trip
        self.measured_at = server
        self.error = None

    async def measure(self, client: httpx.AsyncClient, samples: int = 5) -> None:
        """Keep the sample with the shortest round trip (least asymmetric delay)."""
        best: tuple[timedelta, datetime, datetime, datetime] | None = None
        try:
            for _ in range(samples):
                sent = datetime.now(UTC)
                start = time.perf_counter()
                response = await client.get(TIME_URL, timeout=5)
                elapsed = timedelta(seconds=time.perf_counter() - start)
                response.raise_for_status()
                server = datetime.fromtimestamp(response.json()["serverTime"] / 1000, tz=UTC)
                if best is None or elapsed < best[0]:
                    best = (elapsed, server, sent, sent + elapsed)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            self.error = f"clock check failed: {type(exc).__name__}"
            return
        if best is not None:
            self.apply(best[1], best[2], best[3])

    def status(self) -> dict[str, object]:
        return {
            "offset_ms": round(self.offset.total_seconds() * 1000, 1),
            "round_trip_ms": None
            if self.round_trip is None
            else round(self.round_trip.total_seconds() * 1000, 1),
            "measured_at": self.measured_at.isoformat() if self.measured_at else None,
            "error": self.error,
        }
