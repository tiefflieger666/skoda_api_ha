"""Sensors from ``GET /api/v1/vehicles/{vin}`` (Swagger schema names)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfLength,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTime,
)

from .commands import CommandGroup
from .const import CONF_API_KEY_CANDIDATE
from .entity import (
    SkodaEntity,
    SkodaEntityDescription,
    ValueFn,
    enum,
    number,
    path,
    supported_descriptions,
    timestamp,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import SkodaDataUpdateCoordinator
    from .data import SkodaConfigEntry

# Read-only: the coordinator fetches, entities only read its data.
PARALLEL_UPDATES = 0

# ChargingStatus.state; a missing state stays unknown (never "disconnected").
CHARGING_STATES = [
    "connect_cable",
    "charging",
    "conserving",
    "ready_for_charging",
    "discharging",
    "charging_interrupted",
]
CHARGE_TYPES = ["ac", "dc", "off"]
# AirConditioning.state without UNKNOWN and UNSUPPORTED (shown as unknown).
CLIMATE_STATES = [
    "off",
    "cooling",
    "heating",
    "heating_auxiliary",
    "ventilation",
    "completed",
]

type StateValue = str | int | float | datetime | None


@dataclass(frozen=True, kw_only=True)
class SkodaSensorEntityDescription(SensorEntityDescription, SkodaEntityDescription):
    """Sensor read from one section of the GET body."""

    value_fn: ValueFn[StateValue]


def _text(*keys: str) -> ValueFn[StateValue]:
    def value(vehicle: dict | None) -> str | None:
        text = path(vehicle, *keys)
        return text.strip() or None if isinstance(text, str) else None

    return value


def _captured(section: str) -> SkodaSensorEntityDescription:
    """Diagnostic: when the vehicle captured the data of one section."""
    key = {
        "status": "status",
        "odometer": "odometer",
        "parkingPosition": "parking_position",
        "airConditioning": "climate",
        "charging": "charging",
        "chargingProfiles": "charging_profiles",
    }[section]
    return SkodaSensorEntityDescription(
        key=f"{key}_captured_at",
        translation_key=f"{key}_captured_at",
        section=section,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda v: timestamp(v, section, "carCapturedTimestamp"),
    )


SENSORS: tuple[SkodaSensorEntityDescription, ...] = (
    # No translation_key: Home Assistant names the battery sensor itself.
    SkodaSensorEntityDescription(
        key="battery_level",
        section="charging",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda v: number(
            v, "charging", "status", "battery", "stateOfChargeInPercent"
        ),
    ),
    SkodaSensorEntityDescription(
        key="range",
        translation_key="range",
        section="charging",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda v: number(
            v, "charging", "status", "battery", "remainingCruisingRangeInMeters"
        ),
    ),
    SkodaSensorEntityDescription(
        key="odometer",
        translation_key="odometer",
        section="odometer",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda v: number(v, "odometer", "mileageInKm"),
    ),
    SkodaSensorEntityDescription(
        key="charging_state",
        translation_key="charging_state",
        section="charging",
        device_class=SensorDeviceClass.ENUM,
        options=CHARGING_STATES,
        value_fn=lambda v: enum(v, ("charging", "status", "state"), CHARGING_STATES),
    ),
    SkodaSensorEntityDescription(
        key="charge_type",
        translation_key="charge_type",
        section="charging",
        device_class=SensorDeviceClass.ENUM,
        options=CHARGE_TYPES,
        value_fn=lambda v: enum(v, ("charging", "status", "chargeType"), CHARGE_TYPES),
    ),
    SkodaSensorEntityDescription(
        key="charging_power",
        translation_key="charging_power",
        section="charging",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda v: number(v, "charging", "status", "chargePowerInKw"),
    ),
    SkodaSensorEntityDescription(
        key="charging_rate",
        translation_key="charging_rate",
        section="charging",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda v: number(
            v, "charging", "status", "chargingRateInKilometersPerHour"
        ),
    ),
    SkodaSensorEntityDescription(
        key="remaining_charging_time",
        translation_key="remaining_charging_time",
        section="charging",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda v: number(
            v, "charging", "status", "remainingTimeToFullyChargedInMinutes"
        ),
    ),
    SkodaSensorEntityDescription(
        key="fully_charged_at",
        translation_key="fully_charged_at",
        section="charging",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda v: timestamp(v, "charging", "status", "fullyChargedAt"),
    ),
    SkodaSensorEntityDescription(
        key="battery_care_limit",
        translation_key="battery_care_limit",
        section="charging",
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda v: number(
            v, "charging", "settings", "batteryCareModeTargetValueInPercent"
        ),
    ),
    SkodaSensorEntityDescription(
        key="max_charge_current",
        translation_key="max_charge_current",
        section="charging",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        value_fn=lambda v: number(
            v, "charging", "settings", "maxChargeCurrentAcAmpere"
        ),
    ),
    SkodaSensorEntityDescription(
        key="climate_state",
        translation_key="climate_state",
        section="airConditioning",
        device_class=SensorDeviceClass.ENUM,
        options=CLIMATE_STATES,
        value_fn=lambda v: enum(v, ("airConditioning", "state"), CLIMATE_STATES),
    ),
    SkodaSensorEntityDescription(
        key="climate_target_reached_at",
        translation_key="climate_target_reached_at",
        section="airConditioning",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda v: timestamp(
            v, "airConditioning", "estimatedReachOfTargetTemperatureAt"
        ),
    ),
    SkodaSensorEntityDescription(
        key="charging_profile",
        translation_key="charging_profile",
        section="chargingProfiles",
        value_fn=_text("chargingProfiles", "currentVehiclePositionProfile", "name"),
    ),
    SkodaSensorEntityDescription(
        key="next_charging_time",
        translation_key="next_charging_time",
        section="chargingProfiles",
        value_fn=_text(
            "chargingProfiles", "currentVehiclePositionProfile", "nextChargingTime"
        ),
    ),
    # Private data: off until the user enables it.
    SkodaSensorEntityDescription(
        key="parking_address",
        translation_key="parking_address",
        section="parkingPosition",
        entity_registry_enabled_default=False,
        value_fn=_text("parkingPosition", "formattedAddress"),
    ),
    *(
        _captured(section)
        for section in (
            "status",
            "odometer",
            "parkingPosition",
            "airConditioning",
            "charging",
            "chargingProfiles",
        )
    ),
)


@dataclass(frozen=True, kw_only=True)
class SkodaDiagnosticSensorDescription(SensorEntityDescription, SkodaEntityDescription):
    """Sensor about the integration itself (quota, polling, key, commands)."""

    value_fn: Callable[[SkodaDataUpdateCoordinator], StateValue]
    attributes_fn: Callable[[SkodaDataUpdateCoordinator], dict[str, Any]] | None = None


COMMAND_RESULTS = [
    "accepted",
    "confirmed",
    "not_confirmed",
    "rejected",
    "uncertain",
    "cleared",
]


def _plan(attr: str) -> Callable[[SkodaDataUpdateCoordinator], Any]:
    def value(coordinator: SkodaDataUpdateCoordinator) -> Any:
        return getattr(coordinator.plan, attr) if coordinator.plan else None

    return value


def _next_update(coordinator: SkodaDataUpdateCoordinator) -> datetime | None:
    if not coordinator.regular_polling or coordinator.plan is None:
        return None
    return coordinator.plan.next_get_at


def _api_errors(coordinator: SkodaDataUpdateCoordinator) -> int | None:
    data = coordinator.data
    return None if data is None else len(data.errors)


def _api_error_types(coordinator: SkodaDataUpdateCoordinator) -> dict[str, Any]:
    data = coordinator.data
    types = [
        error.get("type")
        for error in (data.errors if data else [])
        if isinstance(error, dict) and isinstance(error.get("type"), str)
    ]
    return {"types": types}


def _key_change(coordinator: SkodaDataUpdateCoordinator) -> str:
    pending = coordinator.config_entry.data.get(CONF_API_KEY_CANDIDATE)
    return "pending" if pending else "none"


def _last_command(group: CommandGroup) -> SkodaDiagnosticSensorDescription:
    def value(coordinator: SkodaDataUpdateCoordinator) -> str | None:
        result = coordinator.commands.results.get(group)
        return result.result if result and result.result in COMMAND_RESULTS else None

    def attributes(coordinator: SkodaDataUpdateCoordinator) -> dict[str, Any]:
        result = coordinator.commands.results.get(group)
        if result is None:
            return {"command": None, "at": None, "detail": None}
        return {
            "command": result.key,
            "at": result.at.isoformat(),
            "detail": result.detail,
        }

    key = f"last_command_{LAST_COMMAND_KEYS[group]}"
    return SkodaDiagnosticSensorDescription(
        key=key,
        translation_key=key,
        section="integration",
        device_class=SensorDeviceClass.ENUM,
        options=COMMAND_RESULTS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=value,
        attributes_fn=attributes,
    )


LAST_COMMAND_KEYS = {
    CommandGroup.CHARGING: "charging",
    CommandGroup.CLIMATE: "climate",
    CommandGroup.CHARGE_MODE: "charge_mode",
    CommandGroup.CHARGING_LIMIT: "charge_limit",
    CommandGroup.PROFILE: "profile",
}


def _diagnostic(
    key: str,
    value_fn: Callable[[SkodaDataUpdateCoordinator], StateValue],
    *,
    enabled: bool = False,
    **kwargs: Any,
) -> SkodaDiagnosticSensorDescription:
    return SkodaDiagnosticSensorDescription(
        key=key,
        translation_key=key,
        section="integration",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=enabled,
        value_fn=value_fn,
        **kwargs,
    )


DIAGNOSTIC_SENSORS: tuple[SkodaDiagnosticSensorDescription, ...] = (
    _diagnostic("quota_limit", lambda c: c.quota_state.limit),
    _diagnostic(
        "quota_remaining",
        lambda c: c.quota_state.remaining,
        enabled=True,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _diagnostic(
        "quota_reset",
        lambda c: c.quota_state.reset_at,
        enabled=True,
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    _diagnostic("quota_reserve", _plan("reserve")),
    _diagnostic("regular_updates_left", _plan("available_gets")),
    _diagnostic(
        "update_interval",
        lambda c: c.plan.interval if c.plan and c.regular_polling else None,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
    ),
    _diagnostic(
        "effective_interval",
        lambda c: c.plan.effective_interval if c.plan and c.regular_polling else None,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
    ),
    _diagnostic("next_update", _next_update, device_class=SensorDeviceClass.TIMESTAMP),
    _diagnostic(
        "last_update",
        lambda c: c.last_success_at,
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    _diagnostic("api_errors", _api_errors, attributes_fn=_api_error_types),
    _diagnostic(
        "api_key_expires",
        lambda c: c.key_expires_at,
        enabled=True,
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    _diagnostic(
        "api_key_change",
        _key_change,
        device_class=SensorDeviceClass.ENUM,
        options=["none", "pending"],
    ),
    *(_last_command(group) for group in LAST_COMMAND_KEYS),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: SkodaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors of the sections the vehicle reports."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = [
        SkodaSensor(coordinator, description)
        for description in supported_descriptions(coordinator, SENSORS)
    ]
    entities.extend(
        SkodaDiagnosticSensor(coordinator, description)
        for description in DIAGNOSTIC_SENSORS
    )
    async_add_entities(entities)


class SkodaSensor(SkodaEntity, SensorEntity):
    """Sensor with a value from the last GET; a missing field is unknown."""

    entity_description: SkodaSensorEntityDescription

    @property
    def native_value(self) -> StateValue:
        """Value from the last GET."""
        return self.entity_description.value_fn(self.vehicle)


class SkodaDiagnosticSensor(SkodaEntity, SensorEntity):
    """State of the integration; stays available when the API fails."""

    entity_description: SkodaDiagnosticSensorDescription

    @property
    def available(self) -> bool:
        """Always available: most useful exactly when updates fail."""
        return True

    @property
    def native_value(self) -> StateValue:
        """Value from the coordinator."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Details (error types, last command)."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator)
