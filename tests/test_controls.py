"""Control entities: one request per action, pending attribute, command rules."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.const import STATE_UNKNOWN, Platform
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache_with_extra_data,
    snapshot_platform,
)

from custom_components.skoda_api_ha import commands as cmd
from custom_components.skoda_api_ha.climate import TemperatureMemory
from custom_components.skoda_api_ha.entity import (
    PENDING_ICON,
    operation_supported,
    pending_value,
)

from .conftest import setup_integration
from .const import QUOTA_HEADERS, VEHICLE_URL, full_vehicle

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )
    from syrupy.assertion import SnapshotAssertion


# The Start/Stop charging buttons are disabled by default.
pytestmark = pytest.mark.usefixtures("entity_registry_enabled_by_default")

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
COMMAND_HEADERS = {"RateLimit-Remaining": "10", "RateLimit-Reset": "1700"}
PROFILE_URL = f"{VEHICLE_URL}/charging-profiles/4"
CONTROL_PLATFORMS = [
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.TIME,
]
CAR = "fiction_car"
CLIMATE = f"climate.{CAR}_climate"


@dataclass
class Env:
    """Fixtures one control test needs."""

    hass: HomeAssistant
    aioclient_mock: AiohttpClientMocker
    entry: MockConfigEntry
    freezer: FrozenDateTimeFactory


@pytest.fixture
def env(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> Env:
    """Bundle the fixtures."""
    return Env(hass, aioclient_mock, config_entry, freezer)


async def _setup(
    env: Env,
    *,
    vehicle: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    platforms: list[Platform] | None = None,
) -> None:
    hass, aioclient_mock, entry, freezer = (
        env.hass,
        env.aioclient_mock,
        env.entry,
        env.freezer,
    )
    freezer.move_to(NOW)
    aioclient_mock.get(
        VEHICLE_URL,
        json={"vehicle": vehicle or full_vehicle()},
        headers=headers or QUOTA_HEADERS,
    )
    with patch(
        "custom_components.skoda_api_ha.PLATFORMS", platforms or CONTROL_PLATFORMS
    ):
        await setup_integration(hass, entry)
    aioclient_mock.clear_requests()
    freezer.tick(timedelta(seconds=30))


def _plugged(state: str = "READY_FOR_CHARGING") -> dict[str, Any]:
    """Vehicle with the cable plugged in and the given charging state."""
    vehicle = full_vehicle()
    vehicle["charging"]["status"]["state"] = state
    return vehicle


def _sent(aioclient_mock: AiohttpClientMocker) -> list[tuple[str, str, Any]]:
    return [
        (method, str(url), body) for method, url, body, _ in aioclient_mock.mock_calls
    ]


async def _call(hass: HomeAssistant, domain: str, service: str, **data: Any) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)
    await hass.async_block_till_done()


@pytest.mark.parametrize("platform", CONTROL_PLATFORMS)
async def test_controls_snapshot(
    env: Env,
    snapshot: SnapshotAssertion,
    platform: Platform,
) -> None:
    """All control entities of a vehicle with every operation."""
    hass = env.hass
    config_entry = env.entry
    await _setup(env, platforms=[platform])
    await snapshot_platform(hass, er.async_get(hass), snapshot, config_entry.entry_id)


async def test_start_charging_button(
    env: Env,
) -> None:
    """One POST; the state stays from the GET, the command shows as pending."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    await _setup(env, vehicle=_plugged())
    aioclient_mock.post(
        f"{VEHICLE_URL}/charging/start", status=202, headers=COMMAND_HEADERS
    )
    entity_id = f"button.{CAR}_start_charging"
    assert hass.states.get(entity_id).attributes["pending"] is None
    await _call(hass, "button", "press", entity_id=entity_id)
    assert _sent(aioclient_mock) == [("POST", f"{VEHICLE_URL}/charging/start", None)]
    assert hass.states.get(entity_id).attributes["pending"] == "start"
    assert (
        hass.states.get(f"button.{CAR}_stop_charging").attributes["pending"] == "start"
    )


async def test_command_error_is_raised(
    env: Env,
) -> None:
    """A refused command raises the translated error and sends nothing."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    await _setup(env)
    aioclient_mock.post(
        f"{VEHICLE_URL}/charging/stop", status=202, headers=COMMAND_HEADERS
    )
    await _call(hass, "button", "press", entity_id=f"button.{CAR}_stop_charging")
    # The same command again within 25 s: debounced, nothing sent.
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "button", "press", entity_id=f"button.{CAR}_stop_charging")
    assert err.value.translation_key == "command_debounced"
    assert len(aioclient_mock.mock_calls) == 1


async def test_refresh_button(
    env: Env,
) -> None:
    """Refresh = one GET from the reserve; stays usable after a failure."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    freezer = env.freezer
    await _setup(env)
    entity_id = f"button.{CAR}_refresh"
    aioclient_mock.get(VEHICLE_URL, status=500)
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "button", "press", entity_id=entity_id)
    assert err.value.translation_key == "command_refresh_failed"
    assert hass.states.get(entity_id).state != "unavailable"
    assert hass.states.get(CLIMATE).state == "unavailable"

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        VEHICLE_URL, json={"vehicle": full_vehicle()}, headers=QUOTA_HEADERS
    )
    freezer.tick(timedelta(seconds=10))
    await _call(hass, "button", "press", entity_id=entity_id)
    assert len(aioclient_mock.mock_calls) == 1
    assert hass.states.get(CLIMATE).state == "off"


async def test_refresh_button_keeps_golden_reserve(
    env: Env,
) -> None:
    """No extra GET when it would use the last request in reserve."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    # 6 left, reset in 30 min: the regular GETs need all of them.
    await _setup(
        env,
        headers={"RateLimit-Remaining": "6", "RateLimit-Reset": "1800"},
    )
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "button", "press", entity_id=f"button.{CAR}_refresh")
    assert err.value.translation_key == "command_refresh_reserve"
    assert aioclient_mock.mock_calls == []


async def test_climate_temperature_while_off(
    env: Env,
) -> None:
    """Off: after the confirmation the temperature starts the climate."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    config_entry = env.entry
    await _setup(env)
    state = hass.states.get(CLIMATE)
    assert state.state == "off"
    assert state.attributes["temperature"] == 21.5
    assert state.attributes["temperature_sync"] == "in_sync"

    # First call: the warning that this starts the climate; nothing sent.
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "climate", "set_temperature", entity_id=CLIMATE, temperature=23.5
        )
    assert err.value.translation_key == "climate_temperature_confirm_start"
    assert aioclient_mock.mock_calls == []
    assert hass.states.get(CLIMATE).attributes["temperature"] == 21.5

    aioclient_mock.post(
        f"{VEHICLE_URL}/air-conditioning/start", status=202, headers=COMMAND_HEADERS
    )
    await _call(hass, "climate", "set_temperature", entity_id=CLIMATE, temperature=23.5)
    method, url, body = _sent(aioclient_mock)[0]
    assert (method, url) == ("POST", f"{VEHICLE_URL}/air-conditioning/start")
    assert body == {
        "targetTemperature": {"value": 23.5, "unit": "CELSIUS"},
        "airConditioningWithoutExternalPower": True,
    }
    state = hass.states.get(CLIMATE)
    # Requested mode at once; the action stays off until a GET confirms.
    assert state.state == "heat_cool"
    assert state.attributes["hvac_action"] == "off"
    assert state.attributes["pending"] == 23.5
    assert state.attributes["temperature"] == 23.5
    assert state.attributes["temperature_sync"] == "sent_awaiting_vehicle"

    assert state.attributes["icon"] == PENDING_ICON

    # A GET shows the climate running: START confirmed, hourglass gone.
    running = full_vehicle()
    running["airConditioning"].update(
        state="HEATING",
        targetTemperature={"value": 23.5, "unit": "CELSIUS"},
        carCapturedTimestamp="2026-11-23T10:02:00Z",
    )
    await _refresh(env, running)
    state = hass.states.get(CLIMATE)
    assert (state.state, state.attributes["hvac_action"]) == ("heat_cool", "heating")
    assert state.attributes["pending"] is None
    assert "icon" not in state.attributes
    assert state.attributes["temperature_sync"] == "in_sync"

    # After STOP the Elroq reports its stored value again: the HA value stays.
    stopped = full_vehicle()
    stopped["airConditioning"]["carCapturedTimestamp"] = "2026-11-23T10:10:00Z"
    coordinator = config_entry.runtime_data.coordinator
    coordinator.async_set_updated_data(
        type(coordinator.data)(vehicle=stopped, quota=coordinator.data.quota)
    )
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).attributes["temperature"] == 23.5


async def _refresh(env: Env, vehicle: dict[str, Any]) -> None:
    """One regular GET with ``vehicle`` (confirms pending commands)."""
    env.aioclient_mock.clear_requests()
    env.aioclient_mock.get(
        VEHICLE_URL, json={"vehicle": vehicle}, headers=QUOTA_HEADERS
    )
    env.freezer.tick(timedelta(minutes=5))
    await env.entry.runtime_data.coordinator.async_refresh()
    await env.hass.async_block_till_done()
    env.aioclient_mock.clear_requests()


def _running(
    temperature: float = 21.5, captured: str = "2026-11-23T09:58:00Z"
) -> dict[str, Any]:
    vehicle = full_vehicle()
    vehicle["airConditioning"].update(
        state="HEATING",
        targetTemperature={"value": temperature, "unit": "CELSIUS"},
        carCapturedTimestamp=captured,
    )
    return vehicle


async def test_climate_temperature_while_running(
    env: Env,
) -> None:
    """Running: a new temperature is sent at once after a confirmation."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    await _setup(env, vehicle=_running())
    aioclient_mock.post(
        f"{VEHICLE_URL}/air-conditioning/start", status=202, headers=COMMAND_HEADERS
    )
    # The same temperature: nothing to send.
    await _call(hass, "climate", "set_temperature", entity_id=CLIMATE, temperature=21.5)
    assert aioclient_mock.mock_calls == []

    # First call: only the confirmation with the quota left (nothing sent).
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "climate", "set_temperature", entity_id=CLIMATE, temperature=23
        )
    assert err.value.translation_key == "climate_temperature_confirm"
    placeholders = err.value.translation_placeholders
    assert placeholders["temperature"] == "23"
    assert placeholders["remaining"] == "11"
    assert placeholders["seconds"] == "60"
    assert set(placeholders) == {
        "temperature",
        "seconds",
        "remaining",
        "spare",
        "reset",
    }
    assert aioclient_mock.mock_calls == []
    assert hass.states.get(CLIMATE).attributes["temperature"] == 21.5

    # Another value starts a new confirmation.
    with pytest.raises(ServiceValidationError):
        await _call(
            hass, "climate", "set_temperature", entity_id=CLIMATE, temperature=24
        )
    # The same value again within 60 s: one START with the new temperature.
    env.freezer.tick(timedelta(seconds=20))
    await _call(hass, "climate", "set_temperature", entity_id=CLIMATE, temperature=24)
    assert _sent(aioclient_mock) == [
        (
            "POST",
            f"{VEHICLE_URL}/air-conditioning/start",
            {
                "targetTemperature": {"value": 24.0, "unit": "CELSIUS"},
                "airConditioningWithoutExternalPower": True,
            },
        )
    ]
    state = hass.states.get(CLIMATE)
    assert state.attributes["temperature"] == 24
    assert state.attributes["pending"] == 24
    assert state.attributes["icon"] == PENDING_ICON
    assert state.attributes["temperature_sync"] == "sent_awaiting_vehicle"

    # Guard: no new climate command until a GET confirms the pending one.
    env.freezer.tick(timedelta(seconds=30))
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "climate", "set_temperature", entity_id=CLIMATE, temperature=25
        )
    assert err.value.translation_key == "command_pending"
    assert len(aioclient_mock.mock_calls) == 1

    # The next GET shows the new temperature: confirmed.
    await _refresh(env, _running(24, "2026-11-23T10:05:00Z"))
    coordinator = env.entry.runtime_data.coordinator
    result = coordinator.commands.results[cmd.CommandGroup.CLIMATE]
    assert (result.key, result.result) == ("climate_temperature", "confirmed")
    assert hass.states.get(CLIMATE).attributes["pending"] is None


async def test_climate_temperature_without_quota(
    env: Env,
) -> None:
    """No quota for a command: refused at once with the hint to the app."""
    hass = env.hass
    await _setup(
        env,
        vehicle=_running(),
        headers={"RateLimit-Remaining": "6", "RateLimit-Reset": "1800"},
    )
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "climate", "set_temperature", entity_id=CLIMATE, temperature=23
        )
    assert err.value.translation_key == "command_quota_reserve"
    assert err.value.translation_placeholders["remaining"] == "6"
    assert env.aioclient_mock.mock_calls == []
    # Not kept either: the value was not sent.
    assert hass.states.get(CLIMATE).attributes["temperature"] == 21.5


async def test_climate_temperature_with_off_mode_while_running(
    env: Env,
) -> None:
    """Temperature together with off while running: kept, then STOP."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    await _setup(env, vehicle=_running())
    aioclient_mock.post(
        f"{VEHICLE_URL}/air-conditioning/stop", status=202, headers=COMMAND_HEADERS
    )
    await _call(
        hass,
        "climate",
        "set_temperature",
        entity_id=CLIMATE,
        temperature=20,
        hvac_mode="off",
    )
    assert _sent(aioclient_mock) == [
        ("POST", f"{VEHICLE_URL}/air-conditioning/stop", None)
    ]
    assert hass.states.get(CLIMATE).attributes["temperature"] == 20


async def test_charging_buttons_pending_and_guard(
    env: Env,
) -> None:
    """Hourglass on the pressed button; STOP may follow START, not the reverse."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    await _setup(env, vehicle=_plugged())
    aioclient_mock.post(
        f"{VEHICLE_URL}/charging/start", status=202, headers=COMMAND_HEADERS
    )
    aioclient_mock.post(
        f"{VEHICLE_URL}/charging/stop", status=202, headers=COMMAND_HEADERS
    )
    start = f"button.{CAR}_start_charging"
    stop = f"button.{CAR}_stop_charging"
    assert "icon" not in hass.states.get(start).attributes
    await _call(hass, "button", "press", entity_id=start)
    assert hass.states.get(start).attributes["icon"] == PENDING_ICON
    assert "icon" not in hass.states.get(stop).attributes

    # A pending START may be stopped at once.
    env.freezer.tick(timedelta(seconds=10))
    await _call(hass, "button", "press", entity_id=stop)
    assert hass.states.get(stop).attributes["icon"] == PENDING_ICON
    assert "icon" not in hass.states.get(start).attributes

    # START while STOP waits: refused until a GET confirms the STOP.
    env.freezer.tick(timedelta(seconds=30))
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "button", "press", entity_id=start)
    assert err.value.translation_key == "command_pending"
    assert len(aioclient_mock.mock_calls) == 2


async def test_climate_modes_and_invalid_temperature(
    env: Env,
) -> None:
    """Off sends STOP; a temperature off the 0.5 grid is refused."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    await _setup(env)
    aioclient_mock.post(
        f"{VEHICLE_URL}/air-conditioning/stop", status=202, headers=COMMAND_HEADERS
    )
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "climate", "set_temperature", entity_id=CLIMATE, temperature=22.3
        )
    assert err.value.translation_key == "command_invalid_value"
    await _call(
        hass,
        "climate",
        "set_temperature",
        entity_id=CLIMATE,
        temperature=22,
        hvac_mode="off",
    )
    assert hass.states.get(CLIMATE).attributes["temperature"] == 22
    assert _sent(aioclient_mock) == [
        ("POST", f"{VEHICLE_URL}/air-conditioning/stop", None)
    ]
    assert hass.states.get(CLIMATE).attributes["pending"] == "off"
    # turn_off is the same STOP; within 25 s it is debounced (nothing sent).
    with pytest.raises(ServiceValidationError):
        await _call(hass, "climate", "turn_off", entity_id=CLIMATE)


async def test_climate_start_without_any_temperature(
    env: Env,
) -> None:
    """Without an HA or vehicle value the field is left out (never guessed)."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    vehicle = full_vehicle()
    del vehicle["airConditioning"]["targetTemperature"]
    vehicle["airConditioning"]["state"] = "UNKNOWN"
    await _setup(env, vehicle=vehicle)
    state = hass.states.get(CLIMATE)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["temperature_sync"] == "unknown"
    aioclient_mock.post(
        f"{VEHICLE_URL}/air-conditioning/start", status=202, headers=COMMAND_HEADERS
    )
    await _call(
        hass, "climate", "set_hvac_mode", entity_id=CLIMATE, hvac_mode="heat_cool"
    )
    assert _sent(aioclient_mock)[0][2] == {"airConditioningWithoutExternalPower": True}


async def test_climate_temperature_survives_restart(
    env: Env,
) -> None:
    """The kept temperature comes back after a restart."""
    hass = env.hass
    memory = TemperatureMemory(
        desired=25.0,
        desired_at=datetime(2026, 11, 23, 9, 0, tzinfo=UTC),
        sent=25.0,
        sent_at=datetime(2026, 11, 23, 9, 1, tzinfo=UTC),
    )
    mock_restore_cache_with_extra_data(
        hass, [(State(CLIMATE, "off"), json.loads(json.dumps(memory.as_dict())))]
    )
    await _setup(env)
    state = hass.states.get(CLIMATE)
    assert state.attributes["temperature"] == 25.0
    # Sent before the vehicle captured 21.5 while off: waits for the next START.
    assert state.attributes["temperature_sync"] == "pending_next_start"
    assert TemperatureMemory.from_dict({"desired": True, "sent_at": 5}) == (
        TemperatureMemory()
    )


async def test_charge_mode_select(
    env: Env,
) -> None:
    """Only the modes the vehicle offers; one PUT."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    await _setup(env)
    entity_id = f"select.{CAR}_charge_mode"
    state = hass.states.get(entity_id)
    assert state.state == "manual"
    assert state.attributes["options"] == ["manual", "preferred_charging_times"]
    aioclient_mock.put(
        f"{VEHICLE_URL}/charging/mode", status=202, headers=COMMAND_HEADERS
    )
    await _call(
        hass,
        "select",
        "select_option",
        entity_id=entity_id,
        option="preferred_charging_times",
    )
    assert _sent(aioclient_mock) == [
        (
            "PUT",
            f"{VEHICLE_URL}/charging/mode",
            {"chargeMode": "PREFERRED_CHARGING_TIMES"},
        )
    ]
    state = hass.states.get(entity_id)
    assert state.state == "manual"
    assert state.attributes["pending"] == "preferred_charging_times"
    assert state.attributes["icon"] == PENDING_ICON


async def test_charge_limit_and_profile_numbers(
    env: Env,
) -> None:
    """Charge limit: PUT /charging/limit; profile: PUT of the whole profile."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    freezer = env.freezer
    await _setup(env)
    aioclient_mock.put(
        f"{VEHICLE_URL}/charging/limit", status=202, headers=COMMAND_HEADERS
    )
    aioclient_mock.put(PROFILE_URL, status=202, headers=COMMAND_HEADERS)
    limit = f"number.{CAR}_charge_limit"
    minimum = f"number.{CAR}_profile_minimum_charge"
    assert hass.states.get(limit).state == "80"
    assert hass.states.get(minimum).state == "0"

    await _call(hass, "number", "set_value", entity_id=limit, value=90)
    assert _sent(aioclient_mock)[-1] == (
        "PUT",
        f"{VEHICLE_URL}/charging/limit",
        {"targetStateOfChargeInPercent": 90},
    )
    assert hass.states.get(limit).attributes["pending"] == 90
    assert hass.states.get(limit).attributes["icon"] == PENDING_ICON

    freezer.tick(timedelta(seconds=10))
    await _call(hass, "number", "set_value", entity_id=minimum, value=20)
    method, url, body = _sent(aioclient_mock)[-1]
    assert (method, url) == ("PUT", PROFILE_URL)
    assert body["settings"]["minBatteryStateOfCharge"] == {
        "minimumBatteryStateOfChargeInPercent": 20
    }
    assert hass.states.get(minimum).attributes["pending"] == 20
    # Another profile setting does not show the pending change.
    assert (
        hass.states.get(f"number.{CAR}_profile_target_charge").attributes["pending"]
        is None
    )


async def test_profile_switches(
    env: Env,
) -> None:
    """Reduced current off = MAXIMUM; charging time 1 on; one PUT each."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    freezer = env.freezer
    await _setup(env)
    aioclient_mock.put(PROFILE_URL, status=202, headers=COMMAND_HEADERS)
    reduced = f"switch.{CAR}_reduced_current"
    assert hass.states.get(reduced).state == "on"
    assert hass.states.get(f"switch.{CAR}_unlock_cable_when_charged").state == "on"
    assert hass.states.get(f"switch.{CAR}_charging_time_2").state == "on"
    assert hass.states.get(f"switch.{CAR}_charging_time_3").state == STATE_UNKNOWN

    await _call(hass, "switch", "turn_off", entity_id=reduced)
    body = _sent(aioclient_mock)[-1][2]
    assert body["settings"]["maxChargingCurrent"] == "MAXIMUM"
    assert hass.states.get(reduced).attributes["pending"] == "off"
    assert hass.states.get(reduced).attributes["icon"] == PENDING_ICON
    assert hass.states.get(reduced).state == "on"
    # Only the changed setting shows the hourglass.
    assert "icon" not in hass.states.get(f"switch.{CAR}_charging_time_1").attributes

    # The previous profile change is not visible in a GET yet: refused.
    freezer.tick(timedelta(seconds=10))
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "switch", "turn_on", entity_id=f"switch.{CAR}_charging_time_1"
        )
    assert err.value.translation_key == "command_previous_not_visible"


async def test_charging_time_1_switch_on(
    env: Env,
) -> None:
    """Switching a preferred time on sends enabled=true in its slot."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    await _setup(env)
    aioclient_mock.put(PROFILE_URL, status=202, headers=COMMAND_HEADERS)
    entity_id = f"switch.{CAR}_charging_time_1"
    await _call(hass, "switch", "turn_on", entity_id=entity_id)
    body = _sent(aioclient_mock)[-1][2]
    assert body["preferredChargingTimes"][0]["enabled"] is True
    assert hass.states.get(entity_id).attributes["pending"] == "on"


async def test_charging_time_entities(
    env: Env,
) -> None:
    """A new start keeps the end; a missing slot is refused."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    freezer = env.freezer
    await _setup(env)
    aioclient_mock.put(PROFILE_URL, status=202, headers=COMMAND_HEADERS)
    start = f"time.{CAR}_charging_time_2_from"
    assert hass.states.get(start).state == "22:00:00"
    assert hass.states.get(f"time.{CAR}_charging_time_2_to").state == "06:00:00"
    assert hass.states.get(f"time.{CAR}_charging_time_4_to").state == STATE_UNKNOWN

    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "time",
            "set_value",
            entity_id=f"time.{CAR}_charging_time_3_from",
            time="01:00",
        )
    assert err.value.translation_key == "command_no_preferred_time"

    await _call(hass, "time", "set_value", entity_id=start, time="23:30")
    body = _sent(aioclient_mock)[-1][2]
    assert body["preferredChargingTimes"][1] == {
        "id": 14,
        "enabled": True,
        "startTime": "23:30",
        "endTime": "06:00",
    }
    assert hass.states.get(start).attributes["pending"] == "23:30-06:00"
    assert hass.states.get(start).attributes["icon"] == PENDING_ICON

    end = f"time.{CAR}_charging_time_1_to"
    freezer.tick(timedelta(seconds=10))
    with pytest.raises(ServiceValidationError):
        await _call(hass, "time", "set_value", entity_id=end, time="16:00")


async def test_controls_follow_operations(
    env: Env,
) -> None:
    """Controls only for listed operations; without the list all are created."""
    hass = env.hass
    vehicle = full_vehicle()
    vehicle["operations"] = [{"name": "startCharging"}, {"name": "stopCharging"}]
    await _setup(env, vehicle=vehicle)
    assert hass.states.get(f"button.{CAR}_start_charging") is not None
    assert hass.states.get(f"select.{CAR}_charge_mode") is None
    assert hass.states.get(CLIMATE) is None
    assert hass.states.get(f"button.{CAR}_refresh") is not None


async def test_controls_without_operations_list(
    env: Env,
) -> None:
    """``operations`` unavailable: entities exist, the command check decides."""
    hass = env.hass
    vehicle = full_vehicle()
    del vehicle["operations"]
    vehicle["chargingProfiles"]["currentVehiclePositionProfile"] = {}
    await _setup(env, vehicle=vehicle)
    assert hass.states.get(f"select.{CAR}_charge_mode") is not None
    # No location profile: profile values are unknown.
    assert hass.states.get(f"switch.{CAR}_reduced_current").state == STATE_UNKNOWN
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "select",
            "select_option",
            entity_id=f"select.{CAR}_charge_mode",
            option="manual",
        )
    assert err.value.translation_key == "command_not_supported"


async def test_entities_unavailable_without_data(
    env: Env,
) -> None:
    """Value helpers work without coordinator data."""
    hass = env.hass
    config_entry = env.entry
    await _setup(env)
    coordinator = config_entry.runtime_data.coordinator
    entity = next(
        e
        for e in hass.data["entity_components"]["switch"].entities
        if e.entity_id == f"switch.{CAR}_reduced_current"
    )
    with patch.object(coordinator, "data", None):
        assert entity.is_on is None
        assert entity.vehicle is None


def _at(minute: int) -> datetime:
    return datetime(2026, 11, 22, 8, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        # Cases of the climate temperature rules.
        ({"desired": None, "vehicle_value": 22.0}, "in_sync"),
        ({"desired": None, "vehicle_value": None}, "unknown"),
        ({"desired": 22.0, "vehicle_value": None}, "unknown"),
        ({"desired": 22.0, "vehicle_value": 22.0}, "in_sync"),
        (
            {"desired": 23.0, "desired_at": _at(0), "vehicle_value": 22.0},
            "pending_next_start",
        ),
        (
            {
                "desired": 23.0,
                "sent": 23.0,
                "sent_at": _at(0),
                "captured": _at(0) - timedelta(minutes=2),
            },
            "sent_awaiting_vehicle",
        ),
        (
            {
                "desired": 23.0,
                "sent": 23.0,
                "sent_at": _at(0),
                "captured": _at(3),
                "active": True,
            },
            "vehicle_differs",
        ),
        (
            {"desired": 23.0, "sent": 23.0, "sent_at": _at(0), "captured": _at(10)},
            "pending_next_start",
        ),
        (
            {
                "desired": 23.0,
                "desired_at": _at(4),
                "sent": 23.0,
                "sent_at": _at(0),
                "captured": _at(5),
            },
            "pending_next_start",
        ),
    ],
)
def test_temperature_sync(case: dict[str, Any], expected: str) -> None:
    """Sync states of the climate entity."""
    args = {
        "desired": None,
        "desired_at": None,
        "sent": None,
        "sent_at": None,
        "vehicle_value": 22.0,
        "captured": None,
        "active": False,
    } | case
    assert cmd.temperature_sync(**args) == expected


def test_helpers_without_data() -> None:
    """Pending values and operations without a GET."""
    assert pending_value(True) == "on"  # noqa: FBT003
    assert pending_value(False) == "off"  # noqa: FBT003
    assert pending_value(90) == 90
    assert operation_supported(None, "startCharging") is False


@pytest.mark.parametrize(
    ("charging", "plug", "start", "stop", "switch"),
    [
        ("READY_FOR_CHARGING", "CONNECTED", True, False, "off"),
        ("CHARGING", "CONNECTED", False, True, "on"),
        ("CONNECT_CABLE", None, False, False, "unavailable"),
        ("CONSERVING", "DISCONNECTED", False, False, "unavailable"),
    ],
)
async def test_charging_controls_follow_cable_and_state(  # noqa: PLR0913, PLR0917
    env: Env,
    charging: str,
    plug: str | None,
    start: bool,  # noqa: FBT001
    stop: bool,  # noqa: FBT001
    switch: str,
) -> None:
    """Start only plugged in and not charging, stop only while charging."""
    vehicle = _plugged(charging)
    if plug is None:
        del vehicle["charging"]["status"]["plugConnectionState"]
    else:
        vehicle["charging"]["status"]["plugConnectionState"] = plug
    await _setup(env, vehicle=vehicle)
    hass = env.hass
    states = {
        key: hass.states.get(f"button.{CAR}_{key}").state != "unavailable"
        for key in ("start_charging", "stop_charging")
    }
    assert states == {"start_charging": start, "stop_charging": stop}
    assert hass.states.get(f"switch.{CAR}_charging").state == switch


async def test_charging_switch(
    env: Env,
) -> None:
    """On = START, off = STOP; the requested state shows with an hourglass."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    await _setup(env, vehicle=_plugged())
    aioclient_mock.post(
        f"{VEHICLE_URL}/charging/start", status=202, headers=COMMAND_HEADERS
    )
    aioclient_mock.post(
        f"{VEHICLE_URL}/charging/stop", status=202, headers=COMMAND_HEADERS
    )
    entity_id = f"switch.{CAR}_charging"
    state = hass.states.get(entity_id)
    assert (state.state, state.attributes["pending"]) == ("off", None)
    await _call(hass, "switch", "turn_on", entity_id=entity_id)
    assert _sent(aioclient_mock) == [("POST", f"{VEHICLE_URL}/charging/start", None)]
    state = hass.states.get(entity_id)
    assert (state.state, state.attributes["pending"]) == ("on", "on")
    assert state.attributes["icon"] == PENDING_ICON
    # The stop button becomes usable to undo the pending start.
    assert hass.states.get(f"button.{CAR}_stop_charging").state != "unavailable"

    env.freezer.tick(timedelta(seconds=10))
    await _call(hass, "switch", "turn_off", entity_id=entity_id)
    assert _sent(aioclient_mock)[-1] == (
        "POST",
        f"{VEHICLE_URL}/charging/stop",
        None,
    )
    assert hass.states.get(entity_id).state == "off"

    # A GET that does not confirm the STOP: back to the vehicle's state.
    still = _plugged("CHARGING")
    still["charging"]["carCapturedTimestamp"] = "2026-11-23T10:04:00Z"
    await _refresh(env, still)
    state = hass.states.get(entity_id)
    assert (state.state, state.attributes["pending"]) == ("on", None)
    assert "icon" not in state.attributes


async def test_climate_stop_shows_requested_mode(
    env: Env,
) -> None:
    """STOP while running: mode off at once, the action from the GET."""
    hass = env.hass
    await _setup(env, vehicle=_running())
    env.aioclient_mock.post(
        f"{VEHICLE_URL}/air-conditioning/stop", status=202, headers=COMMAND_HEADERS
    )
    await _call(hass, "climate", "turn_off", entity_id=CLIMATE)
    state = hass.states.get(CLIMATE)
    assert (state.state, state.attributes["hvac_action"]) == ("off", "heating")
    assert state.attributes["icon"] == PENDING_ICON


async def test_stop_while_not_charging_resolves(
    env: Env,
) -> None:
    """STOP pressed with the cable unplugged: the next GET confirms it."""
    hass = env.hass
    unplugged = _plugged("CONNECT_CABLE")
    unplugged["charging"]["status"]["plugConnectionState"] = "DISCONNECTED"
    await _setup(env, vehicle=unplugged)
    env.aioclient_mock.post(
        f"{VEHICLE_URL}/charging/stop", status=202, headers=COMMAND_HEADERS
    )
    coordinator = env.entry.runtime_data.coordinator
    # Sent directly (an automation); the button itself is unavailable.
    await coordinator.async_send_command(cmd.charging_stop)
    assert coordinator.pending_command(cmd.CommandGroup.CHARGING) is not None
    # The car sends no new charging data; the GET still decides.
    await _refresh(env, unplugged)
    assert coordinator.pending_command(cmd.CommandGroup.CHARGING) is None
    assert coordinator.commands.results[cmd.CommandGroup.CHARGING].result == (
        "confirmed"
    )
    assert "icon" not in hass.states.get(f"switch.{CAR}_charging").attributes


async def test_clear_pending_button(
    env: Env,
) -> None:
    """The button forgets every pending command (no request)."""
    hass = env.hass
    aioclient_mock = env.aioclient_mock
    await _setup(env, vehicle=_plugged())
    aioclient_mock.post(
        f"{VEHICLE_URL}/charging/start", status=202, headers=COMMAND_HEADERS
    )
    aioclient_mock.put(
        f"{VEHICLE_URL}/charging/limit", status=202, headers=COMMAND_HEADERS
    )
    await _call(hass, "switch", "turn_on", entity_id=f"switch.{CAR}_charging")
    env.freezer.tick(timedelta(seconds=10))
    limit = f"number.{CAR}_charge_limit"
    await _call(hass, "number", "set_value", entity_id=limit, value=90)
    aioclient_mock.clear_requests()
    clear = f"button.{CAR}_clear_pending_commands"
    await _call(hass, "button", "press", entity_id=clear)
    assert aioclient_mock.mock_calls == []
    coordinator = env.entry.runtime_data.coordinator
    assert coordinator.commands.pending == {}
    assert coordinator.commands.results[cmd.CommandGroup.CHARGING].result == ("cleared")
    state = hass.states.get(f"switch.{CAR}_charging")
    assert (state.state, state.attributes["pending"]) == ("off", None)
    assert "icon" not in state.attributes
