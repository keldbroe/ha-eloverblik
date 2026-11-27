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
from custom_components.eloverblik_dk.statistics import (
    StatisticLookupError,
    request_statistic_rebuild,
)
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


@pytest.fixture
def recorder_stub(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the recorder lookups and the import with in-memory stand-ins.

    ``state["last"]`` is the newest stored hour the coordinator sees, and
    ``state["modes"]`` records each import mode it asked for.
    """
    state: dict = {"last": None, "modes": [], "lookup_error": False}

    async def fake_last(hass, statistic_id):
        del hass, statistic_id
        if state["lookup_error"]:
            raise StatisticLookupError("recorder offline")
        return state["last"]

    async def fake_import(hass, point, readings, **kwargs):
        del hass, point
        state["modes"].append(kwargs["mode"])
        state["readings"] = list(readings)
        return 1

    monkeypatch.setattr(
        "custom_components.eloverblik_dk.coordinator.async_last_statistic_start",
        fake_last,
    )
    monkeypatch.setattr(
        "custom_components.eloverblik_dk.coordinator.async_import_meter_statistics",
        fake_import,
    )
    return state


def _set_options(hass, entry, *, days: int, statistics: bool) -> None:
    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: days,
            CONF_IMPORT_STATISTICS: statistics,
        },
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
    hass, coordinator_entry, frozen_clock, recorder_stub
) -> None:
    """730 and 1825 days fetch back to that day, then later polls trail."""
    cases = (
        (730, date(2024, 11, 26)),
        (1825, date(2021, 11, 27)),
    )
    for days, start in cases:
        _set_options(hass, coordinator_entry, days=days, statistics=True)
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
    hass, coordinator_entry, frozen_clock, recorder_stub, monkeypatch
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
    hass, coordinator_entry, frozen_clock, recorder_stub, monkeypatch
) -> None:
    """A covered window is appended; a pending history increase rewrites it."""
    recorder_stub["last"] = datetime(2026, 11, 24, 10, tzinfo=UTC)
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


async def test_without_statistics_only_the_year_is_fetched(
    hass, coordinator_entry, frozen_clock
) -> None:
    """With statistics off, a five-year history is not downloaded at all."""
    _set_options(hass, coordinator_entry, days=1825, statistics=False)
    FakeElOverblikClient.reset()
    FakeElOverblikClient.points = [POINT]
    coordinator = ElOverblikCoordinator(
        hass, coordinator_entry, FakeElOverblikClient("token")
    )
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert FakeElOverblikClient.calls[0][0] == date(2026, 1, 1)


async def test_restart_does_not_download_the_history_again(
    hass, coordinator_entry, frozen_clock, recorder_stub
) -> None:
    """The backfill marker survives a restart, so only the year is fetched."""
    _set_options(hass, coordinator_entry, days=1825, statistics=True)
    FakeElOverblikClient.reset()
    FakeElOverblikClient.points = [POINT]
    FakeElOverblikClient.readings = [
        _reading(datetime(2022, 3, 1, 0, tzinfo=UTC), 1.0),
        _reading(datetime(2026, 11, 24, 10, tzinfo=UTC), 3.0),
    ]
    first = ElOverblikCoordinator(
        hass, coordinator_entry, FakeElOverblikClient("token")
    )
    await first.async_refresh()
    assert FakeElOverblikClient.calls[-1][0] == date(2021, 11, 27)
    assert recorder_stub["modes"] == ["auto"]
    assert first.covered_from == {POINT.metering_point_id: date(2021, 11, 27)}
    # The 2022 reading was written to statistics and is not kept in memory.
    assert first.cached_reading_count == 1

    recorder_stub["last"] = datetime(2026, 11, 24, 10, tzinfo=UTC)
    restarted = ElOverblikCoordinator(
        hass, coordinator_entry, FakeElOverblikClient("token")
    )
    await restarted.async_refresh()
    assert restarted.last_update_success
    assert FakeElOverblikClient.calls[-1][0] == date(2026, 1, 1)
    assert recorder_stub["modes"] == ["auto", "append"]


async def test_restart_after_a_long_outage_fetches_from_the_last_hour(
    hass, coordinator_entry, frozen_clock, recorder_stub
) -> None:
    """Hours missed while Home Assistant was off are fetched and appended."""
    _set_options(hass, coordinator_entry, days=1825, statistics=True)
    FakeElOverblikClient.reset()
    FakeElOverblikClient.points = [POINT]
    first = ElOverblikCoordinator(
        hass, coordinator_entry, FakeElOverblikClient("token")
    )
    await first.async_refresh()

    recorder_stub["last"] = datetime(2025, 10, 3, 21, tzinfo=UTC)
    restarted = ElOverblikCoordinator(
        hass, coordinator_entry, FakeElOverblikClient("token")
    )
    await restarted.async_refresh()
    assert FakeElOverblikClient.calls[-1][0] == date(2025, 10, 3)
    assert recorder_stub["modes"][-1] == "append"


async def test_longer_history_than_the_marker_backfills_again(
    hass, coordinator_entry, frozen_clock, recorder_stub
) -> None:
    """Raising the history past what was written fetches the longer window."""
    _set_options(hass, coordinator_entry, days=180, statistics=True)
    FakeElOverblikClient.reset()
    FakeElOverblikClient.points = [POINT]
    recorder_stub["last"] = datetime(2026, 11, 24, 10, tzinfo=UTC)
    first = ElOverblikCoordinator(
        hass, coordinator_entry, FakeElOverblikClient("token")
    )
    await first.async_refresh()
    assert first.covered_from[POINT.metering_point_id] == date(2026, 5, 30)

    _set_options(hass, coordinator_entry, days=730, statistics=True)
    second = ElOverblikCoordinator(
        hass, coordinator_entry, FakeElOverblikClient("token")
    )
    await second.async_refresh()
    assert FakeElOverblikClient.calls[-1][0] == date(2024, 11, 26)
    assert recorder_stub["modes"] == ["auto", "auto"]

    _set_options(hass, coordinator_entry, days=365, statistics=True)
    third = ElOverblikCoordinator(
        hass, coordinator_entry, FakeElOverblikClient("token")
    )
    await third.async_refresh()
    assert recorder_stub["modes"][-1] == "append"
    assert FakeElOverblikClient.calls[-1][0] == date(2026, 1, 1)


async def test_unreadable_recorder_skips_statistics_but_updates_sensors(
    hass, coordinator_entry, frozen_clock, recorder_stub
) -> None:
    """A recorder that is not ready yet does not trigger a history download."""
    _set_options(hass, coordinator_entry, days=1825, statistics=True)
    recorder_stub["lookup_error"] = True
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
    assert coordinator.statistics_failed
    assert recorder_stub["modes"] == []
    assert FakeElOverblikClient.calls[-1][0] == date(2026, 1, 1)
    summary = coordinator.data.points[POINT.metering_point_id].summary
    assert summary.day_total_kwh == pytest.approx(3.0)
