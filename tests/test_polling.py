"""Regular updates 0-20 per hour and the switch that stops all API traffic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE, Platform
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.skoda_api_ha.const import (
    CONF_API_ENABLED,
    CONF_POLLS_PER_HOUR,
    DOMAIN,
    ISSUE_NO_COMMAND_QUOTA,
)
from custom_components.skoda_api_ha.quota import (
    QuotaState,
    can_spend,
    commands_possible,
    plan_next_get,
    regular_gets_until_reset,
    reserve_for_polls,
)

from .const import QUOTA_HEADERS, VEHICLE_URL, full_vehicle

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )


# The Start/Stop charging buttons are disabled by default.
pytestmark = pytest.mark.usefixtures("entity_registry_enabled_by_default")

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
CAR = "fiction_car"
SWITCH = f"switch.{CAR}_api_access"
PLATFORMS = [Platform.SWITCH, Platform.BUTTON, Platform.SENSOR, Platform.NUMBER]


@pytest.mark.parametrize(
    ("polls", "reserve", "commands"),
    [
        (0, 8, True),
        (12, 8, True),
        (15, 5, True),
        (18, 2, True),
        (19, 1, False),
        (20, 0, False),
    ],
)
def test_reserve_follows_polls(polls: int, reserve: int, commands: bool) -> None:  # noqa: FBT001
    """The reserve is never more than the user leaves free (limit 20)."""
    assert reserve_for_polls(20, polls) == reserve
    assert reserve_for_polls(None, polls) == reserve
    assert commands_possible(20, polls) is commands


def test_twenty_polls_use_the_whole_quota() -> None:
    """20 per hour: every 180 s, no reserve, no command."""
    state = QuotaState(
        limit=20, remaining=19, reset_at=NOW + timedelta(hours=1), last_get_at=NOW
    )
    plan = plan_next_get(state, NOW, 20)
    assert (plan.interval, plan.reserve, plan.delay) == (180, 0, 180)
    assert not can_spend(state, NOW, 20)


def test_zero_polls_have_no_regular_gets() -> None:
    """0 per hour: nothing to keep for regular GETs, so extras are allowed."""
    state = QuotaState(
        limit=20, remaining=5, reset_at=NOW + timedelta(minutes=30), last_get_at=NOW
    )
    assert regular_gets_until_reset(state, NOW, 0) == 0
    assert can_spend(state, NOW, 0)


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
    options: dict[str, Any] | None = None,
) -> None:
    entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, **options}
        )
    aioclient_mock.get(
        VEHICLE_URL, json={"vehicle": full_vehicle()}, headers=QUOTA_HEADERS
    )
    with patch("custom_components.skoda_api_ha.PLATFORMS", PLATFORMS):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    aioclient_mock.clear_requests()


async def _switch(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call(
        "switch", service, {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()


async def test_api_switch_stops_all_traffic(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Off: no GET and no command, controls grey, sensors keep their data."""
    freezer.move_to(NOW)
    await _setup(hass, aioclient_mock, config_entry)
    coordinator = config_entry.runtime_data.coordinator
    assert hass.states.get(SWITCH).state == "on"

    await _switch(hass, "turn_off")
    assert config_entry.options[CONF_API_ENABLED] is False
    assert hass.states.get(SWITCH).state == "off"
    assert hass.states.get(f"button.{CAR}_stop_charging").state == STATE_UNAVAILABLE
    assert hass.states.get(f"button.{CAR}_refresh").state == STATE_UNAVAILABLE
    assert hass.states.get(f"number.{CAR}_charge_limit").state == STATE_UNAVAILABLE
    assert hass.states.get(f"sensor.{CAR}_battery").state == "64"
    assert coordinator.update_interval is None

    # Hours pass: no request at all.
    freezer.tick(timedelta(hours=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 0
    # Called directly (a service of another integration): refused, nothing sent.
    for call in (
        coordinator.async_manual_refresh(),
        coordinator.async_send_command(lambda _v: None),
    ):
        with pytest.raises(ServiceValidationError) as err:
            await call
        assert err.value.translation_key == "command_api_disabled"
    await coordinator.async_refresh()
    assert aioclient_mock.call_count == 0

    # On again: available, the next regular GET follows the plan.
    await _switch(hass, "turn_on")
    assert hass.states.get(f"button.{CAR}_stop_charging").state != STATE_UNAVAILABLE
    assert coordinator.update_interval is not None
    assert aioclient_mock.call_count == 0


async def test_restart_with_api_off_makes_no_request(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Restart with the switch off: stored data, no request, even when due."""
    freezer.move_to(NOW)
    await _setup(hass, aioclient_mock, config_entry)
    await _switch(hass, "turn_off")
    freezer.tick(timedelta(hours=3))
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 0
    assert hass.states.get(f"sensor.{CAR}_battery").state == "64"
    assert hass.states.get(SWITCH).state == "off"


async def test_api_off_without_stored_data(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Off before any data: set up without a request; the switch is there."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_API_ENABLED: False}
    )
    with patch("custom_components.skoda_api_ha.PLATFORMS", PLATFORMS):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert aioclient_mock.call_count == 0
    # No vehicle name yet: the device has the generic name.
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, "TMBTEST0000000001_api_access"
    )
    assert entity_id == "switch.skoda_api_access"
    assert hass.states.get(entity_id).state == "off"
    coordinator = config_entry.runtime_data.coordinator
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    assert aioclient_mock.call_count == 0


async def test_zero_polls_only_manual_refresh(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """0 per hour: no timer, no GET on restart; refresh is one extra request."""
    freezer.move_to(NOW)
    await _setup(hass, aioclient_mock, config_entry, {CONF_POLLS_PER_HOUR: 0})
    coordinator = config_entry.runtime_data.coordinator
    assert coordinator.update_interval is None

    freezer.tick(timedelta(hours=2))
    async_fire_time_changed(hass)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 0

    aioclient_mock.get(
        VEHICLE_URL, json={"vehicle": full_vehicle()}, headers=QUOTA_HEADERS
    )
    await hass.services.async_call(
        "button", "press", {"entity_id": f"button.{CAR}_refresh"}, blocking=True
    )
    assert aioclient_mock.call_count == 1
    coordinator = config_entry.runtime_data.coordinator
    assert coordinator.quota_state.extra_spent(dt_now(freezer)) == 1


def dt_now(freezer: FrozenDateTimeFactory) -> datetime:
    """Return the current frozen time."""
    return freezer.time_to_freeze.replace(tzinfo=UTC)


@pytest.mark.parametrize(("polls", "issue"), [(18, False), (19, True), (20, True)])
async def test_no_command_quota_issue(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    polls: int,
    issue: bool,  # noqa: FBT001
) -> None:
    """From 19 per hour a repair warning says no quota is left for commands."""
    await _setup(hass, aioclient_mock, config_entry, {CONF_POLLS_PER_HOUR: polls})
    registry = ir.async_get(hass)
    assert (
        registry.async_get_issue(DOMAIN, ISSUE_NO_COMMAND_QUOTA) is not None
    ) is issue

    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_POLLS_PER_HOUR: 12}
    )
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, ISSUE_NO_COMMAND_QUOTA) is None


async def test_issue_removed_with_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Removing the vehicle removes its warning."""
    await _setup(hass, aioclient_mock, config_entry, {CONF_POLLS_PER_HOUR: 20})
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_NO_COMMAND_QUOTA) is None
