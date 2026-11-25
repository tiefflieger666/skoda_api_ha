"""Persistence of the last GET, the quota state and the last response.

Kept in ``.storage/skoda_api_ha.<entry_id>`` so that a restart of Home
Assistant (or a reload of the entry) does not need an extra GET. The API key
is never written here; it stays in the config entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .api import VehicleResponse
from .commands import CommandState
from .const import DOMAIN
from .quota import QuotaState

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

STORAGE_VERSION = 1


@dataclass(slots=True)
class StoredState:
    """Contents of the store."""

    quota: QuotaState
    response: VehicleResponse | None = None
    last_success_at: datetime | None = None
    # Expiry (X-API-Key-Expires-At) of the key in use, for the key change.
    key_expires_at: datetime | None = None
    # Pending commands, last accepted change and last result per group.
    commands: CommandState = field(default_factory=CommandState.empty)


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def storage_key(entry_id: str) -> str:
    """Store key for one config entry."""
    return f"{DOMAIN}.{entry_id}"


class SkodaStore:
    """Thin wrapper around ``Store`` with typed load and save."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Create the store for one config entry."""
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry_id), private=True
        )

    async def async_load(self) -> StoredState:
        """Load the state. A missing or damaged file means an empty state."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return StoredState(quota=QuotaState())
        response = None
        raw = data.get("response")
        if isinstance(raw, dict) and isinstance(raw.get("vehicle"), dict):
            errors = raw.get("errors")
            response = VehicleResponse(
                vehicle=raw["vehicle"],
                errors=errors if isinstance(errors, list) else [],
            )
        return StoredState(
            quota=QuotaState.from_dict(data.get("quota")),
            response=response,
            last_success_at=_datetime(data.get("last_success_at")),
            key_expires_at=_datetime(data.get("key_expires_at")),
            commands=CommandState.from_dict(data.get("commands")),
        )

    async def async_save(self, state: StoredState) -> None:
        """Write the state now (one small write per request)."""
        response = None
        if state.response is not None:
            response = {
                "vehicle": state.response.vehicle,
                "errors": state.response.errors,
            }
        await self._store.async_save(
            {
                "quota": state.quota.as_dict(),
                "response": response,
                "last_success_at": (
                    state.last_success_at.isoformat() if state.last_success_at else None
                ),
                "key_expires_at": (
                    state.key_expires_at.isoformat() if state.key_expires_at else None
                ),
                "commands": state.commands.as_dict(),
            }
        )

    async def async_remove(self) -> None:
        """Delete the file when the entry is removed."""
        await self._store.async_remove()
