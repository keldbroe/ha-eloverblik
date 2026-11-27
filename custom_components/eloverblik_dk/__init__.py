"""The ElOverblik (DK) Home Assistant integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import ElOverblikClient
from .const import (
    CONF_REFRESH_TOKEN,
    DOMAIN,
    SERVICE_REBUILD_STATISTICS,
    STORAGE_VERSION,
)
from .coordinator import ElOverblikCoordinator, ElOverblikRuntime, storage_key
from .helpers import ha_time_zone, token_cache

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration. Configuration is done in the UI."""

    async def _async_rebuild_statistics(call: ServiceCall) -> None:
        """Rewrite long-term statistics for every loaded ElOverblik entry."""
        del call
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if not entries:
            raise HomeAssistantError("ElOverblik is not loaded")
        for entry in entries:
            runtime: ElOverblikRuntime = entry.runtime_data
            await runtime.coordinator.async_rebuild_statistics()

    hass.services.async_register(
        DOMAIN, SERVICE_REBUILD_STATISTICS, _async_rebuild_statistics
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ElOverblik from a config entry."""
    session = async_get_clientsession(hass)
    client = ElOverblikClient(
        entry.data[CONF_REFRESH_TOKEN],
        session=session,
        token_cache=token_cache(hass),
        time_zone=ha_time_zone(hass),
    )
    coordinator = ElOverblikCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = ElOverblikRuntime(client=client, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _LOGGER.debug("ElOverblik entry %s is ready", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and release the API client."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime: ElOverblikRuntime = entry.runtime_data
        await runtime.client.async_close()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the entry's backfill marker. Imported statistics are kept."""
    await Store(hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
