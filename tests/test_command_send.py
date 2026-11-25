"""Sending commands through the coordinator: one request, pending, confirmation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.skoda_api_ha import commands as cmd
from custom_components.skoda_api_ha.commands import CommandGroup
from custom_components.skoda_api_ha.const import DOMAIN
from custom_components.skoda_api_ha.store import storage_key

from .conftest import setup_integration
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
START_URL = f"{VEHICLE_URL}/charging/start"
LIMIT_URL = f"{VEHICLE_URL}/charging/limit"
PROFILE_URL = f"{VEHICLE_URL}/charging-profiles/4"
COMMAND_HEADERS = {"RateLimit-Remaining": "10", "RateLimit-Reset": "1700"}


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> SkodaDataUpdateCoordinator:
    freezer.move_to(NOW)
    aioclient_mock.get(
        VEHICLE_URL, json={"vehicle": command_vehicle()}, headers=QUOTA_HEADERS
    )
    await setup_integration(hass, entry)
    add_listener(entry)
    aioclient_mock.clear_requests()
    freezer.tick(timedelta(seconds=30))
    return entry.runtime_data.coordinator


async def test_start_charging_accepted_then_confirmed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """One POST; 202 = pending; the next regular GET confirms it."""
    coordinator = await _setup(hass, aioclient_mock, config_entry, freezer)
    aioclient_mock.post(START_URL, status=202, headers=COMMAND_HEADERS)
    await coordinator.async_send_command(cmd.charging_start)

    assert aioclient_mock.call_count == 1
    method, url, body, headers = aioclient_mock.mock_calls[0]
    assert (method, str(url), body) == ("POST", START_URL, None)
    assert headers["X-API-Key"] == "test-key-not-real"
    accepted_at = NOW + timedelta(seconds=30)
    pending = coordinator.pending_command(CommandGroup.CHARGING)
    assert pending is not None
    assert pending.key == "charging_start"
    assert pending.accepted_at == accepted_at
    assert coordinator.commands.results[CommandGroup.CHARGING].result == "accepted"
    # Counted from the reserve; remaining taken from the answer.
    assert coordinator.quota_state.remaining == 10
    assert coordinator.quota_state.extra_spent(accepted_at) == 1
    assert coordinator.quota_state.last_get_at == NOW
    saved = hass_storage[storage_key(config_entry.entry_id)]["data"]
    assert saved["commands"]["pending"][0]["key"] == "charging_start"

    # Next regular GET: the car reports CHARGING after the command.
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        VEHICLE_URL,
        json={
            "vehicle": command_vehicle(
                captured="2026-11-23T10:03:00+00:00", charging_state="CHARGING"
            )
        },
        headers=QUOTA_HEADERS,
    )
    freezer.tick(timedelta(minutes=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 1
    assert coordinator.pending_command(CommandGroup.CHARGING) is None
    result = coordinator.commands.results[CommandGroup.CHARGING]
    assert (result.key, result.result, result.detail) == (
        "charging_start",
        "confirmed",
        "regular_get",
    )


async def test_pending_survives_restart(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A reload keeps the pending command and makes no request."""
    coordinator = await _setup(hass, aioclient_mock, config_entry, freezer)
    aioclient_mock.put(LIMIT_URL, status=202)
    await coordinator.async_send_command(lambda v: cmd.charging_limit(v, 90))
    assert aioclient_mock.mock_calls[0][2] == {"targetStateOfChargeInPercent": 90}

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 1
    coordinator = config_entry.runtime_data.coordinator
    pending = coordinator.pending_command(CommandGroup.CHARGING_LIMIT)
    assert pending is not None
    assert pending.expected == 90
    # Without a header the reserve request is counted locally.
    assert coordinator.quota_state.remaining == 10
    assert CommandGroup.CHARGING_LIMIT in coordinator.commands.last_accepted


async def test_profile_put_and_previous_change(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The whole profile is sent; the next change waits for a newer GET."""
    coordinator = await _setup(hass, aioclient_mock, config_entry, freezer)
    aioclient_mock.put(PROFILE_URL, status=202)
    await coordinator.async_send_command(
        lambda v: cmd.profile_setting(v, "max_current", "MAXIMUM")
    )
    body = aioclient_mock.mock_calls[0][2]
    assert body["id"] == 4
    assert body["settings"]["maxChargingCurrent"] == "MAXIMUM"
    assert body["timers"][0]["id"] == 10

    freezer.tick(timedelta(seconds=30))
    with pytest.raises(ServiceValidationError) as err:
        await coordinator.async_send_command(
            lambda v: cmd.profile_setting(v, "target_soc", 90)
        )
    assert err.value.translation_key == "command_previous_not_visible"
    assert aioclient_mock.call_count == 1


@pytest.mark.parametrize(
    ("mock_kwargs", "key", "result", "detail"),
    [
        (
            {"status": 400, "json": {"type": "https://x/problems/vehicle-offline"}},
            "command_rejected",
            "rejected",
            "vehicle-offline",
        ),
        ({"status": 409}, "command_rejected", "rejected", "http_409"),
        (
            {"status": 422, "text": "not json"},
            "command_rejected",
            "rejected",
            "http_422",
        ),
        (
            {"status": 503},
            "command_uncertain",
            "uncertain",
            "SkodaCommandUncertainError",
        ),
        (
            {"exc": TimeoutError()},
            "command_uncertain",
            "uncertain",
            "SkodaCommandUncertainError",
        ),
    ],
)
async def test_command_failures(  # noqa: PLR0913
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    *,
    mock_kwargs: dict,
    key: str,
    result: str,
    detail: str,
) -> None:
    """Exactly one request, no retry; the outcome is recorded."""
    coordinator = await _setup(hass, aioclient_mock, config_entry, freezer)
    aioclient_mock.put(LIMIT_URL, **mock_kwargs)
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_send_command(lambda v: cmd.charging_limit(v, 90))
    assert err.value.translation_key == key
    assert aioclient_mock.call_count == 1
    assert coordinator.pending_command(CommandGroup.CHARGING_LIMIT) is None
    recorded = coordinator.commands.results[CommandGroup.CHARGING_LIMIT]
    assert (recorded.result, recorded.detail) == (result, detail)
    assert coordinator.quota_state.extra_spent(NOW + timedelta(seconds=30)) == 1
    # An uncertain change may have happened: the next body waits for a GET.
    assert (CommandGroup.CHARGING_LIMIT in coordinator.commands.last_accepted) is (
        result == "uncertain"
    )


async def test_command_rate_limited(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """429 to a command starts the cooldown without moving the GET clock."""
    coordinator = await _setup(hass, aioclient_mock, config_entry, freezer)
    aioclient_mock.post(START_URL, status=429, headers={"Retry-After": "120"})
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_send_command(cmd.charging_start)
    assert err.value.translation_key == "command_rate_limited"
    state = coordinator.quota_state
    assert state.retry_after == 120
    assert state.last_get_at == NOW
    assert coordinator.plan.delay >= 120

    freezer.tick(timedelta(seconds=30))
    with pytest.raises(ServiceValidationError) as err:
        await coordinator.async_send_command(cmd.charging_stop)
    assert err.value.translation_key == "command_rate_limited"
    assert aioclient_mock.call_count == 1


async def test_command_vehicle_busy_is_not_quota(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """429 vehicle-not-accepting-requests: rejected, no quota cooldown."""
    coordinator = await _setup(hass, aioclient_mock, config_entry, freezer)
    aioclient_mock.post(
        START_URL,
        status=429,
        json={"type": "https://x/problems/vehicle-not-accepting-requests"},
        headers={**COMMAND_HEADERS, "Retry-After": "120"},
    )
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_send_command(cmd.charging_start)
    assert err.value.translation_key == "command_vehicle_busy"
    state = coordinator.quota_state
    assert state.rate_limited_at is None
    assert state.remaining == 10
    assert state.last_get_at == NOW
    recorded = coordinator.commands.results[CommandGroup.CHARGING]
    assert (recorded.result, recorded.detail) == (
        "rejected",
        "vehicle-not-accepting-requests",
    )
    assert coordinator.pending_command(CommandGroup.CHARGING) is None

    # Not blocked by a cooldown: after the debounce another command goes out.
    freezer.tick(timedelta(seconds=60))
    aioclient_mock.clear_requests()
    aioclient_mock.post(START_URL, status=202, headers=COMMAND_HEADERS)
    await coordinator.async_send_command(cmd.charging_start)
    assert aioclient_mock.call_count == 1


async def test_get_vehicle_busy_is_not_quota(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A GET declined by the vehicle fails without a quota cooldown."""
    coordinator = await _setup(hass, aioclient_mock, config_entry, freezer)
    aioclient_mock.get(
        VEHICLE_URL,
        status=429,
        json={"type": "https://x/problems/vehicle-not-accepting-requests"},
        headers={"RateLimit-Remaining": "6", "Retry-After": "120"},
    )
    freezer.tick(timedelta(minutes=5))
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    state = coordinator.quota_state
    assert state.rate_limited_at is None
    assert state.remaining == 6


async def test_command_auth_failed_starts_reauth(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """401 to a command asks for a new key."""
    coordinator = await _setup(hass, aioclient_mock, config_entry, freezer)
    aioclient_mock.post(START_URL, status=401)
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_send_command(cmd.charging_start)
    assert err.value.translation_key == "command_auth_failed"
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_refused_before_sending(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Builder and pre-send errors send nothing and use no quota."""
    coordinator = await _setup(hass, aioclient_mock, config_entry, freezer)
    remaining = coordinator.quota_state.remaining
    with pytest.raises(ServiceValidationError) as err:
        await coordinator.async_send_command(lambda v: cmd.charge_mode(v, "TIMER"))
    assert err.value.translation_key == "command_invalid_value"

    aioclient_mock.post(START_URL, status=202)
    await coordinator.async_send_command(cmd.charging_start)
    freezer.tick(timedelta(seconds=10))
    with pytest.raises(ServiceValidationError) as err:
        await coordinator.async_send_command(cmd.charging_start)
    assert err.value.translation_key == "command_debounced"
    assert aioclient_mock.call_count == 1
    assert coordinator.quota_state.remaining == remaining - 1


async def test_busy_and_no_data(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One command at a time; without data nothing can be built."""
    coordinator = await _setup(hass, aioclient_mock, config_entry, freezer)
    async with coordinator._command_lock:
        with pytest.raises(ServiceValidationError) as err:
            await coordinator.async_send_command(cmd.charging_start)
    assert err.value.translation_key == "command_busy"

    coordinator.data = None
    with pytest.raises(ServiceValidationError) as err:
        await coordinator.async_send_command(cmd.charging_start)
    assert err.value.translation_key == "command_no_data"
    assert aioclient_mock.call_count == 0


async def test_unconfirmed_after_timeout(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without fresh data the command is not confirmed after the timeout."""
    coordinator = await _setup(hass, aioclient_mock, config_entry, freezer)
    aioclient_mock.post(START_URL, status=202)
    await coordinator.async_send_command(cmd.charging_start)
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        VEHICLE_URL, json={"vehicle": command_vehicle()}, headers=QUOTA_HEADERS
    )
    for _ in range(4):
        freezer.tick(timedelta(minutes=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert coordinator.pending_command(CommandGroup.CHARGING) is None
    assert coordinator.commands.results[CommandGroup.CHARGING].result == (
        "not_confirmed"
    )
