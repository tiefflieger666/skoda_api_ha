"""The GET of a config flow (user, reauth) goes to Store and the planner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.skoda_api_ha.api import SkodaConnectionError, VehicleResponse
from custom_components.skoda_api_ha.const import (
    CONF_DEVICE_NAME_SOURCE,
    CONF_VIN,
    DOMAIN,
    NAME_SOURCE_VEHICLE_NAME,
)
from custom_components.skoda_api_ha.coordinator import (
    FLOW_GET_HANDOVER,
    FlowGet,
    quota_after_flow_get,
)
from custom_components.skoda_api_ha.quota import QuotaState
from custom_components.skoda_api_ha.store import storage_key

from .conftest import setup_integration
from .const import (
    API_KEY,
    NEW_API_KEY,
    OTHER_VIN,
    QUOTA_HEADERS,
    VEHICLE_URL,
    VIN,
    vehicle_body,
)
from .test_store import add_listener

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
FLOW_AT = NOW + timedelta(seconds=120)


def saved_quota(hass_storage: dict[str, Any], entry: MockConfigEntry) -> QuotaState:
    """Quota state as written to the store."""
    return QuotaState.from_dict(
        hass_storage[storage_key(entry.entry_id)]["data"]["quota"]
    )


async def _reauth_flow_id(hass: HomeAssistant) -> str:
    """Return the reauth flow that a failed setup started."""
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == SOURCE_REAUTH
    ]
    assert len(flows) == 1
    return flows[0]["flow_id"]


async def test_reauth_get_is_stored_and_reload_does_not_get(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Reauth after a failed setup: its GET is the data for the reload."""
    freezer.move_to(NOW)
    aioclient_mock.get(VEHICLE_URL, status=401)
    await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flow_id = await _reauth_flow_id(hass)

    freezer.move_to(FLOW_AT)
    aioclient_mock.clear_requests()
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_API_KEY: NEW_API_KEY}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"

    # One GET in the flow, none for the reload.
    assert aioclient_mock.call_count == 1
    assert config_entry.state is ConfigEntryState.LOADED
    quota = saved_quota(hass_storage, config_entry)
    assert quota.last_get_at == FLOW_AT
    assert quota.remaining == 11
    saved = hass_storage[storage_key(config_entry.entry_id)]["data"]
    assert saved["response"]["vehicle"]["vin"] == VIN
    assert "test-key" not in str(saved)
    coordinator = config_entry.runtime_data.coordinator
    assert coordinator.data.vin == VIN
    assert coordinator.last_success_at == FLOW_AT
    # Planned from the flow GET: reset - 3 * 300 s - 10 s.
    assert coordinator.plan.delay == 890


async def test_reauth_rate_limited_is_stored(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 429 in the flow starts the Retry-After cooldown in the store."""
    freezer.move_to(NOW)
    aioclient_mock.get(VEHICLE_URL, status=401)
    await setup_integration(hass, config_entry)
    flow_id = await _reauth_flow_id(hass)

    freezer.move_to(FLOW_AT)
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        VEHICLE_URL,
        status=429,
        headers={"Retry-After": "600", "RateLimit-Reset": "900"},
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_API_KEY: NEW_API_KEY}
    )
    assert result["errors"] == {"base": "rate_limited"}
    quota = saved_quota(hass_storage, config_entry)
    assert quota.rate_limited_at == FLOW_AT
    assert quota.retry_after == 600
    assert quota.remaining == 0
    assert quota.retry_delay(FLOW_AT) == 605


async def test_reauth_failed_get_taken_from_reserve(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed flow GET is one reserve request; the GET schedule stays."""
    freezer.move_to(NOW)
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    add_listener(config_entry)
    coordinator = config_entry.runtime_data.coordinator

    freezer.move_to(FLOW_AT)
    aioclient_mock.clear_requests()
    aioclient_mock.get(VEHICLE_URL, status=401)
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_API_KEY}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert config_entry.state is ConfigEntryState.LOADED

    state = coordinator.quota_state
    assert state.last_get_at == NOW
    assert state.last_request_at == FLOW_AT
    assert state.remaining == 10
    assert state.extra_spent(FLOW_AT) == 1
    assert saved_quota(hass_storage, config_entry) == state
    assert coordinator.plan.extra_spent == 1


def test_quota_after_flow_get_connection_error() -> None:
    """A network error is counted conservatively, without moving the GET clock."""
    state = QuotaState(
        remaining=11, reset_at=NOW + timedelta(minutes=30), last_get_at=NOW
    )
    after = quota_after_flow_get(
        state, FLOW_AT, FLOW_AT, None, SkodaConnectionError("timeout")
    )
    assert after.last_get_at == NOW
    assert after.last_request_at == FLOW_AT
    assert after.remaining == 10
    assert after.extra_requests == (FLOW_AT,)


async def test_user_flow_get_is_the_first_setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Adding a vehicle makes one GET: the setup reuses the user step GET."""
    freezer.move_to(NOW)
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_API_KEY: API_KEY,
            CONF_VIN: VIN,
            CONF_DEVICE_NAME_SOURCE: NAME_SOURCE_VEHICLE_NAME,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]

    assert aioclient_mock.call_count == 1
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data.vin == VIN
    assert coordinator.last_success_at == NOW
    # Planned from the user step GET: reset - 3 * 300 s - 10 s.
    assert coordinator.plan.delay == 890
    quota = saved_quota(hass_storage, entry)
    assert quota.last_get_at == NOW
    assert quota.remaining == 11
    assert quota.extra_requests == ()
    saved = hass_storage[storage_key(entry.entry_id)]["data"]
    assert saved["response"]["vehicle"]["vin"] == VIN
    assert "test-key" not in str(saved)
    # The hand-over is used once and then gone.
    assert FLOW_GET_HANDOVER not in hass.data

    # The planned GET happens at its time, not before.
    add_listener(entry)
    freezer.tick(timedelta(seconds=880))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 1
    freezer.tick(timedelta(seconds=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 2


async def test_hand_over_only_for_its_vin(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A hand-over for another VIN is left alone; this setup makes its GET."""
    freezer.move_to(NOW)
    other = FlowGet(NOW, VehicleResponse(vehicle={"vin": OTHER_VIN}))
    hass.data[FLOW_GET_HANDOVER] = {OTHER_VIN: other}
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert aioclient_mock.call_count == 1
    assert hass.data[FLOW_GET_HANDOVER] == {OTHER_VIN: other}
