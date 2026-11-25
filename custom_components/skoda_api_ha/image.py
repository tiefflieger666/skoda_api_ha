"""Picture of the vehicle (``renderUrl`` of the regular GET, 0 extra requests)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.image import ImageEntity
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .entity import SkodaEntity, SkodaEntityDescription, Vehicle, path

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .api import VehicleResponse
    from .coordinator import SkodaDataUpdateCoordinator
    from .data import SkodaConfigEntry

PARALLEL_UPDATES = 0

VEHICLE_IMAGE = SkodaEntityDescription(
    key="vehicle_image",
    translation_key="vehicle_image",
    section="info",
)


def render_url(vehicle: Vehicle | None) -> str | None:
    """Image URL of the vehicle from the last GET, or None."""
    value = path(vehicle, "renderUrl")
    return value if isinstance(value, str) and value.startswith("https://") else None


def render_supported(response: VehicleResponse | None) -> bool:
    """Whether the vehicle has a picture (also while it is unavailable)."""
    if response is None:
        return False
    if render_url(response.vehicle) is not None:
        return True
    return any(
        isinstance(error, dict) and error.get("type") == "RENDER_UNAVAILABLE"
        for error in response.errors
    )


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: SkodaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the vehicle picture if the API sends one."""
    coordinator = entry.runtime_data.coordinator
    if render_supported(coordinator.data):
        async_add_entities([SkodaVehicleImage(coordinator, VEHICLE_IMAGE)])


class SkodaVehicleImage(SkodaEntity, ImageEntity):
    """The picture behind ``renderUrl``.

    The URL comes with every regular GET (no extra request to the Škoda API).
    Home Assistant loads the picture from that URL (without the API key)
    when it is shown, and again only when the URL changes.
    """

    def __init__(
        self,
        coordinator: SkodaDataUpdateCoordinator,
        description: SkodaEntityDescription,
    ) -> None:
        """Bind to the vehicle and set up the image proxy."""
        super().__init__(coordinator, description)
        ImageEntity.__init__(self, coordinator.hass)
        self._url = render_url(self.vehicle)
        self._attr_image_last_updated = dt_util.utcnow() if self._url else None

    @property
    def image_url(self) -> str | None:
        """URL of the picture from the last GET that had one."""
        return self._url

    @callback
    def _handle_coordinator_update(self) -> None:
        """Take a new URL; keep the last one while the API sends none."""
        url = render_url(self.vehicle)
        if url is not None and url != self._url:
            self._url = url
            self._cached_image = None
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()
