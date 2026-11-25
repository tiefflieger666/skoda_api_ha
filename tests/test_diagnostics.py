"""Diagnostics download and the diagnostic sensors of the integration."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from homeassistant.const import STATE_UNKNOWN, Platform
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.skoda_api_ha import commands as cmd
from custom_components.skoda_api_ha.const import (
    CONF_API_KEY_CANDIDATE,
    CONF_CHARGER_POWER_ENTITY,
    CONF_POLLS_PER_HOUR,
)

from .const import API_KEY, NEW_API_KEY, QUOTA_HEADERS, VEHICLE_URL, VIN, full_vehicle

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
CAR = "fiction_car"
PRIVATE = (
    API_KEY,
    NEW_API_KEY,
    VIN,
    "XX TEST-01",
    "Fiction Street",
    "46.0",
    "14.5",
    "sensor.wallbox_power",
)


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
    **changes: object,
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, **changes.get("data", {})},
        options={**entry.options, **changes.get("options", {})},
    )
    vehicle = full_vehicle()
    vehicle["renderUrl"] = f"https://example.invalid/render/{VIN}.png"
    aioclient_mock.get(
        VEHICLE_URL,
        json={
            "vehicle": vehicle,
            "errors": [{"type": "AUXILIARY_HEATING_UNSUPPORTED"}, "bad"],
        },
        headers=QUOTA_HEADERS,
    )
    with patch("custom_components.skoda_api_ha.PLATFORMS", [Platform.SENSOR]):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


# Frozen: a real timestamp such as 10:21:46.0… would contain "46.0".
@pytest.mark.freeze_time("2026-11-23T10:00:00+00:00")
async def test_diagnostics_without_private_data(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    """Key, candidate, VIN, plate, GPS, address and render URL are redacted."""
    await _setup(
        hass,
        aioclient_mock,
        config_entry,
        data={CONF_API_KEY_CANDIDATE: NEW_API_KEY},
        options={CONF_CHARGER_POWER_ENTITY: "sensor.wallbox_power"},
    )
    result = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)
    text = json.dumps(result)
    for value in PRIVATE:
        assert value not in text, value
    assert result["entry"]["data"]["api_key"] == "**REDACTED**"
    assert result["entry"]["options"][CONF_CHARGER_POWER_ENTITY] == "**SET**"
    assert result["vehicle"]["parkingPosition"]["gpsCoordinates"] == "**REDACTED**"
    assert result["polling"]["polls_per_hour"] == 12
    assert result["quota"]["remaining"] == 11
    assert result["vehicle"]["charging"]["status"]["state"] == "CHARGING"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_diagnostic_sensors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Quota, errors and key change; available when the API fails."""
    freezer.move_to(NOW)
    await _setup(hass, aioclient_mock, config_entry)
    assert hass.states.get(f"sensor.{CAR}_api_key_change").state == "none"
    # A new key from reconfigure waits for the next regular update.
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_API_KEY_CANDIDATE: NEW_API_KEY}
    )
    await hass.async_block_till_done()
    errors = hass.states.get(f"sensor.{CAR}_api_errors")
    assert errors.state == "2"
    assert errors.attributes["types"] == ["AUXILIARY_HEATING_UNSUPPORTED"]
    assert hass.states.get(f"sensor.{CAR}_api_key_change").state == "pending"
    assert hass.states.get(f"sensor.{CAR}_last_charging_command").state == STATE_UNKNOWN
    assert (
        hass.states.get(f"sensor.{CAR}_last_charging_command").attributes["command"]
        is None
    )

    coordinator = config_entry.runtime_data.coordinator
    aioclient_mock.clear_requests()
    aioclient_mock.get(VEHICLE_URL, status=500)
    freezer.tick(timedelta(minutes=20))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(f"sensor.{CAR}_battery").state == "unavailable"
    assert hass.states.get(f"sensor.{CAR}_quota_remaining").state != "unavailable"
    assert hass.states.get(f"sensor.{CAR}_last_update").state == NOW.isoformat()


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_last_command_and_no_regular_polling(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Last command result; no next update or interval with 0 per hour."""
    freezer.move_to(NOW)
    await _setup(hass, aioclient_mock, config_entry, options={CONF_POLLS_PER_HOUR: 0})
    assert hass.states.get(f"sensor.{CAR}_next_update").state == STATE_UNKNOWN
    assert hass.states.get(f"sensor.{CAR}_update_interval").state == STATE_UNKNOWN

    coordinator = config_entry.runtime_data.coordinator
    aioclient_mock.post(
        f"{VEHICLE_URL}/charging/stop",
        status=202,
        headers={"RateLimit-Remaining": "10", "RateLimit-Reset": "1700"},
    )
    freezer.tick(timedelta(seconds=30))
    await coordinator.async_send_command(cmd.charging_stop)
    await hass.async_block_till_done()
    state = hass.states.get(f"sensor.{CAR}_last_charging_command")
    assert state.state == "accepted"
    assert state.attributes["command"] == "charging_stop"
    assert state.attributes["at"] == (NOW + timedelta(seconds=30)).isoformat()
