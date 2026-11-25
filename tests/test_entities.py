"""Entities: snapshot, unknown values, unavailable, parking position."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache,
    snapshot_platform,
)

from custom_components.skoda_api_ha.api import VehicleResponse
from custom_components.skoda_api_ha.const import DOMAIN
from custom_components.skoda_api_ha.entity import (
    enum,
    number,
    section_supported,
    timestamp,
)

from .conftest import setup_integration
from .const import QUOTA_HEADERS, VEHICLE_URL, VIN, full_vehicle

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )
    from syrupy.assertion import SnapshotAssertion

ENTITY_PREFIX = "fiction_car"


def _body(vehicle: dict[str, Any], errors: list | None = None) -> dict[str, Any]:
    return {"vehicle": vehicle, "errors": errors or []}


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
    body: dict[str, Any],
    platforms: list[Platform] | None = None,
) -> None:
    aioclient_mock.get(VEHICLE_URL, json=body, headers=QUOTA_HEADERS)
    with patch(
        "custom_components.skoda_api_ha.PLATFORMS",
        platforms or [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER],
    ):
        await setup_integration(hass, entry)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.freeze_time("2026-11-23T10:00:00+00:00")
@pytest.mark.parametrize(
    "platform", [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]
)
async def test_entities_snapshot(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    snapshot: SnapshotAssertion,
    platform: Platform,
) -> None:
    """All entities of a vehicle with every section (names, units, states)."""
    await _setup(hass, aioclient_mock, config_entry, _body(full_vehicle()), [platform])
    await snapshot_platform(hass, er.async_get(hass), snapshot, config_entry.entry_id)


async def test_unique_id_and_disabled_defaults(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """unique_id is <VIN>_<key>; private and diagnostic entities start disabled."""
    await _setup(hass, aioclient_mock, config_entry, _body(full_vehicle()))
    entries = er.async_entries_for_config_entry(entity_registry, config_entry.entry_id)
    by_key = {entry.unique_id.split("_", 1)[1]: entry for entry in entries}
    assert all(entry.unique_id.startswith("TMBTEST0000000001_") for entry in entries)
    disabled = {key for key, entry in by_key.items() if entry.disabled_by}
    assert disabled == {
        "parking_address",
        "status_captured_at",
        "odometer_captured_at",
        "parking_position_captured_at",
        "climate_captured_at",
        "charging_captured_at",
        "charging_profiles_captured_at",
        # Diagnostics of the integration, except the quota and the key expiry.
        "quota_limit",
        "quota_reserve",
        "regular_updates_left",
        "update_interval",
        "effective_interval",
        "next_update",
        "last_update",
        "api_errors",
        "api_key_change",
        "last_command_charging",
        "last_command_climate",
        "last_command_charge_mode",
        "last_command_charge_limit",
        "last_command_profile",
    }
    assert by_key["battery_level"].entity_id == f"sensor.{ENTITY_PREFIX}_battery"
    assert hass.states.get(f"sensor.{ENTITY_PREFIX}_battery").state == "64"
    # The API reports meters; Home Assistant shows kilometers.
    assert hass.states.get(f"sensor.{ENTITY_PREFIX}_range").state == "287.0"


async def test_missing_fields_are_unknown(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Fields the API leaves out, UNKNOWN and new enum values are unknown."""
    vehicle = full_vehicle()
    del vehicle["charging"]["status"]["battery"]
    vehicle["charging"]["status"]["state"] = "SOMETHING_NEW"
    vehicle["status"]["overall"]["locked"] = "UNKNOWN"
    del vehicle["airConditioning"]["airConditioningAtUnlock"]
    await _setup(hass, aioclient_mock, config_entry, _body(vehicle))

    for entity_id in (
        f"sensor.{ENTITY_PREFIX}_battery",
        f"sensor.{ENTITY_PREFIX}_range",
        f"sensor.{ENTITY_PREFIX}_charging_state",
        f"binary_sensor.{ENTITY_PREFIX}_vehicle_lock",
        f"binary_sensor.{ENTITY_PREFIX}_climate_at_unlock",
        # UNSUPPORTED sunroof.
        f"binary_sensor.{ENTITY_PREFIX}_sunroof",
    ):
        assert hass.states.get(entity_id).state == STATE_UNKNOWN, entity_id
    # Other fields of the same sections still have values.
    assert hass.states.get(f"sensor.{ENTITY_PREFIX}_charging_power").state == "7.2"


async def test_only_reported_sections(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Unsupported sections give no entities; an outage still creates them."""
    vehicle = full_vehicle()
    for section in ("charging", "airConditioning", "chargingProfiles"):
        del vehicle[section]
    errors = [{"type": "CHARGING_UNAVAILABLE", "description": "down"}]
    await _setup(hass, aioclient_mock, config_entry, _body(vehicle, errors))

    assert hass.states.get(f"sensor.{ENTITY_PREFIX}_climate_state") is None
    assert hass.states.get(f"sensor.{ENTITY_PREFIX}_charging_profile") is None
    assert hass.states.get(f"sensor.{ENTITY_PREFIX}_battery").state == STATE_UNKNOWN
    assert hass.states.get(f"sensor.{ENTITY_PREFIX}_odometer").state == "12345"


async def test_unavailable_after_failed_update(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A failed GET makes the entities unavailable; the next success restores."""
    await _setup(hass, aioclient_mock, config_entry, _body(full_vehicle()))
    coordinator = config_entry.runtime_data.coordinator
    entity_id = f"sensor.{ENTITY_PREFIX}_battery"

    aioclient_mock.clear_requests()
    aioclient_mock.get(VEHICLE_URL, status=500)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    aioclient_mock.clear_requests()
    aioclient_mock.get(VEHICLE_URL, json=_body(full_vehicle()), headers=QUOTA_HEADERS)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "64"


async def test_tracker_keeps_last_parking_position(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """While driving there is no position: the tracker keeps the last one."""
    await _setup(hass, aioclient_mock, config_entry, _body(full_vehicle()))
    coordinator = config_entry.runtime_data.coordinator
    entity_id = f"device_tracker.{ENTITY_PREFIX}_parking_position"
    assert hass.states.get(entity_id).attributes["latitude"] == 46.0

    moving = full_vehicle()
    moving["parkingPosition"] = {"state": "IN_MOTION"}
    coordinator.async_set_updated_data(VehicleResponse(vehicle=moving))
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.attributes["latitude"] == 46.0
    assert state.attributes["longitude"] == 14.5
    assert hass.states.get(f"binary_sensor.{ENTITY_PREFIX}_in_motion").state == "on"

    parked = full_vehicle()
    parked["parkingPosition"]["gpsCoordinates"] = {"latitude": 45.5, "longitude": 13.7}
    coordinator.async_set_updated_data(VehicleResponse(vehicle=parked))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["latitude"] == 45.5


async def test_tracker_restores_position_after_restart(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Restarted while driving: the position from before the restart stays."""
    entity_id = f"device_tracker.{ENTITY_PREFIX}_parking_position"
    mock_restore_cache(
        hass,
        [State(entity_id, "not_home", {"latitude": 45.1, "longitude": 13.2})],
    )
    moving = full_vehicle()
    moving["parkingPosition"] = {"state": "IN_MOTION"}
    await _setup(hass, aioclient_mock, config_entry, _body(moving))
    state = hass.states.get(entity_id)
    assert state.attributes["latitude"] == 45.1
    assert state.attributes["longitude"] == 13.2


async def test_tracker_without_any_position(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """No position yet and nothing to restore: unknown."""
    moving = full_vehicle()
    moving["parkingPosition"] = {"state": "PARKED"}
    await _setup(hass, aioclient_mock, config_entry, _body(moving))
    entity_id = f"device_tracker.{ENTITY_PREFIX}_parking_position"
    assert hass.states.get(entity_id).state == STATE_UNKNOWN


def test_value_helpers() -> None:
    """Helpers reject wrong types and naive date-times."""
    vehicle = {"a": {"b": True, "c": "x", "d": "2026-11-23T10:00:00"}}
    assert number(vehicle, "a", "b") is None
    assert number(vehicle, "a", "c", "d") is None
    assert enum(vehicle, ("a", "b"), ["x"]) is None
    assert timestamp(vehicle, "a", "d") is None
    assert timestamp(vehicle, "a", "c") is None
    assert timestamp(vehicle, "a", "b") is None
    assert section_supported(None, "charging") is False
    response = VehicleResponse(vehicle={}, errors=["not a dict"])
    assert section_supported(response, "charging") is False


async def test_entity_names_follow_server_language(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Entity names come from the server language (here German)."""
    hass.config.language = "de"
    await _setup(hass, aioclient_mock, config_entry, _body(full_vehicle()))
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{VIN}_odometer"
    )
    # A new entity also gets its entity_id in that language.
    assert entity_id == "sensor.fiction_car_kilometerstand"
    state = hass.states.get(entity_id)
    assert state.attributes["friendly_name"] == "FICTION CAR Kilometerstand"


async def test_charging_buttons_disabled_by_default(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Start/Stop charging duplicate the Charging switch, so they start disabled."""
    await _setup(
        hass, aioclient_mock, config_entry, _body(full_vehicle()), [Platform.BUTTON]
    )
    entries = er.async_entries_for_config_entry(entity_registry, config_entry.entry_id)
    disabled = {
        entry.unique_id.split("_", 1)[1]: bool(entry.disabled_by) for entry in entries
    }
    assert disabled == {
        "refresh": False,
        "clear_pending": False,
        "start_charging": True,
        "stop_charging": True,
    }
