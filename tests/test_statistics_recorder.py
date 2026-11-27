"""Statistics import against Home Assistant's real recorder (SQLite in memory)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder.statistics import statistics_during_period
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.eloverblik_dk.api import EnergyReading, MeteringPoint
from custom_components.eloverblik_dk.statistics import (
    async_import_meter_statistics,
    statistic_id_for,
)

TZ = ZoneInfo("Europe/Copenhagen")
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=TZ)
POINT = MeteringPoint(
    metering_point_id="571313174000000077",
    type_of_mp="D07",
    direction="consumption",
    address="Nørregade 12, 8000 Aarhus C",
    meter_reading_occurrence="PT1H",
)
STATISTIC_ID = statistic_id_for(POINT.metering_point_id)


def _readings(values: dict[datetime, float]) -> list[EnergyReading]:
    return [
        EnergyReading(POINT.metering_point_id, start, timedelta(hours=1), kwh)
        for start, kwh in values.items()
    ]


async def _stored(hass) -> list[tuple[datetime, float]]:
    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 1, 1, tzinfo=UTC),
        None,
        {STATISTIC_ID},
        "hour",
        None,
        {"sum"},
    )
    return [
        (datetime.fromtimestamp(row["start"], tz=UTC), row["sum"])
        for row in rows.get(STATISTIC_ID, [])
    ]


async def _import(hass, values: dict[datetime, float], mode: str) -> int:
    written = await async_import_meter_statistics(
        hass,
        POINT,
        _readings(values),
        history_days=30,
        now=NOW,
        tz=TZ,
        mode=mode,
    )
    await async_wait_recording_done(hass)
    return written


@pytest.mark.usefixtures("recorder_mock")
async def test_corrections_are_upserted_with_a_continuous_sum(hass) -> None:
    """Append, then correct and fill hours; the recorder ends up consistent."""
    base = datetime(2026, 11, 20, 0, tzinfo=UTC)
    hours = [base + timedelta(hours=offset) for offset in range(6)]
    values = {hour: 1.0 for hour in hours}
    del values[hours[2]]  # missing when first written

    assert await _import(hass, values, "auto") == 5
    first = await _stored(hass)
    assert [total for _, total in first] == pytest.approx([1, 2, 3, 4, 5])

    # DataHub corrects hour 1, delivers the missing hour 2, and adds hour 6.
    values[hours[1]] = 1.5
    values[hours[2]] = 0.25
    values[base + timedelta(hours=6)] = 2.0
    assert await _import(hass, values, "append") == 6

    stored = await _stored(hass)
    assert [start for start, _ in stored] == [*hours, base + timedelta(hours=6)]
    sums = [total for _, total in stored]
    assert sums == pytest.approx([1.0, 2.5, 2.75, 3.75, 4.75, 5.75, 7.75])
    assert all(later >= earlier for earlier, later in itertools.pairwise(sums))

    # Nothing changed: the next poll writes nothing.
    assert await _import(hass, values, "append") == 0


@pytest.mark.usefixtures("recorder_mock")
async def test_rebuild_clears_and_rewrites_through_the_real_recorder(hass) -> None:
    """A rebuild waits for the clear, so the new series is not deleted with it."""
    base = datetime(2026, 11, 20, 0, tzinfo=UTC)
    assert await _import(hass, {base: 4.0, base + timedelta(hours=1): 4.0}, "auto")
    values = {
        datetime(2026, 11, 1, 0, tzinfo=UTC): 1.0,
        base: 2.0,
        base + timedelta(hours=1): 3.0,
    }
    assert await _import(hass, values, "rebuild") == 3
    stored = await _stored(hass)
    assert [total for _, total in stored] == pytest.approx([1.0, 3.0, 6.0])
