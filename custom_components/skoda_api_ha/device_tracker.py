"""Last parking position of the vehicle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import callback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import (
    SkodaEntity,
    SkodaEntityDescription,
    Vehicle,
    number,
    path,
    supported_descriptions,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .data import SkodaConfigEntry

PARALLEL_UPDATES = 0

PARKING_POSITION = SkodaEntityDescription(
    key="parking_position",
    translation_key="parking_position",
    section="parkingPosition",
)


def parked_at(vehicle: Vehicle | None) -> tuple[float, float] | None:
    """GPS position if the vehicle is parked (the API sends none in motion)."""
    if path(vehicle, "parkingPosition", "state") != "PARKED":
        return None
    latitude = number(vehicle, "parkingPosition", "gpsCoordinates", "latitude")
    longitude = number(vehicle, "parkingPosition", "gpsCoordinates", "longitude")
    if latitude is None or longitude is None:
        return None
    return latitude, longitude


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: SkodaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the tracker if the vehicle reports its parking position."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SkodaParkingTracker(coordinator, description)
        for description in supported_descriptions(coordinator, (PARKING_POSITION,))
    )


class SkodaParkingTracker(SkodaEntity, TrackerEntity, RestoreEntity):
    """Where the vehicle was parked last.

    While driving the API sends no position, so the tracker keeps the last
    parking position (also across a restart) until the vehicle parks again.
    """

    _position: tuple[float, float] | None = None

    async def async_added_to_hass(self) -> None:
        """Take the position from the last GET or from before the restart."""
        await super().async_added_to_hass()
        self._position = parked_at(self.vehicle)
        if self._position is None and (last := await self.async_get_last_state()):
            latitude = last.attributes.get(ATTR_LATITUDE)
            longitude = last.attributes.get(ATTR_LONGITUDE)
            if isinstance(latitude, (int, float)) and isinstance(
                longitude, (int, float)
            ):
                self._position = (latitude, longitude)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Move only when the vehicle reports a new parking position."""
        if (position := parked_at(self.vehicle)) is not None:
            self._position = position
        super()._handle_coordinator_update()

    @property
    def latitude(self) -> float | None:
        """Latitude of the last parking position."""
        return self._position[0] if self._position else None

    @property
    def longitude(self) -> float | None:
        """Longitude of the last parking position."""
        return self._position[1] if self._position else None
