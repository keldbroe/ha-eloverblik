"""Home Assistant helpers shared by setup, the flows, repairs and the coordinator."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from .api import MeteringPoint, TokenCache
from .const import (
    CONF_METERING_POINTS,
    DOMAIN,
    ISSUE_DUPLICATE_POINT,
    LOCAL_TIME_ZONE,
    TOKEN_CACHE_KEY,
)
from .duplicates import DuplicateResult


def token_cache(hass: Any) -> TokenCache:
    """Return the process-wide data-access token cache for this integration.

    It lives in ``hass.data`` only, so tokens are never written to disk. The
    config flow, the first setup and every reload share it, which keeps the
    GetToken endpoint (2 calls per minute) out of the way.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    return domain_data.setdefault(TOKEN_CACHE_KEY, {})


def ha_time_zone(hass: Any) -> ZoneInfo:
    """Return Home Assistant's configured time zone, or Europe/Copenhagen."""
    name = getattr(getattr(hass, "config", None), "time_zone", None)
    try:
        return ZoneInfo(str(name or LOCAL_TIME_ZONE))
    except Exception:
        return ZoneInfo(LOCAL_TIME_ZONE)


def selected_points(entry: ConfigEntry) -> list[MeteringPoint]:
    """Return the metering points stored on the entry, in stored order."""
    points: list[MeteringPoint] = []
    for item in entry.data.get(CONF_METERING_POINTS, []):
        if isinstance(item, dict):
            point = MeteringPoint.from_dict(item)
            if point.metering_point_id:
                points.append(point)
    return points


def points_in_other_entries(hass: HomeAssistant, entry_id: str | None) -> set[str]:
    """Return metering points that another ElOverblik entry already monitors.

    The same point in two entries would create two devices with the same
    identifiers and write one external statistic from two places.
    """
    taken: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == entry_id:
            continue
        for item in entry.data.get(CONF_METERING_POINTS, []):
            if isinstance(item, dict) and item.get("metering_point_id"):
                taken.add(str(item["metering_point_id"]))
    return taken


@callback
def async_set_selected_points(
    hass: HomeAssistant, entry: ConfigEntry, points: Iterable[MeteringPoint]
) -> bool:
    """Store a new selection. The entry's update listener reloads it.

    Imported long-term statistics are kept; only the devices and sensors of
    points that are no longer selected are removed on the next setup.
    """
    stored = [point.as_dict() for point in points]
    if not stored:
        raise ValueError("At least one metering point must stay selected")
    changed = hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_METERING_POINTS: stored}
    )
    async_prune_duplicate_issues(hass, entry)
    return changed


@callback
def async_deselect_points(
    hass: HomeAssistant, entry: ConfigEntry, point_ids: Iterable[str]
) -> bool:
    """Remove some points from the selection, keeping at least one."""
    drop = set(point_ids)
    remaining = [
        point for point in selected_points(entry) if point.metering_point_id not in drop
    ]
    if not remaining:
        return False
    return async_set_selected_points(hass, entry, remaining)


@callback
def async_remove_stale_devices(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Remove devices (and with them their sensors) of deselected points."""
    selected = {point.metering_point_id for point in selected_points(entry)}
    registry = dr.async_get(hass)
    removed = 0
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        ids = {ident for domain, ident in device.identifiers if domain == DOMAIN}
        if ids and not ids & selected:
            registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )
            removed += 1
    return removed


def duplicate_issue_id(entry_id: str, point_id: str) -> str:
    """Return the repair issue id for one redundant point on one entry."""
    return f"{ISSUE_DUPLICATE_POINT}_{entry_id}_{point_id}"


def _duplicate_issue_prefix(entry_id: str) -> str:
    return f"{ISSUE_DUPLICATE_POINT}_{entry_id}_"


def point_label(point: MeteringPoint) -> str:
    """Label a point in a repair message: type, id and address."""
    return f"{point.type_of_mp} {point.metering_point_id} ({point.address})"


@callback
def async_update_duplicate_issues(
    hass: HomeAssistant, entry: ConfigEntry, result: DuplicateResult
) -> None:
    """Raise a fixable repair for each redundant point, withdraw stale ones."""
    current: set[str] = set()
    for finding in result.findings:
        issue_id = duplicate_issue_id(entry.entry_id, finding.remove.metering_point_id)
        current.add(issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_DUPLICATE_POINT,
            translation_placeholders={
                "keep": point_label(finding.keep),
                "remove": point_label(finding.remove),
            },
            data={
                "entry_id": entry.entry_id,
                "keep": finding.keep.metering_point_id,
                "remove": finding.remove.metering_point_id,
            },
        )
    prefix = _duplicate_issue_prefix(entry.entry_id)
    for issue_id in _entry_issue_ids(hass, entry.entry_id):
        if issue_id in current:
            continue
        point_id = issue_id.removeprefix(prefix)
        if point_id in result.different:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
    async_prune_duplicate_issues(hass, entry)


@callback
def async_prune_duplicate_issues(
    hass: HomeAssistant, entry: ConfigEntry, *, remove_all: bool = False
) -> None:
    """Delete issues for points that are no longer selected (or all of them)."""
    selected = {point.metering_point_id for point in selected_points(entry)}
    prefix = _duplicate_issue_prefix(entry.entry_id)
    for issue_id in _entry_issue_ids(hass, entry.entry_id):
        if remove_all or issue_id.removeprefix(prefix) not in selected:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def _entry_issue_ids(hass: HomeAssistant, entry_id: str) -> list[str]:
    prefix = _duplicate_issue_prefix(entry_id)
    registry = ir.async_get(hass)
    return [
        issue_id
        for (domain, issue_id) in list(registry.issues)
        if domain == DOMAIN and issue_id.startswith(prefix)
    ]
