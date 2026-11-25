"""Vehicle picture: URL from the regular GET, no extra request to the API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.const import Platform

from custom_components.skoda_api_ha.image import render_supported, render_url

from .conftest import setup_integration
from .const import QUOTA_HEADERS, RENDER_URL, VEHICLE_URL, full_vehicle

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
IMAGE = "image.fiction_car_vehicle_image"


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
    body: dict[str, Any],
) -> None:
    aioclient_mock.get(VEHICLE_URL, json=body, headers=QUOTA_HEADERS)
    with patch("custom_components.skoda_api_ha.PLATFORMS", [Platform.IMAGE]):
        await setup_integration(hass, entry)


async def test_vehicle_image(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_client: ClientSessionGenerator,
) -> None:
    """The picture is loaded from renderUrl; a new URL updates it."""
    freezer.move_to(NOW)
    await _setup(hass, aioclient_mock, config_entry, {"vehicle": full_vehicle()})
    state = hass.states.get(IMAGE)
    assert state.state == NOW.isoformat()
    assert state.attributes["friendly_name"] == "FICTION CAR Vehicle image"

    # Loaded from the render URL (not the Škoda API) when it is shown.
    aioclient_mock.clear_requests()
    with patch(
        "custom_components.skoda_api_ha.image.SkodaVehicleImage._fetch_url",
        autospec=True,
    ) as fetch:
        fetch.return_value.status_code = HTTPStatus.OK
        fetch.return_value.content = b"png"
        fetch.return_value.headers = {"content-type": "image/png"}
        client = await hass_client()
        response = await client.get(state.attributes["entity_picture"])
        assert response.status == HTTPStatus.OK
        assert await response.read() == b"png"
        assert fetch.call_args.args[1] == RENDER_URL
    assert aioclient_mock.mock_calls == []

    # Without a URL the last one stays; a new one replaces it.
    coordinator = config_entry.runtime_data.coordinator
    vehicle = full_vehicle()
    del vehicle["renderUrl"]
    freezer.tick(timedelta(minutes=5))
    coordinator.async_set_updated_data(
        type(coordinator.data)(vehicle=vehicle, quota=coordinator.data.quota)
    )
    await hass.async_block_till_done()
    assert hass.states.get(IMAGE).state == NOW.isoformat()
    vehicle["renderUrl"] = "https://render.example.com/new.png"
    coordinator.async_set_updated_data(
        type(coordinator.data)(vehicle=vehicle, quota=coordinator.data.quota)
    )
    await hass.async_block_till_done()
    entity = hass.data["entity_components"]["image"].get_entity(IMAGE)
    assert entity.image_url == "https://render.example.com/new.png"
    assert hass.states.get(IMAGE).state == (NOW + timedelta(minutes=5)).isoformat()


@pytest.mark.parametrize(
    ("body", "created"),
    [
        ({"vehicle": {"vin": "X"}}, False),
        (
            {"vehicle": {"vin": "X"}, "errors": [{"type": "RENDER_UNAVAILABLE"}]},
            True,
        ),
    ],
)
async def test_vehicle_image_only_when_supported(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    body: dict[str, Any],
    created: bool,  # noqa: FBT001
) -> None:
    """Created with a URL or a RENDER_UNAVAILABLE error, else not."""
    vehicle = full_vehicle()
    del vehicle["renderUrl"]
    await _setup(
        hass,
        aioclient_mock,
        config_entry,
        {"vehicle": vehicle, "errors": body.get("errors", [])},
    )
    state = hass.states.get(IMAGE)
    assert (state is not None) is created
    if created:
        assert state.state == "unknown"


def test_render_helpers() -> None:
    """Only https URLs; no data, no picture."""
    assert render_url({"renderUrl": "http://x"}) is None
    assert render_url({"renderUrl": 5}) is None
    assert render_url(None) is None
    assert render_supported(None) is False
