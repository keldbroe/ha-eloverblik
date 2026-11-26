"""Sensors for ElOverblik consumption and export."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import ElOverblikCoordinator, ElOverblikData, PointState
from .statistics import UsageSummary

_KIND = {"consumption": "Forbrug", "export": "Salg/eksport"}


def _day(summary: UsageSummary) -> float | None:
    return summary.day_total_kwh


def _month(summary: UsageSummary) -> float | None:
    return summary.month_total_kwh


def _year(summary: UsageSummary) -> float | None:
    return summary.year_total_kwh


def _hour(summary: UsageSummary) -> float | None:
    return summary.latest_hour_kwh


def _latest_date(summary: UsageSummary) -> date | None:
    return summary.latest_data_date


_SENSOR_TYPES: tuple[
    tuple[SensorEntityDescription, Callable[[UsageSummary], Any]], ...
] = (
    (
        SensorEntityDescription(
            key="day_total",
            translation_key="day_total",
            device_class=SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            suggested_display_precision=3,
        ),
        _day,
    ),
    (
        SensorEntityDescription(
            key="month_total",
            translation_key="month_total",
            device_class=SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=3,
        ),
        _month,
    ),
    (
        SensorEntityDescription(
            key="year_total",
            translation_key="year_total",
            device_class=SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=3,
        ),
        _year,
    ),
    (
        SensorEntityDescription(
            key="latest_hour",
            translation_key="latest_hour",
            device_class=SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            suggested_display_precision=3,
        ),
        _hour,
    ),
    (
        SensorEntityDescription(
            key="latest_data_date",
            translation_key="latest_data_date",
            device_class=SensorDeviceClass.DATE,
            entity_category=EntityCategory.DIAGNOSTIC,
            icon="mdi:calendar-clock",
        ),
        _latest_date,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one device of sensors for each selected metering point."""
    coordinator: ElOverblikCoordinator = entry.runtime_data.coordinator
    entities: list[ElOverblikSensor] = []
    data = coordinator.data
    if data is None:
        return
    for point_id in data.points:
        entities.extend(
            ElOverblikSensor(coordinator, point_id, description, reader)
            for description, reader in _SENSOR_TYPES
        )
    async_add_entities(entities)


class ElOverblikSensor(CoordinatorEntity[ElOverblikCoordinator], SensorEntity):
    """A single ElOverblik measurement for one metering point."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: ElOverblikCoordinator,
        point_id: str,
        description: SensorEntityDescription,
        reader: Callable[[UsageSummary], Any],
    ) -> None:
        """Bind the sensor to one metering point and one description."""
        super().__init__(coordinator)
        self.entity_description = description
        self._point_id = point_id
        self._reader = reader
        self._attr_unique_id = f"{point_id}_{description.key}"
        point = self._state.point
        kind = _KIND.get(point.direction, point.direction)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, point_id)},
            name=f"{point.address} · {kind}",
            manufacturer="Energinet",
            model=("Grid export" if point.direction == "export" else "Grid consumption")
            + f" ({point.type_of_mp})",
            serial_number=point_id,
            configuration_url="https://eloverblik.dk/welcome",
        )

    @property
    def available(self) -> bool:
        """Keep the last good value visible while ElOverblik is backing off."""
        data = self.coordinator.data
        return data is not None and self._point_id in data.points

    @property
    def native_value(self) -> float | date | None:
        """Return the summary value for this sensor."""
        summary = self._summary
        if summary is None:
            return None
        return self._reader(summary)

    @property
    def icon(self) -> str | None:
        """Use a solar icon for export and a calendar icon for the data date."""
        if self.entity_description.key == "latest_data_date":
            return "mdi:calendar-clock"
        point = self._state.point if self._state is not None else None
        if point is not None and point.direction == "export":
            return "mdi:solar-power"
        if self.entity_description.key == "latest_hour":
            return "mdi:clock-outline"
        if self.entity_description.key == "day_total":
            return "mdi:calendar-today"
        return "mdi:flash"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the metering point and the period the value belongs to."""
        state = self._state
        if state is None:
            return {}
        summary = state.summary
        attributes: dict[str, Any] = {
            "metering_point_id": state.point.metering_point_id,
            "metering_point_type": state.point.direction,
            "type_of_mp": state.point.type_of_mp,
        }
        if (
            self.entity_description.key == "day_total"
            and summary.latest_data_date is not None
        ):
            attributes["data_date"] = summary.latest_data_date.isoformat()
        if (
            self.entity_description.key == "latest_hour"
            and summary.latest_hour_start is not None
        ):
            attributes["period_start"] = _iso(summary.latest_hour_start)
        return attributes

    @property
    def _state(self) -> PointState | None:
        data: ElOverblikData | None = self.coordinator.data
        if data is None:
            return None
        return data.points.get(self._point_id)

    @property
    def _summary(self) -> UsageSummary | None:
        state = self._state
        if state is None:
            return None
        return state.summary


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")
