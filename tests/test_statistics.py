"""Tests for hourly aggregation and incremental long-term statistics."""

from __future__ import annotations

import asyncio
import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.eloverblik_dk.api import (
    EnergyReading,
    MeteringPoint,
    parse_time_series,
)
from custom_components.eloverblik_dk.const import (
    DEFAULT_HISTORY_DAYS,
    MAX_HISTORY_DAYS,
    MIN_HISTORY_DAYS,
    VERSION,
)
from custom_components.eloverblik_dk.statistics import (
    StatisticLookupError,
    aggregate_hourly,
    async_import_meter_statistics,
    build_statistic_metadata,
    completed_hours,
    metadata_from_annotations,
    needs_statistic_rebuild,
    plan_incremental_statistics,
    resolution_from_duration,
    statistic_id_for,
    summarize_usage,
)

TZ = ZoneInfo("Europe/Copenhagen")
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=TZ)
POINT = MeteringPoint(
    metering_point_id="571313174000000017",
    type_of_mp="E17",
    direction="consumption",
    address="Nørregade 12, 8000 Aarhus C",
    meter_reading_occurrence="PT1H",
)


def test_quarter_hours_roll_up_without_filling_gaps() -> None:
    """Four quarters become one hour, and a missing quarter is not stored as zero."""
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "timeseries_pt15m.json").read_text(
            encoding="utf-8"
        )
    )
    export = [
        reading
        for reading in parse_time_series(payload)
        if reading.metering_point_id == "571313174000000066"
    ]
    hourly = aggregate_hourly(export)
    assert hourly == [
        (datetime(2026, 11, 22, 23, 0, tzinfo=UTC), pytest.approx(0.35)),
        (datetime(2026, 11, 23, 0, 0, tzinfo=UTC), pytest.approx(0.6)),
    ]


def test_incremental_sum_continues_and_does_not_reset() -> None:
    """A second import continues the stored sum and ignores hours already written."""
    first = plan_incremental_statistics(
        [
            (datetime(2026, 11, 23, 0, tzinfo=UTC), 1.0),
            (datetime(2026, 11, 23, 1, tzinfo=UTC), 2.0),
            (datetime(2026, 11, 23, 3, tzinfo=UTC), 0.5),
        ],
        last_start=None,
        last_sum=0.0,
    )
    assert [(item.start.hour, item.sum, item.state) for item in first] == [
        (0, 1.0, 1.0),
        (1, 3.0, 3.0),
        (3, 3.5, 3.5),
    ]

    second = plan_incremental_statistics(
        [
            (datetime(2026, 11, 23, 1, tzinfo=UTC), 2.0),
            (datetime(2026, 11, 23, 3, tzinfo=UTC), 0.5),
            (datetime(2026, 11, 23, 4, tzinfo=UTC), 1.5),
            (datetime(2026, 11, 23, 5, tzinfo=UTC), -0.2),
            (datetime(2026, 11, 23, 6, tzinfo=UTC), 1.0),
        ],
        last_start=first[-1].start,
        last_sum=first[-1].sum,
    )
    assert [item.start.hour for item in second] == [4, 6]
    assert second[0].sum == pytest.approx(5.0)
    assert second[1].sum == pytest.approx(6.0)
    assert second[1].state == second[1].sum

    again = plan_incremental_statistics(
        [(item.start, 1.0) for item in second],
        last_start=second[-1].start,
        last_sum=second[-1].sum,
    )
    assert again == []


def test_completed_hours_leave_the_current_hour_open() -> None:
    """The hour that is still in progress is not imported."""
    hourly = [
        (datetime(2026, 11, 23, 0, tzinfo=UTC), 1.0),
        (datetime(2026, 11, 23, 1, 15, tzinfo=UTC), 2.0),
    ]
    ready = completed_hours(hourly, now=datetime(2026, 11, 23, 1, 30, tzinfo=UTC))
    assert ready == [(datetime(2026, 11, 23, 0, tzinfo=UTC), 1.0)]


def test_summary_uses_danish_dates_and_exposes_the_data_delay() -> None:
    """Day, month, and year totals follow Europe/Copenhagen, not the UTC date."""
    readings = [
        EnergyReading(
            POINT.metering_point_id,
            datetime(2026, 1, 15, 12, tzinfo=UTC),
            timedelta(hours=1),
            4.0,
        ),
        EnergyReading(
            POINT.metering_point_id,
            datetime(2026, 11, 22, 23, tzinfo=UTC),
            timedelta(hours=1),
            1.5,
        ),
        EnergyReading(
            POINT.metering_point_id,
            datetime(2026, 11, 23, 10, tzinfo=UTC),
            timedelta(hours=1),
            2.5,
        ),
        EnergyReading(
            POINT.metering_point_id,
            datetime(2026, 11, 24, 10, tzinfo=UTC),
            timedelta(hours=1),
            3.0,
        ),
    ]
    summary = summarize_usage(readings, now=NOW, tz=TZ)
    assert summary.latest_data_date.isoformat() == "2026-11-24"
    assert summary.day_total_kwh == pytest.approx(3.0)
    assert summary.month_total_kwh == pytest.approx(7.0)
    assert summary.year_total_kwh == pytest.approx(11.0)
    assert summary.latest_hour_start == datetime(2026, 11, 24, 10, tzinfo=UTC)
    assert summary.latest_hour_kwh == pytest.approx(3.0)


def test_summary_without_readings_is_unknown() -> None:
    """No readings leave every total unset rather than zero."""
    summary = summarize_usage([], now=NOW, tz=TZ)
    assert summary.latest_data_date is None
    assert summary.day_total_kwh is None
    assert summary.month_total_kwh is None


def test_metadata_matches_each_supported_home_assistant_generation() -> None:
    """Older releases must not receive metadata keys they do not declare."""
    common = {
        "name": "Nørregade forbrug",
        "statistic_id": statistic_id_for(POINT.metering_point_id),
        "mean_type_none": 0,
        "unit_class": "energy",
    }
    early = metadata_from_annotations(
        {
            "has_mean",
            "has_sum",
            "name",
            "source",
            "statistic_id",
            "unit_of_measurement",
        },
        **common,
    )
    assert early["has_mean"] is False
    assert "mean_type" not in early
    assert "unit_class" not in early

    mid = metadata_from_annotations(
        {
            "has_mean",
            "mean_type",
            "has_sum",
            "name",
            "source",
            "statistic_id",
            "unit_of_measurement",
        },
        **common,
    )
    assert mid["mean_type"] == 0
    assert mid["has_mean"] is False
    assert "unit_class" not in mid

    current = metadata_from_annotations(
        {
            "mean_type",
            "has_sum",
            "name",
            "source",
            "statistic_id",
            "unit_class",
            "unit_of_measurement",
        },
        **common,
    )
    assert current["mean_type"] == 0
    assert current["unit_class"] == "energy"
    assert "has_mean" not in current
    assert current["source"] == "eloverblik_dk"
    assert current["has_sum"] is True


def test_installed_home_assistant_metadata_contract() -> None:
    """The metadata built for this environment satisfies the installed API."""
    from homeassistant.components.recorder.models import (
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import valid_statistic_id

    meta = build_statistic_metadata(
        "Nørregade · forbrug (grid consumption)",
        statistic_id_for(POINT.metering_point_id),
    )
    annotations = set(StatisticMetaData.__annotations__)
    if "mean_type" in annotations:
        assert meta["mean_type"] is StatisticMeanType.NONE
    if "unit_class" in annotations:
        assert meta["unit_class"] == "energy"
    if "has_mean" in annotations:
        assert meta["has_mean"] is False
    assert valid_statistic_id(meta["statistic_id"])


@pytest.mark.asyncio
async def test_import_appends_without_rewriting_when_recorder_has_a_sum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Import continues from the recorder sum and skips the import if lookup fails."""
    captured: dict[str, object] = {"rows": {}}

    def get_last(hass, number, statistic_id, convert, types):
        del hass, number, convert, types
        return captured["rows"]

    def add(hass, metadata, stats):
        del hass
        captured["metadata"] = metadata
        captured["stats"] = list(stats)

    def during(hass, start, end, statistic_ids, period, units, types):
        del hass, start, end, period, units, types
        statistic_id = next(iter(statistic_ids))
        return {statistic_id: [{"start": datetime(2026, 1, 1, tzinfo=UTC), "sum": 0.0}]}

    monkeypatch.setattr(
        "custom_components.eloverblik_dk.statistics._load_recorder",
        lambda: (get_last, add, during, lambda hass: None),
    )
    readings = [
        EnergyReading(
            POINT.metering_point_id,
            datetime(2026, 11, 24, 8, tzinfo=UTC),
            timedelta(hours=1),
            1.25,
        ),
        EnergyReading(
            POINT.metering_point_id,
            datetime(2026, 11, 24, 9, tzinfo=UTC),
            timedelta(hours=1),
            0.75,
        ),
    ]
    written = await async_import_meter_statistics(
        object(),
        POINT,
        readings,
        history_days=30,
        now=NOW,
        tz=TZ,
    )
    assert written == 2
    stats = captured["stats"]
    assert stats[0]["sum"] == pytest.approx(1.25)
    assert stats[1]["sum"] == pytest.approx(2.0)
    assert stats[0]["state"] == stats[0]["sum"]

    captured["rows"] = {
        statistic_id_for(POINT.metering_point_id): [
            {"start": datetime(2026, 11, 24, 9, tzinfo=UTC).timestamp(), "sum": 10.0}
        ]
    }
    captured.pop("stats", None)
    readings.append(
        EnergyReading(
            POINT.metering_point_id,
            datetime(2026, 11, 24, 10, tzinfo=UTC),
            timedelta(hours=1),
            0.5,
        )
    )
    written = await async_import_meter_statistics(
        object(),
        POINT,
        readings,
        history_days=30,
        now=NOW,
        tz=TZ,
    )
    assert written == 1
    assert captured["stats"][0]["sum"] == pytest.approx(10.5)
    assert captured["stats"][0]["start"] == datetime(2026, 11, 24, 10, tzinfo=UTC)

    def broken_lookup(*args, **kwargs):
        raise RuntimeError("recorder offline")

    monkeypatch.setattr(
        "custom_components.eloverblik_dk.statistics._load_recorder",
        lambda: (broken_lookup, add, during, lambda hass: None),
    )
    written = await async_import_meter_statistics(
        object(),
        POINT,
        readings,
        history_days=30,
        now=NOW,
        tz=TZ,
    )
    assert written == -1


def test_lookup_error_is_an_exception() -> None:
    """The lookup error stays a normal exception the coordinator can catch."""
    assert issubclass(StatisticLookupError, Exception)


def test_default_history_is_half_a_year() -> None:
    """New installations import 180 days, still bounded by 1 and 365."""
    assert VERSION == "0.2.0"
    assert DEFAULT_HISTORY_DAYS == 180
    assert MIN_HISTORY_DAYS == 1
    assert MAX_HISTORY_DAYS == 365


def test_resolution_labels_match_the_native_interval() -> None:
    """Quarter-hour, hourly, and daily durations keep their ISO names."""
    assert resolution_from_duration(timedelta(minutes=15)) == "PT15M"
    assert resolution_from_duration(timedelta(hours=1)) == "PT1H"
    assert resolution_from_duration(timedelta(days=1)) == "P1D"


def test_daily_block_is_spread_across_exact_clock_hours() -> None:
    """A P1D total becomes 24 UTC hours instead of one multi-hour slot."""
    reading = EnergyReading(
        POINT.metering_point_id,
        datetime(2026, 11, 1, 0, tzinfo=UTC),
        timedelta(days=1),
        24.0,
    )
    hourly = aggregate_hourly([reading])
    assert len(hourly) == 24
    assert [(start.minute, start.second, start.microsecond) for start, _ in hourly] == [
        (0, 0, 0)
    ] * 24
    assert sum(kwh for _, kwh in hourly) == pytest.approx(24.0)
    assert hourly[0][0] == datetime(2026, 11, 1, 0, tzinfo=UTC)
    assert hourly[-1][0] == datetime(2026, 11, 1, 23, tzinfo=UTC)


def test_summary_reports_the_native_interval() -> None:
    """The latest interval keeps the quarter-hour that DataHub returned."""
    start = datetime(2026, 11, 24, 10, 45, tzinfo=UTC)
    summary = summarize_usage(
        [EnergyReading(POINT.metering_point_id, start, timedelta(minutes=15), 0.25)],
        now=NOW,
        tz=TZ,
    )
    assert summary.latest_resolution == "PT15M"
    assert summary.latest_interval_kwh == pytest.approx(0.25)
    assert summary.latest_interval_start == start
    assert summary.latest_interval_end == datetime(2026, 11, 24, 11, tzinfo=UTC)
    assert summary.latest_hour_kwh == pytest.approx(0.25)


def test_rebuild_is_needed_only_when_older_readings_exist() -> None:
    """A 30-day series needs a rewrite once 180 days of readings are available."""
    earliest = datetime(2026, 11, 1, 0, tzinfo=UTC)
    older = EnergyReading(
        POINT.metering_point_id,
        datetime(2026, 6, 1, 0, tzinfo=UTC),
        timedelta(hours=1),
        1.0,
    )
    recent = EnergyReading(POINT.metering_point_id, earliest, timedelta(hours=1), 1.0)
    assert needs_statistic_rebuild(
        earliest_start=earliest,
        readings=[older, recent],
        history_days=180,
        now=NOW,
        tz=TZ,
    )
    assert not needs_statistic_rebuild(
        earliest_start=earliest,
        readings=[recent],
        history_days=180,
        now=NOW,
        tz=TZ,
    )
    assert not needs_statistic_rebuild(
        earliest_start=None,
        readings=[older],
        history_days=180,
        now=NOW,
        tz=TZ,
    )
    assert not needs_statistic_rebuild(
        earliest_start=datetime(2026, 5, 1, 0, tzinfo=UTC),
        readings=[older, recent],
        history_days=180,
        now=NOW,
        tz=TZ,
    )


@pytest.mark.asyncio
async def test_existing_30_day_statistics_backfill_to_a_continuous_180_day_sum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raising history from 30 to 180 replaces the old sum instead of jumping down."""
    statistic_id = statistic_id_for(POINT.metering_point_id)
    stored = {
        statistic_id: [
            {"start": datetime(2026, 11, 1, 0, tzinfo=UTC), "sum": 4.0, "state": 4.0},
            {"start": datetime(2026, 11, 1, 2, tzinfo=UTC), "sum": 5.0, "state": 5.0},
        ]
    }
    order: list[str] = []
    during_calls = 0

    def get_last(hass, number, statistic_id_arg, convert, types):
        del hass, number, convert, types
        rows = stored.get(statistic_id_arg) or []
        if not rows:
            return {}
        newest = max(rows, key=lambda row: row["start"])
        return {statistic_id_arg: [newest]}

    def during(hass, start, end, statistic_ids, period, units, types):
        del hass, start, end, period, units, types
        nonlocal during_calls
        during_calls += 1
        statistic_id_arg = next(iter(statistic_ids))
        rows = list(stored.get(statistic_id_arg) or [])
        if not rows:
            return {}
        oldest = min(rows, key=lambda row: row["start"])
        return {statistic_id_arg: [oldest]}

    def add(hass, metadata, stats):
        del hass
        order.append("add")
        stored[metadata["statistic_id"]] = [dict(row) for row in stats]

    class _Recorder:
        def async_clear_statistics(self, statistic_ids, *, on_done=None):
            order.append("clear")
            for cleared_id in statistic_ids:
                stored.pop(cleared_id, None)
            if on_done:
                on_done()

    monkeypatch.setattr(
        "custom_components.eloverblik_dk.statistics._load_recorder",
        lambda: (get_last, add, during, lambda hass: _Recorder()),
    )
    readings = [
        EnergyReading(
            POINT.metering_point_id,
            datetime(2026, 1, 15, 12, tzinfo=UTC),
            timedelta(hours=1),
            9.0,
        ),
        EnergyReading(
            POINT.metering_point_id,
            datetime(2026, 6, 1, 0, tzinfo=UTC),
            timedelta(hours=1),
            6.0,
        ),
        EnergyReading(
            POINT.metering_point_id,
            datetime(2026, 6, 1, 1, tzinfo=UTC),
            timedelta(hours=1),
            4.0,
        ),
        EnergyReading(
            POINT.metering_point_id,
            datetime(2026, 11, 1, 0, tzinfo=UTC),
            timedelta(hours=1),
            4.0,
        ),
        EnergyReading(
            POINT.metering_point_id,
            datetime(2026, 11, 1, 2, tzinfo=UTC),
            timedelta(hours=1),
            1.0,
        ),
    ]
    hass = SimpleNamespace(loop=asyncio.get_running_loop())
    written = await async_import_meter_statistics(
        hass,
        POINT,
        readings,
        history_days=180,
        now=NOW,
        tz=TZ,
        mode="auto",
    )
    assert written == 4
    assert order == ["clear", "add"]
    stats = stored[statistic_id]
    assert [row["start"] for row in stats] == [
        datetime(2026, 6, 1, 0, tzinfo=UTC),
        datetime(2026, 6, 1, 1, tzinfo=UTC),
        datetime(2026, 11, 1, 0, tzinfo=UTC),
        datetime(2026, 11, 1, 2, tzinfo=UTC),
    ]
    sums = [row["sum"] for row in stats]
    assert sums == pytest.approx([6.0, 10.0, 14.0, 15.0])
    assert [row["state"] for row in stats] == pytest.approx(sums)
    assert all(later >= earlier for earlier, later in itertools.pairwise(sums))
    deltas = [sums[0], *(right - left for left, right in itertools.pairwise(sums))]
    assert deltas == pytest.approx([6.0, 4.0, 4.0, 1.0])
    assert stats[2]["sum"] != pytest.approx(4.0)

    readings.append(
        EnergyReading(
            POINT.metering_point_id,
            datetime(2026, 11, 1, 3, tzinfo=UTC),
            timedelta(hours=1),
            2.0,
        )
    )
    written = await async_import_meter_statistics(
        hass,
        POINT,
        readings,
        history_days=180,
        now=NOW,
        tz=TZ,
        mode="auto",
    )
    assert written == 1
    assert order == ["clear", "add", "add"]
    assert stored[statistic_id][-1]["sum"] == pytest.approx(17.0)
    assert during_calls == 2

    written = await async_import_meter_statistics(
        hass,
        POINT,
        readings,
        history_days=180,
        now=NOW,
        tz=TZ,
        mode="append",
    )
    assert written == 0
    assert order == ["clear", "add", "add"]
    assert during_calls == 2
