"""Config flow, reauth, and options flow tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import eloverblik_dk
from custom_components.eloverblik_dk import config_flow as config_flow_module
from custom_components.eloverblik_dk.api import (
    ElOverblikApiError,
    ElOverblikAuthError,
    ElOverblikRateLimitError,
    MeteringPoint,
    parse_metering_points,
)
from custom_components.eloverblik_dk.config_flow import (
    account_unique_id,
    default_metering_point_ids,
    option_label,
)
from custom_components.eloverblik_dk.const import (
    CONF_HISTORY_DAYS,
    CONF_IMPORT_STATISTICS,
    CONF_METERING_POINT_IDS,
    CONF_METERING_POINTS,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL_HOURS,
    DOMAIN,
)
from custom_components.eloverblik_dk.statistics import (
    clear_statistic_rebuild,
    statistic_rebuild_requested,
)
from tests.fake_client import FakeElOverblikClient

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

CONSUMPTION_ID = "571313174000000017"
EXPORT_ID = "571313174000000066"


@pytest.fixture
def points() -> list:
    """Metering points from the realistic DataHub fixture."""
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "metering_points.json").read_text(
            encoding="utf-8"
        )
    )
    return parse_metering_points(payload)


@pytest.fixture
def fake_api(
    monkeypatch: pytest.MonkeyPatch, points: list
) -> type[FakeElOverblikClient]:
    """Replace the API client in the config flow and during entry setup."""
    FakeElOverblikClient.reset()
    FakeElOverblikClient.points = list(points)
    monkeypatch.setattr(config_flow_module, "ElOverblikClient", FakeElOverblikClient)
    monkeypatch.setattr(eloverblik_dk, "ElOverblikClient", FakeElOverblikClient)
    return FakeElOverblikClient


async def _open_settings(hass, entry):
    """Open the options flow and pick the settings page in its menu."""
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "settings"}
    )


def test_default_selection_prefers_children_over_a_consumption_parent(
    points: list,
) -> None:
    """A solar parent is not preselected when its D07 and D06 children are present."""
    chosen = set(default_metering_point_ids(points))
    assert CONSUMPTION_ID not in chosen
    assert "571313174000000077" in chosen
    assert EXPORT_ID in chosen
    parent = next(
        point for point in points if point.metering_point_id == CONSUMPTION_ID
    )
    assert default_metering_point_ids([parent]) == [CONSUMPTION_ID]
    assert "Forbrug" in option_label(parent, "da")
    assert "Consumption" in option_label(parent, "en")
    export = next(point for point in points if point.metering_point_id == EXPORT_ID)
    assert "Salg/eksport" in option_label(export, "da")
    assert "Export/sale" in option_label(export, "en")
    # Production and overlapping children are offered but not preselected.
    assert "571313174000000001" not in chosen  # D01 VE production
    assert "571313174000000009" not in chosen  # D09 own production
    assert "571313174000000012" not in chosen  # D12 total consumption
    assert "571313174000000018" in chosen  # E18 production point (to grid)
    assert "571313174000000011" in chosen  # D11 net to grid
    by_id = {point.metering_point_id: point for point in points}
    assert "Produktion" in option_label(by_id["571313174000000001"], "da")
    assert "Other (D12)" in option_label(by_id["571313174000000012"], "en")
    # The DataHub type is shown, so a parent and its child can be told apart.
    assert "Forbrug (E17)" in option_label(parent, "da")
    assert "Export/sale (D06)" in option_label(export, "en")
    assert "(D12) (D12)" not in option_label(by_id["571313174000000012"], "en")
    d07 = by_id["571313174000000077"]
    assert option_label(parent, "da", same_as=d07).endswith("samme tal som D07")
    assert option_label(parent, "en", same_as=d07).endswith("same values as D07")


def test_stored_direction_from_an_older_version_is_corrected(points: list) -> None:
    """0.2 stored D01 as export; the DataHub type now decides on load."""
    d01 = next(point for point in points if point.type_of_mp == "D01")
    stored = {**d01.as_dict(), "direction": "export"}
    assert MeteringPoint.from_dict(stored).direction == "production"
    untyped = {"metering_point_id": "1", "direction": "export"}
    assert MeteringPoint.from_dict(untyped).direction == "export"


async def test_user_flow_creates_an_entry(hass, fake_api, points) -> None:
    """A valid token leads to metering-point selection and a config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["description_placeholders"] == {
        "site": "eloverblik.dk",
        "url": "https://eloverblik.dk/welcome",
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: " refresh-token "}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "metering_points"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_METERING_POINT_IDS: [CONSUMPTION_ID, EXPORT_ID]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ElOverblik (DK)"
    assert result["data"][CONF_REFRESH_TOKEN] == "refresh-token"
    stored = {
        item["metering_point_id"] for item in result["data"][CONF_METERING_POINTS]
    }
    assert stored == {CONSUMPTION_ID, EXPORT_ID}
    assert result["options"][CONF_SCAN_INTERVAL_HOURS] == 6
    assert result["options"][CONF_HISTORY_DAYS] == 180
    assert result["options"][CONF_IMPORT_STATISTICS] is True
    assert fake_api.tokens[0] == "refresh-token"

    duplicate = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    duplicate = await hass.config_entries.flow.async_configure(
        duplicate["flow_id"], {CONF_REFRESH_TOKEN: "another-token"}
    )
    assert duplicate["type"] is FlowResultType.ABORT
    assert duplicate["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ElOverblikAuthError("rejected"), "invalid_auth"),
        (ElOverblikRateLimitError("slow", retry_after=30), "rate_limited"),
        (ElOverblikApiError("down", status=503), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_reports_api_errors(hass, fake_api, error, expected) -> None:
    """Auth, rate-limit, connection, and unexpected errors stay on the form."""
    fake_api.meter_error = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "refresh-token"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == expected


async def test_user_flow_aborts_when_the_account_has_no_points(hass, fake_api) -> None:
    """An account with no linked metering points cannot be added."""
    fake_api.points = []
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "refresh-token"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_metering_points"


async def test_metering_point_step_requires_a_selection(hass, fake_api) -> None:
    """Submitting the second step with nothing selected shows an error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "refresh-token"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_METERING_POINT_IDS: []}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "no_selection"


async def test_reauth_flow_replaces_the_refresh_token(hass, fake_api, points) -> None:
    """Reauth accepts a new token and keeps the entry on the same account."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=account_unique_id(points),
        title="ElOverblik (DK)",
        data={
            CONF_REFRESH_TOKEN: "expired-token",
            CONF_METERING_POINTS: [points[0].as_dict()],
        },
        options={
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: 30,
            CONF_IMPORT_STATISTICS: False,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=dict(entry.data),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    fake_api.meter_error = ElOverblikAuthError("still bad")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "still-bad"}
    )
    assert result["errors"]["base"] == "invalid_auth"

    fake_api.meter_error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "fresh-token"}
    )
    assert result["step_id"] == "metering_points"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_METERING_POINT_IDS: [CONSUMPTION_ID, EXPORT_ID]},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "fresh-token"
    stored = {item["metering_point_id"] for item in entry.data[CONF_METERING_POINTS]}
    assert stored == {CONSUMPTION_ID, EXPORT_ID}


async def test_options_flow_saves_interval_history_and_statistics(
    hass, fake_api
) -> None:
    """The options flow stores the polling interval, history, and import flag."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_REFRESH_TOKEN: "token", CONF_METERING_POINTS: []},
        options={
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: 30,
            CONF_IMPORT_STATISTICS: True,
        },
    )
    entry.add_to_hass(hass)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert menu["type"] is FlowResultType.MENU
    assert set(menu["menu_options"]) == {"metering_points", "settings"}
    result = await _open_settings(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL_HOURS: 12,
            CONF_HISTORY_DAYS: 90,
            CONF_IMPORT_STATISTICS: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL_HOURS] == 12
    assert entry.options[CONF_HISTORY_DAYS] == 90
    assert entry.options[CONF_IMPORT_STATISTICS] is False
    assert statistic_rebuild_requested(hass, entry.entry_id)
    clear_statistic_rebuild(hass, entry.entry_id)

    shrunk = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_REFRESH_TOKEN: "token", CONF_METERING_POINTS: []},
        options={
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: 180,
            CONF_IMPORT_STATISTICS: True,
        },
    )
    shrunk.add_to_hass(hass)
    shrunk_flow = await _open_settings(hass, shrunk)
    shrunk_flow = await hass.config_entries.options.async_configure(
        shrunk_flow["flow_id"],
        {
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: 30,
            CONF_IMPORT_STATISTICS: True,
        },
    )
    assert shrunk_flow["type"] is FlowResultType.CREATE_ENTRY
    assert not statistic_rebuild_requested(hass, shrunk.entry_id)

    invalid = await _open_settings(hass, entry)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            invalid["flow_id"],
            {
                CONF_SCAN_INTERVAL_HOURS: 0,
                CONF_HISTORY_DAYS: 30,
                CONF_IMPORT_STATISTICS: True,
            },
        )


@pytest.mark.parametrize("days", [730, 1825])
async def test_options_flow_accepts_multi_year_history(hass, days: int) -> None:
    """730 and 1825 days are stored, and a longer window asks for a rebuild."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_REFRESH_TOKEN: "token", CONF_METERING_POINTS: []},
        options={
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: 180,
            CONF_IMPORT_STATISTICS: True,
        },
    )
    entry.add_to_hass(hass)
    result = await _open_settings(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: days,
            CONF_IMPORT_STATISTICS: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_HISTORY_DAYS] == days
    assert statistic_rebuild_requested(hass, entry.entry_id)
    clear_statistic_rebuild(hass, entry.entry_id)


async def test_options_flow_rejects_history_past_five_years(hass) -> None:
    """1826 days is above the selector maximum of 1825."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_REFRESH_TOKEN: "token", CONF_METERING_POINTS: []},
        options={
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: 180,
            CONF_IMPORT_STATISTICS: True,
        },
    )
    entry.add_to_hass(hass)
    result = await _open_settings(hass, entry)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SCAN_INTERVAL_HOURS: 6,
                CONF_HISTORY_DAYS: 1826,
                CONF_IMPORT_STATISTICS: True,
            },
        )


async def test_options_flow_shrinking_five_year_history_does_not_rebuild(hass) -> None:
    """Shortening 1825 days to 730 keeps the statistics that are already stored."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_REFRESH_TOKEN: "token", CONF_METERING_POINTS: []},
        options={
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: 1825,
            CONF_IMPORT_STATISTICS: True,
        },
    )
    entry.add_to_hass(hass)
    result = await _open_settings(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL_HOURS: 6,
            CONF_HISTORY_DAYS: 730,
            CONF_IMPORT_STATISTICS: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_HISTORY_DAYS] == 730
    assert not statistic_rebuild_requested(hass, entry.entry_id)


async def test_points_used_by_another_entry_are_not_offered(
    hass, fake_api, points
) -> None:
    """A second token cannot add a metering point another entry monitors."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="other-account",
        data={
            CONF_REFRESH_TOKEN: "first-token",
            CONF_METERING_POINTS: [points[0].as_dict(), points[1].as_dict()],
        },
    )
    existing.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "second-token"}
    )
    assert result["step_id"] == "metering_points"
    selector = result["data_schema"].schema[CONF_METERING_POINT_IDS]
    offered = {option["value"] for option in selector.config["options"]}
    assert points[0].metering_point_id not in offered
    assert points[1].metering_point_id not in offered
    assert points[2].metering_point_id in offered

    fake_api.points = [points[0], points[1]]
    blocked = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    blocked = await hass.config_entries.flow.async_configure(
        blocked["flow_id"], {CONF_REFRESH_TOKEN: "third-token"}
    )
    assert blocked["type"] is FlowResultType.ABORT
    assert blocked["reason"] == "already_configured"
