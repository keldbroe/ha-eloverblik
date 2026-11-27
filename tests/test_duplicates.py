"""Tests for finding metering points that DataHub reports with identical values."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.eloverblik_dk.api import EnergyReading, MeteringPoint
from custom_components.eloverblik_dk.duplicates import (
    find_duplicate_points,
    keep_order,
)

NOW = datetime(2026, 11, 26, 12, tzinfo=UTC)
E17 = MeteringPoint("571313174200175540", "E17", "consumption", "Vej 1", "PT1H")
D07 = MeteringPoint(
    "571313174001057915", "D07", "consumption", "Vej 1", "PT1H", E17.metering_point_id
)
E18 = MeteringPoint("571313174001877827", "E18", "export", "Vej 1", "PT1H")
D06 = MeteringPoint(
    "571313174000942748", "D06", "export", "Vej 1", "PT1H", E17.metering_point_id
)


def _series(point: MeteringPoint, values: list[float]) -> list[EnergyReading]:
    """Hourly readings ending just before NOW."""
    start = NOW - timedelta(hours=len(values))
    return [
        EnergyReading(
            point.metering_point_id,
            start + timedelta(hours=index),
            timedelta(hours=1),
            kwh,
        )
        for index, kwh in enumerate(values)
    ]


def _profile(hours: int = 10 * 24, scale: float = 1.0) -> list[float]:
    return [round(scale * (0.2 + (index % 24) / 10), 3) for index in range(hours)]


def test_parent_and_child_with_the_same_values_keep_the_child() -> None:
    """The real-world case: E17 = D07 and E18 = D06; the D children stay."""
    consumption = _profile()
    export = [
        value if 8 <= index % 24 <= 15 else 0.0
        for index, value in enumerate(consumption)
    ]
    result = find_duplicate_points(
        [E17, D07, E18, D06],
        {
            E17.metering_point_id: _series(E17, consumption),
            D07.metering_point_id: _series(D07, consumption),
            E18.metering_point_id: _series(E18, export),
            D06.metering_point_id: _series(D06, export),
        },
        now=NOW,
    )
    pairs = {(f.remove.type_of_mp, f.keep.type_of_mp) for f in result.findings}
    assert pairs == {("E17", "D07"), ("E18", "D06")}
    assert not result.different


def test_points_in_different_directions_are_never_compared() -> None:
    """A consumption and an export point with equal numbers are not duplicates."""
    values = _profile()
    result = find_duplicate_points(
        [D07, D06],
        {
            D07.metering_point_id: _series(D07, values),
            D06.metering_point_id: _series(D06, values),
        },
        now=NOW,
    )
    assert result.findings == []
    assert result.different == set()


def test_different_series_withdraw_a_warning() -> None:
    """One differing hour is enough to show that the points are not the same."""
    values = _profile()
    other = list(values)
    other[50] += 0.01
    result = find_duplicate_points(
        [E17, D07],
        {
            E17.metering_point_id: _series(E17, values),
            D07.metering_point_id: _series(D07, other),
        },
        now=NOW,
    )
    assert result.findings == []
    assert result.different == {E17.metering_point_id, D07.metering_point_id}


def test_all_zero_or_short_series_are_undecided() -> None:
    """Winter export of zero, or less than a week of data, decides nothing."""
    zeros = [0.0] * (10 * 24)
    short = _profile(hours=3 * 24)
    for values in (zeros, short):
        result = find_duplicate_points(
            [E18, D06],
            {
                E18.metering_point_id: _series(E18, values),
                D06.metering_point_id: _series(D06, values),
            },
            now=NOW,
        )
        assert result.findings == []
        assert result.different == set()


def test_only_the_last_month_is_compared() -> None:
    """Old identical history does not count if the points differ now."""
    old_start = NOW - timedelta(days=90)
    same_old = [
        EnergyReading(
            point.metering_point_id,
            old_start + timedelta(hours=i),
            timedelta(hours=1),
            1.0,
        )
        for point in (E17, D07)
        for i in range(10 * 24)
    ]
    readings = {
        E17.metering_point_id: [
            r for r in same_old if r.metering_point_id == E17.metering_point_id
        ],
        D07.metering_point_id: [
            r for r in same_old if r.metering_point_id == D07.metering_point_id
        ],
    }
    assert find_duplicate_points([E17, D07], readings, now=NOW).findings == []


def test_three_identical_points_keep_one() -> None:
    """A group of identical points keeps the single best one."""
    other = MeteringPoint("571313174000000099", "D10", "consumption", "Vej 1", "PT1H")
    values = _profile()
    result = find_duplicate_points(
        [E17, D07, other],
        {p.metering_point_id: _series(p, values) for p in (E17, D07, other)},
        now=NOW,
    )
    assert {f.keep.metering_point_id for f in result.findings} == {
        D07.metering_point_id
    }
    assert {f.remove.metering_point_id for f in result.findings} == {
        E17.metering_point_id,
        other.metering_point_id,
    }


def test_keep_order_prefers_children_then_d_types() -> None:
    """Children win over parents, and D types over E types."""
    assert keep_order(D07) > keep_order(E17)
    assert keep_order(D06) > keep_order(E18)
    lone_d = MeteringPoint("571313174000000098", "D10", "consumption", "Vej 1", "PT1H")
    assert keep_order(lone_d) > keep_order(E17)
