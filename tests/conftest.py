"""Fixtures for the Škoda API tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.const import CONF_API_KEY
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension

from custom_components.skoda_api_ha.const import (
    CONF_CONFIRM_GET,
    CONF_DEVICE_NAME_SOURCE,
    CONF_POLLS_PER_HOUR,
    CONF_VIN,
    DOMAIN,
    NAME_SOURCE_VEHICLE_NAME,
)

from .const import API_KEY, QUOTA_HEADERS, VEHICLE_URL, VIN, vehicle_body

if TYPE_CHECKING:
    from collections.abc import Generator

    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )
    from syrupy.assertion import SnapshotAssertion


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components in every test."""


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Snapshots in the Home Assistant format (stable ids and times)."""
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Enable the entities that are disabled by default (as in HA core)."""
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        return_value=True,
        new_callable=PropertyMock,
    ):
        yield


@pytest.fixture
def mock_setup_entry() -> Generator[None]:
    """Skip the real setup after a config flow creates an entry."""
    with patch(
        "custom_components.skoda_api_ha.async_setup_entry", return_value=True
    ) as mock:
        yield mock


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a config entry as the user step creates it."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="FICTION CAR",
        unique_id=VIN,
        data={CONF_API_KEY: API_KEY, CONF_VIN: VIN},
        options={
            CONF_DEVICE_NAME_SOURCE: NAME_SOURCE_VEHICLE_NAME,
            CONF_POLLS_PER_HOUR: 12,
            CONF_CONFIRM_GET: False,
        },
    )


@pytest.fixture
def mock_vehicle_ok(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """GET returns 200 with a vehicle."""
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    return aioclient_mock


async def setup_integration(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add the entry and set it up."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
