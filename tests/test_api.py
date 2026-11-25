"""Tests for the API client (no Home Assistant involved)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.skoda_api_ha.api import (
    SkodaApiClient,
    SkodaCommandRejectedError,
    SkodaConnectionError,
    SkodaForbiddenError,
    SkodaRateLimitError,
    SkodaResponseError,
    SkodaVehicleBusyError,
    parse_quota,
)

from .const import API_KEY, QUOTA_HEADERS, VEHICLE_URL, VIN, vehicle_body

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def test_parse_quota_full() -> None:
    """All headers are read; reset is relative seconds."""
    quota = parse_quota({**QUOTA_HEADERS, "Retry-After": "60"}, NOW)
    assert quota.limit == 20
    assert quota.remaining == 11
    assert quota.reset_at == NOW + timedelta(seconds=1800)
    assert quota.retry_after == 60
    assert quota.key_expires_at == datetime(2027, 5, 1, tzinfo=UTC)


def test_parse_quota_http_date_and_invalid() -> None:
    """Retry-After as HTTP date; invalid values become None."""
    quota = parse_quota(
        {
            "Retry-After": "Mon, 23 Nov 2026 10:02:00 GMT",
            "RateLimit-Remaining": "-1",
            "RateLimit-Limit": "x",
            "X-API-Key-Expires-At": "2027-05-01T00:00:00",  # no time zone
        },
        NOW,
    )
    assert quota.retry_after == 120
    assert quota.remaining is None
    assert quota.limit is None
    assert quota.key_expires_at is None
    assert parse_quota({"Retry-After": "soon"}, NOW).retry_after is None
    assert parse_quota({}, NOW).reset_at is None


async def test_get_vehicle(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """One GET returns vehicle, errors and quota."""
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    client = SkodaApiClient(async_get_clientsession(hass), API_KEY)
    response = await client.async_get_vehicle(VIN)
    assert response.vin == VIN
    assert response.name == "FICTION CAR"
    assert response.license_plate == "XX TEST-01"
    assert response.errors == []
    assert response.quota.limit == 20
    assert aioclient_mock.call_count == 1
    assert API_KEY not in repr(client)


async def test_get_vehicle_errors_not_list(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A malformed ``errors`` field is ignored; blank names are unknown."""
    body = vehicle_body(name="  ")
    body["errors"] = "oops"
    aioclient_mock.get(VEHICLE_URL, json=body)
    client = SkodaApiClient(async_get_clientsession(hass), API_KEY)
    response = await client.async_get_vehicle(VIN)
    assert response.errors == []
    assert response.name is None


async def test_get_vehicle_invalid_json(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Invalid JSON is a response error."""
    aioclient_mock.get(VEHICLE_URL, text="not json")
    client = SkodaApiClient(async_get_clientsession(hass), API_KEY)
    with pytest.raises(SkodaResponseError):
        await client.async_get_vehicle(VIN)


async def test_get_vehicle_client_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Aiohttp errors are temporary connection errors."""
    aioclient_mock.get(VEHICLE_URL, exc=aiohttp.ClientError())
    client = SkodaApiClient(async_get_clientsession(hass), API_KEY)
    with pytest.raises(SkodaConnectionError):
        await client.async_get_vehicle(VIN)


async def test_send_command_accepted_and_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """2xx is accepted; 403 and odd problem bodies map to clear errors."""
    client = SkodaApiClient(async_get_clientsession(hass), API_KEY)
    url = f"{VEHICLE_URL}/charging/stop"
    aioclient_mock.post(url, status=202, headers={"RateLimit-Remaining": "9"})
    response = await client.async_send_command(VIN, "POST", "/charging/stop")
    assert response.status == 202
    assert response.quota.remaining == 9

    aioclient_mock.clear_requests()
    aioclient_mock.post(url, status=403)
    with pytest.raises(SkodaForbiddenError):
        await client.async_send_command(VIN, "POST", "/charging/stop")

    for body in ([1, 2], {"type": ""}, {"type": 5}):
        aioclient_mock.clear_requests()
        aioclient_mock.post(url, status=400, json=body)
        with pytest.raises(SkodaCommandRejectedError) as err:
            await client.async_send_command(VIN, "POST", "/charging/stop")
        assert err.value.problem is None
        assert err.value.status == 400

    for method, path in (("GET", "/charging/stop"), ("POST", "charging/stop")):
        with pytest.raises(ValueError, match="Unsupported"):
            await client.async_send_command(VIN, method, path)


BUSY = {
    "type": "https://public.api.connect.skoda-auto.cz/problems/vehicle-not-accepting-requests"
}
LIMITED = {
    "type": "https://public.api.connect.skoda-auto.cz/problems/rate-limit-exceeded"
}


async def test_429_vehicle_busy_is_not_rate_limit(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """429 of the vehicle is its own error; the quota headers are kept."""
    client = SkodaApiClient(async_get_clientsession(hass), API_KEY)
    url = f"{VEHICLE_URL}/charging/start"
    headers = {"RateLimit-Remaining": "7", "Retry-After": "30"}
    for method, register, call in (
        (
            "POST",
            aioclient_mock.post,
            client.async_send_command(VIN, "POST", "/charging/start"),
        ),
        ("GET", aioclient_mock.get, client.async_get_vehicle(VIN)),
    ):
        aioclient_mock.clear_requests()
        register(
            url if method == "POST" else VEHICLE_URL,
            status=429,
            json=BUSY,
            headers=headers,
        )
        with pytest.raises(SkodaVehicleBusyError) as err:
            await call
        assert not isinstance(err.value, SkodaRateLimitError)
        assert err.value.quota.remaining == 7
        assert str(err.value) == "vehicle-not-accepting-requests"


@pytest.mark.parametrize("body", [LIMITED, None])
async def test_429_rate_limit_with_or_without_type(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, body: dict | None
) -> None:
    """Any other 429 (or one without a body) is the quota."""
    client = SkodaApiClient(async_get_clientsession(hass), API_KEY)
    aioclient_mock.get(
        VEHICLE_URL, status=429, json=body, headers={"Retry-After": "30"}
    )
    with pytest.raises(SkodaRateLimitError) as err:
        await client.async_get_vehicle(VIN)
    assert err.value.retry_after == 30
