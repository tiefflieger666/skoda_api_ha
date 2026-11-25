"""Command builders, confirmation and pre-send checks (no Home Assistant)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.skoda_api_ha import commands as cmd
from custom_components.skoda_api_ha.commands import (
    CommandError,
    CommandGroup,
    CommandResult,
    CommandState,
    PendingCommand,
    Verdict,
)
from custom_components.skoda_api_ha.quota import QuotaState

from .const import command_vehicle

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
LATER = "2026-11-23T10:02:00+00:00"


def _reason(func: Any, *args: Any) -> str:
    with pytest.raises(CommandError) as err:
        func(*args)
    return err.value.reason


# --- Builders ------------------------------------------------------------------


def test_simple_commands() -> None:
    """Start/stop of charging and climate: method, path, expected state."""
    vehicle = command_vehicle()
    start = cmd.charging_start(vehicle)
    assert (start.method, start.path, start.body, start.expected) == (
        "POST",
        "/charging/start",
        None,
        True,
    )
    stop = cmd.charging_stop(vehicle)
    assert (stop.path, stop.expected, stop.group) == (
        "/charging/stop",
        False,
        CommandGroup.CHARGING,
    )
    off = cmd.climate_stop(vehicle)
    assert (off.path, off.expected, off.group) == (
        "/air-conditioning/stop",
        False,
        CommandGroup.CLIMATE,
    )


@pytest.mark.parametrize(
    "build",
    [
        cmd.charging_start,
        cmd.charging_stop,
        cmd.climate_start,
        cmd.climate_stop,
        lambda v: cmd.charge_mode(v, "MANUAL"),
        lambda v: cmd.charging_limit(v, 80),
        lambda v: cmd.profile_setting(v, "max_current", "MAXIMUM"),
    ],
)
def test_operation_must_be_listed(build: Any) -> None:
    """Only operations of the last GET are sent (also as plain strings)."""
    assert _reason(build, command_vehicle(operations=())) == "not_supported"
    vehicle = command_vehicle()
    vehicle["operations"] = [op["name"] for op in vehicle["operations"]]
    build(vehicle)


def test_climate_start_body() -> None:
    """Requested temperature, else the vehicle's; without external power kept."""
    vehicle = command_vehicle()
    assert cmd.climate_start(vehicle, 23.5).body == {
        "targetTemperature": {"value": 23.5, "unit": "CELSIUS"},
        "airConditioningWithoutExternalPower": False,
    }
    assert cmd.climate_start(vehicle).body["targetTemperature"]["value"] == 22.0
    # No temperature known: the field is left out, never guessed.
    del vehicle["airConditioning"]
    assert cmd.climate_start(vehicle).body == {
        "airConditioningWithoutExternalPower": True
    }
    for bad in (15.0, 30.5, 22.3, True):
        assert _reason(cmd.climate_start, command_vehicle(), bad) == "invalid_value"


def test_vehicle_temperature_needs_celsius() -> None:
    """Only a Celsius number counts as the vehicle temperature."""
    vehicle = command_vehicle()
    vehicle["airConditioning"]["targetTemperature"]["unit"] = "FAHRENHEIT"
    assert cmd.vehicle_temperature(vehicle) is None
    vehicle["airConditioning"]["targetTemperature"] = {"value": "22", "unit": "CELSIUS"}
    assert cmd.vehicle_temperature(vehicle) is None


def test_charge_mode_only_available() -> None:
    """Only modes the last GET lists are allowed."""
    command = cmd.charge_mode(command_vehicle(), "PREFERRED_CHARGING_TIMES")
    assert command.body == {"chargeMode": "PREFERRED_CHARGING_TIMES"}
    assert command.path == "/charging/mode"
    assert _reason(cmd.charge_mode, command_vehicle(), "TIMER") == "invalid_value"


def test_charging_limit_values() -> None:
    """50-100 % in steps of 10."""
    assert cmd.charging_limit(command_vehicle(), 90.0).body == {
        "targetStateOfChargeInPercent": 90
    }
    for bad in (40, 85, 110, True, "80"):
        assert _reason(cmd.charging_limit, command_vehicle(), bad) == "invalid_value"


@pytest.mark.parametrize(
    ("setting", "value", "path", "expected"),
    [
        ("max_current", "MAXIMUM", ("settings", "maxChargingCurrent"), "MAXIMUM"),
        ("target_soc", 90, ("settings", "targetStateOfChargeInPercent"), 90),
        ("auto_unlock", "OFF", ("settings", "autoUnlockPlugWhenCharged"), "OFF"),
        (
            "min_soc",
            30,
            (
                "settings",
                "minBatteryStateOfCharge",
                "minimumBatteryStateOfChargeInPercent",
            ),
            30,
        ),
        ("time_1_enabled", True, ("preferredChargingTimes", 0, "enabled"), True),
        (
            "time_2",
            "23:00 - 05:30",
            ("preferredChargingTimes", 1, "startTime"),
            "23:00",
        ),
    ],
)
def test_profile_body_changes_one_setting(
    setting: str, value: Any, path: tuple, expected: Any
) -> None:
    """The whole location profile is sent; only one setting changes."""
    vehicle = command_vehicle()
    before = copy.deepcopy(vehicle)
    command = cmd.profile_setting(vehicle, setting, value)
    assert command.method == "PUT"
    assert command.path == "/charging-profiles/4"
    assert command.profile_id == 4
    assert vehicle == before  # the last GET is never modified

    sent = command.body
    assert set(sent) == {"id", "name", "settings", "preferredChargingTimes", "timers"}
    node: Any = sent
    for part in path:
        node = node[part]
    assert node == expected
    # Everything else is the profile as the API sent it.
    original = copy.deepcopy(before["chargingProfiles"]["profiles"][1])
    node = original
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = expected
    if setting == "time_2":
        original["preferredChargingTimes"][1]["endTime"] = "05:30"
    assert sent == original


def test_min_soc_toggles_enabled_when_reported() -> None:
    """``enabled`` follows the value only when the vehicle reports it."""
    vehicle = command_vehicle()
    settings = vehicle["chargingProfiles"]["profiles"][1]["settings"]
    settings["minBatteryStateOfCharge"]["enabled"] = True
    body = cmd.profile_setting(vehicle, "min_soc", 0).body
    assert body["settings"]["minBatteryStateOfCharge"] == {
        "minimumBatteryStateOfChargeInPercent": 0,
        "enabled": False,
    }
    del settings["minBatteryStateOfCharge"]
    body = cmd.profile_setting(vehicle, "min_soc", 20).body
    assert body["settings"]["minBatteryStateOfCharge"] == {
        "minimumBatteryStateOfChargeInPercent": 20
    }


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("max_current", "HALF"),
        ("target_soc", 45),
        ("min_soc", 60),
        ("auto_unlock", "ON"),
        ("time_1_enabled", "yes"),
        ("time_1", "25:00-06:00"),
        ("time_1", 2200),
        ("unknown", 1),
    ],
)
def test_profile_invalid_values(setting: str, value: Any) -> None:
    """Invalid values are refused before anything is sent."""
    assert _reason(cmd.profile_setting, command_vehicle(), setting, value) == (
        "invalid_value"
    )


def test_profile_preconditions() -> None:
    """Location profile, complete profile and the time slot must exist."""
    vehicle = command_vehicle()
    assert _reason(cmd.profile_setting, vehicle, "time_3", "01:00-02:00") == (
        "no_preferred_time"
    )
    del vehicle["chargingProfiles"]["currentVehiclePositionProfile"]
    assert _reason(cmd.profile_setting, vehicle, "target_soc", 90) == (
        "no_location_profile"
    )
    vehicle = command_vehicle()
    vehicle["chargingProfiles"]["profiles"].pop()
    assert _reason(cmd.profile_setting, vehicle, "target_soc", 90) == (
        "no_location_profile"
    )
    vehicle = command_vehicle()
    del vehicle["chargingProfiles"]["profiles"][1]["timers"]
    assert _reason(cmd.profile_setting, vehicle, "target_soc", 90) == (
        "profile_incomplete"
    )


def test_profile_values() -> None:
    """Reading the profile; ``enabled: false`` means a minimum of 0."""
    profile = command_vehicle()["chargingProfiles"]["profiles"][1]
    profile["settings"]["minBatteryStateOfCharge"] = {
        "minimumBatteryStateOfChargeInPercent": 30,
        "enabled": False,
    }
    values = cmd.profile_values(profile)
    assert values["min_soc"] == 0
    assert values["max_current"] == "REDUCED"
    assert values["time_2"] == "22:00-06:00"
    assert values["time_3"] is None
    assert values["time_3_enabled"] is None
    assert cmd.profile_values({})["target_soc"] is None


# --- Confirmation -------------------------------------------------------------


def _pending(command: cmd.Command) -> PendingCommand:
    return PendingCommand.from_command(command, NOW)


@pytest.mark.parametrize(
    ("build", "section_state", "captured", "verdict"),
    [
        (cmd.charging_start, ("charging", "CHARGING"), LATER, Verdict.CONFIRMED),
        (
            cmd.charging_start,
            ("charging", "READY_FOR_CHARGING"),
            LATER,
            Verdict.NOT_CONFIRMED,
        ),
        # Captured before the command: the opposite says nothing yet ...
        (
            cmd.charging_start,
            ("charging", "READY_FOR_CHARGING"),
            "2026-11-23T09:59:00+00:00",
            Verdict.PENDING,
        ),
        # ... the requested state confirms (the vehicle was there already).
        (
            cmd.charging_start,
            ("charging", "CHARGING"),
            "2026-11-23T09:59:00+00:00",
            Verdict.CONFIRMED,
        ),
        (cmd.charging_start, ("charging", "SOMETHING_NEW"), LATER, Verdict.PENDING),
        (cmd.charging_stop, ("charging", "CONSERVING"), LATER, Verdict.CONFIRMED),
        (cmd.charging_stop, ("charging", "CHARGING"), LATER, Verdict.NOT_CONFIRMED),
        (cmd.climate_start, ("airConditioning", "HEATING"), LATER, Verdict.CONFIRMED),
        (cmd.climate_start, ("airConditioning", "OFF"), LATER, Verdict.NOT_CONFIRMED),
        (cmd.climate_start, ("airConditioning", "UNKNOWN"), LATER, Verdict.PENDING),
        (cmd.climate_stop, ("airConditioning", "COMPLETED"), LATER, Verdict.CONFIRMED),
        (
            cmd.climate_stop,
            ("airConditioning", "COOLING"),
            LATER,
            Verdict.NOT_CONFIRMED,
        ),
        (cmd.climate_stop, ("airConditioning", "OFF"), "not a time", Verdict.CONFIRMED),
        (
            cmd.climate_stop,
            ("airConditioning", "HEATING"),
            "not a time",
            Verdict.PENDING,
        ),
    ],
)
def test_evaluate_states(
    build: Any, section_state: tuple[str, str], captured: str, verdict: Verdict
) -> None:
    """Only data captured after the command says not confirmed."""
    pending = _pending(build(command_vehicle()))
    section, state = section_state
    vehicle = command_vehicle(captured=captured)
    key = "status" if section == "charging" else None
    if key:
        vehicle[section][key]["state"] = state
    else:
        vehicle[section]["state"] = state
    assert cmd.evaluate(pending, vehicle, NOW + timedelta(minutes=5), 300) is verdict


def test_evaluate_settings() -> None:
    """Mode, limit and profile: the new value confirms, else still pending."""
    vehicle = command_vehicle()
    mode = _pending(cmd.charge_mode(vehicle, "PREFERRED_CHARGING_TIMES"))
    limit = _pending(cmd.charging_limit(vehicle, 90))
    current = _pending(cmd.profile_setting(vehicle, "max_current", "MAXIMUM"))
    soon = NOW + timedelta(minutes=5)
    for pending in (mode, limit, current):
        assert cmd.evaluate(pending, vehicle, soon, 300) is Verdict.PENDING

    after = command_vehicle()
    after["charging"]["settings"]["preferredChargeMode"] = "PREFERRED_CHARGING_TIMES"
    after["charging"]["settings"]["targetStateOfChargeInPercent"] = 90
    after["chargingProfiles"]["profiles"][1]["settings"]["maxChargingCurrent"] = (
        "MAXIMUM"
    )
    for pending in (mode, limit, current):
        assert cmd.evaluate(pending, after, soon, 300) is Verdict.CONFIRMED
    # A profile that is gone from the GET decides nothing.
    del after["chargingProfiles"]
    assert cmd.evaluate(current, after, soon, 300) is Verdict.PENDING


def test_evaluate_timeout() -> None:
    """Without a deciding GET: not confirmed after 30 min (or 2 intervals + 2 min)."""
    pending = _pending(cmd.charging_start(command_vehicle()))
    old = command_vehicle()
    assert cmd.evaluate(pending, old, NOW + timedelta(minutes=29), 300) is (
        Verdict.PENDING
    )
    assert cmd.evaluate(pending, old, NOW + timedelta(minutes=30), 300) is (
        Verdict.NOT_CONFIRMED
    )
    # 2 polls per hour: 2 * 30 min + 2 min.
    assert cmd.evaluate(pending, old, NOW + timedelta(minutes=61), 1800) is (
        Verdict.PENDING
    )
    assert cmd.evaluate(pending, old, NOW + timedelta(minutes=62), 1800) is (
        Verdict.NOT_CONFIRMED
    )


def test_command_state_round_trip() -> None:
    """Pending commands, last accepted changes and results survive Store."""
    vehicle = command_vehicle()
    state = CommandState.empty()
    current = cmd.profile_setting(vehicle, "max_current", "MAXIMUM")
    state.pending[CommandGroup.PROFILE] = _pending(current)
    state.pending[CommandGroup.CHARGING] = _pending(cmd.charging_start(vehicle))
    state.last_accepted[CommandGroup.PROFILE] = NOW
    state.results[CommandGroup.CHARGING] = CommandResult(
        "charging_start", "accepted", NOW
    )
    restored = CommandState.from_dict(state.as_dict())
    assert restored == state

    broken = {
        "pending": [
            {"key": "x", "group": "nope", "accepted_at": NOW.isoformat()},
            {"key": "x", "group": "charging"},
            {"key": 1, "group": "charging", "accepted_at": NOW.isoformat()},
            {"key": "x", "group": "charging", "accepted_at": "2026-11-23T10:00:00"},
            "junk",
        ],
        "last_accepted": {"nope": NOW.isoformat(), "profile": "junk"},
        "results": {"charging": {"key": "x"}, "climate": "junk"},
    }
    assert CommandState.from_dict(broken) == CommandState.empty()
    assert CommandState.from_dict(None) == CommandState.empty()


# --- Pre-send checks -------------------------------------------------------------


def _quota(**kwargs: Any) -> QuotaState:
    values = {
        "limit": 20,
        "remaining": 19,
        "reset_at": NOW + timedelta(minutes=60),
        "last_get_at": NOW - timedelta(minutes=1),
        "last_request_at": NOW - timedelta(minutes=1),
    }
    values.update(kwargs)
    return QuotaState(**values)


def _check(command: cmd.Command, **kwargs: Any) -> None:
    args: dict[str, Any] = {
        "now": NOW,
        "quota": _quota(),
        "polls_per_hour": 12,
        "last_success_at": NOW - timedelta(minutes=1),
        "state": CommandState.empty(),
        "last_sent": None,
    }
    args.update(kwargs)
    cmd.preflight(command, **args)


def test_preflight() -> None:
    """Every preflight rule and the golden reserve."""
    vehicle = command_vehicle()
    start = cmd.charging_start(vehicle)
    limit = cmd.charging_limit(vehicle, 90)
    _check(start)
    _check(limit)

    def reason(command: cmd.Command, **kwargs: Any) -> str:
        with pytest.raises(CommandError) as err:
            _check(command, **kwargs)
        return err.value.reason

    limited = _quota(rate_limited_at=NOW, retry_after=60)
    assert reason(start, quota=limited) == "rate_limited"
    assert reason(start, last_sent=NOW - timedelta(seconds=20)) == "debounced"
    _check(start, last_sent=NOW - timedelta(seconds=26))
    assert reason(start, quota=_quota(last_request_at=NOW - timedelta(seconds=3))) == (
        "too_soon"
    )
    # 12 regular GETs until the reset (:04 ... :59) + golden reserve 1: 13 must
    # remain after the command.
    assert reason(start, quota=_quota(remaining=13)) == "quota_reserve"
    _check(start, quota=_quota(remaining=14))
    # Charging STOP may use the golden reserve (design decision).
    stop = cmd.charging_stop(vehicle)
    _check(stop, quota=_quota(remaining=13))
    assert reason(stop, quota=_quota(remaining=12)) == "quota_reserve"
    _check(stop, quota=_quota(reset_at=NOW - timedelta(seconds=1)))
    # The quota window must be known.
    assert reason(start, quota=_quota(reset_at=NOW - timedelta(seconds=1))) == (
        "quota_reserve"
    )

    # A body from the last GET: recent data, newer than the last change.
    old = NOW - timedelta(minutes=16)
    assert reason(limit, last_success_at=old) == "data_too_old"
    assert reason(limit, last_success_at=None) == "data_too_old"
    _check(start, last_success_at=old)  # POST start/stop: no body from the GET
    # 2 polls per hour: 30 min + 5 min.
    _check(limit, last_success_at=old, polls_per_hour=2, quota=_quota(remaining=19))
    state = CommandState.empty()
    state.last_accepted[CommandGroup.CHARGING_LIMIT] = NOW - timedelta(seconds=30)
    assert reason(limit, state=state) == "previous_not_visible"
    state.last_accepted[CommandGroup.CHARGING_LIMIT] = NOW - timedelta(minutes=2)
    _check(limit, state=state)


@pytest.mark.parametrize(
    ("state", "temperature", "verdict"),
    [
        ("HEATING", 24.0, Verdict.CONFIRMED),
        ("COOLING", 22.0, Verdict.NOT_CONFIRMED),
        ("OFF", 24.0, Verdict.NOT_CONFIRMED),
        ("UNKNOWN", 24.0, Verdict.PENDING),
    ],
)
def test_evaluate_climate_temperature(
    state: str, temperature: float, verdict: Verdict
) -> None:
    """A new temperature: running with that value confirms it."""
    command = cmd.climate_temperature(command_vehicle(), 24)
    assert (command.key, command.expected) == ("climate_temperature", 24.0)
    assert command.body["targetTemperature"] == {"value": 24.0, "unit": "CELSIUS"}
    pending = _pending(command)
    vehicle = command_vehicle(captured="2026-11-23T10:03:00Z")
    vehicle["airConditioning"]["state"] = state
    vehicle["airConditioning"]["targetTemperature"]["value"] = temperature
    assert cmd.evaluate(pending, vehicle, NOW + timedelta(minutes=5), 300) is verdict
    with pytest.raises(CommandError):
        cmd.climate_temperature(command_vehicle(), 24.3)


def test_preflight_pending_guard() -> None:
    """No new command of a group while one waits; STOP may follow a START."""
    vehicle = command_vehicle()
    start = cmd.charging_start(vehicle)
    stop = cmd.charging_stop(vehicle)
    state = CommandState.empty()
    state.pending[CommandGroup.CHARGING] = _pending(start)
    with pytest.raises(CommandError) as err:
        _check(start, state=state)
    assert err.value.reason == "pending"
    _check(stop, state=state)
    # Another group is not blocked.
    _check(cmd.climate_start(vehicle), state=state)

    state.pending[CommandGroup.CHARGING] = _pending(stop)
    for command in (start, stop):
        with pytest.raises(CommandError):
            _check(command, state=state)
    # A temperature change waits like a START; STOP may follow it.
    state.pending[CommandGroup.CLIMATE] = _pending(cmd.climate_temperature(vehicle, 23))
    _check(cmd.climate_stop(vehicle), state=state)
    with pytest.raises(CommandError):
        _check(cmd.climate_start(vehicle), state=state)


def test_evaluate_setting_not_confirmed_by_fresh_data() -> None:
    """A setting: a different value captured after the command decides."""
    pending = _pending(cmd.charging_limit(command_vehicle(), 90))
    soon = NOW + timedelta(minutes=5)
    old = command_vehicle()
    assert cmd.evaluate(pending, old, soon, 300) is Verdict.PENDING
    fresh = command_vehicle(captured="2026-11-23T10:03:00+00:00")
    assert cmd.evaluate(pending, fresh, soon, 300) is Verdict.NOT_CONFIRMED
