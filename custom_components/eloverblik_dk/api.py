"""Async client for Energinet's ElOverblik Customer API.

The client is intentionally free of Home Assistant imports so it can be tested
and reused on its own. Authentication uses a long-lived refresh token to mint
a data-access token, which is cached until shortly before it expires.

The time-series body follows the published swagger schema
``{"meteringPoints": {"meteringPoint": ["<18-digit id>", ...]}}``.
A flat ``meteringPointIds`` array is rejected by the API (``additionalProperties``
is false on ``MeteringPointsRequest``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from base64 import urlsafe_b64decode
from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiohttp

from .const import (
    AGGREGATION_HOUR,
    API_CALLS_PER_MINUTE,
    CONSUMPTION_TYPES,
    EXPORT_TYPES,
    MAX_DAYS_PER_CALL,
    MAX_METERING_POINTS_PER_CALL,
    METERING_POINTS_URL,
    RATE_LIMIT_COOLDOWN_SECONDS,
    TIME_SERIES_URL,
    TOKEN_CALLS_PER_MINUTE,
    TOKEN_LIFETIME,
    TOKEN_REFRESH_SKEW,
    TOKEN_URL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

Direction = Literal["consumption", "export"]
_LOCAL_ZONE = ZoneInfo("Europe/Copenhagen")
_METERING_POINT_ID = re.compile(r"^\d{18}$")
_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
_AGGREGATIONS = {
    "actual": "Actual",
    "quarter": "Quarter",
    "hour": "Hour",
    "day": "Day",
    "month": "Month",
    "year": "Year",
}
_UNIT_TO_KWH = {
    "WH": 0.001,
    "KWH": 1.0,
    "MWH": 1000.0,
    "GWH": 1_000_000.0,
}
_MISSING_QUALITY = "A02"
_CONSUMPTION_SERIES = ("A04", "A01", "A64")
_EXPORT_SERIES = ("A01", "A04", "A64")
_TOKEN_TIMEOUT = aiohttp.ClientTimeout(total=30)
_API_TIMEOUT = aiohttp.ClientTimeout(total=120)


class ElOverblikError(Exception):
    """Base error for the ElOverblik client."""


class ElOverblikAuthError(ElOverblikError):
    """The refresh token or data-access token was rejected."""


class ElOverblikRateLimitError(ElOverblikError):
    """ElOverblik or DataHub asked the client to back off (HTTP 429 or 503)."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        """Store the server-provided backoff, when one was given."""
        super().__init__(message)
        self.retry_after = retry_after


class ElOverblikApiError(ElOverblikError):
    """The Customer API returned an unexpected response."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_code: int | None = None,
    ) -> None:
        """Remember the HTTP status and the ElOverblik error code, if any."""
        super().__init__(message)
        self.status = status
        self.error_code = error_code

    @property
    def aggregation_rejected(self) -> bool:
        """Return True when DataHub refused the requested aggregation."""
        if self.error_code in {30008, 30011}:
            return True
        return "aggregat" in str(self).lower()


@dataclass(frozen=True, slots=True)
class MeteringPoint:
    """A metering point the customer can read."""

    metering_point_id: str
    type_of_mp: str
    direction: Direction
    address: str
    meter_reading_occurrence: str
    parent_metering_point_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize the point for config-entry storage."""
        return {
            "metering_point_id": self.metering_point_id,
            "type_of_mp": self.type_of_mp,
            "direction": self.direction,
            "address": self.address,
            "meter_reading_occurrence": self.meter_reading_occurrence,
            "parent_metering_point_id": self.parent_metering_point_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MeteringPoint:
        """Restore a point stored on the config entry."""
        type_of_mp = str(data.get("type_of_mp") or "")
        direction = data.get("direction")
        if direction not in {"consumption", "export"}:
            direction = classify_direction(type_of_mp)
        return cls(
            metering_point_id=str(data.get("metering_point_id") or ""),
            type_of_mp=type_of_mp,
            direction=direction,
            address=str(data.get("address") or ""),
            meter_reading_occurrence=str(
                data.get("meter_reading_occurrence") or "PT1H"
            ),
            parent_metering_point_id=_optional_str(
                data.get("parent_metering_point_id")
            ),
        )


@dataclass(frozen=True, slots=True)
class EnergyReading:
    """One energy quantity beginning at ``start``."""

    metering_point_id: str
    start: datetime
    duration: timedelta
    kwh: float
    quality: str | None = None
    business_type: str | None = None


class AsyncIntervalLimiter:
    """Sliding-window limiter. Callers wait instead of bursting past the budget."""

    def __init__(self, max_calls: int, period_seconds: float) -> None:
        """Limit ``max_calls`` acquisitions inside each ``period_seconds`` window."""
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self._max_calls = max_calls
        self._period = period_seconds
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until another call is allowed, then record it."""
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= self._period:
                    self._stamps.popleft()
                if len(self._stamps) < self._max_calls:
                    self._stamps.append(now)
                    return
                wait = self._period - (now - self._stamps[0])
            await asyncio.sleep(max(wait, 0.0))


class ElOverblikClient:
    """Customer API client with cached data-access tokens and rate limiting."""

    def __init__(
        self,
        refresh_token: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Create a client. A session passed in is not closed by this client."""
        self._refresh_token = refresh_token.strip()
        self._session = session
        self._owns_session = session is None
        self._access_token: str | None = None
        self._access_expiry: datetime | None = None
        self._token_generation = 0
        self._token_lock = asyncio.Lock()
        self._token_limiter = AsyncIntervalLimiter(TOKEN_CALLS_PER_MINUTE, 60)
        self._api_limiter = AsyncIntervalLimiter(API_CALLS_PER_MINUTE, 60)
        self._token_cooldown_until = 0.0
        self._api_cooldown_until = 0.0

    def __repr__(self) -> str:
        """Hide the refresh token from logs and tracebacks."""
        return "ElOverblikClient(<refresh token hidden>)"

    async def async_close(self) -> None:
        """Close the session when this client created it."""
        if (
            self._owns_session
            and self._session is not None
            and not self._session.closed
        ):
            await self._session.close()

    async def async_get_metering_points(self) -> list[MeteringPoint]:
        """Return linked metering points, including child points such as D06/D07."""
        payload = await self._authorized(
            "GET",
            METERING_POINTS_URL,
            params={"includeAll": "false"},
        )
        return parse_metering_points(payload)

    async def async_get_time_series(
        self,
        metering_point_ids: Sequence[str],
        date_from: date,
        date_to: date,
        aggregation: str = AGGREGATION_HOUR,
        directions: Mapping[str, str] | None = None,
    ) -> list[EnergyReading]:
        """Fetch an inclusive date range, batched by 10 points and 31 days.

        ``date_to`` is inclusive. The API rejects a range whose ends are equal
        and treats the end date as the exclusive boundary, so the client sends
        the following day and never asks for a day after tomorrow.
        """
        ids = _valid_ids(metering_point_ids)
        if not ids:
            return []
        if date_to < date_from:
            raise ValueError("date_to is before date_from")
        canonical = _canonical_aggregation(aggregation)
        tomorrow = datetime.now(_LOCAL_ZONE).date() + timedelta(days=1)
        end_inclusive = min(date_to, tomorrow - timedelta(days=1))
        if end_inclusive < date_from:
            end_inclusive = date_from
        readings: list[EnergyReading] = []
        for id_chunk in _chunks(ids, MAX_METERING_POINTS_PER_CALL):
            for chunk_start, chunk_end in _date_chunks(
                date_from, end_inclusive, MAX_DAYS_PER_CALL
            ):
                exclusive_end = min(chunk_end + timedelta(days=1), tomorrow)
                if exclusive_end <= chunk_start:
                    continue
                payload = await self._fetch_series(
                    id_chunk, chunk_start, exclusive_end, canonical
                )
                readings.extend(parse_time_series(payload, directions=directions))
        return readings

    async def _fetch_series(
        self,
        metering_point_ids: Sequence[str],
        date_from: date,
        date_to_exclusive: date,
        aggregation: str,
    ) -> Any:
        """POST one time-series batch, falling back to Actual aggregation once."""
        url = (
            f"{TIME_SERIES_URL}/{date_from.isoformat()}/"
            f"{date_to_exclusive.isoformat()}/{aggregation}"
        )
        body = {"meteringPoints": {"meteringPoint": list(metering_point_ids)}}
        try:
            return await self._authorized("POST", url, json_body=body)
        except ElOverblikApiError as err:
            if aggregation == "Actual" or not err.aggregation_rejected:
                raise
            _LOGGER.debug(
                "Aggregation %s was rejected; retrying Actual for %s points",
                aggregation,
                len(metering_point_ids),
            )
        fallback = (
            f"{TIME_SERIES_URL}/{date_from.isoformat()}/"
            f"{date_to_exclusive.isoformat()}/Actual"
        )
        return await self._authorized("POST", fallback, json_body=body)

    async def _authorized(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        """Send a data request, refreshing the access token once after HTTP 401."""
        token = await self._get_access_token()
        generation = self._token_generation
        return await self._raw_api(
            method,
            url,
            token,
            generation,
            params=params,
            json_body=json_body,
            allow_auth_retry=True,
        )

    async def _raw_api(
        self,
        method: str,
        url: str,
        token: str,
        generation: int,
        *,
        params: Mapping[str, str] | None,
        json_body: Any,
        allow_auth_retry: bool,
    ) -> Any:
        self._raise_if_cooling_down(self._api_cooldown_until, "Customer API")
        await self._api_limiter.acquire()
        status, body, retry_after = await self._send(
            method,
            url,
            token=token,
            params=params,
            json_body=json_body,
            client_timeout=_API_TIMEOUT,
        )
        if status in {429, 503}:
            delay = _retry_after_seconds(retry_after)
            self._api_cooldown_until = time.monotonic() + delay
            raise ElOverblikRateLimitError(
                f"ElOverblik returned HTTP {status}",
                retry_after=delay,
            )
        if status in {401, 403} and allow_auth_retry:
            token = await self._refresh_after_rejection(generation)
            return await self._raw_api(
                method,
                url,
                token,
                self._token_generation,
                params=params,
                json_body=json_body,
                allow_auth_retry=False,
            )
        if status in {401, 403}:
            raise ElOverblikAuthError("Data access token was rejected")
        if status >= 400:
            message, error_code = _describe_error(body, status)
            raise ElOverblikApiError(message, status=status, error_code=error_code)
        return _decode_json(body)

    async def _refresh_after_rejection(self, generation: int) -> str:
        """Mint a new access token unless another caller already did."""
        async with self._token_lock:
            if generation != self._token_generation and self._access_token:
                return self._access_token
            _LOGGER.debug("Data access token was rejected; refreshing once")
            token = await self._exchange_token()
            self._store_token(token)
            return token

    async def _get_access_token(self, *, force: bool = False) -> str:
        """Return a cached data-access token, exchanging the refresh token if needed."""
        if not force and self._token_is_fresh():
            assert self._access_token is not None
            return self._access_token
        async with self._token_lock:
            if not force and self._token_is_fresh():
                assert self._access_token is not None
                return self._access_token
            token = await self._exchange_token()
            self._store_token(token)
            return token

    def _token_is_fresh(self) -> bool:
        if self._access_token is None or self._access_expiry is None:
            return False
        return datetime.now(UTC) < self._access_expiry - TOKEN_REFRESH_SKEW

    def _store_token(self, token: str) -> None:
        expiry = jwt_expiry(token) or (datetime.now(UTC) + TOKEN_LIFETIME)
        self._access_token = token
        self._access_expiry = expiry
        self._token_generation += 1

    async def _exchange_token(self) -> str:
        """Exchange the refresh token. Only this call counts against GetToken."""
        self._raise_if_cooling_down(self._token_cooldown_until, "GetToken")
        await self._token_limiter.acquire()
        status, body, retry_after = await self._send(
            "GET",
            TOKEN_URL,
            token=self._refresh_token,
            params=None,
            json_body=None,
            client_timeout=_TOKEN_TIMEOUT,
        )
        if status in {429, 503}:
            delay = _retry_after_seconds(retry_after)
            self._token_cooldown_until = time.monotonic() + delay
            raise ElOverblikRateLimitError(
                f"ElOverblik token endpoint returned HTTP {status}",
                retry_after=delay,
            )
        if status in {401, 403}:
            raise ElOverblikAuthError("Refresh token was rejected")
        if status >= 400:
            message, error_code = _describe_error(body, status)
            raise ElOverblikApiError(message, status=status, error_code=error_code)
        return _parse_access_token(body)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        token: str,
        params: Mapping[str, str] | None,
        json_body: Any,
        client_timeout: aiohttp.ClientTimeout,
    ) -> tuple[int, str, str | None]:
        session = await self._get_session()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "X-User-Correlation-ID": str(uuid4()),
        }
        _LOGGER.debug("ElOverblik %s %s", method, url)
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=client_timeout,
            ) as response:
                body = await response.text()
                return response.status, body, response.headers.get("Retry-After")
        except TimeoutError as err:
            raise ElOverblikApiError("ElOverblik request timed out") from err
        except aiohttp.ClientError as err:
            raise ElOverblikApiError("Could not reach ElOverblik") from err

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    @staticmethod
    def _raise_if_cooling_down(cooldown_until: float, what: str) -> None:
        remaining = cooldown_until - time.monotonic()
        if remaining > 0:
            raise ElOverblikRateLimitError(
                f"Waiting out an ElOverblik {what} backoff",
                retry_after=remaining,
            )


def classify_direction(type_of_mp: str) -> Direction:
    """Classify a DataHub ``typeOfMP`` as grid consumption or grid export.

    Unknown codes are treated as consumption and the raw code is kept on the
    metering point so the user can still see it.
    """
    if (type_of_mp or "").upper() in EXPORT_TYPES:
        return "export"
    if (type_of_mp or "").upper() in CONSUMPTION_TYPES:
        return "consumption"
    return "consumption"


def format_address(payload: Mapping[str, Any]) -> str:
    """Build a short Danish address from the metering-point master data."""
    street = " ".join(
        part
        for part in (
            _optional_str(payload.get("streetName")),
            _optional_str(payload.get("buildingNumber")),
        )
        if part
    )
    floor = " ".join(
        part
        for part in (
            _optional_str(payload.get("floorId")),
            _optional_str(payload.get("roomId")),
        )
        if part
    )
    line = ", ".join(part for part in (street, floor) if part)
    city = " ".join(
        part
        for part in (
            _optional_str(payload.get("postcode")),
            _optional_str(payload.get("cityName")),
        )
        if part
    )
    text = ", ".join(part for part in (line, city) if part)
    return text or "Målepunkt"


def parse_metering_points(payload: Any) -> list[MeteringPoint]:
    """Parse ``GET /meteringpoints`` and flatten child metering points."""
    points: list[MeteringPoint] = []
    seen: set[str] = set()
    for item in _result_items(payload):
        if not isinstance(item, dict):
            continue
        address = format_address(item)
        _append_point(points, seen, item, address=address, parent_id=None)
        for child in item.get("childMeteringPoints") or []:
            if isinstance(child, dict):
                _append_point(
                    points,
                    seen,
                    child,
                    address=address,
                    parent_id=_optional_str(item.get("meteringPointId")),
                )
    return points


def parse_time_series(
    payload: Any,
    *,
    directions: Mapping[str, str] | None = None,
) -> list[EnergyReading]:
    """Parse a ``MyEnergyData_MarketDocument`` response into kWh readings.

    Missing positions are left as gaps. Quality ``A02`` (not available) is
    skipped even when the quantity is zero. Profiled ``A64`` series are ignored
    when a measured series for the same metering point is present.
    """
    readings: list[EnergyReading] = []
    if isinstance(payload, dict) and "TimeSeries" in payload:
        readings.extend(
            _readings_from_document(payload, fallback_id="", direction=None)
        )
        return readings
    for item in _result_items(payload):
        if not isinstance(item, dict):
            continue
        if "TimeSeries" in item and "MyEnergyData_MarketDocument" not in item:
            readings.extend(
                _readings_from_document(item, fallback_id="", direction=None)
            )
            continue
        if item.get("success") is False:
            _LOGGER.info(
                "No time series for %s: %s (%s)",
                item.get("id"),
                item.get("errorText") or "request failed",
                item.get("errorCode"),
            )
            continue
        document = item.get("MyEnergyData_MarketDocument")
        if not isinstance(document, dict):
            continue
        fallback_id = str(item.get("id") or "")
        direction = None
        if directions and fallback_id in directions:
            direction = directions[fallback_id]
        readings.extend(
            _readings_from_document(
                document, fallback_id=fallback_id, direction=direction
            )
        )
    return readings


def jwt_expiry(token: str) -> datetime | None:
    """Read the ``exp`` claim from a JWT without verifying its signature."""
    try:
        segment = token.split(".")[1]
    except IndexError:
        return None
    padded = segment + "=" * (-len(segment) % 4)
    try:
        payload = json.loads(urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if isinstance(exp, int | float) and exp > 0:
        return datetime.fromtimestamp(float(exp), tz=UTC)
    return None


def _append_point(
    points: list[MeteringPoint],
    seen: set[str],
    payload: Mapping[str, Any],
    *,
    address: str,
    parent_id: str | None,
) -> None:
    metering_point_id = str(payload.get("meteringPointId") or "")
    if not _METERING_POINT_ID.fullmatch(metering_point_id) or metering_point_id in seen:
        return
    seen.add(metering_point_id)
    type_of_mp = str(payload.get("typeOfMP") or "").upper()
    occurrence = str(payload.get("meterReadingOccurrence") or "PT1H")
    points.append(
        MeteringPoint(
            metering_point_id=metering_point_id,
            type_of_mp=type_of_mp,
            direction=classify_direction(type_of_mp),
            address=address or format_address(payload),
            meter_reading_occurrence=occurrence,
            parent_metering_point_id=parent_id
            or _optional_str(payload.get("parentMeteringPointId")),
        )
    )


def _readings_from_document(
    document: Mapping[str, Any],
    *,
    fallback_id: str,
    direction: str | None,
) -> list[EnergyReading]:
    series_list = [
        item for item in _as_list(document.get("TimeSeries")) if isinstance(item, dict)
    ]
    chosen = _choose_series(series_list, direction)
    if chosen is None:
        return []
    metering_point_id = _series_metering_point_id(chosen, fallback_id)
    factor = _unit_factor(chosen.get("measurement_Unit.name"))
    business_type = _optional_str(chosen.get("businessType"))
    readings: list[EnergyReading] = []
    for period in _as_list(chosen.get("Period")):
        if not isinstance(period, dict):
            continue
        interval = period.get("timeInterval") or {}
        if not isinstance(interval, dict):
            interval = {}
        start = _parse_datetime(interval.get("start"))
        end = _parse_datetime(interval.get("end"))
        if start is None:
            continue
        resolution = str(period.get("resolution") or "")
        duration = _parse_duration(resolution, start, end)
        if duration is None or duration <= timedelta(0):
            continue
        for point in _as_list(period.get("Point")):
            reading = _reading_from_point(
                point,
                metering_point_id=metering_point_id,
                start=start,
                duration=duration,
                factor=factor,
                business_type=business_type,
            )
            if reading is not None:
                readings.append(reading)
    return readings


def _reading_from_point(
    point: Any,
    *,
    metering_point_id: str,
    start: datetime,
    duration: timedelta,
    factor: float,
    business_type: str | None,
) -> EnergyReading | None:
    if not isinstance(point, dict):
        return None
    quality = _optional_str(point.get("out_Quantity.quality"))
    if quality == _MISSING_QUALITY:
        return None
    raw_quantity = point.get("out_Quantity.quantity")
    if raw_quantity is None or raw_quantity == "":
        return None
    try:
        position = int(str(point.get("position")))
    except (TypeError, ValueError):
        return None
    if position < 1:
        return None
    try:
        quantity = float(str(raw_quantity).replace(",", "."))
    except ValueError:
        return None
    return EnergyReading(
        metering_point_id=metering_point_id,
        start=start + (position - 1) * duration,
        duration=duration,
        kwh=quantity * factor,
        quality=quality,
        business_type=business_type,
    )


def _choose_series(
    series_list: Sequence[Mapping[str, Any]],
    direction: str | None,
) -> Mapping[str, Any] | None:
    if not series_list:
        return None
    non_profiled = [
        series for series in series_list if series.get("businessType") != "A64"
    ]
    candidates = non_profiled or list(series_list)
    if len(candidates) == 1:
        return candidates[0]
    preference = _EXPORT_SERIES if direction == "export" else _CONSUMPTION_SERIES
    by_type = {str(series.get("businessType")): series for series in candidates}
    for code in preference:
        if code in by_type:
            return by_type[code]
    return candidates[0]


def _series_metering_point_id(series: Mapping[str, Any], fallback_id: str) -> str:
    market = series.get("MarketEvaluationPoint")
    if isinstance(market, dict):
        mrid = market.get("mRID")
        if isinstance(mrid, dict) and mrid.get("name"):
            return str(mrid["name"])
        if isinstance(mrid, str) and mrid:
            return mrid
    mrid = series.get("mRID")
    if isinstance(mrid, str) and _METERING_POINT_ID.fullmatch(mrid):
        return mrid
    return fallback_id


def _unit_factor(unit: Any) -> float:
    key = str(unit or "KWH").upper().replace(" ", "")
    factor = _UNIT_TO_KWH.get(key)
    if factor is None:
        _LOGGER.debug("Unknown measurement unit %s; treating quantities as kWh", unit)
        return 1.0
    return factor


def _parse_duration(
    resolution: str,
    start: datetime,
    end: datetime | None,
) -> timedelta | None:
    text = resolution.strip().upper()
    if text:
        match = _ISO_DURATION.fullmatch(text)
        if match:
            return timedelta(
                days=int(match.group("days") or 0),
                hours=int(match.group("hours") or 0),
                minutes=int(match.group("minutes") or 0),
                seconds=int(match.group("seconds") or 0),
            )
    if end is not None and end > start:
        return end - start
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_access_token(body: str) -> str:
    text = body.strip()
    if not text:
        raise ElOverblikAuthError("ElOverblik returned an empty token response")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        if text.count(".") == 2:
            return text
        raise ElOverblikApiError("Token response was not JSON") from None
    if isinstance(payload, str) and payload:
        return payload
    if isinstance(payload, dict):
        if payload.get("success") is False:
            raise ElOverblikAuthError(
                str(payload.get("errorText") or "Refresh token was rejected")
            )
        result = payload.get("result")
        if isinstance(result, str) and result:
            return result
    raise ElOverblikAuthError("ElOverblik did not return a data access token")


def _decode_json(body: str) -> Any:
    if not body.strip():
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as err:
        raise ElOverblikApiError("ElOverblik returned invalid JSON") from err


def _describe_error(body: str, status: int) -> tuple[str, int | None]:
    detail = ""
    error_code: int | None = None
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        raw_code = payload.get("errorCode")
        if isinstance(raw_code, int):
            error_code = raw_code
        for key in ("errorText", "detail", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
    message = f"ElOverblik request failed with HTTP {status}"
    if detail:
        message = f"{message}: {detail[:300]}"
    return message, error_code


def _retry_after_seconds(value: str | None) -> float:
    if value is None or not str(value).strip():
        return RATE_LIMIT_COOLDOWN_SECONDS
    try:
        return max(float(value), 1.0)
    except ValueError:
        return RATE_LIMIT_COOLDOWN_SECONDS


def _result_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        result = payload.get("result", payload)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
    return []


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _canonical_aggregation(value: str) -> str:
    canonical = _AGGREGATIONS.get(value.strip().lower())
    if canonical is None:
        raise ValueError(f"Unsupported aggregation: {value}")
    return canonical


def _valid_ids(metering_point_ids: Iterable[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw in metering_point_ids:
        metering_point_id = str(raw).strip()
        if metering_point_id in seen or not _METERING_POINT_ID.fullmatch(
            metering_point_id
        ):
            continue
        seen.add(metering_point_id)
        unique.append(metering_point_id)
    return unique


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for index in range(0, len(items), size):
        yield list(items[index : index + size])


def _date_chunks(
    start: date, end_inclusive: date, span_days: int
) -> Iterator[tuple[date, date]]:
    cursor = start
    while cursor <= end_inclusive:
        chunk_end = min(cursor + timedelta(days=span_days - 1), end_inclusive)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)
