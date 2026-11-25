"""Commands of the official MyŠkoda Public API and their confirmation.

The rules come from a standalone client that was used and tested with the
same API before this integration. No Home Assistant imports.

The seven operations of the Elroq (``vehicle.operations``):

=========================  ======  =====================================
Operation                  Method  Path below ``/api/v1/vehicles/{vin}``
=========================  ======  =====================================
``startCharging``          POST    ``/charging/start``
``stopCharging``           POST    ``/charging/stop``
``startAirConditioning``   POST    ``/air-conditioning/start``
``stopAirConditioning``    POST    ``/air-conditioning/stop``
``setChargeMode``          PUT     ``/charging/mode``
``setChargingLimit``       PUT     ``/charging/limit``
``updateChargingProfile``  PUT     ``/charging-profiles/{id}``
=========================  ======  =====================================

A builder checks everything that only depends on the last GET (operation
listed, value allowed, profile complete) and returns the exact request. It
never sends anything. ``evaluate`` decides on an accepted (202) command from
a later GET: confirmed, not confirmed, or still pending.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from .quota import QuotaState, can_spend, can_spend_stop, interval_seconds

# --- Groups and keys ---------------------------------------------------------


class CommandGroup(StrEnum):
    """Commands of one group share one pending state (the newest wins)."""

    CHARGING = "charging"
    CLIMATE = "climate"
    CHARGE_MODE = "charge_mode"
    CHARGING_LIMIT = "charging_limit"
    # Charging current and all other profile settings: one PUT of the whole
    # location profile.
    PROFILE = "profile"


# Groups whose request body is built from the last GET. A new command of such
# a group waits until a GET newer than the last accepted one exists, so that
# an old snapshot never silently undoes the previous change.
SNAPSHOT_GROUPS: frozenset[CommandGroup] = frozenset(
    {CommandGroup.CHARGE_MODE, CommandGroup.CHARGING_LIMIT, CommandGroup.PROFILE}
)

OP_START_CHARGING = "startCharging"
OP_STOP_CHARGING = "stopCharging"
OP_START_CLIMATE = "startAirConditioning"
OP_STOP_CLIMATE = "stopAirConditioning"
OP_CHARGE_MODE = "setChargeMode"
OP_CHARGING_LIMIT = "setChargingLimit"
OP_PROFILE = "updateChargingProfile"

# --- Limits (Swagger and the Elroq, as read on 2026-11-22) -------------------

LIMIT_MIN, LIMIT_MAX, LIMIT_STEP = 50, 100, 10
TARGET_SOC_MIN, TARGET_SOC_MAX, TARGET_SOC_STEP = 50, 100, 10
MIN_SOC_MIN, MIN_SOC_MAX, MIN_SOC_STEP = 0, 50, 10
TEMP_MIN, TEMP_MAX, TEMP_STEP = 15.5, 30.0, 0.5
PROFILE_SLOTS = 4
CURRENT_VALUES = ("REDUCED", "MAXIMUM")
AUTO_UNLOCK_VALUES = ("PERMANENT", "OFF")
PROFILE_FIELDS = ("id", "name", "settings", "preferredChargingTimes", "timers")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# charging.status.state of the official API (Swagger 2026-11-21); older names
# are kept for compatibility.
CHARGING_STATE = "CHARGING"
STOPPED_STATES = frozenset(
    {
        "CONNECT_CABLE",
        "CONSERVING",
        "READY_FOR_CHARGING",
        "DISCHARGING",
        "CHARGING_INTERRUPTED",
        "NOT_CHARGING",
        "OFF",
        "READY",
        "COMPLETED",
        "STOPPED",
        "PAUSED",
        "CHARGED",
    }
)
CLIMATE_ACTIVE_STATES = frozenset(
    {"COOLING", "HEATING", "HEATING_AUXILIARY", "VENTILATION"}
)
CLIMATE_OFF_STATES = frozenset({"OFF", "COMPLETED"})

# A command without a deciding GET is given up after this long (at
# least two regular intervals plus a margin).
CONFIRM_TIMEOUT = timedelta(minutes=30)
CONFIRM_MARGIN = timedelta(seconds=120)


class CommandError(Exception):
    """A command was refused before sending; ``reason`` is a translation key."""

    def __init__(self, reason: str, **placeholders: str) -> None:
        """Store the reason and its translation placeholders."""
        super().__init__(reason)
        self.reason = reason
        self.placeholders = placeholders


@dataclass(frozen=True, slots=True)
class Command:
    """One request, ready to send. Never holds the VIN or the key."""

    key: str
    group: CommandGroup
    operation: str
    method: str
    path: str
    body: dict[str, Any] | None = None
    # What the next GET must show (see ``evaluate``).
    expected: Any = None
    # Profile commands: which profile and which setting.
    profile_id: int | None = None
    setting: str | None = None


# --- Reading the last GET ----------------------------------------------------


def operation_names(vehicle: dict[str, Any]) -> set[str]:
    """Operation names of ``vehicle.operations`` (strings or ``{"name": ...}``)."""
    names: set[str] = set()
    operations = vehicle.get("operations")
    for item in operations if isinstance(operations, list) else []:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("operation")
            if isinstance(name, str):
                names.add(name)
    return names


def _section(vehicle: dict[str, Any], name: str) -> dict[str, Any]:
    value = vehicle.get(name)
    return value if isinstance(value, dict) else {}


def charging_settings(vehicle: dict[str, Any]) -> dict[str, Any]:
    """``charging.settings`` or an empty dict."""
    return _section(_section(vehicle, "charging"), "settings")


def charging_state(vehicle: dict[str, Any]) -> str | None:
    """``charging.status.state`` in upper case, or None."""
    state = _section(_section(vehicle, "charging"), "status").get("state")
    return state.strip().upper() if isinstance(state, str) and state.strip() else None


def cable_connected(vehicle: dict[str, Any]) -> bool | None:
    """Whether the charging cable is plugged in, from the last GET, or None.

    ``plugConnectionState`` decides; without it the state ``CONNECT_CABLE``
    means unplugged.
    """
    plug = _section(_section(vehicle, "charging"), "status").get("plugConnectionState")
    if plug == "CONNECTED":
        return True
    if plug == "DISCONNECTED" or charging_state(vehicle) == "CONNECT_CABLE":
        return False
    return None


def climate_state(vehicle: dict[str, Any]) -> str | None:
    """``airConditioning.state`` in upper case, or None."""
    state = _section(vehicle, "airConditioning").get("state")
    return state.strip().upper() if isinstance(state, str) and state.strip() else None


def captured_at(vehicle: dict[str, Any], section: str) -> datetime | None:
    """``<section>.carCapturedTimestamp`` (aware), or None."""
    value = _section(vehicle, section).get("carCapturedTimestamp")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def current_profile(vehicle: dict[str, Any]) -> dict[str, Any]:
    """Profile of the current vehicle position, copied as the API sent it."""
    profiles = _section(vehicle, "chargingProfiles")
    current = profiles.get("currentVehiclePositionProfile")
    if not isinstance(current, dict) or not isinstance(current.get("id"), int):
        raise CommandError("no_location_profile")
    items = profiles.get("profiles")
    matches = [
        item
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict) and item.get("id") == current["id"]
    ]
    if len(matches) != 1:
        raise CommandError("no_location_profile")
    profile = matches[0]
    if any(name not in profile for name in PROFILE_FIELDS) or not isinstance(
        profile.get("settings"), dict
    ):
        raise CommandError("profile_incomplete")
    return profile


def _require(vehicle: dict[str, Any], operation: str) -> None:
    if operation not in operation_names(vehicle):
        raise CommandError("not_supported", operation=operation)


# --- Builders ----------------------------------------------------------------


def charging_start(vehicle: dict[str, Any]) -> Command:
    """POST /charging/start."""
    _require(vehicle, OP_START_CHARGING)
    return Command(
        "charging_start",
        CommandGroup.CHARGING,
        OP_START_CHARGING,
        "POST",
        "/charging/start",
        expected=True,
    )


def charging_stop(vehicle: dict[str, Any]) -> Command:
    """POST /charging/stop."""
    _require(vehicle, OP_STOP_CHARGING)
    return Command(
        "charging_stop",
        CommandGroup.CHARGING,
        OP_STOP_CHARGING,
        "POST",
        "/charging/stop",
        expected=False,
    )


def valid_temperature(value: float) -> float | None:
    """Temperature rounded to the 0.5 °C grid, or None outside 15.5-30 °C."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not TEMP_MIN <= value <= TEMP_MAX:
        return None
    steps = (value - TEMP_MIN) / TEMP_STEP
    if abs(steps - round(steps)) > 1e-6:  # noqa: PLR2004
        return None
    return round(float(value), 1)


def vehicle_temperature(vehicle: dict[str, Any]) -> float | None:
    """Target temperature the vehicle reports, in °C, or None."""
    target = _section(vehicle, "airConditioning").get("targetTemperature")
    if not isinstance(target, dict) or target.get("unit") != "CELSIUS":
        return None
    value = target.get("value")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return round(float(value), 1)


def climate_start(vehicle: dict[str, Any], temperature: float | None = None) -> Command:
    """POST /air-conditioning/start.

    Temperature: the requested value, else the vehicle's last value. Without
    either the field is left out and the vehicle keeps its own setting (never
    a guessed default).
    """
    _require(vehicle, OP_START_CLIMATE)
    body: dict[str, Any] = {}
    if temperature is not None:
        value = valid_temperature(temperature)
        if value is None:
            raise CommandError("invalid_value")
    else:
        value = vehicle_temperature(vehicle)
    if value is not None:
        body["targetTemperature"] = {"value": value, "unit": "CELSIUS"}
    without_power = _section(vehicle, "airConditioning").get(
        "airConditioningWithoutExternalPower"
    )
    body["airConditioningWithoutExternalPower"] = (
        without_power if isinstance(without_power, bool) else True
    )
    return Command(
        "climate_start",
        CommandGroup.CLIMATE,
        OP_START_CLIMATE,
        "POST",
        "/air-conditioning/start",
        body=body,
        expected=True,
    )


KEY_CLIMATE_TEMPERATURE = "climate_temperature"
# Stopping undoes a start: it may follow a pending START at once (otherwise a
# START pressed by mistake could not be stopped until the next GET).
STOP_KEYS = frozenset({"charging_stop", "climate_stop"})


def climate_temperature(vehicle: dict[str, Any], temperature: float) -> Command:
    """POST /air-conditioning/start with a new temperature while the climate runs.

    The API has no command that only sets the temperature (Swagger 1.1.0,
    checked 2026-11-23); START with the new value is the only way. The next
    GET confirms it when the climate runs with that temperature.
    """
    value = valid_temperature(temperature)
    if value is None:
        raise CommandError("invalid_value")
    return replace(
        climate_start(vehicle, value), key=KEY_CLIMATE_TEMPERATURE, expected=value
    )


def climate_stop(vehicle: dict[str, Any]) -> Command:
    """POST /air-conditioning/stop."""
    _require(vehicle, OP_STOP_CLIMATE)
    return Command(
        "climate_stop",
        CommandGroup.CLIMATE,
        OP_STOP_CLIMATE,
        "POST",
        "/air-conditioning/stop",
        expected=False,
    )


def available_charge_modes(vehicle: dict[str, Any]) -> list[str]:
    """``charging.settings.availableChargeModes`` (strings only)."""
    modes = charging_settings(vehicle).get("availableChargeModes")
    return [m for m in modes if isinstance(m, str)] if isinstance(modes, list) else []


def charge_mode(vehicle: dict[str, Any], mode: str) -> Command:
    """PUT /charging/mode; only modes the last GET lists are allowed."""
    _require(vehicle, OP_CHARGE_MODE)
    if mode not in available_charge_modes(vehicle):
        raise CommandError("invalid_value")
    return Command(
        "charge_mode",
        CommandGroup.CHARGE_MODE,
        OP_CHARGE_MODE,
        "PUT",
        "/charging/mode",
        body={"chargeMode": mode},
        expected=mode,
    )


def _percent(value: Any, low: int, high: int, step: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and low <= value <= high and value % step == 0:
        return value
    return None


def charging_limit(vehicle: dict[str, Any], percent: float) -> Command:
    """PUT /charging/limit, 50-100 % in steps of 10."""
    _require(vehicle, OP_CHARGING_LIMIT)
    value = _percent(percent, LIMIT_MIN, LIMIT_MAX, LIMIT_STEP)
    if value is None:
        raise CommandError("invalid_value")
    return Command(
        "charging_limit",
        CommandGroup.CHARGING_LIMIT,
        OP_CHARGING_LIMIT,
        "PUT",
        "/charging/limit",
        body={"targetStateOfChargeInPercent": value},
        expected=value,
    )


# --- Profile settings --------------------------------------------------------

PROFILE_SETTINGS: tuple[str, ...] = (
    "max_current",
    "target_soc",
    "min_soc",
    "auto_unlock",
    *(f"time_{n}_enabled" for n in range(1, PROFILE_SLOTS + 1)),
    *(f"time_{n}" for n in range(1, PROFILE_SLOTS + 1)),
)
_SLOT_RE = re.compile(r"^time_(\d)(_enabled)?$")


def _slot(setting: str) -> tuple[int, bool]:
    match = _SLOT_RE.match(setting)
    if match is None:  # pragma: no cover - guarded by PROFILE_SETTINGS
        raise CommandError("invalid_value")
    return int(match.group(1)) - 1, bool(match.group(2))


def _window(value: Any) -> str | None:
    """Canonical ``HH:MM-HH:MM`` or None."""
    if not isinstance(value, str):
        return None
    parts = [part.strip() for part in value.strip().split("-")]
    if len(parts) != 2 or not all(_TIME_RE.match(part) for part in parts):  # noqa: PLR2004
        return None
    return f"{parts[0]}-{parts[1]}"


def normalize_profile_value(setting: str, value: Any) -> Any:
    """Canonical value for a profile setting, or None when invalid."""
    if setting == "max_current":
        return value if value in CURRENT_VALUES else None
    if setting == "target_soc":
        return _percent(value, TARGET_SOC_MIN, TARGET_SOC_MAX, TARGET_SOC_STEP)
    if setting == "min_soc":
        return _percent(value, MIN_SOC_MIN, MIN_SOC_MAX, MIN_SOC_STEP)
    if setting == "auto_unlock":
        return value if value in AUTO_UNLOCK_VALUES else None
    _, is_enabled = _slot(setting)
    if is_enabled:
        return value if isinstance(value, bool) else None
    return _window(value)


def profile_values(profile: dict[str, Any]) -> dict[str, Any]:
    """Read ``{setting: value}`` from a profile; unknown values are None."""
    settings = profile.get("settings")
    settings = settings if isinstance(settings, dict) else {}
    minimum = settings.get("minBatteryStateOfCharge")
    minimum = minimum if isinstance(minimum, dict) else {}
    min_value = minimum.get("minimumBatteryStateOfChargeInPercent")
    if minimum.get("enabled") is False:
        min_value = 0
    target = settings.get("targetStateOfChargeInPercent")
    out: dict[str, Any] = {
        "max_current": settings.get("maxChargingCurrent")
        if settings.get("maxChargingCurrent") in CURRENT_VALUES
        else None,
        "target_soc": target if type(target) is int else None,
        "min_soc": min_value if type(min_value) is int else None,
        "auto_unlock": settings.get("autoUnlockPlugWhenCharged")
        if settings.get("autoUnlockPlugWhenCharged") in AUTO_UNLOCK_VALUES
        else None,
    }
    times = profile.get("preferredChargingTimes")
    times = times if isinstance(times, list) else []
    for n in range(1, PROFILE_SLOTS + 1):
        item = (
            times[n - 1] if n <= len(times) and isinstance(times[n - 1], dict) else {}
        )
        enabled = item.get("enabled")
        out[f"time_{n}_enabled"] = enabled if isinstance(enabled, bool) else None
        out[f"time_{n}"] = _window(f"{item.get('startTime')}-{item.get('endTime')}")
    return out


def profile_setting(vehicle: dict[str, Any], setting: str, value: Any) -> Command:
    """PUT /charging-profiles/{id}: the whole profile, one setting changed.

    The vehicle applies the profile as a whole, so the body is the complete
    location profile from the last GET (id, name, settings, preferred
    charging times and timers). Only ``setting`` changes.
    """
    if setting not in PROFILE_SETTINGS:
        raise CommandError("invalid_value")
    _require(vehicle, OP_PROFILE)
    profile = current_profile(vehicle)
    value = normalize_profile_value(setting, value)
    if value is None:
        raise CommandError("invalid_value")
    body = {name: copy.deepcopy(profile[name]) for name in PROFILE_FIELDS}
    settings = body["settings"]
    if setting == "max_current":
        settings["maxChargingCurrent"] = value
    elif setting == "target_soc":
        settings["targetStateOfChargeInPercent"] = value
    elif setting == "min_soc":
        minimum = settings.get("minBatteryStateOfCharge")
        minimum = copy.deepcopy(minimum) if isinstance(minimum, dict) else {}
        minimum["minimumBatteryStateOfChargeInPercent"] = value
        if "enabled" in minimum:
            minimum["enabled"] = value > 0
        settings["minBatteryStateOfCharge"] = minimum
    elif setting == "auto_unlock":
        settings["autoUnlockPlugWhenCharged"] = value
    else:
        index, is_enabled = _slot(setting)
        times = body["preferredChargingTimes"]
        if (
            not isinstance(times, list)
            or index >= len(times)
            or not isinstance(times[index], dict)
        ):
            raise CommandError("no_preferred_time", slot=str(index + 1))
        if is_enabled:
            times[index]["enabled"] = value
        else:
            times[index]["startTime"], times[index]["endTime"] = value.split("-")
    return Command(
        f"profile_{setting}",
        CommandGroup.PROFILE,
        OP_PROFILE,
        "PUT",
        f"/charging-profiles/{profile['id']}",
        body=body,
        expected=value,
        profile_id=profile["id"],
        setting=setting,
    )


# --- Pending commands and confirmation ---------------------------------------


class Verdict(StrEnum):
    """Outcome of an accepted command after a later GET."""

    CONFIRMED = "confirmed"
    NOT_CONFIRMED = "not_confirmed"
    PENDING = "pending"


@dataclass(slots=True)
class PendingCommand:
    """An accepted (202) command waiting for confirmation. Kept in Store."""

    key: str
    group: CommandGroup
    accepted_at: datetime
    expected: Any = None
    profile_id: int | None = None
    setting: str | None = None
    # Charging: power of the user's own charger (W) when the command was
    # accepted, if a charger power sensor is set. Decides whether the meter
    # can confirm the command (see ``charger_meter.py``).
    baseline_w: float | None = None

    @classmethod
    def from_command(
        cls,
        command: Command,
        accepted_at: datetime,
        baseline_w: float | None = None,
    ) -> PendingCommand:
        """Pending state of a command that the API accepted."""
        return cls(
            key=command.key,
            group=command.group,
            accepted_at=accepted_at,
            expected=command.expected,
            profile_id=command.profile_id,
            setting=command.setting,
            baseline_w=baseline_w,
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form for ``Store``."""
        return {
            "key": self.key,
            "group": self.group.value,
            "accepted_at": self.accepted_at.isoformat(),
            "expected": self.expected,
            "profile_id": self.profile_id,
            "setting": self.setting,
            "baseline_w": self.baseline_w,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PendingCommand | None:
        """Read the stored form; anything invalid is dropped."""
        if not isinstance(data, dict):
            return None
        try:
            group = CommandGroup(data.get("group"))
            accepted_at = datetime.fromisoformat(data["accepted_at"])
        except KeyError, TypeError, ValueError:
            return None
        key = data.get("key")
        if not isinstance(key, str) or accepted_at.tzinfo is None:
            return None
        profile_id = data.get("profile_id")
        setting = data.get("setting")
        baseline = data.get("baseline_w")
        return cls(
            key=key,
            group=group,
            accepted_at=accepted_at,
            expected=data.get("expected"),
            profile_id=profile_id if isinstance(profile_id, int) else None,
            setting=setting if isinstance(setting, str) else None,
            baseline_w=float(baseline)
            if isinstance(baseline, int | float) and not isinstance(baseline, bool)
            else None,
        )


def confirm_timeout(interval_seconds: int) -> timedelta:
    """How long a command may wait for a deciding GET."""
    return max(
        CONFIRM_TIMEOUT, 2 * timedelta(seconds=interval_seconds) + CONFIRM_MARGIN
    )


def _fresh(vehicle: dict[str, Any], section: str, accepted_at: datetime) -> bool:
    captured = captured_at(vehicle, section)
    return captured is not None and captured > accepted_at


def _decide(verdict: Verdict, *, fresh: bool) -> Verdict:
    """Apply the freshness rule to a verdict read from the GET.

    Only data the car captured after the command may say *not confirmed*
    Data from before the command that already shows the
    requested state confirms it: the vehicle was there already (a STOP
    while not charging, a START while running), so nothing else will come
    and the command must not hang until the timeout (user 2026-11-23).
    """
    if verdict is Verdict.NOT_CONFIRMED and not fresh:
        return Verdict.PENDING
    return verdict


def _evaluate_charging(pending: PendingCommand, vehicle: dict[str, Any]) -> Verdict:
    # Cable unplugged: no charging is possible, so a STOP is done and a START
    # failed, even if the car sent no new charging data.
    if cable_connected(vehicle) is False:
        return Verdict.NOT_CONFIRMED if pending.expected else Verdict.CONFIRMED
    state = charging_state(vehicle)
    if state == CHARGING_STATE:
        verdict = Verdict.CONFIRMED if pending.expected else Verdict.NOT_CONFIRMED
    elif state in STOPPED_STATES:
        verdict = Verdict.NOT_CONFIRMED if pending.expected else Verdict.CONFIRMED
    else:
        return Verdict.PENDING
    return _decide(verdict, fresh=_fresh(vehicle, "charging", pending.accepted_at))


def _evaluate_temperature(
    pending: PendingCommand, vehicle: dict[str, Any], state: str | None
) -> Verdict:
    """Decide on a new temperature: running with that value confirms it."""
    if state in CLIMATE_ACTIVE_STATES:
        reached = same_temperature(vehicle_temperature(vehicle), pending.expected)
        return Verdict.CONFIRMED if reached else Verdict.NOT_CONFIRMED
    if state in CLIMATE_OFF_STATES:
        return Verdict.NOT_CONFIRMED
    return Verdict.PENDING


def _evaluate_climate(pending: PendingCommand, vehicle: dict[str, Any]) -> Verdict:
    state = climate_state(vehicle)
    if pending.key == KEY_CLIMATE_TEMPERATURE:
        verdict = _evaluate_temperature(pending, vehicle, state)
    elif state in CLIMATE_ACTIVE_STATES:
        verdict = Verdict.CONFIRMED if pending.expected else Verdict.NOT_CONFIRMED
    elif state in CLIMATE_OFF_STATES:
        verdict = Verdict.NOT_CONFIRMED if pending.expected else Verdict.CONFIRMED
    else:
        return Verdict.PENDING
    return _decide(
        verdict, fresh=_fresh(vehicle, "airConditioning", pending.accepted_at)
    )


def _setting_value(pending: PendingCommand, vehicle: dict[str, Any]) -> Any:
    if pending.group is CommandGroup.CHARGE_MODE:
        return charging_settings(vehicle).get("preferredChargeMode")
    if pending.group is CommandGroup.CHARGING_LIMIT:
        return charging_settings(vehicle).get("targetStateOfChargeInPercent")
    profiles = _section(vehicle, "chargingProfiles").get("profiles")
    for profile in profiles if isinstance(profiles, list) else []:
        if isinstance(profile, dict) and profile.get("id") == pending.profile_id:
            return profile_values(profile).get(pending.setting or "")
    return None


def evaluate(
    pending: PendingCommand,
    vehicle: dict[str, Any],
    now: datetime,
    interval_seconds: int,
) -> Verdict:
    """Decide on an accepted command from a GET made after it.

    The requested state in any GET confirms (also from before the command:
    then the vehicle was there already). The opposite state decides *not
    confirmed* only when the car captured it after the command; until then
    the command is pending (settings: the car may not have reported yet).
    Charging with the cable unplugged: STOP confirmed, START not. Without a
    deciding GET the command is not confirmed after ``confirm_timeout``, so
    no pending state can hang (user 2026-11-23).
    """
    if pending.group is CommandGroup.CHARGING:
        verdict = _evaluate_charging(pending, vehicle)
    elif pending.group is CommandGroup.CLIMATE:
        verdict = _evaluate_climate(pending, vehicle)
    else:
        section = (
            "chargingProfiles" if pending.group is CommandGroup.PROFILE else "charging"
        )
        verdict = _decide(
            Verdict.CONFIRMED
            if _setting_value(pending, vehicle) == pending.expected
            else Verdict.NOT_CONFIRMED,
            fresh=_fresh(vehicle, section, pending.accepted_at),
        )
    if verdict is Verdict.PENDING and now - pending.accepted_at >= confirm_timeout(
        interval_seconds
    ):
        return Verdict.NOT_CONFIRMED
    return verdict


# --- State kept across restarts ----------------------------------------------


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Last outcome of a group, for diagnostics and entities.

    ``result``: accepted (pending), confirmed, not_confirmed, rejected or
    uncertain. ``detail`` is a short code without personal data.
    """

    key: str
    result: str
    at: datetime
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form for ``Store``."""
        return {
            "key": self.key,
            "result": self.result,
            "at": self.at.isoformat(),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Any) -> CommandResult | None:
        """Read the stored form; anything invalid is dropped."""
        if not isinstance(data, dict):
            return None
        key, result, at = (
            data.get("key"),
            data.get("result"),
            _parse_time(data.get("at")),
        )
        if not isinstance(key, str) or not isinstance(result, str) or at is None:
            return None
        detail = data.get("detail")
        return cls(key, result, at, detail if isinstance(detail, str) else None)


@dataclass(slots=True)
class CommandState:
    """Pending commands, last accepted change per group and last results."""

    pending: dict[CommandGroup, PendingCommand]
    last_accepted: dict[CommandGroup, datetime]
    results: dict[CommandGroup, CommandResult]

    @classmethod
    def empty(cls) -> CommandState:
        """No commands yet."""
        return cls({}, {}, {})

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form for ``Store``."""
        return {
            "pending": [p.as_dict() for p in self.pending.values()],
            "last_accepted": {
                g.value: t.isoformat() for g, t in self.last_accepted.items()
            },
            "results": {g.value: r.as_dict() for g, r in self.results.items()},
        }

    @classmethod
    def from_dict(cls, data: Any) -> CommandState:
        """Read the stored form; invalid parts are dropped."""
        state = cls.empty()
        if not isinstance(data, dict):
            return state
        pending = data.get("pending")
        for item in pending if isinstance(pending, list) else []:
            if (command := PendingCommand.from_dict(item)) is not None:
                state.pending[command.group] = command
        for raw, value in _items(data.get("last_accepted")):
            if (group := _group(raw)) and (at := _parse_time(value)):
                state.last_accepted[group] = at
        for raw, value in _items(data.get("results")):
            if (group := _group(raw)) and (result := CommandResult.from_dict(value)):
                state.results[group] = result
        return state


def _items(value: Any) -> list[tuple[Any, Any]]:
    return list(value.items()) if isinstance(value, dict) else []


def _group(value: Any) -> CommandGroup | None:
    try:
        return CommandGroup(value)
    except ValueError:
        return None


# --- Checks right before sending ---------------------------------------------

DEBOUNCE = timedelta(seconds=25)
MIN_REQUEST_SPACING = timedelta(seconds=5)
# A body built from the last GET must come from a recent GET: at least 15
# minutes, or one regular interval plus 5 minutes when the user
# polls less often.
MIN_DATA_AGE = timedelta(minutes=15)
DATA_AGE_MARGIN = timedelta(minutes=5)


def max_data_age(polls_per_hour: int) -> timedelta:
    """Oldest GET a request body may be built from."""
    return max(
        MIN_DATA_AGE,
        timedelta(seconds=interval_seconds(polls_per_hour)) + DATA_AGE_MARGIN,
    )


def preflight(  # noqa: PLR0913 - all inputs of the checks
    command: Command,
    *,
    now: datetime,
    quota: QuotaState,
    polls_per_hour: int,
    last_success_at: datetime | None,
    state: CommandState,
    last_sent: datetime | None,
) -> None:
    """Every check before a command is sent; raises ``CommandError``.

    Rules: no request during a 429 cooldown, at least 5 s between
    requests, the same command at most once per 25 s, a body from the last
    GET only when that GET is recent and newer than the last accepted change
    of the group. Design decision: while a command
    of the same group waits for confirmation, no new one is sent; only a
    STOP may follow a pending START. Golden reserve: after
    the command, enough requests must remain for every regular GET until
    the reset plus one. Exception: charging STOP may use that last request
    (design decision).
    """
    if (delay := quota.retry_delay(now)) > 0:
        raise CommandError("rate_limited", seconds=str(delay))
    if last_sent is not None and now - last_sent < DEBOUNCE:
        raise CommandError("debounced")
    if (
        quota.last_request_at is not None
        and now - quota.last_request_at < MIN_REQUEST_SPACING
    ):
        raise CommandError("too_soon")
    if command.group in SNAPSHOT_GROUPS:
        if last_success_at is None or now - last_success_at > max_data_age(
            polls_per_hour
        ):
            raise CommandError("data_too_old")
        accepted = state.last_accepted.get(command.group)
        if accepted is not None and accepted >= last_success_at:
            raise CommandError("previous_not_visible")
    pending = state.pending.get(command.group)
    if pending is not None and not (
        command.key in STOP_KEYS and pending.key not in STOP_KEYS
    ):
        raise CommandError("pending")
    allowed = (
        can_spend_stop(quota, now, polls_per_hour)
        if command.key == "charging_stop"
        else can_spend(quota, now, polls_per_hour)
    )
    if not allowed:
        raise CommandError("quota_reserve")


# --- Climate temperature ----------------------------------------------------
#
# The API has no "set temperature" command: the value travels in the body of
# START. A temperature set in Home Assistant is kept (0 requests) and sent with
# the next START. Observed on the Elroq (2026-11-22): the START temperature
# applies only to that run; after STOP the vehicle reports its stored value
# again, so a difference while the climate is off is not a failure.

SYNC_IN_SYNC = "in_sync"
SYNC_PENDING_NEXT_START = "pending_next_start"
SYNC_SENT_AWAITING_VEHICLE = "sent_awaiting_vehicle"
SYNC_VEHICLE_DIFFERS = "vehicle_differs"
SYNC_UNKNOWN = "unknown"


def same_temperature(a: float | None, b: float | None) -> bool:
    """Equal within 0.05 °C (both known)."""
    return a is not None and b is not None and abs(a - b) < 0.05  # noqa: PLR2004


def temperature_sync(  # noqa: PLR0913
    *,
    desired: float | None,
    desired_at: datetime | None,
    sent: float | None,
    sent_at: datetime | None,
    vehicle_value: float | None,
    captured: datetime | None,
    active: bool,
) -> str:
    """How the Home Assistant temperature relates to the vehicle's value."""
    if desired is None:
        return SYNC_IN_SYNC if vehicle_value is not None else SYNC_UNKNOWN
    if vehicle_value is None:
        return SYNC_UNKNOWN
    if same_temperature(desired, vehicle_value):
        return SYNC_IN_SYNC
    if same_temperature(sent, desired) and not (
        desired_at and sent_at and desired_at > sent_at
    ):
        # A newer HA value than the one sent still waits for a START.
        if not sent_at or not captured or captured <= sent_at:
            return SYNC_SENT_AWAITING_VEHICLE
        return SYNC_VEHICLE_DIFFERS if active else SYNC_PENDING_NEXT_START
    return SYNC_PENDING_NEXT_START
