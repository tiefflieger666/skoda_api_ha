"""Start and end of the preferred charging times 1-4 (location profile)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import TYPE_CHECKING

from homeassistant.components.time import TimeEntity, TimeEntityDescription
from homeassistant.exceptions import ServiceValidationError

from . import commands
from .commands import CommandGroup
from .const import DOMAIN
from .entity import (
    SkodaControlDescription,
    SkodaControlEntity,
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
class SkodaTimeDescription(TimeEntityDescription, SkodaControlDescription):
    """One end of a preferred charging time."""

    slot: int
    start: bool


TIMES: tuple[SkodaTimeDescription, ...] = tuple(
    SkodaTimeDescription(
        key=f"charging_time_{n}_{end}",
        translation_key=f"charging_time_{n}_{end}",
        section="chargingProfiles",
        operation=commands.OP_PROFILE,
        group=CommandGroup.PROFILE,
        slot=n,
        start=end == "start",
    )
    for n in range(1, commands.PROFILE_SLOTS + 1)
    for end in ("start", "end")
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: SkodaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the charging time entities the vehicle supports."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SkodaChargingTime(coordinator, description)
        for description in supported_controls(coordinator, TIMES)
    )


def _parse(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


class SkodaChargingTime(SkodaControlEntity, TimeEntity):
    """Start or end of a preferred charging time; a change sends the profile."""

    entity_description: SkodaTimeDescription

    def __init__(
        self,
        coordinator: SkodaDataUpdateCoordinator,
        description: SkodaTimeDescription,
    ) -> None:
        """Remember which profile setting a pending command must match."""
        super().__init__(coordinator, description)
        self._setting = f"time_{description.slot}"
        self._pending_setting = self._setting

    def _window(self) -> list[str] | None:
        value = profile_value(self.vehicle, self._setting)
        return value.split("-") if isinstance(value, str) else None

    @property
    def native_value(self) -> time | None:
        """Start or end from the last GET."""
        window = self._window()
        if window is None:
            return None
        return _parse(window[0] if self.entity_description.start else window[1])

    async def async_set_value(self, value: time) -> None:
        """Send the window with the new start or end (the other end stays)."""
        window = self._window()
        if window is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="command_no_preferred_time",
                translation_placeholders={"slot": str(self.entity_description.slot)},
            )
        new = value.strftime("%H:%M")
        start, end = (
            (new, window[1]) if self.entity_description.start else (window[0], new)
        )
        setting = self._setting
        await self.async_send(
            lambda v: commands.profile_setting(v, setting, f"{start}-{end}")
        )
