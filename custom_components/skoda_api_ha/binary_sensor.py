"""Binary sensors from ``GET /api/v1/vehicles/{vin}`` (Swagger schema names)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)

from .entity import (
    SkodaEntity,
    SkodaEntityDescription,
    ValueFn,
    flag,
    mapped,
    supported_descriptions,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .data import SkodaConfigEntry

PARALLEL_UPDATES = 0

# Device class "lock": on means unlocked.
LOCK_STATE = {"YES": False, "NO": True}
DOORS_LOCK_STATE = {"YES": False, "NO": True, "OPENED": True, "TRUNK_OPENED": True}
OPEN_STATE = {"OPEN": True, "CLOSED": False}
ON_STATE = {"ON": True, "OFF": False}


@dataclass(frozen=True, kw_only=True)
class SkodaBinarySensorEntityDescription(
    BinarySensorEntityDescription, SkodaEntityDescription
):
    """Binary sensor read from one section of the GET body."""

    value_fn: ValueFn[bool | None]


def _status(
    key: str,
    keys: tuple[str, ...],
    mapping: dict[str, bool],
    device_class: BinarySensorDeviceClass,
) -> SkodaBinarySensorEntityDescription:
    return SkodaBinarySensorEntityDescription(
        key=key,
        translation_key=key,
        section="status",
        device_class=device_class,
        value_fn=lambda v: mapped(v, ("status", *keys), mapping),
    )


BINARY_SENSORS: tuple[SkodaBinarySensorEntityDescription, ...] = (
    SkodaBinarySensorEntityDescription(
        key="plug_connected",
        translation_key="plug_connected",
        section="charging",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda v: mapped(
            v,
            ("charging", "status", "plugConnectionState"),
            {"CONNECTED": True, "DISCONNECTED": False},
        ),
    ),
    SkodaBinarySensorEntityDescription(
        key="plug_lock",
        translation_key="plug_lock",
        section="charging",
        device_class=BinarySensorDeviceClass.LOCK,
        value_fn=lambda v: mapped(
            v,
            ("charging", "status", "plugLockState"),
            {"LOCKED": False, "UNLOCKED": True},
        ),
    ),
    SkodaBinarySensorEntityDescription(
        key="battery_care_mode",
        translation_key="battery_care_mode",
        section="charging",
        value_fn=lambda v: mapped(
            v,
            ("charging", "settings", "chargingCareMode"),
            {"ACTIVATED": True, "DEACTIVATED": False},
        ),
    ),
    SkodaBinarySensorEntityDescription(
        key="in_saved_location",
        translation_key="in_saved_location",
        section="charging",
        value_fn=lambda v: flag(v, "charging", "isVehicleInSavedLocation"),
    ),
    _status("locked", ("overall", "locked"), LOCK_STATE, BinarySensorDeviceClass.LOCK),
    _status(
        "doors_locked",
        ("overall", "doorsLocked"),
        DOORS_LOCK_STATE,
        BinarySensorDeviceClass.LOCK,
    ),
    _status("doors", ("overall", "doors"), OPEN_STATE, BinarySensorDeviceClass.DOOR),
    _status(
        "windows", ("overall", "windows"), OPEN_STATE, BinarySensorDeviceClass.WINDOW
    ),
    _status(
        "sunroof", ("detail", "sunroof"), OPEN_STATE, BinarySensorDeviceClass.WINDOW
    ),
    _status("trunk", ("detail", "trunk"), OPEN_STATE, BinarySensorDeviceClass.OPENING),
    _status(
        "bonnet", ("detail", "bonnet"), OPEN_STATE, BinarySensorDeviceClass.OPENING
    ),
    _status("lights", ("overall", "lights"), ON_STATE, BinarySensorDeviceClass.LIGHT),
    SkodaBinarySensorEntityDescription(
        key="in_motion",
        translation_key="in_motion",
        section="parkingPosition",
        device_class=BinarySensorDeviceClass.MOVING,
        value_fn=lambda v: mapped(
            v, ("parkingPosition", "state"), {"IN_MOTION": True, "PARKED": False}
        ),
    ),
    SkodaBinarySensorEntityDescription(
        key="window_heating_front",
        translation_key="window_heating_front",
        section="airConditioning",
        value_fn=lambda v: mapped(
            v, ("airConditioning", "windowHeating", "front"), ON_STATE
        ),
    ),
    SkodaBinarySensorEntityDescription(
        key="window_heating_rear",
        translation_key="window_heating_rear",
        section="airConditioning",
        value_fn=lambda v: mapped(
            v, ("airConditioning", "windowHeating", "rear"), ON_STATE
        ),
    ),
    SkodaBinarySensorEntityDescription(
        key="climate_at_unlock",
        translation_key="climate_at_unlock",
        section="airConditioning",
        value_fn=lambda v: flag(v, "airConditioning", "airConditioningAtUnlock"),
    ),
    SkodaBinarySensorEntityDescription(
        key="climate_without_external_power",
        translation_key="climate_without_external_power",
        section="airConditioning",
        value_fn=lambda v: flag(
            v, "airConditioning", "airConditioningWithoutExternalPower"
        ),
    ),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: SkodaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensors of the sections the vehicle reports."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SkodaBinarySensor(coordinator, description)
        for description in supported_descriptions(coordinator, BINARY_SENSORS)
    )


class SkodaBinarySensor(SkodaEntity, BinarySensorEntity):
    """On/off from the last GET; UNKNOWN or a missing field is unknown."""

    entity_description: SkodaBinarySensorEntityDescription

    @property
    def is_on(self) -> bool | None:
        """State from the last GET."""
        return self.entity_description.value_fn(self.vehicle)
