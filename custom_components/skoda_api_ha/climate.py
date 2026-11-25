"""Climate (air conditioning) of the vehicle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util import dt as dt_util

from . import commands
from .commands import CommandGroup
from .const import DOMAIN
from .entity import (
    SkodaControlDescription,
    SkodaControlEntity,
    supported_controls,
    timestamp,
)

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .data import SkodaConfigEntry
    from .entity import Vehicle

PARALLEL_UPDATES = 1

# A temperature change while the climate runs is sent only when the same value
# is set again within this time (confirmation of the request from the quota).
CONFIRM_WINDOW = timedelta(seconds=60)

CLIMATE = SkodaControlDescription(
    key="climate",
    translation_key="climate",
    section="airConditioning",
    operation=commands.OP_START_CLIMATE,
    group=CommandGroup.CLIMATE,
)

HVAC_ACTIONS = {
    "OFF": HVACAction.OFF,
    "COMPLETED": HVACAction.IDLE,
    "COOLING": HVACAction.COOLING,
    "HEATING": HVACAction.HEATING,
    "HEATING_AUXILIARY": HVACAction.HEATING,
    "VENTILATION": HVACAction.FAN,
}


@dataclass
class TemperatureMemory(ExtraStoredData):
    """Temperature set in Home Assistant and the last one sent (survives restart)."""

    desired: float | None = None
    desired_at: datetime | None = None
    sent: float | None = None
    sent_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the restore state store."""
        return {
            "desired": self.desired,
            "desired_at": self.desired_at.isoformat() if self.desired_at else None,
            "sent": self.sent,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TemperatureMemory:
        """Load; invalid values are dropped."""

        def _temp(value: Any) -> float | None:
            if isinstance(value, bool) or not isinstance(value, int | float):
                return None
            return commands.valid_temperature(value)

        def _time(value: Any) -> datetime | None:
            return timestamp({"t": value}, "t")

        return cls(
            desired=_temp(data.get("desired")),
            desired_at=_time(data.get("desired_at")),
            sent=_temp(data.get("sent")),
            sent_at=_time(data.get("sent_at")),
        )


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: SkodaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the climate entity if the vehicle supports air conditioning."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SkodaClimate(coordinator, description)
        for description in supported_controls(coordinator, (CLIMATE,))
    )


class SkodaClimate(SkodaControlEntity, ClimateEntity, RestoreEntity):
    """Start or stop the air conditioning.

    The API has no command that only sets the temperature: it travels in the
    body of START. A new temperature is sent at once with START (one
    request) after a confirmation: the first call explains the cost and the
    quota left, the same value again within 60 s sends it (design decisions
    2026-11-23). While the climate is off this starts it (the API has no
    other way). Together with mode off the value is only kept. The
    value stays kept after the vehicle reports it, because the Elroq reports
    its own stored value again after STOP.
    """

    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]  # noqa: RUF012
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = commands.TEMP_MIN
    _attr_max_temp = commands.TEMP_MAX
    _attr_target_temperature_step = commands.TEMP_STEP
    _memory: TemperatureMemory
    # Temperature waiting for confirmation and until when it may be confirmed.
    _confirm: tuple[float, datetime] | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the kept temperature."""
        await super().async_added_to_hass()
        self._memory = TemperatureMemory()
        if (extra := await self.async_get_last_extra_data()) is not None:
            self._memory = TemperatureMemory.from_dict(extra.as_dict())

    @property
    def extra_restore_state_data(self) -> TemperatureMemory:
        """Data kept across a restart."""
        return self._memory

    @property
    def _state(self) -> str | None:
        return commands.climate_state(self.vehicle) if self.vehicle else None

    @property
    def _vehicle_temperature(self) -> float | None:
        return commands.vehicle_temperature(self.vehicle) if self.vehicle else None

    @property
    def _is_pending(self) -> bool:
        """Hourglass while a climate command waits for confirmation."""
        return self.coordinator.pending_command(CommandGroup.CLIMATE) is not None

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Requested mode while START/STOP waits, else the mode from the last GET.

        Home Assistant's own split: ``hvac_mode`` is what is wanted,
        ``hvac_action`` what the vehicle does (always from the last GET). So
        after START the mode shows on at once while the action still says off
        until a GET confirms it; a command that is not confirmed falls back
        to the GET (design decision).
        """
        pending = self.coordinator.pending_command(CommandGroup.CLIMATE)
        if pending is not None:
            return HVACMode.HEAT_COOL if pending.expected else HVACMode.OFF
        state = self._state
        if state in commands.CLIMATE_ACTIVE_STATES:
            return HVACMode.HEAT_COOL
        if state in commands.CLIMATE_OFF_STATES:
            return HVACMode.OFF
        return None

    @property
    def hvac_action(self) -> HVACAction | None:
        """What the air conditioning does now."""
        return HVAC_ACTIONS.get(self._state or "")

    @property
    def target_temperature(self) -> float | None:
        """Temperature set in Home Assistant, else the vehicle's value."""
        if self._memory.desired is not None:
            return self._memory.desired
        return self._vehicle_temperature

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Pending command, both temperatures and whether they agree."""
        pending = self.coordinator.pending_command(CommandGroup.CLIMATE)
        captured = (
            commands.captured_at(self.vehicle, "airConditioning")
            if self.vehicle
            else None
        )
        if pending is None:
            pending_state: Any = None
        elif pending.key == commands.KEY_CLIMATE_TEMPERATURE:
            pending_state = pending.expected
        else:
            pending_state = "on" if pending.expected else "off"
        return {
            "pending": pending_state,
            "vehicle_target_temperature": self._vehicle_temperature,
            "ha_target_temperature": self._memory.desired,
            "temperature_sync": commands.temperature_sync(
                desired=self._memory.desired,
                desired_at=self._memory.desired_at,
                sent=self._memory.sent,
                sent_at=self._memory.sent_at,
                vehicle_value=self._vehicle_temperature,
                captured=captured,
                active=self._state in commands.CLIMATE_ACTIVE_STATES,
            ),
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Start (with the kept temperature) or stop: one request."""
        if hvac_mode == HVACMode.OFF:
            await self.async_send(commands.climate_stop)
            return
        sent: list[float] = []

        def build(vehicle: Vehicle) -> commands.Command:
            command = commands.climate_start(vehicle, self._memory.desired)
            target = (command.body or {}).get("targetTemperature")
            if isinstance(target, dict):
                sent.append(target["value"])
            return command

        await self.async_send(build)
        if sent:
            self._memory.sent = sent[0]
            self._memory.sent_at = dt_util.utcnow()
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Start the air conditioning."""
        await self.async_set_hvac_mode(HVACMode.HEAT_COOL)

    async def async_turn_off(self) -> None:
        """Stop the air conditioning."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Keep the temperature (climate off) or send it now (climate running)."""
        mode = kwargs.get(ATTR_HVAC_MODE)
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            value = commands.valid_temperature(temperature)
            if value is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="command_invalid_value",
                )
            if mode != HVACMode.OFF:
                if not commands.same_temperature(value, self.target_temperature):
                    await self._async_send_temperature(value)
                    return
            else:
                self._memory.desired = value
                self._memory.desired_at = dt_util.utcnow()
                self.async_write_ha_state()
        if mode is not None:
            await self.async_set_hvac_mode(mode)

    async def _async_send_temperature(self, value: float) -> None:
        """Send a new temperature to the running climate (one request).

        The first call only checks that the command may be sent and asks for
        a confirmation (with the quota left); setting the same value again
        within 60 s sends it. Refused at once when the quota or a pending
        command does not allow it.
        """

        def build(vehicle: Vehicle) -> commands.Command:
            return commands.climate_temperature(vehicle, value)

        now = dt_util.utcnow()
        confirm = self._confirm
        if (
            confirm is None
            or not commands.same_temperature(confirm[0], value)
            or now > confirm[1]
        ):
            self._confirm = None
            self.coordinator.check_command(build)
            self._confirm = (value, now + CONFIRM_WINDOW)
            running = self._state in commands.CLIMATE_ACTIVE_STATES
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="climate_temperature_confirm"
                if running
                else "climate_temperature_confirm_start",
                translation_placeholders={
                    "temperature": f"{value:g}",
                    "seconds": str(int(CONFIRM_WINDOW.total_seconds())),
                    **self.coordinator.quota_placeholders(now),
                },
            )
        self._confirm = None
        await self.async_send(build)
        sent_at = dt_util.utcnow()
        self._memory.desired = value
        self._memory.desired_at = sent_at
        self._memory.sent = value
        self._memory.sent_at = sent_at
        self.async_write_ha_state()
