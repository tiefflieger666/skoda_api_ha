"""Common base for the entities of one vehicle (one device per VIN)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .commands import (
    Command,
    CommandError,
    CommandGroup,
    current_profile,
    operation_names,
    profile_values,
)
from .const import DOMAIN
from .coordinator import SkodaDataUpdateCoordinator

if TYPE_CHECKING:
    from datetime import datetime

    from .api import VehicleResponse

type Vehicle = dict[str, Any]

# Icon of a control while its command waits for confirmation by a GET. The
# state stays from the last GET (no optimistic state); only the icon shows
# that the command was accepted and is on its way (design decision).
PENDING_ICON = "mdi:timer-sand"

# Section of the GET body -> prefix of its VehicleError types (Swagger).
SECTION_ERRORS: dict[str, str] = {
    "status": "VEHICLE_STATUS",
    "fuelStatus": "FUEL_STATUS",
    "odometer": "ODOMETER",
    "parkingPosition": "PARKING_POSITION",
    "airConditioning": "AIR_CONDITIONING",
    "auxiliaryHeating": "AUXILIARY_HEATING",
    "activeVentilation": "ACTIVE_VENTILATION",
    "charging": "CHARGING",
    "chargingProfiles": "CHARGING_PROFILES",
}


def path(vehicle: Vehicle | None, *keys: str) -> Any:
    """Value at ``keys`` in the vehicle, or None if any part is missing."""
    value: Any = vehicle
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def number(vehicle: Vehicle | None, *keys: str) -> float | None:
    """Return the number at ``keys``; None for a missing or non-numeric value."""
    value = path(vehicle, *keys)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def flag(vehicle: Vehicle | None, *keys: str) -> bool | None:
    """Boolean at ``keys``; None if the API omitted it."""
    value = path(vehicle, *keys)
    return value if isinstance(value, bool) else None


def mapped(
    vehicle: Vehicle | None, keys: tuple[str, ...], mapping: dict[str, bool]
) -> bool | None:
    """Enum at ``keys`` mapped to on/off; other values (UNKNOWN ...) give None."""
    value = path(vehicle, *keys)
    return mapping.get(value) if isinstance(value, str) else None


def enum(
    vehicle: Vehicle | None, keys: tuple[str, ...], options: list[str]
) -> str | None:
    """Enum at ``keys`` in lower case if known, else None (new API values)."""
    value = path(vehicle, *keys)
    if not isinstance(value, str):
        return None
    value = value.lower()
    return value if value in options else None


def timestamp(vehicle: Vehicle | None, *keys: str) -> datetime | None:
    """ISO 8601 date-time at ``keys`` as an aware datetime, or None."""
    value = path(vehicle, *keys)
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed


def section_supported(response: VehicleResponse | None, section: str) -> bool:
    """Whether the vehicle reports the section, now or after an outage.

    Without ``include`` the API returns all parts the vehicle supports and
    leaves out the rest without an error. A supported part that is down at
    the moment is missing too, but comes with a ``*_UNAVAILABLE`` or
    ``*_DISABLED`` error, so its entities are still created.
    """
    if response is None:
        return False
    if isinstance(response.vehicle.get(section), dict):
        return True
    prefix = SECTION_ERRORS.get(section)
    for error in response.errors:
        kind = error.get("type") if isinstance(error, dict) else None
        if prefix and kind in (f"{prefix}_UNAVAILABLE", f"{prefix}_DISABLED"):
            return True
    return False


@dataclass(frozen=True, kw_only=True)
class SkodaEntityDescription(EntityDescription):
    """Entity description with the GET section the entity reads."""

    section: str


class SkodaEntity(CoordinatorEntity[SkodaDataUpdateCoordinator]):
    """Entity of one vehicle; unique_id is ``<VIN>_<entity key>``."""

    _attr_has_entity_name = True
    entity_description: SkodaEntityDescription

    def __init__(
        self,
        coordinator: SkodaDataUpdateCoordinator,
        description: SkodaEntityDescription,
    ) -> None:
        """Bind the entity to the vehicle device."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.vin}_{description.key}"
        # Name and manufacturer are set at setup from the chosen name source.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.vin)})

    @property
    def vehicle(self) -> Vehicle | None:
        """Vehicle part of the last GET."""
        data = self.coordinator.data
        return data.vehicle if data is not None else None


@dataclass(frozen=True, kw_only=True)
class SkodaControlDescription(SkodaEntityDescription):
    """Entity that sends one kind of command (operation name from Swagger)."""

    operation: str
    group: CommandGroup


class SkodaControlEntity(SkodaEntity):
    """Entity that sends commands; shows a command that waits for confirmation.

    The state always comes from the last GET (no optimistic state). An
    accepted command is shown as the ``pending`` attribute and an hourglass
    icon until a GET or the charger meter confirms it, or it times out.
    """

    entity_description: SkodaControlDescription
    # A command entity sends at most one request at a time.
    _pending_setting: str | None = None

    @property
    def available(self) -> bool:
        """Unavailable (grey) while the API switch is off."""
        return super().available and self.coordinator.api_enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """``pending``: the value a sent command waits for, else None."""
        pending = self.coordinator.pending_command(self.entity_description.group)
        if pending is None or pending.setting != self._pending_setting:
            return {"pending": None}
        return {"pending": pending_value(pending.expected)}

    @property
    def icon(self) -> str | None:
        """Hourglass while this entity's command waits; else from icons.json.

        A state-independent override: icon translations cannot follow an
        attribute, so the pending icon is the only dynamic one.
        """
        if self.available and self._is_pending:
            return PENDING_ICON
        return super().icon

    @property
    def _is_pending(self) -> bool:
        """Whether a command of this entity waits for confirmation."""
        return self.extra_state_attributes.get("pending") is not None

    async def async_send(self, build: Callable[[Vehicle], Command]) -> None:
        """Send one command through the coordinator (all checks there)."""
        await self.coordinator.async_send_command(build)


def profile_value(vehicle: Vehicle | None, setting: str) -> Any:
    """Return a setting of the location profile from the last GET, or None."""
    if not vehicle:
        return None
    try:
        profile = current_profile(vehicle)
    except CommandError:
        return None
    return profile_values(profile).get(setting)


def pending_value(value: Any) -> Any:
    """Return the expected value of a pending command as a state attribute."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return value.lower() if isinstance(value, str) and value.isupper() else value


def operation_supported(response: VehicleResponse | None, operation: str) -> bool:
    """Whether the last GET lists the operation (or lists none at all).

    ``operations`` is left out when it is unavailable; then the entity is
    created and the command check decides when it is used.
    """
    if response is None:
        return False
    if not isinstance(response.vehicle.get("operations"), list):
        return True
    return operation in operation_names(response.vehicle)


def supported_controls[D: SkodaControlDescription](
    coordinator: SkodaDataUpdateCoordinator, descriptions: tuple[D, ...]
) -> list[D]:
    """Control descriptions whose section and operation the vehicle reports."""
    return [
        description
        for description in supported_descriptions(coordinator, descriptions)
        if operation_supported(coordinator.data, description.operation)
    ]


def supported_descriptions[D: SkodaEntityDescription](
    coordinator: SkodaDataUpdateCoordinator, descriptions: tuple[D, ...]
) -> list[D]:
    """Descriptions whose section the vehicle reports."""
    return [
        description
        for description in descriptions
        if section_supported(coordinator.data, description.section)
    ]


type ValueFn[T] = Callable[[Vehicle | None], T]
