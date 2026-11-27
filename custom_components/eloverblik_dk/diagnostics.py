"""Diagnostics for ElOverblik (DK), with the token and the household redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_REFRESH_TOKEN
from .coordinator import ElOverblikCoordinator

TO_REDACT = {
    CONF_REFRESH_TOKEN,
    "address",
    "metering_point_id",
    "parent_metering_point_id",
    "unique_id",
    "title",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return what is useful for a bug report, without identifying the home.

    Metering points are listed by position (``point_1``, ...), not by id, so
    the report can be shared in a public issue.
    """
    coordinator: ElOverblikCoordinator = entry.runtime_data.coordinator
    aliases = {
        point_id: f"point_{index}"
        for index, point_id in enumerate(
            (coordinator.data.points if coordinator.data else {}), start=1
        )
    }
    points: dict[str, Any] = {}
    if coordinator.data is not None:
        for point_id, state in coordinator.data.points.items():
            summary = state.summary
            points[aliases[point_id]] = {
                "type_of_mp": state.point.type_of_mp,
                "direction": state.point.direction,
                "meter_reading_occurrence": state.point.meter_reading_occurrence,
                "has_parent": state.point.parent_metering_point_id is not None,
                "latest_data_date": _iso(summary.latest_data_date),
                "latest_resolution": summary.latest_resolution,
                "day_total_kwh": summary.day_total_kwh,
                "month_total_kwh": summary.month_total_kwh,
                "year_total_kwh": summary.year_total_kwh,
                "statistics_covered_from": _iso(coordinator.covered_from.get(point_id)),
            }
    window = coordinator.last_fetch_window
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_exception": repr(coordinator.last_exception)
            if coordinator.last_exception
            else None,
            "statistics_failed": coordinator.statistics_failed,
            "update_interval_hours": coordinator.scan_interval_hours,
            "history_days": coordinator.history_days,
            "import_statistics": coordinator.import_statistics,
            "last_fetch_window": [day.isoformat() for day in window]
            if window
            else None,
            "cached_readings": coordinator.cached_reading_count,
        },
        "points": points,
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None
