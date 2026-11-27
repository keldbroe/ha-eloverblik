"""Small Home Assistant helpers shared by setup, the config flow and the coordinator."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from .api import TokenCache
from .const import DOMAIN, LOCAL_TIME_ZONE, TOKEN_CACHE_KEY


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
