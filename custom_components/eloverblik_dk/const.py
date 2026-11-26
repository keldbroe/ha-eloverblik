"""Constants for the ElOverblik (DK) integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "eloverblik_dk"
NAME = "ElOverblik (DK)"
VERSION = "0.1.0"
ATTRIBUTION = "Energinet ElOverblik / DataHub"
ISSUE_URL = "https://github.com/keldbroe/ha-eloverblik/issues"
DOCS_URL = "https://github.com/keldbroe/ha-eloverblik"

API_BASE_URL = "https://api.eloverblik.dk/customerapi"
TOKEN_URL = f"{API_BASE_URL}/api/token"
METERING_POINTS_URL = f"{API_BASE_URL}/api/meteringpoints/meteringpoints"
TIME_SERIES_URL = f"{API_BASE_URL}/api/meterdata/gettimeseries"
USER_AGENT = f"ha-eloverblik/{VERSION} (+{DOCS_URL})"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_METERING_POINTS = "metering_points"
CONF_METERING_POINT_IDS = "metering_point_ids"
CONF_SCAN_INTERVAL_HOURS = "scan_interval_hours"
CONF_HISTORY_DAYS = "history_days"
CONF_IMPORT_STATISTICS = "import_statistics"

DEFAULT_SCAN_INTERVAL_HOURS = 6
MIN_SCAN_INTERVAL_HOURS = 1
MAX_SCAN_INTERVAL_HOURS = 24
DEFAULT_HISTORY_DAYS = 30
MIN_HISTORY_DAYS = 1
MAX_HISTORY_DAYS = 365

# Energinet's older Customer API note caps the API at 25 calls/minute.
# The 2026 swagger allows up to 120/minute per IP. Stay at the stricter budget.
TOKEN_CALLS_PER_MINUTE = 2
API_CALLS_PER_MINUTE = 25
MAX_METERING_POINTS_PER_CALL = 10
MAX_DAYS_PER_CALL = 31
TOKEN_LIFETIME = timedelta(hours=24)
TOKEN_REFRESH_SKEW = timedelta(minutes=5)
RATE_LIMIT_COOLDOWN_SECONDS = 60.0

# Trailing window re-read on later polls so late DataHub deliveries are picked up.
TRAILING_REFRESH_DAYS = 14

CONSUMPTION_TYPES = frozenset({"E17", "D07", "D12"})
EXPORT_TYPES = frozenset({"D06", "E18", "D01", "D09", "D11"})

LOCAL_TIME_ZONE = "Europe/Copenhagen"
AGGREGATION_HOUR = "Hour"
