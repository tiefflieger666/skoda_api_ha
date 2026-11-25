"""Charge mode (PUT /charging/mode)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity

from . import commands
from .commands import CommandGroup
from .entity import (
    SkodaControlDescription,
    SkodaControlEntity,
    enum,
    supported_controls,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .data import SkodaConfigEntry

PARALLEL_UPDATES = 1

# Charge modes of the Swagger spec (translated); the vehicle offers a subset.
CHARGE_MODES = [
    "manual",
    "timer",
    "timer_charging_with_climatisation",
    "preferred_charging_times",
    "only_own_current",
    "immediate_discharging",
    "home_storage_charging",
]

CHARGE_MODE = SkodaControlDescription(
    key="charge_mode",
    translation_key="charge_mode",
    section="charging",
    operation=commands.OP_CHARGE_MODE,
    group=CommandGroup.CHARGE_MODE,
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: SkodaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the charge mode select if the vehicle supports it."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SkodaChargeModeSelect(coordinator, description)
        for description in supported_controls(coordinator, (CHARGE_MODE,))
    )


class SkodaChargeModeSelect(SkodaControlEntity, SelectEntity):
    """Charge mode; only the modes the vehicle lists are offered."""

    @property
    def options(self) -> list[str]:
        """Modes of ``availableChargeModes`` that have a translation."""
        available = {
            mode.lower() for mode in commands.available_charge_modes(self.vehicle or {})
        }
        current = self.current_option
        return [mode for mode in CHARGE_MODES if mode in available or mode == current]

    @property
    def current_option(self) -> str | None:
        """``preferredChargeMode`` from the last GET."""
        return enum(
            self.vehicle, ("charging", "settings", "preferredChargeMode"), CHARGE_MODES
        )

    async def async_select_option(self, option: str) -> None:
        """Send the new charge mode."""
        await self.async_send(lambda v: commands.charge_mode(v, option.upper()))
