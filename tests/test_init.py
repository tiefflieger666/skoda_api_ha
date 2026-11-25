"""Setup, unload and coordinator error handling."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState

from custom_components.skoda_api_ha.const import DOMAIN

from .conftest import setup_integration
from .const import QUOTA_HEADERS, VEHICLE_URL, VIN, vehicle_body

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )


async def test_setup_and_unload(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Setup makes one GET and creates one device per VIN."""
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert aioclient_mock.call_count == 1

    coordinator = config_entry.runtime_data.coordinator
    assert coordinator.data.vin == VIN
    assert coordinator.data.quota.remaining == 11
    # 11 left, reset in 30 min, reserve 8: 3 regular GETs fit before the
    # reset, so the next one waits until reset - 3 * 300 s - 10 s.
    assert coordinator.plan.interval == 300
    assert coordinator.plan.available_gets == 3
    assert 885 <= coordinator.update_interval.total_seconds() <= 890

    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, VIN), config_entry.entry_id
    )
    assert device is not None
    assert device.name == "FICTION CAR"
    assert device.manufacturer == "Škoda"

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_not_ready(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A failed first GET means ConfigEntryNotReady (retry later)."""
    aioclient_mock.get(VEHICLE_URL, status=503)
    await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_rate_limited(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """429 on the first GET also means retry later."""
    aioclient_mock.get(VEHICLE_URL, status=429, headers={"Retry-After": "600"})
    await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_auth_failed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """401 means ConfigEntryAuthFailed and starts a reauth flow."""
    aioclient_mock.get(VEHICLE_URL, status=401)
    await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH


async def test_update_rate_limited_uses_retry_after(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """429 during a regular update fails the update and honours Retry-After."""
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    coordinator = config_entry.runtime_data.coordinator

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        VEHICLE_URL,
        status=429,
        headers={"Retry-After": "900", "RateLimit-Reset": "600"},
    )
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    # Retry-After plus a 5 s guard (later than the reset).
    # The entities listen, so the next refresh is already scheduled with it.
    timer = coordinator._unsub_refresh.__self__
    delay = timer.when() - hass.loop.time()
    assert 903 <= delay <= 907
