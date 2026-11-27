"""Constants for the ElOverblik (DK) integration."""

from __future__ import annotations

from datetime import date, timedelta

DOMAIN = "eloverblik_dk"
NAME = "ElOverblik (DK)"
VERSION = "0.3.0"
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
DEFAULT_HISTORY_DAYS = 180
MIN_HISTORY_DAYS = 1
# Five 365-day years. This stays inside DataHub's retention of the previous
# five calendar years plus the current year.
MAX_HISTORY_DAYS = 1825
DATAHUB_HISTORY_YEARS = 5

# Energinet's older Customer API note caps the API at 25 calls/minute.
# The 2026 swagger allows up to 120/minute per IP. Stay at the stricter budget.
# A five-year backfill is about 59 time-series calls. Wait one extra second
# after each full minute so the next burst starts in the following window.
TOKEN_CALLS_PER_MINUTE = 2
API_CALLS_PER_MINUTE = 25
MAX_METERING_POINTS_PER_CALL = 10
MAX_DAYS_PER_CALL = 31
TOKEN_LIFETIME = timedelta(hours=24)
TOKEN_REFRESH_SKEW = timedelta(minutes=5)
RATE_LIMIT_COOLDOWN_SECONDS = 60.0
RATE_LIMIT_WINDOW_SLACK_SECONDS = 1.0

# Trailing window re-read on later polls so late DataHub deliveries are picked up.
TRAILING_REFRESH_DAYS = 14

# DataHub ``typeOfMP`` codes, grouped by what they measure.
# Grid consumption: E17 consumption point, D07 consumption from grid,
# D10 net from grid.
CONSUMPTION_TYPES = frozenset({"E17", "D07", "D10"})
# Return to grid: E18 production point (what the installation delivers),
# D06 supply to grid, D11 net to grid.
EXPORT_TYPES = frozenset({"E18", "D06", "D11"})
# Production that is not grid export: D01 VE production, D04 surplus
# production group 6, D05 net production, D09 own production.
PRODUCTION_TYPES = frozenset({"D01", "D04", "D05", "D09"})
# Anything else (D12 total consumption, D14 electrical heating, D15 net
# consumption, D02 analysis, ...) is classified as "other". It can still be
# selected, but it is never preselected and is not labelled as grid usage,
# because it overlaps the grid points and would be counted twice.

# Access tokens are shared across clients for the same refresh token, so a
# reload or the config flow followed by setup does not spend GetToken calls.
TOKEN_CACHE_KEY = "access_tokens"
STORAGE_VERSION = 1

LOCAL_TIME_ZONE = "Europe/Copenhagen"
AGGREGATION_ACTUAL = "Actual"
AGGREGATION_HOUR = "Hour"

SERVICE_REBUILD_STATISTICS = "rebuild_statistics"


def earliest_history_date(today: date) -> date:
    """Return the oldest day ElOverblik will serve on ``today``.

    DataHub keeps the previous five calendar years plus the current year.
    On any day in 2026 that first day is 1 January 2021.
    """
    return date(today.year - DATAHUB_HISTORY_YEARS, 1, 1)
