"""Data update coordinator for ElOverblik metering points."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ElOverblikApiError,
    ElOverblikAuthError,
    ElOverblikClient,
    ElOverblikRateLimitError,
    EnergyReading,
    MeteringPoint,
)
from .const import (
    AGGREGATION_ACTUAL,
    CONF_HISTORY_DAYS,
    CONF_IMPORT_STATISTICS,
    CONF_METERING_POINTS,
    CONF_SCAN_INTERVAL_HOURS,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_SCAN_INTERVAL_HOURS,
    LOCAL_TIME_ZONE,
    MAX_HISTORY_DAYS,
    MAX_SCAN_INTERVAL_HOURS,
    MIN_HISTORY_DAYS,
    MIN_SCAN_INTERVAL_HOURS,
    NAME,
    TRAILING_REFRESH_DAYS,
)
from .statistics import (
    ImportMode,
    UsageSummary,
    async_import_meter_statistics,
    clear_statistic_rebuild,
    statistic_rebuild_requested,
    summarize_usage,
)

_LOGGER = logging.getLogger(__name__)


def current_time(tz: ZoneInfo) -> datetime:
    """Return the current time in ``tz``. Tests replace this clock."""
    return datetime.now(tz)


@dataclass(frozen=True, slots=True)
class PointState:
    """The latest summary for one selected metering point."""

    point: MeteringPoint
    summary: UsageSummary


@dataclass(frozen=True, slots=True)
class ElOverblikData:
    """Coordinator payload. Raw readings stay on the coordinator, not in state."""

    points: dict[str, PointState]


@dataclass(slots=True)
class ElOverblikRuntime:
    """Objects kept on the config entry while it is loaded."""

    client: ElOverblikClient
    coordinator: ElOverblikCoordinator


class ElOverblikCoordinator(DataUpdateCoordinator[ElOverblikData]):
    """Poll ElOverblik and keep the last good snapshot when the API backs off."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ElOverblikClient,
    ) -> None:
        """Create a coordinator that polls on the configured interval."""
        self.entry = entry
        self.client = client
        self._readings: dict[str, dict[datetime, EnergyReading]] = {}
        self._history_days_loaded: int | None = None
        self._has_full_history = False
        self._force_rebuild = False
        self._statistics_ready: set[str] = set()
        self.statistics_failed = False
        super().__init__(
            hass,
            _LOGGER,
            name=NAME,
            update_interval=timedelta(hours=self.scan_interval_hours),
            config_entry=entry,
        )

    @property
    def scan_interval_hours(self) -> int:
        """Return the polling interval, clamped to the supported range."""
        return _clamped_int(
            self.entry.options.get(
                CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS
            ),
            default=DEFAULT_SCAN_INTERVAL_HOURS,
            minimum=MIN_SCAN_INTERVAL_HOURS,
            maximum=MAX_SCAN_INTERVAL_HOURS,
        )

    @property
    def history_days(self) -> int:
        """Return how many days of statistics to import on the first run."""
        return _clamped_int(
            self.entry.options.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS),
            default=DEFAULT_HISTORY_DAYS,
            minimum=MIN_HISTORY_DAYS,
            maximum=MAX_HISTORY_DAYS,
        )

    @property
    def import_statistics(self) -> bool:
        """Return whether Energy dashboard statistics should be written."""
        return bool(self.entry.options.get(CONF_IMPORT_STATISTICS, True))

    async def async_rebuild_statistics(self) -> None:
        """Fetch the full history window and rewrite its long-term statistics."""
        self._force_rebuild = True
        self._readings.clear()
        self._has_full_history = False
        await self.async_refresh()
        if not self.last_update_success or self.statistics_failed:
            message = "ElOverblik could not rebuild statistics"
            if self.last_exception is not None:
                message = f"{message}: {self.last_exception}"
            raise HomeAssistantError(message)

    async def _async_update_data(self) -> ElOverblikData:
        """Fetch metering data. Rate limits and outages keep the previous data."""
        selected = self._selected_points()
        if not selected:
            raise UpdateFailed("No metering points are selected")
        if self.import_statistics and statistic_rebuild_requested(
            self.hass, self.entry.entry_id
        ):
            self._force_rebuild = True
            self._readings.clear()
            self._has_full_history = False
        self.statistics_failed = False
        live = await self._guard(self.client.async_get_metering_points())
        by_id = {point.metering_point_id: point for point in live}
        points = [by_id.get(point.metering_point_id, point) for point in selected]
        zone = self._zone()
        now = current_time(zone)
        start, end = self._fetch_window(now.date())
        readings = await self._guard(
            self.client.async_get_time_series(
                [point.metering_point_id for point in points],
                start,
                end,
                directions={
                    point.metering_point_id: point.direction for point in points
                },
                aggregation=AGGREGATION_ACTUAL,
            )
        )
        self._merge(readings)
        self._has_full_history = True
        states: dict[str, PointState] = {}
        statistics_ok = True
        for point in points:
            cached = list(self._readings.get(point.metering_point_id, {}).values())
            summary = summarize_usage(cached, now=now, tz=zone)
            states[point.metering_point_id] = PointState(point=point, summary=summary)
            if not self.import_statistics and not self._force_rebuild:
                continue
            try:
                written = await async_import_meter_statistics(
                    self.hass,
                    point,
                    cached,
                    history_days=self.history_days,
                    now=now,
                    tz=zone,
                    mode=self._import_mode(point.metering_point_id),
                )
            except Exception:
                statistics_ok = False
                _LOGGER.exception(
                    "Failed to import long-term statistics for %s",
                    point.metering_point_id,
                )
            else:
                if written < 0:
                    statistics_ok = False
                else:
                    self._statistics_ready.add(point.metering_point_id)
        if self._force_rebuild and statistics_ok:
            self._force_rebuild = False
            clear_statistic_rebuild(self.hass, self.entry.entry_id)
        self.statistics_failed = not statistics_ok
        latest = [
            state.summary.latest_data_date
            for state in states.values()
            if state.summary.latest_data_date is not None
        ]
        _LOGGER.debug(
            "Updated %s ElOverblik metering points; latest data date %s",
            len(states),
            max(latest).isoformat() if latest else "unknown",
        )
        return ElOverblikData(points=states)

    def _import_mode(self, point_id: str) -> ImportMode:
        """Rebuild on request; otherwise check coverage once, then append."""
        if self._force_rebuild:
            return "rebuild"
        if point_id not in self._statistics_ready:
            return "auto"
        return "append"

    def _selected_points(self) -> list[MeteringPoint]:
        stored = self.entry.data.get(CONF_METERING_POINTS, [])
        points: list[MeteringPoint] = []
        for item in stored:
            if isinstance(item, dict):
                point = MeteringPoint.from_dict(item)
                if point.metering_point_id:
                    points.append(point)
        return points

    def _fetch_window(self, today: date) -> tuple[date, date]:
        """Return the inclusive range to request.

        The first poll in a process (and any change of the history option)
        loads the current calendar year, or further back when the statistics
        backfill asks for it. Later polls only refresh the trailing two weeks.
        """
        history_days = self.history_days
        if self._history_days_loaded != history_days:
            self._readings.clear()
            self._history_days_loaded = history_days
            self._has_full_history = False
        full_start = min(
            date(today.year, 1, 1),
            today - timedelta(days=history_days),
        )
        if not self._has_full_history:
            return full_start, today
        trailing = today - timedelta(days=TRAILING_REFRESH_DAYS)
        return max(full_start, trailing), today

    def _merge(self, readings: list[EnergyReading]) -> None:
        for reading in readings:
            bucket = self._readings.setdefault(reading.metering_point_id, {})
            bucket[reading.start] = reading

    def _zone(self) -> ZoneInfo:
        name = getattr(self.hass.config, "time_zone", None) or LOCAL_TIME_ZONE
        try:
            return ZoneInfo(str(name))
        except Exception:
            return ZoneInfo(LOCAL_TIME_ZONE)

    async def _guard(self, awaitable: Any) -> Any:
        try:
            return await awaitable
        except ElOverblikAuthError as err:
            raise ConfigEntryAuthFailed(
                "ElOverblik rejected the refresh token"
            ) from err
        except ElOverblikRateLimitError as err:
            delay = err.retry_after if err.retry_after and err.retry_after > 0 else 60
            raise UpdateFailed(
                "ElOverblik is rate limiting requests",
                retry_after=float(delay),
            ) from err
        except ElOverblikApiError as err:
            raise UpdateFailed(str(err)) from err


def _clamped_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))
