"""Runtime data types for the Škoda API integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .api import SkodaApiClient
    from .coordinator import SkodaDataUpdateCoordinator


type SkodaConfigEntry = ConfigEntry[SkodaRuntimeData]


@dataclass
class SkodaRuntimeData:
    """Objects kept in ``entry.runtime_data``."""

    client: SkodaApiClient
    coordinator: SkodaDataUpdateCoordinator
