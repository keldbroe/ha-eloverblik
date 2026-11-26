"""Sensor and device tests for consumption and export metering points."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import eloverblik_dk
from custom_components.eloverblik_dk.api import (
    ElOverblikRateLimitError,
    EnergyReading,
    MeteringPoint,
)
from custom_components.eloverblik_dk.const import (
    CONF_HISTORY_DAYS,
    CONF_IMPORT_STATISTICS,
    CONF_METERING_POINTS,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL_HOURS,
    DOMAIN,
)
from tests.fake_client import FakeElOverblikClient

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

CONSUMPTION = MeteringPoint(
    metering_point_id="571313174000000017",
    type_of_mp="E17",
    direction="consumption",
    address="Nørregade 12, 8000 Aarhus C",
    meter_reading_occurrence="PT1H",
)
EXPORT = MeteringPoint(
    metering_point_id="571313174000000066",
    type_of_mp="D06",
    direction="export",
    address="Nørregade 12, 8000 Aarhus C",
    meter_reading_occurrence="PT15M",
    parent_metering_point_id=CONSUMPTION.metering_point_id,
)


def _reading(point: MeteringPoint, moment: datetime, kwh: float) -> EnergyReading:
    return EnergyReading(point.metering_point_id, moment, timedelta(hours=1), kwh)


@pytest.fixture
def configured_entry(hass, monkeypatch: pytest.MonkeyPatch) -> MockConfigEntry:
    """Set up two metering points against the fake client and a fixed clock."""
    FakeElOverblikClient.reset()
    FakeElOverblikClient.points = [CONSUMPTION, EXPORT]
    FakeElOverblikClient.readings = [
        _reading(CONSUMPTION, datetime(2026, 1, 15, 12, tzinfo=UTC), 4.0),
        _reading(CONSUMPTION, datetime(2026, 11, 22, 23, tzinfo=UTC), 1.5),
        _reading(CONSUMPTION, datetime(2026, 11, 23, 10, tzinfo=UTC), 2.5),
        _reading(CONSUMPTION, datetime(2026, 11, 24, 10, tzinfo=UTC), 3.0),
        _reading(EXPORT, datetime(2026, 11, 24, 9, tzinfo=UTC), 0.8),
    ]
    monkeypatch.setattr(eloverblik_dk, "ElOverblikClient", FakeElOverblikClient)
    monkeypatch.setattr(
        "custom_components.eloverblik_dk.coordinator.current_time",
        lambda tz: datetime(2026, 11, 26, 12, tzinfo=tz),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ElOverblik (DK)",
        data={
            CONF_REFRESH_TOKEN: "token",
            CONF_METERING_POINTS: [CONSUMPTION.as_dict(), EXPORT.as_dict()],
        },
        options={
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: 30,
            CONF_IMPORT_STATISTICS: False,
        },
        unique_id="account",
    )
    entry.add_to_hass(hass)
    return entry


def _entity_id(hass, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


async def test_sensors_expose_consumption_and_export(hass, configured_entry) -> None:
    """Each metering point gets a device and the five requested sensors."""
    await hass.config.async_set_time_zone("Europe/Copenhagen")
    assert await hass.config_entries.async_setup(configured_entry.entry_id)
    await hass.async_block_till_done()

    day = hass.states.get(
        _entity_id(hass, f"{CONSUMPTION.metering_point_id}_day_total")
    )
    month = hass.states.get(
        _entity_id(hass, f"{CONSUMPTION.metering_point_id}_month_total")
    )
    year = hass.states.get(
        _entity_id(hass, f"{CONSUMPTION.metering_point_id}_year_total")
    )
    hour = hass.states.get(
        _entity_id(hass, f"{CONSUMPTION.metering_point_id}_latest_hour")
    )
    latest = hass.states.get(
        _entity_id(hass, f"{CONSUMPTION.metering_point_id}_latest_data_date")
    )
    export_day = hass.states.get(
        _entity_id(hass, f"{EXPORT.metering_point_id}_day_total")
    )

    assert float(day.state) == pytest.approx(3.0)
    assert day.attributes["data_date"] == "2026-11-24"
    assert day.attributes["metering_point_type"] == "consumption"
    assert day.attributes["device_class"] == "energy"
    assert "state_class" not in day.attributes

    assert float(month.state) == pytest.approx(7.0)
    assert month.attributes["state_class"] == "total_increasing"
    assert month.attributes["unit_of_measurement"] == "kWh"
    assert float(year.state) == pytest.approx(11.0)
    assert year.attributes["state_class"] == "total_increasing"

    assert float(hour.state) == pytest.approx(3.0)
    assert hour.attributes["period_start"] == "2026-11-24T10:00:00+00:00"
    assert latest.state == "2026-11-24"
    assert latest.attributes["device_class"] == "date"

    assert float(export_day.state) == pytest.approx(0.8)
    assert export_day.attributes["metering_point_type"] == "export"

    registry = er.async_get(hass)
    diagnostic = registry.async_get(latest.entity_id)
    assert diagnostic is not None
    assert diagnostic.entity_category == EntityCategory.DIAGNOSTIC

    device_reg = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(device_reg, configured_entry.entry_id)
    assert len(devices) == 2
    names = {device.name for device in devices}
    assert "Nørregade 12, 8000 Aarhus C · Forbrug" in names
    assert "Nørregade 12, 8000 Aarhus C · Salg/eksport" in names
    assert {device.manufacturer for device in devices} == {"Energinet"}


async def test_sensors_keep_last_value_when_eloverblik_is_rate_limited(
    hass, configured_entry
) -> None:
    """A later 429 leaves the previous sensor values in place."""
    await hass.config.async_set_time_zone("Europe/Copenhagen")
    assert await hass.config_entries.async_setup(configured_entry.entry_id)
    await hass.async_block_till_done()
    entity_id = _entity_id(hass, f"{CONSUMPTION.metering_point_id}_month_total")

    FakeElOverblikClient.series_error = ElOverblikRateLimitError("busy", retry_after=60)
    coordinator = configured_entry.runtime_data.coordinator
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state != "unavailable"
    assert float(state.state) == pytest.approx(7.0)
