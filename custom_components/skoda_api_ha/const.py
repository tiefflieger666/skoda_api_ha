"""Constants for the Škoda API integration."""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "skoda_api_ha"
LOGGER = logging.getLogger(__package__)

CONF_VIN: Final = "vin"
# A new API key entered in the reconfigure flow. It is tried on the next
# regular GET (no extra request) and replaces the active key only if it
# answers for the same VIN and is valid longer (key change rule).
CONF_API_KEY_CANDIDATE: Final = "api_key_candidate"
ISSUE_KEY_REJECTED: Final = "key_candidate_rejected"
ISSUE_NO_COMMAND_QUOTA: Final = "no_command_quota"

# Device name source (config flow and options flow).
CONF_DEVICE_NAME_SOURCE: Final = "device_name_source"
NAME_SOURCE_VEHICLE_NAME: Final = "vehicle_name"
NAME_SOURCE_LICENSE_PLATE: Final = "license_plate"
NAME_SOURCE_VIN: Final = "vin"
NAME_SOURCES: Final = (
    NAME_SOURCE_VEHICLE_NAME,
    NAME_SOURCE_LICENSE_PLATE,
    NAME_SOURCE_VIN,
)
DEFAULT_NAME_SOURCE: Final = NAME_SOURCE_VEHICLE_NAME

# Polling chosen by the user (options flow).
CONF_POLLS_PER_HOUR: Final = "polls_per_hour"
CONF_CONFIRM_GET: Final = "confirm_get"
DEFAULT_POLLS_PER_HOUR: Final = 12
# 0 = no regular GETs (only manual refreshes and commands). 20 = the whole
# hourly quota of the vehicle: nothing is left for commands (design decision).
MIN_POLLS_PER_HOUR: Final = 0
MAX_POLLS_PER_HOUR: Final = 20
# All API traffic on or off (switch entity, kept in the options).
CONF_API_ENABLED: Final = "api_enabled"
DEFAULT_API_ENABLED: Final = True
DEFAULT_CONFIRM_GET: Final = False
# Optional: a sensor with the current power of the user's own charger. Not
# every user has one; without it, commands are confirmed by the API alone.
CONF_CHARGER_POWER_ENTITY: Final = "charger_power_entity"
# Thresholds of that sensor (every charger differs): at or above "active" the
# charger delivers power (START confirmed), at or below "quiet" it is idle
# (STOP confirmed). The defaults are proven values from real chargers.
CONF_CHARGER_ACTIVE_W: Final = "charger_active_w"
CONF_CHARGER_QUIET_W: Final = "charger_quiet_w"
DEFAULT_CHARGER_ACTIVE_W: Final = 500
DEFAULT_CHARGER_QUIET_W: Final = 10
MAX_CHARGER_ACTIVE_W: Final = 50_000
MAX_CHARGER_QUIET_W: Final = 5_000

# Stored vehicle identity from the last successful GET.
DATA_VEHICLE_NAME: Final = "vehicle_name"
DATA_LICENSE_PLATE: Final = "license_plate"

MANUFACTURER: Final = "Škoda"
DEFAULT_DEVICE_NAME: Final = "Škoda"
