"""Config and options flow for ElOverblik (DK)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
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

from .api import (
    ElOverblikApiError,
    ElOverblikAuthError,
    ElOverblikClient,
    ElOverblikRateLimitError,
    MeteringPoint,
)
from .const import (
    CONF_HISTORY_DAYS,
    CONF_IMPORT_STATISTICS,
    CONF_METERING_POINT_IDS,
    CONF_METERING_POINTS,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL_HOURS,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    MAX_HISTORY_DAYS,
    MAX_SCAN_INTERVAL_HOURS,
    MIN_HISTORY_DAYS,
    MIN_SCAN_INTERVAL_HOURS,
    NAME,
)

_LOGGER = logging.getLogger(__name__)

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
    """Preselect export points and consumption points that are not parents.

    A parent E17 together with its D07 (grid consumption) and D06 (grid export)
    children would double-count in the Energy dashboard. The parent is left
    unselected when it has children. A home with only an E17 still gets that
    point selected.
    """
    parent_ids = {
        point.parent_metering_point_id
        for point in points
        if point.parent_metering_point_id
    }
    chosen = [
        point.metering_point_id
        for point in points
        if not (
            point.metering_point_id in parent_ids and point.direction == "consumption"
        )
    ]
    if not chosen:
        return [point.metering_point_id for point in points]
    return chosen


def option_label(point: MeteringPoint, language: str) -> str:
    """Label a metering point with its address, direction, and id."""
    danish = language.lower().startswith("da")
    if point.direction == "export":
        kind = "Salg/eksport" if danish else "Export/sale"
    else:
        kind = "Forbrug" if danish else "Consumption"
    return f"{point.address} — {kind} — {point.metering_point_id}"


async def async_fetch_metering_points(
    hass: Any,
    refresh_token: str,
) -> list[MeteringPoint]:
    """Validate a refresh token by loading the account's metering points."""
    session = async_get_clientsession(hass)
    client = ElOverblikClient(refresh_token, session=session)
    return await client.async_get_metering_points()


class ElOverblikConfigFlow(ConfigFlow, domain=DOMAIN):
    """Guide the user through token validation and metering-point selection."""

    VERSION = 1

    def __init__(self) -> None:
        """Start a flow with no token yet."""
        self._refresh_token: str | None = None
        self._points: list[MeteringPoint] = []
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
                try:
                    points = await async_fetch_metering_points(self.hass, token)
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
                    await self.async_set_unique_id(unique_id)
                    self._refresh_token = token
                    self._points = points
                    return await self.async_step_metering_points()
        return self.async_show_form(
            step_id=step_id,
            data_schema=_TOKEN_SCHEMA,
            errors=errors,
        )

    async def async_step_metering_points(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose which metering points to monitor."""
        errors: dict[str, str] = {}
        if user_input is not None:
            selected_ids = list(user_input.get(CONF_METERING_POINT_IDS) or [])
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
                        unique_id=account_unique_id(self._points),
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
        schema = vol.Schema(
            {
                vol.Required(CONF_METERING_POINT_IDS): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {
                                "value": point.metering_point_id,
                                "label": option_label(point, language),
                            }
                            for point in self._points
                        ],
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            }
        )
        suggested = self._suggested_ids()
        if hasattr(self, "add_suggested_values_to_schema"):
            schema = self.add_suggested_values_to_schema(
                schema, {CONF_METERING_POINT_IDS: suggested}
            )
        return self.async_show_form(
            step_id="metering_points",
            data_schema=schema,
            errors=errors,
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
    """Change how often data is fetched and how far back statistics go."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form."""
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
        if hasattr(self, "add_suggested_values_to_schema"):
            schema = self.add_suggested_values_to_schema(schema, suggested)
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
