"""Location profile switches (PUT of the whole profile)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory

from . import commands
from .commands import CommandGroup
from .const import CONF_API_ENABLED
from .entity import (
    SkodaControlDescription,
    SkodaControlEntity,
    SkodaEntity,
    SkodaEntityDescription,
    profile_value,
    supported_controls,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import SkodaDataUpdateCoordinator
    from .data import SkodaConfigEntry

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class SkodaSwitchDescription(SwitchEntityDescription, SkodaControlDescription):
    """Profile setting shown as a switch; ``on``/``off`` are the API values."""

    setting: str
    on: Any = True
    off: Any = False


def _description(
    key: str, setting: str, *, on: Any = True, off: Any = False
) -> SkodaSwitchDescription:
    return SkodaSwitchDescription(
        key=key,
        translation_key=key,
        section="chargingProfiles",
        operation=commands.OP_PROFILE,
        group=CommandGroup.PROFILE,
        setting=setting,
        on=on,
        off=off,
    )


API_SWITCH = SkodaEntityDescription(
    key="api_access",
    translation_key="api_access",
    section="vehicle",
    entity_category=EntityCategory.CONFIG,
)

CHARGING_SWITCH = SkodaControlDescription(
    key="charging",
    translation_key="charging",
    section="charging",
    operation=commands.OP_START_CHARGING,
    group=CommandGroup.CHARGING,
)

SWITCHES: tuple[SkodaSwitchDescription, ...] = (
    _description("reduced_current", "max_current", on="REDUCED", off="MAXIMUM"),
    _description("auto_unlock_plug", "auto_unlock", on="PERMANENT", off="OFF"),
    *(
        _description(f"charging_time_{n}", f"time_{n}_enabled")
        for n in range(1, commands.PROFILE_SLOTS + 1)
    ),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: SkodaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the profile switches the vehicle supports."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SwitchEntity] = [SkodaApiSwitch(coordinator, API_SWITCH)]
    entities.extend(
        SkodaChargingSwitch(coordinator, description)
        for description in supported_controls(coordinator, (CHARGING_SWITCH,))
    )
    entities.extend(
        SkodaProfileSwitch(coordinator, description)
        for description in supported_controls(coordinator, SWITCHES)
    )
    async_add_entities(entities)


class SkodaApiSwitch(SkodaEntity, SwitchEntity):
    """All API traffic of this vehicle on or off (0 requests to switch).

    Off: no regular GET, no refresh, no command, no confirmation GET. The
    sensors keep the last data; the controls become unavailable. On: the
    next regular GET follows the quota plan (no extra request).
    """

    entity_description: SkodaEntityDescription

    @property
    def available(self) -> bool:
        """Always usable; it does not depend on the API."""
        return True

    @property
    def is_on(self) -> bool:
        """Whether the integration talks to the API."""
        return self.coordinator.api_enabled

    def _set(self, enabled: bool) -> None:  # noqa: FBT001
        entry = self.coordinator.config_entry
        self.hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_API_ENABLED: enabled}
        )

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Allow API requests again."""
        self._set(True)  # noqa: FBT003

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Stop all API requests."""
        self._set(False)  # noqa: FBT003


class SkodaProfileSwitch(SkodaControlEntity, SwitchEntity):
    """State from the last GET; switching sends the whole location profile."""

    entity_description: SkodaSwitchDescription

    def __init__(
        self,
        coordinator: SkodaDataUpdateCoordinator,
        description: SkodaSwitchDescription,
    ) -> None:
        """Remember which profile setting a pending command must match."""
        super().__init__(coordinator, description)
        self._pending_setting = description.setting

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """``pending``: on or off while a change of this setting waits."""
        pending = self.coordinator.pending_command(CommandGroup.PROFILE)
        if pending is None or pending.setting != self.entity_description.setting:
            return {"pending": None}
        return {
            "pending": "on" if pending.expected == self.entity_description.on else "off"
        }

    @property
    def is_on(self) -> bool | None:
        """On, off or unknown from the last GET."""
        value = profile_value(self.vehicle, self.entity_description.setting)
        if value is None:
            return None
        return value == self.entity_description.on

    async def _async_set(self, value: Any) -> None:
        setting = self.entity_description.setting
        await self.async_send(lambda v: commands.profile_setting(v, setting, value))

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn the setting on."""
        await self._async_set(self.entity_description.on)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn the setting off."""
        await self._async_set(self.entity_description.off)


class SkodaChargingSwitch(SkodaControlEntity, SwitchEntity):
    """Charging on or off in one control (START or STOP, one request).

    Unavailable while the cable is unplugged. While a command waits for
    confirmation the switch shows the requested state with an hourglass;
    a GET then decides (not confirmed: back to the vehicle's state).
    """

    @property
    def available(self) -> bool:
        """Only with the cable plugged in (unknown does not block)."""
        return (
            super().available
            and commands.cable_connected(self.vehicle or {}) is not False
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """``pending``: on or off while a charging command waits."""
        pending = self.coordinator.pending_command(CommandGroup.CHARGING)
        if pending is None:
            return {"pending": None}
        return {"pending": "on" if pending.expected else "off"}

    @property
    def is_on(self) -> bool | None:
        """Requested state while a command waits, else charging from the GET."""
        pending = self.coordinator.pending_command(CommandGroup.CHARGING)
        if pending is not None:
            return bool(pending.expected)
        state = commands.charging_state(self.vehicle or {})
        if state == commands.CHARGING_STATE:
            return True
        if state in commands.STOPPED_STATES:
            return False
        return None

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Start charging."""
        await self.async_send(commands.charging_start)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Stop charging."""
        await self.async_send(commands.charging_stop)
