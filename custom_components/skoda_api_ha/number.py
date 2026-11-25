"""Charge limit and location profile percentages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE

from . import commands
from .commands import CommandGroup
from .entity import (
    SkodaControlDescription,
    SkodaControlEntity,
    ValueFn,
    Vehicle,
    number,
    profile_value,
    supported_controls,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import SkodaDataUpdateCoordinator
    from .data import SkodaConfigEntry

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class SkodaNumberDescription(NumberEntityDescription, SkodaControlDescription):
    """Number that sends one command with the new value."""

    value_fn: ValueFn[float | None]
    build: Callable[[Vehicle, float], commands.Command]
    setting: str | None = None


def _profile(setting: str, low: int, high: int, step: int) -> SkodaNumberDescription:
    return SkodaNumberDescription(
        key=f"profile_{setting}",
        translation_key=f"profile_{setting}",
        section="chargingProfiles",
        operation=commands.OP_PROFILE,
        group=CommandGroup.PROFILE,
        setting=setting,
        native_min_value=low,
        native_max_value=high,
        native_step=step,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        value_fn=lambda v: profile_value(v, setting),
        build=lambda v, value: commands.profile_setting(v, setting, value),
    )


NUMBERS: tuple[SkodaNumberDescription, ...] = (
    SkodaNumberDescription(
        key="charge_limit",
        translation_key="charge_limit",
        section="charging",
        operation=commands.OP_CHARGING_LIMIT,
        group=CommandGroup.CHARGING_LIMIT,
        native_min_value=commands.LIMIT_MIN,
        native_max_value=commands.LIMIT_MAX,
        native_step=commands.LIMIT_STEP,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        value_fn=lambda v: number(
            v, "charging", "settings", "targetStateOfChargeInPercent"
        ),
        build=commands.charging_limit,
    ),
    _profile(
        "target_soc",
        commands.TARGET_SOC_MIN,
        commands.TARGET_SOC_MAX,
        commands.TARGET_SOC_STEP,
    ),
    _profile(
        "min_soc", commands.MIN_SOC_MIN, commands.MIN_SOC_MAX, commands.MIN_SOC_STEP
    ),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: SkodaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the numbers the vehicle supports."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SkodaNumber(coordinator, description)
        for description in supported_controls(coordinator, NUMBERS)
    )


class SkodaNumber(SkodaControlEntity, NumberEntity):
    """Value from the last GET; a change sends one PUT."""

    entity_description: SkodaNumberDescription

    def __init__(
        self,
        coordinator: SkodaDataUpdateCoordinator,
        description: SkodaNumberDescription,
    ) -> None:
        """Remember which profile setting a pending command must match."""
        super().__init__(coordinator, description)
        self._pending_setting = description.setting

    @property
    def native_value(self) -> float | None:
        """Value from the last GET."""
        return self.entity_description.value_fn(self.vehicle)

    async def async_set_native_value(self, value: float) -> None:
        """Send the new value."""
        build = self.entity_description.build
        await self.async_send(lambda v: build(v, value))
