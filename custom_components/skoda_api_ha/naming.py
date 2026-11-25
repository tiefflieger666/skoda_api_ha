"""Device name from the source chosen by the user."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DEFAULT_DEVICE_NAME, NAME_SOURCE_LICENSE_PLATE, NAME_SOURCE_VIN

if TYPE_CHECKING:
    from .api import VehicleResponse


def device_name(source: str, response: VehicleResponse | None, vin: str) -> str:
    """Return the device name.

    ``vin`` always works. A missing license plate falls back to the vehicle
    name, a missing vehicle name to a generic name, never to the VIN or the
    plate, because those are private.
    """
    if source == NAME_SOURCE_VIN:
        return vin
    plate = response.license_plate if response else None
    name = response.name if response else None
    if source == NAME_SOURCE_LICENSE_PLATE and plate:
        return plate
    return name or DEFAULT_DEVICE_NAME
