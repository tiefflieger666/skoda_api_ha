"""Diagnostics download; never contains the key, VIN, plate, GPS or address."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY

from .const import CONF_API_KEY_CANDIDATE, CONF_CHARGER_POWER_ENTITY, CONF_VIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import SkodaConfigEntry

# Secrets and private data, wherever they appear (entry data and API answer).
TO_REDACT = {
    CONF_API_KEY,
    CONF_API_KEY_CANDIDATE,
    CONF_VIN,
    "licensePlate",
    "gpsCoordinates",
    "latitude",
    "longitude",
    "formattedAddress",
    "renderUrl",
    "name",
    "title",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    _hass: HomeAssistant, entry: SkodaConfigEntry
) -> dict[str, Any]:
    """Return what helps to analyse a problem, without private data."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data
    plan = coordinator.plan
    options = dict(entry.options)
    if options.get(CONF_CHARGER_POWER_ENTITY):
        options[CONF_CHARGER_POWER_ENTITY] = "**SET**"
    return async_redact_data(
        {
            "entry": {
                "title": entry.title,
                "unique_id": entry.unique_id,
                "data": dict(entry.data),
                "options": options,
            },
            "polling": {
                "api_enabled": coordinator.api_enabled,
                "regular_polling": coordinator.regular_polling,
                "polls_per_hour": coordinator.polls_per_hour,
                "update_interval": coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None,
                "plan": None
                if plan is None
                else {
                    "delay": plan.delay,
                    "next_get_at": plan.next_get_at.isoformat(),
                    "interval": plan.interval,
                    "effective_interval": plan.effective_interval,
                    "reserve": plan.reserve,
                    "reserve_total": plan.reserve_total,
                    "extra_spent": plan.extra_spent,
                    "available_gets": plan.available_gets,
                },
                "last_update_success": coordinator.last_update_success,
                "last_success_at": coordinator.last_success_at.isoformat()
                if coordinator.last_success_at
                else None,
            },
            "quota": coordinator.quota_state.as_dict(),
            "key_expires_at": coordinator.key_expires_at.isoformat()
            if coordinator.key_expires_at
            else None,
            "commands": coordinator.commands.as_dict(),
            "vehicle": None if data is None else data.vehicle,
            "errors": None if data is None else data.errors,
        },
        TO_REDACT,
    )
