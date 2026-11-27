"""Changing the selected metering points, and the duplicate-point repair."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import eloverblik_dk
from custom_components.eloverblik_dk import config_flow as config_flow_module
from custom_components.eloverblik_dk.api import EnergyReading, MeteringPoint
from custom_components.eloverblik_dk.const import (
    CONF_HISTORY_DAYS,
    CONF_IMPORT_STATISTICS,
    CONF_METERING_POINT_IDS,
    CONF_METERING_POINTS,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL_HOURS,
    DOMAIN,
)
from custom_components.eloverblik_dk.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.eloverblik_dk.helpers import duplicate_issue_id
from tests.fake_client import FakeElOverblikClient

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

ADDRESS = "Nørregade 12, 8000 Aarhus C"
E17 = MeteringPoint("571313174200175540", "E17", "consumption", ADDRESS, "PT1H")
D07 = MeteringPoint(
    "571313174001057915", "D07", "consumption", ADDRESS, "PT1H", E17.metering_point_id
)
E18 = MeteringPoint("571313174001877827", "E18", "export", ADDRESS, "PT1H")
D06 = MeteringPoint(
    "571313174000942748", "D06", "export", ADDRESS, "PT1H", E17.metering_point_id
)
ALL = [E17, D07, E18, D06]
NOW = datetime(2026, 11, 26, 12, tzinfo=UTC)


def _readings(point: MeteringPoint, *, export: bool, bump: float = 0.0) -> list:
    start = NOW - timedelta(days=10)
    out = []
    for index in range(10 * 24):
        hour = index % 24
        kwh = (0.3 + hour / 20) if not export else (0.5 if 9 <= hour <= 14 else 0.0)
        if index == 100:
            kwh += bump
        out.append(
            EnergyReading(
                point.metering_point_id,
                start + timedelta(hours=index),
                timedelta(hours=1),
                round(kwh, 3),
            )
        )
    return out


@pytest.fixture
def entry(hass, monkeypatch: pytest.MonkeyPatch) -> MockConfigEntry:
    """An entry that monitors all four points of one installation."""
    FakeElOverblikClient.reset()
    FakeElOverblikClient.points = list(ALL)
    FakeElOverblikClient.readings = [
        *_readings(E17, export=False),
        *_readings(D07, export=False),
        *_readings(E18, export=True),
        *_readings(D06, export=True),
    ]
    monkeypatch.setattr(eloverblik_dk, "ElOverblikClient", FakeElOverblikClient)
    monkeypatch.setattr(config_flow_module, "ElOverblikClient", FakeElOverblikClient)
    monkeypatch.setattr(
        "custom_components.eloverblik_dk.coordinator.current_time",
        lambda tz: NOW.astimezone(tz),
    )
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="ElOverblik (DK)",
        data={
            CONF_REFRESH_TOKEN: "token",
            CONF_METERING_POINTS: [point.as_dict() for point in ALL],
        },
        options={
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: 30,
            CONF_IMPORT_STATISTICS: False,
        },
        unique_id="account",
    )
    config_entry.add_to_hass(hass)
    return config_entry


async def _setup(hass, entry) -> None:
    await hass.config.async_set_time_zone("Europe/Copenhagen")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _issues(hass) -> dict[str, ir.IssueEntry]:
    return {
        issue_id: issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    }


def _device_points(hass, entry) -> set[str]:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return {
        ident
        for device in devices
        for domain, ident in device.identifiers
        if domain == DOMAIN
    }


def _selected(entry) -> set[str]:
    return {item["metering_point_id"] for item in entry.data[CONF_METERING_POINTS]}


async def test_identical_points_raise_a_fixable_repair(hass, entry) -> None:
    """E17 = D07 and E18 = D06 each give one warning, naming the point to drop."""
    await _setup(hass, entry)
    issues = _issues(hass)
    assert set(issues) == {
        duplicate_issue_id(entry.entry_id, E17.metering_point_id),
        duplicate_issue_id(entry.entry_id, E18.metering_point_id),
    }
    issue = issues[duplicate_issue_id(entry.entry_id, E17.metering_point_id)]
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "duplicate_metering_point"
    assert issue.translation_placeholders["keep"].startswith(
        f"D07 {D07.metering_point_id}"
    )
    assert issue.translation_placeholders["remove"].startswith(
        f"E17 {E17.metering_point_id}"
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert len(diagnostics["coordinator"]["duplicates"]) == 2
    assert E17.metering_point_id not in repr(diagnostics)


async def test_fixing_the_repair_deselects_the_point_and_removes_its_device(
    hass, entry
) -> None:
    """Fix removes E17 from the selection; its device and sensors go, D07 stays."""
    await _setup(hass, entry)
    registry = er.async_get(hass)
    e17_entity = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{E17.metering_point_id}_year_total"
    )
    assert e17_entity is not None
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    issue_id = duplicate_issue_id(entry.entry_id, E17.metering_point_id)

    flow = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "confirm"
    assert flow["description_placeholders"]["keep"].startswith("D07")
    result = await manager.async_configure(flow["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert _selected(entry) == {
        D07.metering_point_id,
        E18.metering_point_id,
        D06.metering_point_id,
    }
    assert E17.metering_point_id not in _device_points(hass, entry)
    assert D07.metering_point_id in _device_points(hass, entry)
    assert registry.async_get(e17_entity) is None
    assert issue_id not in _issues(hass)
    # The E18/D06 warning is still there until that one is fixed too.
    assert duplicate_issue_id(entry.entry_id, E18.metering_point_id) in _issues(hass)


async def test_options_can_add_and_remove_metering_points(hass, entry) -> None:
    """The options page changes the selection without setting up again."""
    await _setup(hass, entry)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert menu["type"] is FlowResultType.MENU
    form = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "metering_points"}
    )
    assert form["step_id"] == "metering_points"
    assert not form["errors"]
    selector = form["data_schema"].schema[CONF_METERING_POINT_IDS]
    offered = {option["value"] for option in selector.config["options"]}
    assert offered == {point.metering_point_id for point in ALL}
    labels = {o["value"]: o["label"] for o in selector.config["options"]}
    # The two redundant points say which point they repeat; the kept ones don't.
    assert "(E17)" in labels[E17.metering_point_id]
    assert labels[E17.metering_point_id].endswith("same values as D07")
    assert labels[E18.metering_point_id].endswith("same values as D06")
    assert "same values" not in labels[D07.metering_point_id]
    assert "same values" not in labels[D06.metering_point_id]

    options_before = dict(entry.options)
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {CONF_METERING_POINT_IDS: [D07.metering_point_id, D06.metering_point_id]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert dict(entry.options) == options_before
    assert _selected(entry) == {D07.metering_point_id, D06.metering_point_id}
    assert _device_points(hass, entry) == {D07.metering_point_id, D06.metering_point_id}
    # Both warnings referred to points that are no longer selected.
    assert _issues(hass) == {}

    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "metering_points"}
    )
    empty = await hass.config_entries.options.async_configure(
        form["flow_id"], {CONF_METERING_POINT_IDS: []}
    )
    assert empty["errors"]["base"] == "no_selection"
    added = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_METERING_POINT_IDS: [
                D07.metering_point_id,
                D06.metering_point_id,
                E18.metering_point_id,
            ]
        },
    )
    assert added["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert E18.metering_point_id in _device_points(hass, entry)


async def test_options_still_work_when_elovervblik_is_unreachable(hass, entry) -> None:
    """Without the API, the selected points can still be removed."""
    await _setup(hass, entry)
    from custom_components.eloverblik_dk.api import ElOverblikApiError

    FakeElOverblikClient.meter_error = ElOverblikApiError("down")
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "metering_points"}
    )
    assert form["errors"]["base"] == "cannot_connect"
    selector = form["data_schema"].schema[CONF_METERING_POINT_IDS]
    offered = {option["value"] for option in selector.config["options"]}
    assert offered == {point.metering_point_id for point in ALL}


async def test_deleting_a_device_in_the_ui_deselects_its_point(hass, entry) -> None:
    """The device page's Delete button removes the point from the selection."""
    await _setup(hass, entry)
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, E18.metering_point_id)}
    )
    assert device is not None
    assert await eloverblik_dk.async_remove_config_entry_device(hass, entry, device)
    await hass.async_block_till_done()
    assert E18.metering_point_id not in _selected(entry)
    assert E18.metering_point_id not in _device_points(hass, entry)


async def test_the_last_point_cannot_be_deleted_from_the_device_page(
    hass, entry
) -> None:
    """Removing everything is done by removing the integration instead."""
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_METERING_POINTS: [D07.as_dict()]}
    )
    await _setup(hass, entry)
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, D07.metering_point_id)}
    )
    assert not await eloverblik_dk.async_remove_config_entry_device(hass, entry, device)
    assert _selected(entry) == {D07.metering_point_id}


async def test_warning_is_withdrawn_when_the_points_start_to_differ(
    hass, entry
) -> None:
    """If DataHub later reports different values, the warning goes away."""
    await _setup(hass, entry)
    assert duplicate_issue_id(entry.entry_id, E17.metering_point_id) in _issues(hass)
    FakeElOverblikClient.readings = [
        *_readings(E17, export=False, bump=0.5),
        *_readings(D07, export=False),
        *_readings(E18, export=True),
        *_readings(D06, export=True),
    ]
    coordinator = entry.runtime_data.coordinator
    coordinator._readings.clear()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert duplicate_issue_id(entry.entry_id, E17.metering_point_id) not in _issues(
        hass
    )
    assert duplicate_issue_id(entry.entry_id, E18.metering_point_id) in _issues(hass)


async def test_removing_the_entry_removes_its_repairs(hass, entry) -> None:
    """No warning outlives the integration."""
    await _setup(hass, entry)
    assert _issues(hass)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _issues(hass) == {}
