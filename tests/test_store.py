"""Store: a restart or reload does not cause an extra GET; planned refreshes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.skoda_api_ha.const import CONF_POLLS_PER_HOUR
from custom_components.skoda_api_ha.quota import QuotaState
from custom_components.skoda_api_ha.store import (
    STORAGE_VERSION,
    SkodaStore,
    storage_key,
)

from .conftest import setup_integration
from .const import QUOTA_HEADERS, VEHICLE_URL, VIN, vehicle_body

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def stored(
    entry: MockConfigEntry,
    quota: QuotaState,
    response: Any = None,
    last_success_at: Any = None,
) -> dict[str, Any]:
    """Return a store file as the integration writes it."""
    return {
        storage_key(entry.entry_id): {
            "version": STORAGE_VERSION,
            "minor_version": 1,
            "key": storage_key(entry.entry_id),
            "data": {
                "quota": quota.as_dict(),
                "response": response,
                "last_success_at": last_success_at,
            },
        }
    }


def add_listener(entry: MockConfigEntry) -> None:
    """Act like an entity: the first listener starts the refresh timer."""

    @callback
    def _listener() -> None:
        """Do nothing."""

    entry.async_on_unload(entry.runtime_data.coordinator.async_add_listener(_listener))


async def test_restart_does_not_get(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Second setup (restart, reload) restores the response without a GET."""
    freezer.move_to(NOW)
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    assert aioclient_mock.call_count == 1

    saved = hass_storage[storage_key(config_entry.entry_id)]["data"]
    assert saved["quota"]["remaining"] == 11
    assert saved["quota"]["last_get_at"] == NOW.isoformat()
    assert saved["response"]["vehicle"]["vin"] == VIN
    assert "test-key" not in str(saved)  # the key stays in the config entry

    freezer.tick(120)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert aioclient_mock.call_count == 1
    coordinator = config_entry.runtime_data.coordinator
    assert coordinator.data.vin == VIN
    assert coordinator.last_success_at == NOW
    assert coordinator.quota_state.remaining == 11
    # Same plan as before the restart: reset - 3 * 300 s - 10 s from the GET.
    assert coordinator.plan.delay == 890 - 120


async def test_restored_entry_gets_at_planned_time(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The first GET after a restart waits for its regular slot."""
    freezer.move_to(NOW)
    quota = QuotaState(
        limit=20,
        remaining=19,
        reset_at=NOW + timedelta(seconds=3400),
        last_get_at=NOW - timedelta(seconds=100),
        last_request_at=NOW - timedelta(seconds=100),
    )
    hass_storage.update(stored(config_entry, quota, vehicle_body(), NOW.isoformat()))
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert aioclient_mock.call_count == 0
    add_listener(config_entry)

    freezer.tick(150)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 0

    freezer.tick(60)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 1


async def test_restore_when_due_gets(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A regular GET that is due now is made at setup."""
    freezer.move_to(NOW)
    quota = QuotaState(remaining=15, reset_at=NOW - timedelta(seconds=5))
    quota = QuotaState.from_dict(
        quota.as_dict() | {"last_get_at": (NOW - timedelta(hours=2)).isoformat()}
    )
    hass_storage.update(stored(config_entry, quota, vehicle_body()))
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert aioclient_mock.call_count == 1


async def test_cooldown_without_response_waits_without_get(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """429 before a restart and nothing stored: retry setup later, no request."""
    freezer.move_to(NOW)
    quota = QuotaState().with_rate_limit(NOW, NOW, 900, None)
    hass_storage.update(stored(config_entry, quota))
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert aioclient_mock.call_count == 0


async def test_damaged_store_is_ignored(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Invalid stored values are unknown; the first GET then runs as usual."""
    hass_storage.update(
        stored(config_entry, QuotaState(), {"vehicle": "x"}, "not a date")
    )
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert aioclient_mock.call_count == 1


async def test_damaged_store_values(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """A stored response without errors list and an invalid time are tolerated."""
    hass_storage.update(
        stored(config_entry, QuotaState(), {"vehicle": {"vin": VIN}}, "bad")
    )
    state = await SkodaStore(hass, config_entry.entry_id).async_load()
    assert state.response is not None
    assert state.response.errors == []
    assert state.last_success_at is None

    hass_storage[storage_key(config_entry.entry_id)]["data"] = "garbage"
    state = await SkodaStore(hass, config_entry.entry_id).async_load()
    assert state.response is None
    assert state.quota == QuotaState()


async def test_manual_refresh_uses_reserve_only_with_golden_reserve(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A refresh before the planned time is an extra request from the reserve."""
    freezer.move_to(NOW)
    headers = QUOTA_HEADERS | {"RateLimit-Remaining": "19", "RateLimit-Reset": "3600"}
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=headers)
    await setup_integration(hass, config_entry)
    coordinator = config_entry.runtime_data.coordinator
    assert aioclient_mock.call_count == 1

    freezer.tick(70)
    await coordinator.async_refresh()
    assert aioclient_mock.call_count == 2
    assert coordinator.quota_state.extra_spent(NOW + timedelta(seconds=70)) == 1

    # 12 left with 11 regular GETs to go: one more extra request would break
    # the golden reserve, so the next early refresh sends nothing.
    aioclient_mock.clear_requests()
    tight = headers | {"RateLimit-Remaining": "12", "RateLimit-Reset": "3500"}
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=tight)
    freezer.tick(70)
    await coordinator.async_refresh()
    assert aioclient_mock.call_count == 1
    freezer.tick(70)
    await coordinator.async_refresh()
    assert aioclient_mock.call_count == 1  # refused: no request
    assert coordinator.last_update_success is True
    assert coordinator.data.vin == VIN


async def test_options_replan_running_timer(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Fewer polls per hour move the next GET later, without a request."""
    freezer.move_to(NOW)
    headers = QUOTA_HEADERS | {"RateLimit-Remaining": "19", "RateLimit-Reset": "3600"}
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=headers)
    await setup_integration(hass, config_entry)
    add_listener(config_entry)
    coordinator = config_entry.runtime_data.coordinator
    assert coordinator.plan.delay == 300

    hass.config_entries.async_update_entry(
        config_entry, options=config_entry.options | {CONF_POLLS_PER_HOUR: 2}
    )
    await hass.async_block_till_done()
    assert coordinator.plan.delay == 1800

    freezer.tick(400)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 1


async def test_failures_are_persisted(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed GET and 401 still count for spacing after a restart."""
    freezer.move_to(NOW)
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    coordinator = config_entry.runtime_data.coordinator

    for status in (503, 401):
        freezer.tick(900)
        aioclient_mock.clear_requests()
        aioclient_mock.get(VEHICLE_URL, status=status)
        await coordinator.async_refresh()
        assert aioclient_mock.call_count == 1
        saved = hass_storage[storage_key(config_entry.entry_id)]["data"]
        assert (
            saved["quota"]["last_get_at"]
            == coordinator.quota_state.last_get_at.isoformat()
        )
        assert saved["response"]["vehicle"]["vin"] == VIN
        assert coordinator.plan.delay >= 61


async def test_remove_entry_deletes_store(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Removing the entry removes its store file."""
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    assert storage_key(config_entry.entry_id) in hass_storage
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert storage_key(config_entry.entry_id) not in hass_storage
