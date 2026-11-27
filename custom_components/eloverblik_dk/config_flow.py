"""Config and options flow for ElOverblik (DK)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import coordinator as coordinator_module
from .api import (
    ElOverblikApiError,
    ElOverblikAuthError,
    ElOverblikClient,
    ElOverblikRateLimitError,
    EnergyReading,
    MeteringPoint,
    is_grid_point,
)
from .const import (
    AGGREGATION_ACTUAL,
    CONF_HISTORY_DAYS,
    CONF_IMPORT_STATISTICS,
    CONF_METERING_POINT_IDS,
    CONF_METERING_POINTS,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL_HOURS,
    CONF_SHOW_ALL,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    MAX_HISTORY_DAYS,
    MAX_SCAN_INTERVAL_HOURS,
    MIN_HISTORY_DAYS,
    MIN_SCAN_INTERVAL_HOURS,
    NAME,
)
from .duplicates import COMPARE_DAYS, find_duplicate_points
from .helpers import (
    async_set_selected_points,
    ha_time_zone,
    points_in_other_entries,
    selected_points,
    token_cache,
)
from .statistics import request_statistic_rebuild

_LOGGER = logging.getLogger(__name__)

_TOKEN_PLACEHOLDERS = {
    "site": "eloverblik.dk",
    "url": "https://eloverblik.dk/welcome",
}

_TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REFRESH_TOKEN): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
    }
)


def account_unique_id(points: Sequence[MeteringPoint]) -> str:
    """Identify an ElOverblik account by the metering points it can see."""
    return ",".join(sorted(point.metering_point_id for point in points))


def default_metering_point_ids(points: Sequence[MeteringPoint]) -> list[str]:
    """Preselect grid points, leaving out a consumption parent with children.

    Only grid consumption (E17, D07, D10) and return to grid (E18, D06, D11)
    are preselected. Production such as D01/D09 and other children such as
    D12 total consumption overlap those and would be counted twice, so the
    user has to pick them on purpose. A parent E17 is left unselected when it
    has children, because its D07 child already measures grid consumption. A
    home with only an E17 still gets that point selected.
    """
    parent_ids = {
        point.parent_metering_point_id
        for point in points
        if point.parent_metering_point_id
    }
    chosen = [
        point.metering_point_id
        for point in points
        if is_grid_point(point)
        and not (
            point.metering_point_id in parent_ids and point.direction == "consumption"
        )
    ]
    if not chosen:
        return [point.metering_point_id for point in points]
    return chosen


def option_label(
    point: MeteringPoint,
    language: str,
    *,
    same_as: MeteringPoint | None = None,
) -> str:
    """Label a point with its address, kind, DataHub type and id.

    The type (E17, D07, ...) tells a parent from its child, which otherwise
    look alike. ``same_as`` adds a note when this point reports the same
    values as another selected point, so the user can see which one to drop.
    """
    danish = language.lower().startswith("da")
    if point.direction == "export":
        kind = "Salg/eksport" if danish else "Export/sale"
    elif point.direction == "production":
        kind = "Produktion" if danish else "Production"
    elif point.direction == "other":
        kind = "Andet" if danish else "Other"
    else:
        kind = "Forbrug" if danish else "Consumption"
    if point.type_of_mp:
        kind = f"{kind} ({point.type_of_mp})"
    label = f"{point.address} — {kind} — {point.metering_point_id}"
    if same_as is not None:
        note = "samme tal som" if danish else "same values as"
        label = f"{label} — {note} {same_as.type_of_mp or same_as.metering_point_id}"
    return label


def hidden_note(
    points: Sequence[MeteringPoint], duplicates: Mapping[str, str], language: str
) -> str:
    """Say which points are hidden because they repeat another point."""
    by_id = {point.metering_point_id: point for point in points}
    parts = [
        f"{point.type_of_mp} (…{point.metering_point_id[-6:]}) = "
        f"{by_id[duplicates[point.metering_point_id]].type_of_mp}"
        for point in points
        if duplicates.get(point.metering_point_id) in by_id
    ]
    if not parts:
        return ""
    if language.lower().startswith("da"):
        return (
            "Skjult, fordi de viser præcis de samme tal som et andet målepunkt: "
            + ", ".join(parts)
            + ". Normalt skal du ikke vælge dem: det andet målepunkt måler det "
            "samme, og begge giver dobbelte sensorer og kan tælle strømmen to "
            "gange i Energi-dashboardet."
        )
    return (
        "Hidden because they report exactly the same values as another point: "
        + ", ".join(parts)
        + ". You do not normally need them: the other point measures the same "
        "thing, and selecting both gives duplicate sensors and can count the "
        "same electricity twice in the Energy dashboard."
    )


def point_selection(
    flow: ConfigFlow | OptionsFlow,
    points: Sequence[MeteringPoint],
    *,
    language: str,
    duplicates: Mapping[str, str],
    show_all: bool,
    suggested: Sequence[str],
) -> tuple[vol.Schema, list[MeteringPoint], str]:
    """Build the metering point list, hiding points that repeat another.

    ``duplicates`` maps a redundant point to the point it repeats. Those are
    left out unless ``show_all`` is set. A "show all" checkbox is added only
    when something can be hidden. Returns the schema, the visible points and
    the note to put in the form's description.
    """
    by_id = {point.metering_point_id: point for point in points}
    redundant = {
        point_id
        for point_id, keep in duplicates.items()
        if point_id in by_id and keep in by_id
    }
    visible = [
        point
        for point in points
        if show_all or point.metering_point_id not in redundant
    ]
    fields: dict[Any, Any] = {
        vol.Required(CONF_METERING_POINT_IDS): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    {
                        "value": point.metering_point_id,
                        "label": option_label(
                            point,
                            language,
                            same_as=by_id.get(
                                duplicates.get(point.metering_point_id, "")
                            )
                            if point.metering_point_id in redundant
                            else None,
                        ),
                    }
                    for point in visible
                ],
                multiple=True,
                mode=selector.SelectSelectorMode.LIST,
            )
        )
    }
    if redundant:
        fields[vol.Optional(CONF_SHOW_ALL, default=False)] = selector.BooleanSelector()
    visible_ids = {point.metering_point_id for point in visible}
    schema = flow.add_suggested_values_to_schema(
        vol.Schema(fields),
        {
            CONF_METERING_POINT_IDS: [i for i in suggested if i in visible_ids],
            CONF_SHOW_ALL: show_all,
        },
    )
    note = "" if show_all else hidden_note(points, duplicates, language)
    return schema, visible, note


def _make_client(hass: Any, refresh_token: str) -> ElOverblikClient:
    return ElOverblikClient(
        refresh_token,
        session=async_get_clientsession(hass),
        token_cache=token_cache(hass),
        time_zone=ha_time_zone(hass),
    )


async def async_fetch_metering_points(
    hass: Any,
    refresh_token: str,
) -> list[MeteringPoint]:
    """Validate a refresh token by loading the account's metering points."""
    return await _make_client(hass, refresh_token).async_get_metering_points()


async def async_find_duplicates(
    hass: Any, client: Any, points: Sequence[MeteringPoint]
) -> dict[str, str]:
    """Download the last month once and return redundant -> kept point ids.

    This is one request per ten points. Any failure just means nothing is
    hidden: the user then sees every point, as before.
    """
    if len(points) < 2:
        return {}
    now = coordinator_module.current_time(ha_time_zone(hass))
    try:
        readings = await client.async_get_time_series(
            [point.metering_point_id for point in points],
            now.date() - timedelta(days=COMPARE_DAYS),
            now.date(),
            aggregation=AGGREGATION_ACTUAL,
            directions={point.metering_point_id: point.direction for point in points},
        )
    except Exception as err:
        _LOGGER.debug("Could not compare metering points: %s", err)
        return {}
    grouped: dict[str, list[EnergyReading]] = {}
    for reading in readings:
        grouped.setdefault(reading.metering_point_id, []).append(reading)
    result = find_duplicate_points(points, grouped, now=now)
    return {
        finding.remove.metering_point_id: finding.keep.metering_point_id
        for finding in result.findings
    }


class ElOverblikConfigFlow(ConfigFlow, domain=DOMAIN):
    """Guide the user through token validation and metering-point selection."""

    VERSION = 1

    def __init__(self) -> None:
        """Start a flow with no token yet."""
        self._refresh_token: str | None = None
        self._points: list[MeteringPoint] = []
        self._account_id: str | None = None
        self._duplicates: dict[str, str] = {}
        self._show_all = False
        self._reauth = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ElOverblikOptionsFlow:
        """Return the options flow."""
        return ElOverblikOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the refresh token and validate it."""
        return await self._async_token_step(
            step_id="user",
            user_input=user_input,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start a reauth flow when the refresh token is rejected."""
        self._reauth = True
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh refresh token."""
        return await self._async_token_step(
            step_id="reauth_confirm",
            user_input=user_input,
        )

    async def _async_token_step(
        self,
        *,
        step_id: str,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token = str(user_input.get(CONF_REFRESH_TOKEN, "")).strip()
            if not token:
                errors["base"] = "invalid_auth"
            else:
                client = _make_client(self.hass, token)
                try:
                    points = await client.async_get_metering_points()
                except ElOverblikAuthError:
                    errors["base"] = "invalid_auth"
                except ElOverblikRateLimitError:
                    errors["base"] = "rate_limited"
                except ElOverblikApiError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error contacting ElOverblik")
                    errors["base"] = "unknown"
                else:
                    if not points:
                        return self.async_abort(reason="no_metering_points")
                    unique_id = account_unique_id(points)
                    conflict = self._unique_id_conflict(unique_id)
                    if conflict is not None:
                        return self.async_abort(reason="already_configured")
                    taken = self._points_in_other_entries()
                    points = [
                        point
                        for point in points
                        if point.metering_point_id not in taken
                    ]
                    if not points:
                        return self.async_abort(reason="already_configured")
                    await self.async_set_unique_id(unique_id)
                    self._account_id = unique_id
                    self._refresh_token = token
                    self._points = points
                    self._duplicates = await async_find_duplicates(
                        self.hass, client, points
                    )
                    return await self.async_step_metering_points()
        return self.async_show_form(
            step_id=step_id,
            data_schema=_TOKEN_SCHEMA,
            description_placeholders=_TOKEN_PLACEHOLDERS,
            errors=errors,
        )

    async def async_step_metering_points(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose which metering points to monitor.

        Points that report the same values as another point are hidden, so
        only one of each pair can be picked; "show all" brings them back.
        """
        errors: dict[str, str] = {}
        suggested = self._suggested_ids()
        if user_input is not None:
            selected_ids = list(user_input.get(CONF_METERING_POINT_IDS) or [])
            want_all = bool(user_input.get(CONF_SHOW_ALL, False))
            if want_all != self._show_all:
                # The checkbox only changes what is listed; show the form again.
                self._show_all = want_all
                suggested = selected_ids or suggested
            else:
                known = {point.metering_point_id: point for point in self._points}
                selected = [known[mpid] for mpid in selected_ids if mpid in known]
                if not selected:
                    errors["base"] = "no_selection"
                else:
                    data = {
                        CONF_REFRESH_TOKEN: self._refresh_token,
                        CONF_METERING_POINTS: [point.as_dict() for point in selected],
                    }
                    if self._reauth:
                        return self.async_update_reload_and_abort(
                            self._get_reauth_entry(),
                            unique_id=self._account_id,
                            data_updates=data,
                        )
                    return self.async_create_entry(
                        title=NAME,
                        data=data,
                        options={
                            CONF_SCAN_INTERVAL_HOURS: DEFAULT_SCAN_INTERVAL_HOURS,
                            CONF_HISTORY_DAYS: DEFAULT_HISTORY_DAYS,
                            CONF_IMPORT_STATISTICS: True,
                        },
                    )
        language = str(getattr(self.hass.config, "language", "en") or "en")
        schema, _, note = point_selection(
            self,
            self._points,
            language=language,
            duplicates=self._duplicates,
            show_all=self._show_all,
            suggested=suggested,
        )
        return self.async_show_form(
            step_id="metering_points",
            data_schema=schema,
            errors=errors,
            description_placeholders={"hidden": note},
        )

    def _suggested_ids(self) -> list[str]:
        available = {point.metering_point_id for point in self._points}
        if self._reauth:
            entry = self._get_reauth_entry()
            previous = [
                str(item.get("metering_point_id"))
                for item in entry.data.get(CONF_METERING_POINTS, [])
                if isinstance(item, dict) and item.get("metering_point_id") in available
            ]
            if previous:
                return previous
        return default_metering_point_ids(self._points)

    def _points_in_other_entries(self) -> set[str]:
        current_id = self._get_reauth_entry().entry_id if self._reauth else None
        return points_in_other_entries(self.hass, current_id)

    def _unique_id_conflict(self, unique_id: str) -> ConfigEntry | None:
        existing = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, unique_id
        )
        if existing is None:
            return None
        if self.source == SOURCE_REAUTH:
            current = self._get_reauth_entry()
            if existing.entry_id == current.entry_id:
                return None
        return existing


class ElOverblikOptionsFlow(OptionsFlow):
    """Change the selected metering points, polling and statistics history."""

    def __init__(self) -> None:
        """Start without a list of available points."""
        self._available: list[MeteringPoint] = []
        self._duplicates: dict[str, str] = {}
        self._show_all = False

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the two option pages."""
        return self.async_show_menu(
            step_id="init", menu_options=["metering_points", "settings"]
        )

    async def async_step_metering_points(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add or remove metering points without setting the entry up again.

        Deselected points lose their device and sensors; their imported
        statistics stay in the recorder. Points another entry monitors are
        not offered.
        """
        errors: dict[str, str] = {}
        entry = self.config_entry
        current = selected_points(entry)
        if not self._available:
            self._available, fetch_error = await self._async_available_points()
            if fetch_error:
                errors["base"] = fetch_error
        known = {point.metering_point_id: point for point in self._available}
        for point in current:
            known.setdefault(point.metering_point_id, point)
        suggested = [point.metering_point_id for point in current]
        if user_input is not None:
            chosen_ids = list(user_input.get(CONF_METERING_POINT_IDS) or [])
            want_all = bool(user_input.get(CONF_SHOW_ALL, False))
            if want_all != self._show_all:
                self._show_all = want_all
                suggested = chosen_ids or suggested
            else:
                chosen = [known[mpid] for mpid in chosen_ids if mpid in known]
                if not chosen:
                    errors["base"] = "no_selection"
                else:
                    async_set_selected_points(self.hass, entry, chosen)
                    # Options are unchanged, so this does not reload twice.
                    return self.async_create_entry(title="", data=dict(entry.options))
        language = str(getattr(self.hass.config, "language", "en") or "en")
        schema, _, note = point_selection(
            self,
            list(known.values()),
            language=language,
            duplicates=self._known_duplicates(),
            show_all=self._show_all,
            suggested=suggested,
        )
        return self.async_show_form(
            step_id="metering_points",
            data_schema=schema,
            errors=errors,
            description_placeholders={"hidden": note},
        )

    def _known_duplicates(self) -> dict[str, str]:
        """Return redundant point id -> kept point id from the last poll."""
        runtime = getattr(self.config_entry, "runtime_data", None)
        coordinator = getattr(runtime, "coordinator", None)
        known = dict(getattr(coordinator, "duplicates", {}) or {})
        known.update(self._duplicates)
        return known

    async def _async_available_points(self) -> tuple[list[MeteringPoint], str | None]:
        """Load the account's points, reusing the running client when loaded."""
        entry = self.config_entry
        runtime = getattr(entry, "runtime_data", None)
        client = getattr(runtime, "client", None)
        if client is None:
            client = _make_client(self.hass, entry.data[CONF_REFRESH_TOKEN])
        try:
            points = await client.async_get_metering_points()
        except ElOverblikAuthError:
            return [], "invalid_auth"
        except ElOverblikRateLimitError:
            return [], "rate_limited"
        except ElOverblikApiError:
            return [], "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error contacting ElOverblik")
            return [], "unknown"
        taken = points_in_other_entries(self.hass, entry.entry_id)
        available = [p for p in points if p.metering_point_id not in taken]
        # Compare every available point, not only the selected ones, so a
        # point that was removed as a duplicate stays hidden afterwards.
        self._duplicates = await async_find_duplicates(self.hass, client, available)
        return available, None

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change polling, statistics history and the statistics import."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                hours = int(user_input[CONF_SCAN_INTERVAL_HOURS])
                days = int(user_input[CONF_HISTORY_DAYS])
            except (KeyError, TypeError, ValueError):
                errors["base"] = "invalid_option"
            else:
                hours_ok = MIN_SCAN_INTERVAL_HOURS <= hours <= MAX_SCAN_INTERVAL_HOURS
                days_ok = MIN_HISTORY_DAYS <= days <= MAX_HISTORY_DAYS
                if not hours_ok or not days_ok:
                    errors["base"] = "invalid_option"
                else:
                    previous = _option_int(
                        self.config_entry.options.get(CONF_HISTORY_DAYS),
                        DEFAULT_HISTORY_DAYS,
                    )
                    if days > previous:
                        request_statistic_rebuild(self.hass, self.config_entry.entry_id)
                    return self.async_create_entry(
                        title="",
                        data={
                            CONF_SCAN_INTERVAL_HOURS: hours,
                            CONF_HISTORY_DAYS: days,
                            CONF_IMPORT_STATISTICS: bool(
                                user_input.get(CONF_IMPORT_STATISTICS, True)
                            ),
                        },
                    )
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL_HOURS): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL_HOURS,
                        max=MAX_SCAN_INTERVAL_HOURS,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_HISTORY_DAYS): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_HISTORY_DAYS,
                        max=MAX_HISTORY_DAYS,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_IMPORT_STATISTICS): selector.BooleanSelector(),
            }
        )
        suggested = {
            CONF_SCAN_INTERVAL_HOURS: options.get(
                CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS
            ),
            CONF_HISTORY_DAYS: options.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS),
            CONF_IMPORT_STATISTICS: options.get(CONF_IMPORT_STATISTICS, True),
        }
        schema = self.add_suggested_values_to_schema(schema, suggested)
        return self.async_show_form(
            step_id="settings", data_schema=schema, errors=errors
        )


def _option_int(value: Any, default: int) -> int:
    """Return an option as an int, or the default when it is unset."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
