"""Hourly long-term statistics for the Home Assistant Energy dashboard.

Home Assistant 2026.11 requires ``mean_type`` and ``unit_class`` on external
statistics. Earlier 2025 releases reject unknown metadata keys, so the metadata
builder only includes fields the installed ``StatisticMetaData`` declares.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .api import EnergyReading, MeteringPoint
from .const import DOMAIN

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


@dataclass(frozen=True, slots=True)
class PlannedStatistic:
    """One hourly external statistic, with a cumulative ``sum`` and ``state``."""

    start: datetime
    state: float
    sum: float


class StatisticLookupError(Exception):
    """The recorder could not be asked for the previous cumulative sum."""


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
    return UsageSummary(
        latest_data_date=latest,
        day_total_kwh=_round_kwh(by_date[latest]),
        month_total_kwh=_round_kwh(month_total),
        year_total_kwh=_round_kwh(year_total),
        latest_hour_start=latest_hour_start,
        latest_hour_kwh=latest_hour_kwh,
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
) -> int:
    """Import completed hours that are newer than the stored cumulative sum."""
    statistic_id = statistic_id_for(point.metering_point_id)
    try:
        last_start, last_sum = await _read_last_sum(hass, statistic_id)
    except StatisticLookupError:
        _LOGGER.warning(
            "Skipping statistics import for %s until the recorder can be read",
            point.metering_point_id,
        )
        return 0
    window = readings
    if last_start is None:
        cutoff = now.astimezone(tz).date() - timedelta(days=history_days)
        window = [
            reading
            for reading in readings
            if reading.start.astimezone(tz).date() >= cutoff
        ]
    hourly = completed_hours(aggregate_hourly(window), now=now.astimezone(UTC))
    planned = plan_incremental_statistics(
        hourly, last_start=last_start, last_sum=last_sum
    )
    if not planned:
        return 0
    metadata = build_statistic_metadata(statistic_name_for(point), statistic_id)
    stats = [
        {"start": item.start, "state": item.state, "sum": item.sum} for item in planned
    ]
    try:
        _, add_external = _load_recorder()
    except ImportError:
        _LOGGER.warning("Recorder statistics API is not available")
        return 0
    add_external(hass, metadata, stats)
    _LOGGER.debug(
        "Imported %s hourly statistics for %s, continuing from %s kWh",
        len(stats),
        point.metering_point_id,
        last_sum,
    )
    return len(stats)


async def _read_last_sum(hass: Any, statistic_id: str) -> tuple[datetime | None, float]:
    """Load the previous sum, running a synchronous lookup in the executor."""
    try:
        get_last_statistics, _ = _load_recorder()
    except ImportError as err:
        raise StatisticLookupError("Recorder statistics are unavailable") from err
    try:
        if _is_async(get_last_statistics):
            result = await get_last_statistics(
                hass, 1, statistic_id, True, {"sum", "state"}
            )
        elif hasattr(hass, "async_add_executor_job"):
            result = await hass.async_add_executor_job(
                get_last_statistics,
                hass,
                1,
                statistic_id,
                True,
                {"sum", "state"},
            )
        else:
            result = get_last_statistics(hass, 1, statistic_id, True, {"sum", "state"})
    except Exception as err:
        raise StatisticLookupError("Could not read the previous statistic") from err
    return _last_sum_from_result(result, statistic_id)


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


def _load_recorder() -> tuple[Any, Any]:
    module = importlib.import_module("homeassistant.components.recorder.statistics")
    return module.get_last_statistics, module.async_add_external_statistics


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
