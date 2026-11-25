"""Confirmation by the optional charger power sensor and the confirmation GET."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.core import State
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.skoda_api_ha import commands as cmd
from custom_components.skoda_api_ha.charger_meter import (
    meter_can_decide,
    power_watts,
    target_reached,
)
from custom_components.skoda_api_ha.commands import CommandGroup, PendingCommand
from custom_components.skoda_api_ha.const import (
    CONF_CHARGER_POWER_ENTITY,
    CONF_CONFIRM_GET,
)

from .const import QUOTA_HEADERS, VEHICLE_URL, command_vehicle
from .test_store import add_listener

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )

    from custom_components.skoda_api_ha.coordinator import (
        SkodaDataUpdateCoordinator,
    )

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
METER = "sensor.wallbox_power"
START_URL = f"{VEHICLE_URL}/charging/start"
STOP_URL = f"{VEHICLE_URL}/charging/stop"


@pytest.mark.parametrize(
    ("value", "unit", "watts"),
    [
        ("7400", "W", 7400.0),
        ("7.4", "kW", 7400.0),
        ("0.0074", "MW", 7400.0),
        ("5", "Wh", None),
        ("abc", "W", None),
        ("-3", "W", None),
        ("inf", "W", None),
        ("unavailable", "W", None),
    ],
)
def test_power_watts(value: str, unit: str, watts: float | None) -> None:
    """Power in W from W, kW or MW; anything else is unusable."""
    state = State(METER, value, {"unit_of_measurement": unit})
    assert power_watts(state) == (pytest.approx(watts) if watts else None)
    assert power_watts(None) is None


def test_meter_can_decide() -> None:
    """START needs an idle charger, STOP a charging one."""
    vehicle = command_vehicle()
    start = cmd.charging_start(vehicle)
    stop = cmd.charging_stop(vehicle)
    assert meter_can_decide(PendingCommand.from_command(start, NOW, 0.0))
    assert not meter_can_decide(PendingCommand.from_command(start, NOW, 3000.0))
    assert not meter_can_decide(PendingCommand.from_command(start, NOW, None))
    assert meter_can_decide(PendingCommand.from_command(stop, NOW, 7000.0))
    assert not meter_can_decide(PendingCommand.from_command(stop, NOW, 5.0))
    limit = cmd.charging_limit(vehicle, 90)
    assert not meter_can_decide(PendingCommand.from_command(limit, NOW, 0.0))


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    **options: Any,
) -> SkodaDataUpdateCoordinator:
    freezer.move_to(NOW)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={**entry.options, **options})
    aioclient_mock.get(
        VEHICLE_URL, json={"vehicle": command_vehicle()}, headers=QUOTA_HEADERS
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    add_listener(entry)
    aioclient_mock.clear_requests()
    freezer.tick(timedelta(seconds=30))
    return entry.runtime_data.coordinator


def _power(hass: HomeAssistant, watts: str, unit: str = "W") -> None:
    hass.states.async_set(METER, watts, {"unit_of_measurement": unit})


async def _tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_meter_confirms_start(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Idle -> >= 500 W for 20 s confirms START without any request."""
    _power(hass, "0")
    coordinator = await _setup(
        hass,
        aioclient_mock,
        config_entry,
        freezer,
        **{CONF_CHARGER_POWER_ENTITY: METER},
    )
    aioclient_mock.post(START_URL, status=202)
    await coordinator.async_send_command(cmd.charging_start)
    pending = coordinator.pending_command(CommandGroup.CHARGING)
    assert pending is not None
    assert pending.baseline_w == 0.0

    # A short peak does not count.
    _power(hass, "7.2", "kW")
    await _tick(hass, freezer, 10)
    _power(hass, "200")
    await _tick(hass, freezer, 15)
    assert coordinator.pending_command(CommandGroup.CHARGING) is not None

    # A sensor that drops out cancels the hold, too.
    _power(hass, "7200")
    await _tick(hass, freezer, 10)
    hass.states.async_set(METER, "unavailable")
    await _tick(hass, freezer, 15)
    assert coordinator.pending_command(CommandGroup.CHARGING) is not None
    # Asking again for the same command keeps the running watcher.
    coordinator._meter.async_update()

    # A steady value confirms without further reports.
    _power(hass, "7200")
    await _tick(hass, freezer, 21)
    assert coordinator.pending_command(CommandGroup.CHARGING) is None
    result = coordinator.commands.results[CommandGroup.CHARGING]
    assert (result.result, result.detail) == ("confirmed", "charger_power")
    assert aioclient_mock.call_count == 1  # only the POST


async def test_meter_confirms_stop_after_restart(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Charging -> <= 10 W for 20 s confirms STOP, also after a reload."""
    _power(hass, "7400")
    coordinator = await _setup(
        hass,
        aioclient_mock,
        config_entry,
        freezer,
        **{CONF_CHARGER_POWER_ENTITY: METER},
    )
    aioclient_mock.post(STOP_URL, status=202)
    await coordinator.async_send_command(cmd.charging_stop)

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = config_entry.runtime_data.coordinator
    pending = coordinator.pending_command(CommandGroup.CHARGING)
    assert pending is not None
    assert pending.baseline_w == 7400.0

    _power(hass, "3")
    await _tick(hass, freezer, 21)
    assert coordinator.pending_command(CommandGroup.CHARGING) is None
    assert coordinator.commands.results[CommandGroup.CHARGING].detail == (
        "charger_power"
    )


async def test_meter_cannot_decide(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Sensor unavailable at START: the API decides; removing the sensor stops it."""
    hass.states.async_set(METER, "unavailable")
    coordinator = await _setup(
        hass,
        aioclient_mock,
        config_entry,
        freezer,
        **{CONF_CHARGER_POWER_ENTITY: METER},
    )
    aioclient_mock.post(START_URL, status=202)
    await coordinator.async_send_command(cmd.charging_start)
    _power(hass, "7400")
    await _tick(hass, freezer, 25)
    assert coordinator.pending_command(CommandGroup.CHARGING) is not None

    # Options without the sensor: no watcher at all.
    options = {
        k: v for k, v in config_entry.options.items() if k != CONF_CHARGER_POWER_ENTITY
    }
    hass.config_entries.async_update_entry(config_entry, options=options)
    await hass.async_block_till_done()
    assert coordinator._meter is None


async def test_meter_switched_on_later_and_unloaded(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A sensor set in the options while a STOP is pending; unload stops it."""
    _power(hass, "7400")
    coordinator = await _setup(hass, aioclient_mock, config_entry, freezer)
    aioclient_mock.post(STOP_URL, status=202)
    await coordinator.async_send_command(cmd.charging_stop)
    # No sensor when the STOP was accepted: no baseline, the API decides.
    assert coordinator.pending_command(CommandGroup.CHARGING).baseline_w is None
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, CONF_CHARGER_POWER_ENTITY: METER},
    )
    await hass.async_block_till_done()
    _power(hass, "0")
    await _tick(hass, freezer, 25)
    assert coordinator.pending_command(CommandGroup.CHARGING) is not None

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_regular_get_first_stops_meter(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """When the API confirms first, the meter timer does nothing more."""
    _power(hass, "0")
    coordinator = await _setup(
        hass,
        aioclient_mock,
        config_entry,
        freezer,
        **{CONF_CHARGER_POWER_ENTITY: METER},
    )
    aioclient_mock.post(START_URL, status=202)
    await coordinator.async_send_command(cmd.charging_start)
    _power(hass, "7400")
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        VEHICLE_URL,
        json={
            "vehicle": command_vehicle(
                captured="2026-11-23T10:00:40+00:00", charging_state="CHARGING"
            )
        },
        headers=QUOTA_HEADERS,
    )
    await coordinator.async_refresh()
    assert coordinator.commands.results[CommandGroup.CHARGING].detail == (
        "confirmation_get"
    )
    await _tick(hass, freezer, 25)
    assert coordinator.commands.results[CommandGroup.CHARGING].detail == (
        "confirmation_get"
    )


async def test_confirmation_get(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Option on: one extra GET 3 minutes after the command confirms it."""
    coordinator = await _setup(
        hass, aioclient_mock, config_entry, freezer, **{CONF_CONFIRM_GET: True}
    )
    aioclient_mock.put(f"{VEHICLE_URL}/charging/limit", status=202)
    await coordinator.async_send_command(lambda v: cmd.charging_limit(v, 90))
    after = command_vehicle()
    after["charging"]["settings"]["targetStateOfChargeInPercent"] = 90
    aioclient_mock.clear_requests()
    aioclient_mock.get(VEHICLE_URL, json={"vehicle": after}, headers=QUOTA_HEADERS)

    await _tick(hass, freezer, 170)
    assert aioclient_mock.call_count == 0
    await _tick(hass, freezer, 15)
    assert aioclient_mock.call_count == 1
    result = coordinator.commands.results[CommandGroup.CHARGING_LIMIT]
    assert (result.result, result.detail) == ("confirmed", "confirmation_get")
    # Taken from the reserve: the command and the confirmation GET.
    assert coordinator.quota_state.extra_spent(NOW + timedelta(minutes=4)) == 2


async def test_confirmation_get_skipped(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No confirmation GET when off, already confirmed or the reserve is short."""
    coordinator = await _setup(hass, aioclient_mock, config_entry, freezer)
    aioclient_mock.put(f"{VEHICLE_URL}/charging/limit", status=202)
    await coordinator.async_send_command(lambda v: cmd.charging_limit(v, 90))
    aioclient_mock.clear_requests()
    await _tick(hass, freezer, 200)
    assert aioclient_mock.call_count == 0  # option off (default)

    # Option on, but the command is confirmed before the GET is due.
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_CONFIRM_GET: True}
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=5))
    aioclient_mock.put(f"{VEHICLE_URL}/charging/mode", status=202)
    await coordinator.async_send_command(
        lambda v: cmd.charge_mode(v, "PREFERRED_CHARGING_TIMES")
    )
    del coordinator.commands.pending[CommandGroup.CHARGE_MODE]
    aioclient_mock.clear_requests()
    await _tick(hass, freezer, 200)
    assert aioclient_mock.call_count == 0

    # A newer command replaces the planned confirmation GET.
    freezer.tick(timedelta(minutes=5))
    aioclient_mock.post(f"{VEHICLE_URL}/air-conditioning/start", status=202)
    await coordinator.async_send_command(cmd.climate_start)
    freezer.tick(timedelta(seconds=30))
    aioclient_mock.post(f"{VEHICLE_URL}/air-conditioning/stop", status=202)
    await coordinator.async_send_command(cmd.climate_stop)
    del coordinator.commands.pending[CommandGroup.CLIMATE]
    aioclient_mock.clear_requests()
    await _tick(hass, freezer, 200)
    assert aioclient_mock.call_count == 0

    # Still pending, but the golden reserve would break: nothing is sent.
    freezer.tick(timedelta(minutes=5))
    aioclient_mock.post(START_URL, status=202)
    await coordinator.async_send_command(cmd.charging_start)
    coordinator.quota_state = coordinator.quota_state.with_command_response(
        None, 3, None
    )
    aioclient_mock.clear_requests()
    await _tick(hass, freezer, 200)
    assert aioclient_mock.call_count == 0
    assert coordinator.pending_command(CommandGroup.CHARGING) is not None


def test_custom_thresholds() -> None:
    """Thresholds chosen in the options replace the defaults (500 W / 10 W)."""
    start = PendingCommand.from_command(
        cmd.charging_start(command_vehicle()), NOW, 40.0
    )
    stop = PendingCommand.from_command(cmd.charging_stop(command_vehicle()), NOW, 900.0)
    assert not meter_can_decide(start)
    assert meter_can_decide(start, active_w=1200.0, quiet_w=50.0)
    assert not meter_can_decide(stop, active_w=1200.0, quiet_w=50.0)
    assert target_reached(start, 1300.0, 1200.0, 50.0)
    assert not target_reached(start, 900.0, 1200.0, 50.0)
    assert target_reached(stop, 40.0, 1200.0, 50.0)
    assert not target_reached(stop, 60.0, 1200.0, 50.0)
