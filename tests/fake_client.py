"""In-memory ElOverblik client used by Home Assistant tests."""

from __future__ import annotations

from datetime import date
from typing import ClassVar

from custom_components.eloverblik_dk.api import EnergyReading, MeteringPoint


class FakeElOverblikClient:
    """Stand-in that records calls and returns canned metering data."""

    points: ClassVar[list[MeteringPoint]] = []
    readings: ClassVar[list[EnergyReading]] = []
    meter_error: ClassVar[Exception | None] = None
    series_error: ClassVar[Exception | None] = None
    calls: ClassVar[list[tuple[date, date, str, tuple[str, ...]]]] = []
    tokens: ClassVar[list[str]] = []

    def __init__(
        self,
        refresh_token: str,
        session: object | None = None,
        *,
        token_cache: object | None = None,
        time_zone: object | None = None,
    ) -> None:
        """Remember the token and ignore the HTTP session."""
        self.refresh_token = refresh_token
        self.session = session
        self.token_cache = token_cache
        self.time_zone = time_zone
        type(self).tokens.append(refresh_token)

    async def async_get_metering_points(self) -> list[MeteringPoint]:
        """Return the canned metering points or the configured error."""
        if type(self).meter_error is not None:
            raise type(self).meter_error
        return list(type(self).points)

    async def async_get_time_series(
        self,
        metering_point_ids: list[str],
        date_from: date,
        date_to: date,
        aggregation: str = "Hour",
        directions: dict[str, str] | None = None,
    ) -> list[EnergyReading]:
        """Return readings for the requested ids and record the window."""
        del directions
        type(self).calls.append(
            (date_from, date_to, aggregation, tuple(metering_point_ids))
        )
        if type(self).series_error is not None:
            raise type(self).series_error
        wanted = set(metering_point_ids)
        return [
            reading
            for reading in type(self).readings
            if reading.metering_point_id in wanted
        ]

    async def async_close(self) -> None:
        """Match the real client's close method."""

    @classmethod
    def reset(cls) -> None:
        """Clear class-level state between tests."""
        cls.points = []
        cls.readings = []
        cls.meter_error = None
        cls.series_error = None
        cls.calls = []
        cls.tokens = []
