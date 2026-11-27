"""Find selected metering points that DataHub reports with identical values.

DataHub often sends the same hourly values for a parent and its child, for
example an E17 consumption point and its D07 "consumption from grid" child,
or an E18 production point and a D06 "supply to grid" child. Selecting both
gives two devices with the same numbers and, in the Energy dashboard, counts
the same electricity twice. This module decides, from the data itself, which
selected points are identical and which one to keep.

It has no Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .api import EnergyReading, MeteringPoint
from .statistics import aggregate_hourly

# At least a week of overlapping hours, of which at least a day has a real
# (non-zero) value. Two export points that are both zero all winter are not
# evidence that they are the same meter.
MIN_OVERLAP_HOURS = 7 * 24
MIN_NONZERO_HOURS = 24
COMPARE_DAYS = 31
TOLERANCE_KWH = 0.0005


@dataclass(frozen=True, slots=True)
class DuplicateFinding:
    """``remove`` reports the same values as ``keep`` and can be deselected."""

    keep: MeteringPoint
    remove: MeteringPoint


@dataclass(frozen=True, slots=True)
class DuplicateResult:
    """Outcome of one comparison run.

    ``findings`` are the points to deselect. ``different`` holds the ids of
    points that were shown to differ from every other point in their
    direction, so an earlier warning about them can be withdrawn. Points that
    could not be compared (too little data) are in neither.
    """

    findings: list[DuplicateFinding]
    different: set[str]


def keep_order(point: MeteringPoint) -> tuple[int, int, str]:
    """Return a sort key; the highest key in a group of identical points wins.

    A child point (D06, D07, ...) is kept over its parent: it is the specific
    grid measurement and usually has the longer history. Ties are broken by
    id, so the choice is stable from poll to poll.
    """
    is_child = point.parent_metering_point_id is not None
    is_d_type = point.type_of_mp.upper().startswith("D")
    return (int(is_child), int(is_d_type), point.metering_point_id)


def compare_series(
    first: Mapping[datetime, float], second: Mapping[datetime, float]
) -> bool | None:
    """Return True if identical, False if different, None if undecided."""
    shared = first.keys() & second.keys()
    if len(shared) < MIN_OVERLAP_HOURS:
        return None
    nonzero = 0
    for hour in shared:
        a = first[hour]
        b = second[hour]
        if abs(a - b) > TOLERANCE_KWH:
            return False
        if abs(a) > TOLERANCE_KWH:
            nonzero += 1
    if nonzero < MIN_NONZERO_HOURS:
        return None
    return True


def find_duplicate_points(
    points: Sequence[MeteringPoint],
    readings: Mapping[str, Sequence[EnergyReading]],
    *,
    now: datetime,
) -> DuplicateResult:
    """Compare the last month of every pair of points in the same direction."""
    since = now - timedelta(days=COMPARE_DAYS)
    hourly: dict[str, dict[datetime, float]] = {}
    for point in points:
        recent = [
            reading
            for reading in readings.get(point.metering_point_id, ())
            if reading.start >= since
        ]
        hourly[point.metering_point_id] = dict(aggregate_hourly(recent))

    identical: dict[str, set[str]] = {p.metering_point_id: set() for p in points}
    decided_different: dict[str, int] = {p.metering_point_id: 0 for p in points}
    same_direction: dict[str, int] = {p.metering_point_id: 0 for p in points}
    for left, right in itertools.combinations(points, 2):
        if left.direction != right.direction:
            continue
        left_id = left.metering_point_id
        right_id = right.metering_point_id
        same_direction[left_id] += 1
        same_direction[right_id] += 1
        verdict = compare_series(hourly[left_id], hourly[right_id])
        if verdict is True:
            identical[left_id].add(right_id)
            identical[right_id].add(left_id)
        elif verdict is False:
            decided_different[left_id] += 1
            decided_different[right_id] += 1

    by_id = {point.metering_point_id: point for point in points}
    findings: list[DuplicateFinding] = []
    seen: set[str] = set()
    for point in points:
        point_id = point.metering_point_id
        if point_id in seen or not identical[point_id]:
            continue
        group = _connected(point_id, identical)
        seen |= group
        members = sorted((by_id[item] for item in group), key=keep_order)
        keep = members[-1]
        findings.extend(
            DuplicateFinding(keep=keep, remove=member) for member in members[:-1]
        )
    different = {
        point_id
        for point_id, count in decided_different.items()
        if count and count == same_direction[point_id]
    }
    return DuplicateResult(findings=findings, different=different)


def _connected(start: str, edges: Mapping[str, set[str]]) -> set[str]:
    group = {start}
    stack = [start]
    while stack:
        for neighbour in edges[stack.pop()]:
            if neighbour not in group:
                group.add(neighbour)
                stack.append(neighbour)
    return group
