"""Hourly long-term statistics for the Home Assistant Energy dashboard.

Home Assistant 2026.11 requires ``mean_type`` and ``unit_class`` on external
statistics. Earlier 2025 releases reject unknown metadata keys, so the metadata
builder only includes fields the installed ``StatisticMetaData`` declares.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .api import EnergyReading, MeteringPoint
from .const import DOMAIN, earliest_history_date

ImportMode = Literal["auto", "append", "rebuild"]
_CLEAR_TIMEOUT_SECONDS = 60.0
_PENDING_REBUILD = "pending_statistic_rebuild"

_LOGGER = logging.getLogger(__name__)

_STATISTIC_UNIT = "kWh"


@dataclass(frozen=True, slots=True)
class UsageSummary:
    """Totals derived from the readings currently held for one metering point."""

    latest_data_date: date | None
    day_total_kwh: float | None
    month_total_kwh: float | None
    year_total_kwh: float | None
    latest_hour_start: datetime | None
    latest_hour_kwh: float | None
    latest_interval_start: datetime | None = None
    latest_interval_end: datetime | None = None
    latest_interval_kwh: float | None = None
    latest_resolution: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedStatistic:
    """One hourly external statistic, with a cumulative ``sum`` and ``state``."""

    start: datetime
    state: float
    sum: float


class StatisticLookupError(Exception):
    """The recorder could not be asked for the previous cumulative sum."""


def resolution_from_duration(duration: timedelta) -> str:
    """Return an ISO-8601 duration such as ``PT15M``, ``PT1H``, or ``P1D``."""
    seconds = int(duration.total_seconds())
    if seconds <= 0:
        return "PT0S"
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"P{days}D"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"PT{hours}H"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"PT{minutes}M"
    return f"PT{seconds}S"


def history_cutoff(now: datetime, tz: ZoneInfo, history_days: int) -> date:
    """Return the first local date included in a statistics window.

    The cutoff stays on or after 1 January of the year five years before
    ``now``, which is the oldest day DataHub still serves.
    """
    local_today = now.astimezone(tz).date()
    requested = local_today - timedelta(days=history_days)
    return max(requested, earliest_history_date(local_today))


def needs_statistic_rebuild(
    *,
    earliest_start: datetime | None,
    readings: Sequence[EnergyReading],
    history_days: int,
    now: datetime,
    tz: ZoneInfo,
) -> bool:
    """Return whether stored statistics start later than the requested window.

    A rebuild is required when the recorder's oldest hour is newer than the
    history cutoff and the fetched series actually contains an earlier hour.
    Missing DataHub history is not a rebuild: there is nothing older to add,
    and repeating a clear on every poll would rewrite the same sums.
    """
    if earliest_start is None:
        return False
    cutoff = history_cutoff(now, tz, history_days)
    if earliest_start.astimezone(tz).date() <= cutoff:
        return False
    earliest_hour = floor_utc_hour(earliest_start)
    return any(
        reading.start.astimezone(tz).date() >= cutoff
        and floor_utc_hour(reading.start) < earliest_hour
        for reading in readings
    )


def request_statistic_rebuild(hass: Any, entry_id: str) -> None:
    """Remember that the next import for ``entry_id`` must rewrite statistics."""
    pending = hass.data.setdefault(DOMAIN, {}).setdefault(_PENDING_REBUILD, set())
    pending.add(entry_id)


def statistic_rebuild_requested(hass: Any, entry_id: str) -> bool:
    """Return whether options flow asked for a statistics rebuild."""
    pending = (hass.data.get(DOMAIN) or {}).get(_PENDING_REBUILD, ())
    return entry_id in pending


def clear_statistic_rebuild(hass: Any, entry_id: str) -> None:
    """Drop a pending rebuild request after it has been applied."""
    pending = (hass.data.get(DOMAIN) or {}).get(_PENDING_REBUILD)
    if isinstance(pending, set):
        pending.discard(entry_id)


def floor_utc_hour(moment: datetime) -> datetime:
    """Return the UTC hour that contains ``moment``."""
    if moment.tzinfo is None:
        aware = moment.replace(tzinfo=UTC)
    else:
        aware = moment.astimezone(UTC)
    return aware.replace(minute=0, second=0, microsecond=0)


def aggregate_hourly(
    readings: Sequence[EnergyReading],
) -> list[tuple[datetime, float]]:
    """Sum readings into UTC clock hours.

    Quarter-hour and hourly values land in the hour they start in. Longer
    blocks, such as a daily total, are spread evenly across the hours they
    cover so a coarse series is not dropped.
    """
    buckets: dict[datetime, float] = {}
    for reading in readings:
        if reading.duration > timedelta(hours=1):
            hours = _hours_touched(reading.start, reading.duration)
            if not hours:
                continue
            share = reading.kwh / len(hours)
            for hour in hours:
                buckets[hour] = buckets.get(hour, 0.0) + share
            continue
        hour = floor_utc_hour(reading.start)
        buckets[hour] = buckets.get(hour, 0.0) + reading.kwh
    return sorted(buckets.items())


def completed_hours(
    hourly: Sequence[tuple[datetime, float]],
    *,
    now: datetime,
) -> list[tuple[datetime, float]]:
    """Drop the still-open UTC hour so a partial period is not locked in."""
    current = floor_utc_hour(now)
    return [
        (floor_utc_hour(start), kwh)
        for start, kwh in hourly
        if floor_utc_hour(start) < current
    ]


def plan_incremental_statistics(
    hourly: Sequence[tuple[datetime, float]],
    *,
    last_start: datetime | None,
    last_sum: float,
) -> list[PlannedStatistic]:
    """Continue a cumulative sum after the last recorded hour.

    Hours at or before ``last_start`` are left untouched, so a later poll
    cannot duplicate them or rebuild the sum from zero. A negative hour is
    skipped and the cursor still moves past it; otherwise the following hours
    would be appended again on the next run.
    """
    merged: dict[datetime, float] = {}
    for start, kwh in hourly:
        hour = floor_utc_hour(start)
        merged[hour] = merged.get(hour, 0.0) + kwh
    cursor = floor_utc_hour(last_start) if last_start is not None else None
    total = float(last_sum)
    planned: list[PlannedStatistic] = []
    for hour in sorted(merged):
        if cursor is not None and hour <= cursor:
            continue
        kwh = merged[hour]
        if kwh < 0:
            cursor = hour
            continue
        total = round(total + kwh, 6)
        planned.append(PlannedStatistic(start=hour, state=total, sum=total))
        cursor = hour
    return planned


def summarize_usage(
    readings: Sequence[EnergyReading],
    *,
    now: datetime,
    tz: ZoneInfo,
) -> UsageSummary:
    """Build the sensor totals in the Home Assistant time zone.

    The latest day follows the newest reading, which is normally one to three
    days behind today. Month and year totals follow the calendar month and
    year of ``now`` and are zero when DataHub has not published that period yet.
    """
    if not readings:
        return UsageSummary(None, None, None, None, None, None)
    local_today = now.astimezone(tz).date()
    by_date: dict[date, float] = {}
    for reading in readings:
        local_day = reading.start.astimezone(tz).date()
        by_date[local_day] = by_date.get(local_day, 0.0) + reading.kwh
    latest = max(by_date)
    month_total = sum(
        kwh
        for day, kwh in by_date.items()
        if day.year == local_today.year and day.month == local_today.month
    )
    year_total = sum(
        kwh for day, kwh in by_date.items() if day.year == local_today.year
    )
    hourly = aggregate_hourly(readings)
    if hourly:
        hour_start, hour_kwh = hourly[-1]
        latest_hour_start: datetime | None = hour_start
        latest_hour_kwh: float | None = _round_kwh(hour_kwh)
    else:
        latest_hour_start = None
        latest_hour_kwh = None
    latest_reading = max(readings, key=lambda item: item.start)
    return UsageSummary(
        latest_data_date=latest,
        day_total_kwh=_round_kwh(by_date[latest]),
        month_total_kwh=_round_kwh(month_total),
        year_total_kwh=_round_kwh(year_total),
        latest_hour_start=latest_hour_start,
        latest_hour_kwh=latest_hour_kwh,
        latest_interval_start=latest_reading.start,
        latest_interval_end=latest_reading.start + latest_reading.duration,
        latest_interval_kwh=_round_kwh(latest_reading.kwh),
        latest_resolution=resolution_from_duration(latest_reading.duration),
    )


def statistic_id_for(metering_point_id: str) -> str:
    """Return the external statistic id for a metering point."""
    return f"{DOMAIN}:energy_{metering_point_id}"


def statistic_name_for(point: MeteringPoint) -> str:
    """Return the name shown in the Energy dashboard statistic picker."""
    if point.direction == "export":
        return f"{point.address} · salg til net (return to grid)"
    return f"{point.address} · forbrug (grid consumption)"


def metadata_from_annotations(
    annotations: set[str],
    *,
    name: str,
    statistic_id: str,
    mean_type_none: object,
    unit_class: str,
) -> dict[str, Any]:
    """Build statistic metadata using only fields this Home Assistant knows."""
    meta: dict[str, Any] = {
        "has_sum": True,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": _STATISTIC_UNIT,
    }
    if "mean_type" in annotations:
        meta["mean_type"] = mean_type_none
    if "has_mean" in annotations:
        meta["has_mean"] = False
    if "unit_class" in annotations:
        meta["unit_class"] = unit_class
    if "mean_type" not in meta and "has_mean" not in meta:
        meta["has_mean"] = False
    return meta


def build_statistic_metadata(name: str, statistic_id: str) -> dict[str, Any]:
    """Build metadata for the Home Assistant version that is installed."""
    annotations: set[str] = set()
    try:
        module = importlib.import_module("homeassistant.components.recorder.models")
        metadata_type = module.StatisticMetaData
        annotations = set(getattr(metadata_type, "__annotations__", {}))
    except ImportError:
        annotations = set()
    return metadata_from_annotations(
        annotations,
        name=name,
        statistic_id=statistic_id,
        mean_type_none=_mean_type_none(),
        unit_class=_energy_unit_class(),
    )


async def async_import_meter_statistics(
    hass: Any,
    point: MeteringPoint,
    readings: Sequence[EnergyReading],
    *,
    history_days: int,
    now: datetime,
    tz: ZoneInfo,
    mode: ImportMode = "auto",
) -> int:
    """Import completed hours, rebuilding the cumulative sum when history grows.

    ``append`` continues after the newest stored hour and is what the 6-hour
    poll uses once a full window is in place. ``rebuild`` clears the external
    statistic and writes one continuous sum across the configured window.
    ``auto`` rebuilds only when the oldest stored hour is newer than that
    window and older readings are actually available. A lookup or clear
    failure returns ``-1`` so the caller can retry without treating the
    window as up to date.
    """
    statistic_id = statistic_id_for(point.metering_point_id)
    try:
        if mode == "append":
            last_start, last_sum = await _read_last_sum(hass, statistic_id)
            earliest: datetime | None = last_start
        else:
            last_start, last_sum, earliest = await _read_bounds(hass, statistic_id)
    except StatisticLookupError:
        _LOGGER.warning(
            "Skipping statistics import for %s until the recorder can be read",
            point.metering_point_id,
        )
        return -1
    replace = mode == "rebuild" or (
        mode == "auto"
        and needs_statistic_rebuild(
            earliest_start=earliest,
            readings=readings,
            history_days=history_days,
            now=now,
            tz=tz,
        )
    )
    if replace or last_start is None:
        window = _history_window(readings, history_days=history_days, now=now, tz=tz)
        baseline_start: datetime | None = None
        baseline_sum = 0.0
    else:
        window = readings
        baseline_start = last_start
        baseline_sum = last_sum
    hourly = completed_hours(aggregate_hourly(window), now=now.astimezone(UTC))
    planned = plan_incremental_statistics(
        hourly, last_start=baseline_start, last_sum=baseline_sum
    )
    if not planned:
        return 0
    metadata = build_statistic_metadata(statistic_name_for(point), statistic_id)
    stats = [
        {"start": item.start, "state": item.state, "sum": item.sum} for item in planned
    ]
    try:
        _, add_external, _, _ = _load_recorder()
    except ImportError:
        _LOGGER.warning("Recorder statistics API is not available")
        return -1
    if replace:
        cleared = await _async_clear_external_statistic(hass, statistic_id)
        if not cleared:
            return -1
    add_external(hass, metadata, stats)
    if replace:
        _LOGGER.info(
            "Rebuilt %s hourly statistics for %s from %s",
            len(stats),
            point.metering_point_id,
            stats[0]["start"].isoformat(),
        )
    else:
        _LOGGER.debug(
            "Imported %s hourly statistics for %s, continuing from %s kWh",
            len(stats),
            point.metering_point_id,
            baseline_sum,
        )
    return len(stats)


def _history_window(
    readings: Sequence[EnergyReading],
    *,
    history_days: int,
    now: datetime,
    tz: ZoneInfo,
) -> list[EnergyReading]:
    """Keep readings on or after the first day of the statistics window."""
    cutoff = history_cutoff(now, tz, history_days)
    return [
        reading for reading in readings if reading.start.astimezone(tz).date() >= cutoff
    ]


async def _read_last_sum(hass: Any, statistic_id: str) -> tuple[datetime | None, float]:
    """Load the newest sum, running a synchronous lookup in the executor."""
    try:
        get_last_statistics, _, _, _ = _load_recorder()
    except ImportError as err:
        raise StatisticLookupError("Recorder statistics are unavailable") from err
    try:
        result = await _invoke(
            hass,
            get_last_statistics,
            hass,
            1,
            statistic_id,
            True,
            {"sum", "state"},
        )
    except Exception as err:
        raise StatisticLookupError("Could not read the previous statistic") from err
    return _last_sum_from_result(result, statistic_id)


async def _read_bounds(
    hass: Any, statistic_id: str
) -> tuple[datetime | None, float, datetime | None]:
    """Load the newest sum and the oldest stored hour for one statistic."""
    last_start, last_sum = await _read_last_sum(hass, statistic_id)
    try:
        _, _, during_period, _ = _load_recorder()
    except ImportError as err:
        raise StatisticLookupError("Recorder statistics are unavailable") from err
    if during_period is None:
        return last_start, last_sum, None
    try:
        result = await _invoke(
            hass,
            during_period,
            hass,
            datetime(2000, 1, 1, tzinfo=UTC),
            None,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
    except Exception as err:
        raise StatisticLookupError("Could not read the oldest statistic") from err
    return last_start, last_sum, _earliest_start(result, statistic_id)


def _earliest_start(
    result: Mapping[str, Sequence[Mapping[str, Any]]] | None, statistic_id: str
) -> datetime | None:
    """Return the oldest start timestamp in a statistics result."""
    rows = list((result or {}).get(statistic_id) or [])
    if not rows:
        return None
    row = min(rows, key=lambda item: _coerce_utc(item["start"]))
    return _coerce_utc(row["start"])


async def _async_clear_external_statistic(hass: Any, statistic_id: str) -> bool:
    """Delete one external statistic and wait until the recorder has finished.

    ``Recorder.async_clear_statistics`` removes the statistic metadata and its
    rows. ``async_add_external_statistics`` then inserts a new series. The
    import is queued only after that clear has finished: a clear still waiting
    on the recorder queue would delete the rows just written, and updating
    rows in place leaves any hour that was not rewritten on the old cumulative
    ``sum``. The Energy dashboard differences those sums, so a leftover smaller
    sum becomes a negative hour. A time-range clear is not used, because it
    deletes rows without rebuilding the sums that follow them.
    """
    try:
        _, _, _, get_instance = _load_recorder()
    except ImportError:
        _LOGGER.warning("Recorder is not available; statistics were not cleared")
        return False
    try:
        clear = get_instance(hass).async_clear_statistics
    except Exception:
        _LOGGER.warning(
            "Recorder is not ready; leaving statistics for %s unchanged",
            statistic_id,
        )
        return False
    if _is_async(clear):
        await clear([statistic_id])
        return True
    done = asyncio.Event()

    def _finished() -> None:
        loop = getattr(hass, "loop", None)
        if loop is None:
            done.set()
            return
        loop.call_soon_threadsafe(done.set)

    try:
        clear([statistic_id], on_done=_finished)
    except TypeError:
        clear([statistic_id])
        return True
    try:
        async with asyncio.timeout(_CLEAR_TIMEOUT_SECONDS):
            await done.wait()
    except TimeoutError:
        _LOGGER.warning(
            "Timed out clearing statistics for %s; the series was not rewritten",
            statistic_id,
        )
        return False
    return True


async def _invoke(hass: Any, func: Any, *args: Any) -> Any:
    """Call a recorder helper, moving a synchronous one off the event loop."""
    if _is_async(func):
        return await func(*args)
    if hasattr(hass, "async_add_executor_job"):
        return await hass.async_add_executor_job(func, *args)
    return func(*args)


def _last_sum_from_result(
    result: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    statistic_id: str,
) -> tuple[datetime | None, float]:
    rows = list((result or {}).get(statistic_id) or [])
    if not rows:
        return None, 0.0
    row = max(rows, key=lambda item: _coerce_utc(item["start"]))
    return _coerce_utc(row["start"]), float(row.get("sum") or 0.0)


def _coerce_utc(value: datetime | int | float) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    timestamp = float(value)
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    return datetime.fromtimestamp(timestamp, tz=UTC)


def _load_recorder() -> tuple[Any, Any, Any, Any]:
    statistics = importlib.import_module("homeassistant.components.recorder.statistics")
    recorder = importlib.import_module("homeassistant.components.recorder")
    return (
        statistics.get_last_statistics,
        statistics.async_add_external_statistics,
        statistics.statistics_during_period,
        recorder.get_instance,
    )


def _is_async(func: Any) -> bool:
    import inspect

    return inspect.iscoroutinefunction(func)


def _mean_type_none() -> object:
    for module_name in (
        "homeassistant.components.recorder.models",
        "homeassistant.components.recorder.models.statistics",
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        mean_type = getattr(module, "StatisticMeanType", None)
        if mean_type is not None:
            return mean_type.NONE
    return 0


def _energy_unit_class() -> str:
    try:
        module = importlib.import_module("homeassistant.util.unit_conversion")
    except ImportError:
        return "energy"
    converter = getattr(module, "EnergyConverter", None)
    if converter is None:
        return "energy"
    return str(converter.UNIT_CLASS)


def _hours_touched(start: datetime, duration: timedelta) -> list[datetime]:
    end = start + duration
    hour = floor_utc_hour(start)
    last = floor_utc_hour(end - timedelta(microseconds=1))
    hours: list[datetime] = []
    while hour <= last:
        hours.append(hour)
        hour += timedelta(hours=1)
    return hours


def _round_kwh(value: float) -> float:
    return round(value, 3)
