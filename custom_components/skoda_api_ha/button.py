"""Buttons: start and stop charging, refresh now."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory

from . import commands
from .commands import CommandGroup
from .entity import (
    SkodaControlDescription,
    SkodaControlEntity,
    SkodaEntity,
    SkodaEntityDescription,
    supported_controls,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .data import SkodaConfigEntry
    from .entity import Vehicle

# Commands are serialized by the coordinator; one press at a time per entity.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class SkodaCommandButtonDescription(ButtonEntityDescription, SkodaControlDescription):
    """Button that sends one command."""

    build: Callable[[Vehicle], commands.Command]


@dataclass(frozen=True, kw_only=True)
class SkodaRefreshButtonDescription(ButtonEntityDescription, SkodaEntityDescription):
    """Refresh button (one GET)."""


COMMAND_BUTTONS: tuple[SkodaCommandButtonDescription, ...] = (
    SkodaCommandButtonDescription(
        key="start_charging",
        translation_key="start_charging",
        section="charging",
        # The Charging switch is the main control; the buttons duplicate it.
        entity_registry_enabled_default=False,
        operation=commands.OP_START_CHARGING,
        group=CommandGroup.CHARGING,
        build=commands.charging_start,
    ),
    SkodaCommandButtonDescription(
        key="stop_charging",
        translation_key="stop_charging",
        section="charging",
        # The Charging switch is the main control; the buttons duplicate it.
        entity_registry_enabled_default=False,
        operation=commands.OP_STOP_CHARGING,
        group=CommandGroup.CHARGING,
        build=commands.charging_stop,
    ),
)

CLEAR_PENDING_BUTTON = SkodaRefreshButtonDescription(
    key="clear_pending",
    translation_key="clear_pending",
    section="vehicle",
    entity_category=EntityCategory.CONFIG,
)

REFRESH_BUTTON = SkodaRefreshButtonDescription(
    key="refresh", translation_key="refresh", section="vehicle"
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: SkodaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the refresh button and the charging buttons the vehicle supports."""
    coordinator = entry.runtime_data.coordinator
    entities: list[ButtonEntity] = [
        SkodaRefreshButton(coordinator, REFRESH_BUTTON),
        SkodaClearPendingButton(coordinator, CLEAR_PENDING_BUTTON),
    ]
    entities.extend(
        SkodaCommandButton(coordinator, description)
        for description in supported_controls(coordinator, COMMAND_BUTTONS)
    )
    async_add_entities(entities)


class SkodaCommandButton(SkodaControlEntity, ButtonEntity):
    """Sends one command per press (202 = accepted, not yet executed)."""

    entity_description: SkodaCommandButtonDescription

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """``pending``: start or stop while a charging command waits."""
        pending = self.coordinator.pending_command(CommandGroup.CHARGING)
        if pending is None:
            return {"pending": None}
        return {"pending": "start" if pending.expected else "stop"}

    @property
    def available(self) -> bool:
        """Start: cable plugged in and not charging. Stop: charging.

        Stop also while a charging command waits (to undo a start). Unknown
        values from the API do not block (the command checks decide).
        """
        if not super().available:
            return False
        vehicle = self.vehicle or {}
        pending = self.coordinator.pending_command(CommandGroup.CHARGING)
        charging = commands.charging_state(vehicle) == commands.CHARGING_STATE
        if self.entity_description.key == "start_charging":
            return commands.cable_connected(vehicle) is not False and not charging
        return charging or pending is not None

    @property
    def _is_pending(self) -> bool:
        """Hourglass only on the button whose command waits."""
        pending = self.coordinator.pending_command(CommandGroup.CHARGING)
        if pending is None:
            return False
        is_start = self.entity_description.key == "start_charging"
        return bool(pending.expected) == is_start

    async def async_press(self) -> None:
        """Send the command."""
        await self.async_send(self.entity_description.build)


class SkodaRefreshButton(SkodaEntity, ButtonEntity):
    """One GET now: the regular one if due, else one from the reserve."""

    entity_description: SkodaRefreshButtonDescription

    @property
    def available(self) -> bool:
        """Usable after a failed update (when it is needed); not with the API off."""
        return self.coordinator.api_enabled

    async def async_press(self) -> None:
        """Refresh the vehicle data."""
        await self.coordinator.async_manual_refresh()


class SkodaClearPendingButton(SkodaEntity, ButtonEntity):
    """Forget all commands waiting for confirmation (0 requests)."""

    entity_description: SkodaRefreshButtonDescription

    @property
    def available(self) -> bool:
        """Always usable; it does not depend on the API."""
        return True

    async def async_press(self) -> None:
        """Clear the pending commands."""
        await self.coordinator.async_clear_pending()
