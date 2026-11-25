"""The Škoda API integration (official MyŠkoda Public API)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SkodaApiClient
from .const import (
    CONF_DEVICE_NAME_SOURCE,
    CONF_VIN,
    DEFAULT_NAME_SOURCE,
    DOMAIN,
    ISSUE_NO_COMMAND_QUOTA,
    MANUFACTURER,
)
from .coordinator import SkodaDataUpdateCoordinator, async_take_over_flow_get
from .data import SkodaConfigEntry, SkodaRuntimeData
from .naming import device_name
from .store import SkodaStore

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.DEVICE_TRACKER,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]


async def async_setup_entry(hass: HomeAssistant, entry: SkodaConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    # Right after the user step: its GET becomes the stored state (no 2nd GET).
    await async_take_over_flow_get(hass, entry)
    client = SkodaApiClient(async_get_clientsession(hass), entry.data[CONF_API_KEY])
    coordinator = SkodaDataUpdateCoordinator(
        hass, entry, client, SkodaStore(hass, entry.entry_id)
    )
    # A restart or reload uses the stored response; a GET only when one is due.
    if not await coordinator.async_restore():
        await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = SkodaRuntimeData(client=client, coordinator=coordinator)
    _async_apply_names(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: SkodaConfigEntry) -> None:
    """React to a changed entry.

    A new API key from reauth needs a reload (the reload uses the stored
    reauth GET, no request). Everything else (options, a key candidate, an
    activated candidate) is applied in place, without a reload and a GET.
    """
    if not entry.runtime_data.client.uses_key(entry.data[CONF_API_KEY]):
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    await entry.runtime_data.coordinator.async_options_updated()
    _async_apply_names(hass, entry)


@callback
def _async_apply_names(hass: HomeAssistant, entry: SkodaConfigEntry) -> None:
    """Set the device name and the entry title from the chosen source.

    Only display names change; entity_id and unique_id stay as they are.
    """
    name = device_name(
        entry.options.get(CONF_DEVICE_NAME_SOURCE, DEFAULT_NAME_SOURCE),
        entry.runtime_data.coordinator.data,
        entry.data[CONF_VIN],
    )
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data[CONF_VIN])},
        manufacturer=MANUFACTURER,
        name=name,
    )
    if entry.title != name:
        hass.config_entries.async_update_entry(entry, title=name)


async def async_unload_entry(hass: HomeAssistant, entry: SkodaConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: SkodaConfigEntry) -> None:
    """Delete the stored state when the entry is removed."""
    await SkodaStore(hass, entry.entry_id).async_remove()
    ir.async_delete_issue(hass, DOMAIN, ISSUE_NO_COMMAND_QUOTA)
