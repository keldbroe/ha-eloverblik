"""Tests for the standalone ElOverblik API client."""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aioresponses import aioresponses

from custom_components.eloverblik_dk.api import (
    ElOverblikApiError,
    ElOverblikAuthError,
    ElOverblikClient,
    ElOverblikRateLimitError,
    classify_direction,
    parse_metering_points,
    parse_time_series,
)
from custom_components.eloverblik_dk.const import METERING_POINTS_URL, TOKEN_URL

FIXTURES = Path(__file__).parent / "fixtures"
CONSUMPTION_ID = "571313174000000017"
EXPORT_CHILD_ID = "571313174000000066"
PRODUCTION_ID = "571313174000000018"


def _jwt(exp: float) -> str:
    def encode(payload: dict[str, object]) -> str:
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode({'exp': exp})}.sig"


def _load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _calls(
    mocked: aioresponses, method: str, fragment: str
) -> list[tuple[str, object]]:
    found: list[tuple[str, object]] = []
    for key, calls in mocked.requests.items():
        if key[0] == method and fragment in str(key[1]):
            found.extend((str(key[1]), call) for call in calls)
    return found


@pytest.fixture
def metering_payload() -> dict[str, object]:
    """Return the metering-point fixture."""
    return _load("metering_points.json")


def test_metering_points_classify_parent_and_children(metering_payload: dict) -> None:
    """Consumption and export codes are classified, and children inherit the address."""
    points = parse_metering_points(metering_payload)
    by_id = {point.metering_point_id: point for point in points}

    assert by_id[CONSUMPTION_ID].direction == "consumption"
    assert by_id[CONSUMPTION_ID].type_of_mp == "E17"
    assert by_id[CONSUMPTION_ID].meter_reading_occurrence == "PT1H"
    assert by_id["571313174000000077"].direction == "consumption"
    assert by_id["571313174000000077"].type_of_mp == "D07"
    assert by_id[EXPORT_CHILD_ID].direction == "export"
    assert by_id[EXPORT_CHILD_ID].type_of_mp == "D06"
    assert by_id[EXPORT_CHILD_ID].meter_reading_occurrence == "PT15M"
    assert by_id[PRODUCTION_ID].direction == "export"
    assert by_id["571313174000000001"].direction == "export"
    assert by_id["571313174000000012"].direction == "consumption"
    assert by_id["571313174000000009"].direction == "export"
    assert by_id["571313174000000011"].direction == "export"
    assert by_id[EXPORT_CHILD_ID].address == "Nørregade 12, 2 tv, 8000 Aarhus C"
    assert by_id[EXPORT_CHILD_ID].parent_metering_point_id == CONSUMPTION_ID
    assert classify_direction("D99") == "consumption"


def test_hourly_document_skips_gaps_missing_quality_and_profiled_series() -> None:
    """PT1H parsing keeps measured values and drops A02, gaps, and A64 duplicates."""
    readings = parse_time_series(_load("timeseries_pt1h.json"))
    consumption = [
        item for item in readings if item.metering_point_id == CONSUMPTION_ID
    ]
    production = [item for item in readings if item.metering_point_id == PRODUCTION_ID]

    assert [item.kwh for item in consumption] == [0.420, 0.380, 1.250, 0.510]
    assert consumption[0].start == datetime(2026, 11, 22, 23, 0, tzinfo=UTC)
    assert consumption[2].start == datetime(2026, 11, 23, 3, 0, tzinfo=UTC)
    assert datetime(2026, 11, 23, 1, 0, tzinfo=UTC) not in {
        item.start for item in consumption
    }
    assert datetime(2026, 11, 23, 2, 0, tzinfo=UTC) not in {
        item.start for item in consumption
    }
    assert all(item.kwh != 9.999 for item in consumption)
    assert all(item.business_type == "A04" for item in consumption)
    assert [item.kwh for item in production] == [0.100, 0.200]
    assert all(item.metering_point_id != "571313174000000099" for item in readings)


def test_quarter_document_accepts_single_objects_and_megawatt_hours() -> None:
    """PT15M parsing accepts object-shaped series and converts MWh to kWh."""
    readings = parse_time_series(_load("timeseries_pt15m.json"))
    export = [item for item in readings if item.metering_point_id == EXPORT_CHILD_ID]
    production = [item for item in readings if item.metering_point_id == PRODUCTION_ID]

    assert len(export) == 7
    assert export[0].start == datetime(2026, 11, 22, 23, 0, tzinfo=UTC)
    assert export[3].start == datetime(2026, 11, 22, 23, 45, tzinfo=UTC)
    assert datetime(2026, 11, 23, 0, 0, tzinfo=UTC) not in {
        item.start for item in export
    }
    assert datetime(2026, 11, 23, 1, 0, tzinfo=UTC) not in {
        item.start for item in export
    }
    assert production[0].kwh == pytest.approx(1.0)
    assert production[0].start == datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def test_comma_decimal_quantity_is_parsed() -> None:
    """A Danish decimal comma in a quantity is accepted."""
    payload = {
        "TimeSeries": [
            {
                "businessType": "A04",
                "measurement_Unit.name": "kWh",
                "mRID": CONSUMPTION_ID,
                "Period": [
                    {
                        "resolution": "PT1H",
                        "timeInterval": {"start": "2026-11-01T00:00:00Z"},
                        "Point": [
                            {
                                "position": "1",
                                "out_Quantity.quantity": "1,5",
                                "out_Quantity.quality": "A04",
                            }
                        ],
                    }
                ],
            }
        ]
    }
    readings = parse_time_series(payload)
    assert readings[0].kwh == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_token_is_cached_until_it_nears_expiry(metering_payload: dict) -> None:
    """A fresh data-access token is reused, and one inside the skew is renewed."""
    fresh = _jwt(time.time() + 3 * 60 * 60)
    soon = _jwt(time.time() + 60)
    url = f"{METERING_POINTS_URL}?includeAll=false"

    with aioresponses() as mocked:
        mocked.get(TOKEN_URL, payload={"result": fresh})
        mocked.get(url, payload=metering_payload, repeat=True)
        client = ElOverblikClient("refresh-token-should-stay-secret")
        try:
            first = await client.async_get_metering_points()
            second = await client.async_get_metering_points()
        finally:
            await client.async_close()
        assert len(first) == len(second) == 8
        assert len(_calls(mocked, "GET", "/api/token")) == 1

    with aioresponses() as mocked:
        mocked.get(TOKEN_URL, payload={"result": soon}, repeat=True)
        mocked.get(url, payload={"result": []}, repeat=True)
        client = ElOverblikClient("refresh-token-should-stay-secret")
        try:
            await client.async_get_metering_points()
            await client.async_get_metering_points()
        finally:
            await client.async_close()
        assert len(_calls(mocked, "GET", "/api/token")) == 2


@pytest.mark.asyncio
async def test_data_401_refreshes_the_access_token_once(metering_payload: dict) -> None:
    """HTTP 401 on a data call exchanges the refresh token once and retries."""
    url = f"{METERING_POINTS_URL}?includeAll=false"
    with aioresponses() as mocked:
        mocked.get(TOKEN_URL, payload={"result": _jwt(time.time() + 7200)})
        mocked.get(url, status=401)
        mocked.get(TOKEN_URL, payload={"result": _jwt(time.time() + 7200)})
        mocked.get(url, payload=metering_payload)
        client = ElOverblikClient("refresh-token-should-stay-secret")
        try:
            points = await client.async_get_metering_points()
        finally:
            await client.async_close()
    assert len(points) == 8
    assert len(_calls(mocked, "GET", "/api/token")) == 2
    assert len(_calls(mocked, "GET", "/meteringpoints")) == 2


@pytest.mark.asyncio
async def test_second_401_and_rejected_refresh_token_raise_auth_error() -> None:
    """A rejected refresh token, or a retry that is still unauthorized, is fatal."""
    url = f"{METERING_POINTS_URL}?includeAll=false"
    with aioresponses() as mocked:
        mocked.get(TOKEN_URL, status=401, payload={"detail": "TokenNotValid"})
        client = ElOverblikClient("refresh-token-should-stay-secret")
        try:
            with pytest.raises(ElOverblikAuthError) as caught:
                await client.async_get_metering_points()
        finally:
            await client.async_close()
    assert "refresh-token-should-stay-secret" not in str(caught.value)

    with aioresponses() as mocked:
        mocked.get(TOKEN_URL, payload={"result": _jwt(time.time() + 7200)})
        mocked.get(url, status=401)
        mocked.get(TOKEN_URL, status=403, payload={"title": "WrongTokenType"})
        client = ElOverblikClient("refresh-token-should-stay-secret")
        try:
            with pytest.raises(ElOverblikAuthError):
                await client.async_get_metering_points()
            assert len(_calls(mocked, "GET", "/api/token")) == 2
            assert len(_calls(mocked, "GET", "/meteringpoints")) == 1
        finally:
            await client.async_close()


@pytest.mark.asyncio
async def test_429_and_503_do_not_hammer_the_api(metering_payload: dict) -> None:
    """A 429 or 503 raises immediately, and the next call does not hit the network."""
    url = f"{METERING_POINTS_URL}?includeAll=false"
    with aioresponses() as mocked:
        mocked.get(TOKEN_URL, payload={"result": _jwt(time.time() + 7200)})
        mocked.get(url, status=429, headers={"Retry-After": "30"})
        client = ElOverblikClient("refresh-token-should-stay-secret")
        try:
            with pytest.raises(ElOverblikRateLimitError) as caught:
                await client.async_get_metering_points()
            assert caught.value.retry_after == pytest.approx(30)
            started = time.monotonic()
            with pytest.raises(ElOverblikRateLimitError):
                await client.async_get_metering_points()
            assert time.monotonic() - started < 1
            assert len(_calls(mocked, "GET", "/meteringpoints")) == 1
        finally:
            await client.async_close()

    with aioresponses() as mocked:
        mocked.get(TOKEN_URL, payload={"result": _jwt(time.time() + 7200)})
        mocked.get(url, status=503)
        client = ElOverblikClient("refresh-token-should-stay-secret")
        try:
            with pytest.raises(ElOverblikRateLimitError) as caught:
                await client.async_get_metering_points()
            assert caught.value.retry_after == pytest.approx(60)
        finally:
            await client.async_close()
    assert metering_payload["result"]


@pytest.mark.asyncio
async def test_time_series_is_batched_with_the_official_request_body() -> None:
    """Requests use meteringPoints.meteringPoint and stay within 10 ids per call."""
    ids = [f"57131317400000{index:04d}" for index in range(11)]
    date_from = datetime(2026, 1, 1, tzinfo=UTC).date()
    date_to = date_from + timedelta(days=39)
    with aioresponses() as mocked:
        mocked.get(TOKEN_URL, payload={"result": _jwt(time.time() + 7200)})
        mocked.post(
            re.compile(
                r"https://api\.eloverblik\.dk/customerapi/api/meterdata/gettimeseries/.*"
            ),
            payload={"result": []},
            repeat=True,
        )
        client = ElOverblikClient("refresh-token-should-stay-secret")
        try:
            await client.async_get_time_series(
                ids, date_from, date_to, aggregation="hour"
            )
        finally:
            await client.async_close()
        posts = _calls(mocked, "POST", "/gettimeseries/")
    assert len(posts) == 4
    sent_ids: list[str] = []
    for url, call in posts:
        body = call.kwargs["json"]
        chunk = body["meteringPoints"]["meteringPoint"]
        assert "meteringPointIds" not in body
        assert 1 <= len(chunk) <= 10
        assert url.endswith("/Hour")
        sent_ids.extend(chunk)
    assert sent_ids.count(ids[0]) == 2


@pytest.mark.asyncio
async def test_api_error_does_not_include_the_refresh_token() -> None:
    """Transport failures become ElOverblikApiError without echoing the token."""
    with aioresponses() as mocked:
        mocked.get(TOKEN_URL, status=500, payload={"detail": "boom"})
        client = ElOverblikClient("refresh-token-should-stay-secret")
        try:
            with pytest.raises(ElOverblikApiError) as caught:
                await client.async_get_metering_points()
        finally:
            await client.async_close()
    assert caught.value.status == 500
    assert "refresh-token-should-stay-secret" not in str(caught.value)
