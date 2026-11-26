"""Coordinator polling, backoff, and reauth tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eloverblik_dk.api import (
    ElOverblikAuthError,
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
    earliest_history_date,
)
from custom_components.eloverblik_dk.coordinator import ElOverblikCoordinator
from custom_components.eloverblik_dk.statistics import request_statistic_rebuild
from tests.fake_client import FakeElOverblikClient

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

POINT = MeteringPoint(
    metering_point_id="571313174000000017",
    type_of_mp="E17",
    direction="consumption",
    address="Nørregade 12, 8000 Aarhus C",
    meter_reading_occurrence="PT1H",
)


def _reading(moment: datetime, kwh: float) -> EnergyReading:
    return EnergyReading(POINT.metering_point_id, moment, timedelta(hours=1), kwh)


@pytest.fixture
def coordinator_entry(hass) -> MockConfigEntry:
    """Add a configured entry without setting it up through the platform."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ElOverblik (DK)",
        data={
            CONF_REFRESH_TOKEN: "token",
            CONF_METERING_POINTS: [POINT.as_dict()],
        },
        options={
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: 30,
            CONF_IMPORT_STATISTICS: False,
        },
        unique_id=POINT.metering_point_id,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch):
    """Fix the coordinator clock on 26 November 2026 in Denmark."""

    def _now(tz):
        return datetime(2026, 11, 26, 12, 0, tzinfo=tz)

    monkeypatch.setattr(
        "custom_components.eloverblik_dk.coordinator.current_time", _now
    )


async def test_coordinator_backfills_then_refreshes_the_trailing_window(
    hass, coordinator_entry, frozen_clock
) -> None:
    """The first poll loads the year, and the next poll only asks for two weeks."""
    FakeElOverblikClient.reset()
    FakeElOverblikClient.points = [POINT]
    FakeElOverblikClient.readings = [
        _reading(datetime(2026, 11, 24, 10, tzinfo=UTC), 3.0)
    ]
    coordinator = ElOverblikCoordinator(
        hass, coordinator_entry, FakeElOverblikClient("token")
    )
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert FakeElOverblikClient.calls[0][0] == date(2026, 1, 1)
    assert FakeElOverblikClient.calls[0][1] == date(2026, 11, 26)
    assert FakeElOverblikClient.calls[0][2] == "Actual"
    summary = coordinator.data.points[POINT.metering_point_id].summary
    assert summary.day_total_kwh == pytest.approx(3.0)
    assert summary.latest_data_date.isoformat() == "2026-11-24"

    await coordinator.async_refresh()
    assert FakeElOverblikClient.calls[1][0] == date(2026, 11, 12)


async def test_multi_year_history_loads_that_window_then_trails(
    hass, coordinator_entry, frozen_clock
) -> None:
    """730 and 1825 days fetch back to that day, then later polls trail."""
    cases = (
        (730, date(2024, 11, 26)),
        (1825, date(2021, 11, 27)),
    )
    for days, start in cases:
        hass.config_entries.async_update_entry(
            coordinator_entry,
            options={
                CONF_SCAN_INTERVAL_HOURS: 6,
                CONF_HISTORY_DAYS: days,
                CONF_IMPORT_STATISTICS: False,
            },
        )
        FakeElOverblikClient.reset()
        FakeElOverblikClient.points = [POINT]
        FakeElOverblikClient.readings = [
            _reading(datetime(2021, 11, 27, 0, tzinfo=UTC), 1.0)
        ]
        coordinator = ElOverblikCoordinator(
            hass, coordinator_entry, FakeElOverblikClient("token")
        )
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        assert FakeElOverblikClient.calls[0][0] == start
        assert FakeElOverblikClient.calls[0][1] == date(2026, 11, 26)
        assert start >= earliest_history_date(date(2026, 11, 26))
        await coordinator.async_refresh()
        assert FakeElOverblikClient.calls[1][0] == date(2026, 11, 12)


async def test_rate_limit_keeps_the_last_good_snapshot(
    hass, coordinator_entry, frozen_clock
) -> None:
    """HTTP 429 fails the update without dropping data that was already fetched."""
    FakeElOverblikClient.reset()
    FakeElOverblikClient.points = [POINT]
    FakeElOverblikClient.readings = [
        _reading(datetime(2026, 11, 24, 10, tzinfo=UTC), 3.0)
    ]
    coordinator = ElOverblikCoordinator(
        hass, coordinator_entry, FakeElOverblikClient("token")
    )
    await coordinator.async_refresh()
    snapshot = coordinator.data

    FakeElOverblikClient.series_error = ElOverblikRateLimitError("busy", retry_after=45)
    await coordinator.async_refresh()
    assert coordinator.data is snapshot
    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.last_exception.retry_after == pytest.approx(45)


async def test_rejected_token_starts_reauth(
    hass, coordinator_entry, frozen_clock
) -> None:
    """An authentication failure is reported and a reauth flow is started."""
    FakeElOverblikClient.reset()
    FakeElOverblikClient.meter_error = ElOverblikAuthError("expired")
    coordinator = ElOverblikCoordinator(
        hass, coordinator_entry, FakeElOverblikClient("token")
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == SOURCE_REAUTH for flow in flows)


async def test_statistics_import_is_called_when_enabled(
    hass, coordinator_entry, frozen_clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled statistics import receives the cached readings for each point."""
    imported: list[str] = []

    async def fake_import(hass, point, readings, **kwargs):
        del hass, kwargs
        imported.append(point.metering_point_id)
        assert readings
        return 1

    monkeypatch.setattr(
        "custom_components.eloverblik_dk.coordinator.async_import_meter_statistics",
        fake_import,
    )
    hass.config_entries.async_update_entry(
        coordinator_entry,
        options={
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: 30,
            CONF_IMPORT_STATISTICS: True,
        },
    )
    FakeElOverblikClient.reset()
    FakeElOverblikClient.points = [POINT]
    FakeElOverblikClient.readings = [
        _reading(datetime(2026, 11, 24, 10, tzinfo=UTC), 3.0)
    ]
    coordinator = ElOverblikCoordinator(
        hass, coordinator_entry, FakeElOverblikClient("token")
    )
    await coordinator.async_refresh()
    assert imported == [POINT.metering_point_id]


async def test_later_polls_append_and_a_history_increase_rebuilds(
    hass, coordinator_entry, frozen_clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A covered window is appended; a pending history increase rewrites it."""
    modes: list[str] = []

    async def fake_import(hass, point, readings, **kwargs):
        del hass, point, readings
        modes.append(kwargs["mode"])
        return 1

    monkeypatch.setattr(
        "custom_components.eloverblik_dk.coordinator.async_import_meter_statistics",
        fake_import,
    )
    hass.config_entries.async_update_entry(
        coordinator_entry,
        options={
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: 180,
            CONF_IMPORT_STATISTICS: True,
        },
    )
    FakeElOverblikClient.reset()
    FakeElOverblikClient.points = [POINT]
    FakeElOverblikClient.readings = [
        _reading(datetime(2026, 6, 1, 0, tzinfo=UTC), 1.0),
        _reading(datetime(2026, 11, 24, 10, tzinfo=UTC), 3.0),
    ]
    coordinator = ElOverblikCoordinator(
        hass, coordinator_entry, FakeElOverblikClient("token")
    )
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert modes == ["auto", "append"]
    assert FakeElOverblikClient.calls[0][2] == "Actual"
    assert FakeElOverblikClient.calls[0][0] == date(2026, 1, 1)
    assert FakeElOverblikClient.calls[1][0] == date(2026, 11, 12)

    request_statistic_rebuild(hass, coordinator_entry.entry_id)
    await coordinator.async_refresh()
    assert modes[-1] == "rebuild"
    assert FakeElOverblikClient.calls[-1][0] == date(2026, 1, 1)
    assert FakeElOverblikClient.calls[-1][2] == "Actual"
