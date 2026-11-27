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
from homeassistant.helpers.storage import Store
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
    DOMAIN,
    MAX_HISTORY_DAYS,
    MAX_SCAN_INTERVAL_HOURS,
    MIN_HISTORY_DAYS,
    MIN_SCAN_INTERVAL_HOURS,
    NAME,
    STORAGE_VERSION,
    TRAILING_REFRESH_DAYS,
    earliest_history_date,
)
from .helpers import ha_time_zone
from .statistics import (
    ImportMode,
    StatisticLookupError,
    UsageSummary,
    async_import_meter_statistics,
    async_last_statistic_start,
    clear_statistic_rebuild,
    history_cutoff,
    statistic_id_for,
    statistic_rebuild_requested,
    summarize_usage,
)

_LOGGER = logging.getLogger(__name__)


def current_time(tz: ZoneInfo) -> datetime:
    """Return the current time in ``tz``. Tests replace this clock."""
    return datetime.now(tz)


def storage_key(entry_id: str) -> str:
    """Return the ``.storage`` key that holds one entry's backfill marker."""
    return f"{DOMAIN}.{entry_id}"


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
    """Poll ElOverblik and keep the last good snapshot when the API backs off.

    Only what is needed is fetched. The sensors need the current calendar
    year once per process, and after that the trailing two weeks. The
    statistics need the configured history window only until it has been
    written once. That is remembered per metering point in ``.storage`` as
    the first day the window covered (``covered_from``), so a restart does
    not download several years again. After that, statistics need only the
    hours after the newest stored one, plus the trailing window that is
    checked for DataHub corrections.
    """

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
        self._year_loaded: int | None = None
        self._force_rebuild = False
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        self._covered_from: dict[str, date] | None = None
        self.statistics_failed = False
        self.last_fetch_window: tuple[date, date] | None = None
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

    @property
    def covered_from(self) -> dict[str, date]:
        """Return the first day each point's statistics window was written from."""
        return dict(self._covered_from or {})

    @property
    def cached_reading_count(self) -> int:
        """Return how many readings are held in memory."""
        return sum(len(bucket) for bucket in self._readings.values())

    async def async_rebuild_statistics(self) -> None:
        """Fetch the full history window and rewrite its long-term statistics."""
        self._start_rebuild()
        await self.async_refresh()
        if not self.last_update_success or self.statistics_failed:
            message = "ElOverblik could not rebuild statistics"
            if self.last_exception is not None:
                message = f"{message}: {self.last_exception}"
            raise HomeAssistantError(message)

    def _start_rebuild(self) -> None:
        self._force_rebuild = True
        self._readings.clear()
        self._year_loaded = None

    async def _async_update_data(self) -> ElOverblikData:
        """Fetch metering data. Rate limits and outages keep the previous data."""
        selected = self._selected_points()
        if not selected:
            raise UpdateFailed("No metering points are selected")
        if self.import_statistics and statistic_rebuild_requested(
            self.hass, self.entry.entry_id
        ):
            self._start_rebuild()
        self.statistics_failed = False
        await self._async_load_marker()
        live = await self._guard(self.client.async_get_metering_points())
        by_id = {point.metering_point_id: point for point in live}
        points = [by_id.get(point.metering_point_id, point) for point in selected]
        zone = ha_time_zone(self.hass)
        now = current_time(zone)
        today = now.date()
        cutoff = history_cutoff(now, zone, self.history_days)
        write_statistics = self.import_statistics or self._force_rebuild
        modes: dict[str, ImportMode | None] = {}
        statistic_starts: list[date] = []
        if write_statistics:
            for point in points:
                mode, start = await self._plan_point(point, cutoff, today, zone)
                modes[point.metering_point_id] = mode
                if start is not None:
                    statistic_starts.append(start)
        start, end = self._fetch_window(today, statistic_starts)
        self.last_fetch_window = (start, end)
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
        self._year_loaded = today.year
        states: dict[str, PointState] = {}
        statistics_ok = True
        marker_changed = False
        for point in points:
            point_id = point.metering_point_id
            cached = list(self._readings.get(point_id, {}).values())
            summary = summarize_usage(cached, now=now, tz=zone)
            states[point_id] = PointState(point=point, summary=summary)
            if not write_statistics:
                continue
            mode = modes.get(point_id)
            if mode is None:
                statistics_ok = False
                continue
            try:
                written = await async_import_meter_statistics(
                    self.hass,
                    point,
                    cached,
                    history_days=self.history_days,
                    now=now,
                    tz=zone,
                    mode=mode,
                )
            except Exception:
                statistics_ok = False
                _LOGGER.exception(
                    "Failed to import long-term statistics for %s", point_id
                )
                continue
            if written < 0:
                statistics_ok = False
            elif mode != "append":
                assert self._covered_from is not None
                self._covered_from[point_id] = cutoff
                marker_changed = True
        if marker_changed:
            await self._async_save_marker()
        if self._force_rebuild and statistics_ok:
            self._force_rebuild = False
            clear_statistic_rebuild(self.hass, self.entry.entry_id)
        self.statistics_failed = not statistics_ok
        self._prune(today, zone)
        latest = [
            state.summary.latest_data_date
            for state in states.values()
            if state.summary.latest_data_date is not None
        ]
        _LOGGER.debug(
            "Updated %s ElOverblik metering points from %s; latest data date %s",
            len(states),
            start.isoformat(),
            max(latest).isoformat() if latest else "unknown",
        )
        return ElOverblikData(points=states)

    async def _plan_point(
        self,
        point: MeteringPoint,
        cutoff: date,
        today: date,
        zone: ZoneInfo,
    ) -> tuple[ImportMode | None, date | None]:
        """Return the import mode for one point and the first day it needs.

        ``None`` as the mode means the recorder could not be read; that point
        skips statistics on this poll and is retried on the next one.
        """
        if self._force_rebuild:
            return "rebuild", cutoff
        point_id = point.metering_point_id
        try:
            last_start = await async_last_statistic_start(
                self.hass, statistic_id_for(point_id)
            )
        except StatisticLookupError:
            _LOGGER.warning(
                "Recorder statistics for %s are not readable yet; "
                "trying again on the next update",
                point_id,
            )
            return None, None
        covered = (self._covered_from or {}).get(point_id)
        if covered is None or cutoff < covered:
            return "auto", cutoff
        trailing = today - timedelta(days=TRAILING_REFRESH_DAYS)
        if last_start is None:
            # The window was already tried and DataHub had nothing for it, or
            # the statistic was deleted by hand. Write whatever is new without
            # downloading the whole window again on every poll.
            return "auto", trailing
        return "append", min(trailing, last_start.astimezone(zone).date())

    def _selected_points(self) -> list[MeteringPoint]:
        stored = self.entry.data.get(CONF_METERING_POINTS, [])
        points: list[MeteringPoint] = []
        for item in stored:
            if isinstance(item, dict):
                point = MeteringPoint.from_dict(item)
                if point.metering_point_id:
                    points.append(point)
        return points

    def _fetch_window(
        self, today: date, statistic_starts: list[date]
    ) -> tuple[date, date]:
        """Return the inclusive range to request.

        The sensors need 1 January the first time in a process (or a year),
        and after that the trailing two weeks. Statistics add the start each
        point needs. The range never starts before DataHub's retention of the
        previous five calendar years plus the current year.
        """
        if self._year_loaded != today.year:
            sensor_start = date(today.year, 1, 1)
        else:
            sensor_start = today - timedelta(days=TRAILING_REFRESH_DAYS)
        start = min([sensor_start, *statistic_starts])
        return max(start, earliest_history_date(today)), today

    def _merge(self, readings: list[EnergyReading]) -> None:
        for reading in readings:
            bucket = self._readings.setdefault(reading.metering_point_id, {})
            bucket[reading.start] = reading

    def _prune(self, today: date, zone: ZoneInfo) -> None:
        """Drop readings the sensors and the next poll no longer need.

        A multi-year backfill is written once; keeping it in memory would only
        make every later poll slower.
        """
        keep_from_day = min(
            date(today.year, 1, 1), today - timedelta(days=TRAILING_REFRESH_DAYS)
        )
        keep_from = datetime.combine(keep_from_day, datetime.min.time(), tzinfo=zone)
        for point_id, bucket in self._readings.items():
            self._readings[point_id] = {
                start: reading
                for start, reading in bucket.items()
                if start >= keep_from
            }

    async def _async_load_marker(self) -> None:
        if self._covered_from is not None:
            return
        covered: dict[str, date] = {}
        try:
            stored = await self._store.async_load()
        except Exception:
            _LOGGER.warning("Could not read the ElOverblik backfill marker")
            stored = None
        raw = (stored or {}).get("covered_from") if isinstance(stored, dict) else None
        if isinstance(raw, dict):
            for point_id, value in raw.items():
                try:
                    covered[str(point_id)] = date.fromisoformat(str(value))
                except ValueError:
                    continue
        self._covered_from = covered

    async def _async_save_marker(self) -> None:
        data = {
            "covered_from": {
                point_id: day.isoformat()
                for point_id, day in (self._covered_from or {}).items()
            }
        }
        await self._store.async_save(data)

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
