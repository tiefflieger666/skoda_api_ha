"""Confirm charging START/STOP by the user's own charger power sensor.

Optional: not every user has a charger with a power sensor. Without one, the
Škoda API confirms (next regular GET, or the confirmation GET if enabled).

500 W and 10 W are the defaults, the user can change both in the options:

* START is confirmed when the charger was idle (<= 10 W) when the command was
  accepted and then delivers >= 500 W for 20 s.
* STOP is confirmed when the charger was charging (>= 500 W) when the command
  was accepted and then stays <= 10 W for 20 s.
* Any other starting point (sensor unavailable, already charging at START)
  leaves the decision to the API.

A sensor reports only on change, so a check for fresh values ("heartbeat")
would fail on a steady value. Instead a 20 s timer starts on the first
value past the threshold and is cancelled by a value back across it, so a
steady value confirms without further reports. Confirmation by power is
evidence of energy flow at the charger, not of the API command itself.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfPower
from homeassistant.core import Event, EventStateChangedData, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .commands import CommandGroup, PendingCommand
from .const import DEFAULT_CHARGER_ACTIVE_W, DEFAULT_CHARGER_QUIET_W

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

# Defaults; the user can change both in the options (every charger differs).
METER_ACTIVE_W = float(DEFAULT_CHARGER_ACTIVE_W)
METER_QUIET_W = float(DEFAULT_CHARGER_QUIET_W)
METER_HOLD_SECONDS = 20
MAX_POWER_W = 50_000.0
_UNIT_FACTORS = {
    UnitOfPower.WATT: 1.0,
    UnitOfPower.KILO_WATT: 1000.0,
    UnitOfPower.MEGA_WATT: 1_000_000.0,
}


def power_watts(state: State | None) -> float | None:
    """Power of a sensor state in W (W, kW or MW), or None when unusable."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    factor = _UNIT_FACTORS.get(state.attributes.get("unit_of_measurement"))
    if factor is None:
        return None
    try:
        value = float(state.state) * factor
    except ValueError:
        return None
    if not math.isfinite(value) or not 0 <= value <= MAX_POWER_W:
        return None
    return value


def meter_can_decide(
    pending: PendingCommand,
    active_w: float = METER_ACTIVE_W,
    quiet_w: float = METER_QUIET_W,
) -> bool:
    """Return True when the power at acceptance lets the meter decide."""
    if pending.group is not CommandGroup.CHARGING or pending.baseline_w is None:
        return False
    if pending.expected:
        return pending.baseline_w <= quiet_w
    return pending.baseline_w >= active_w


def target_reached(
    pending: PendingCommand,
    power: float | None,
    active_w: float = METER_ACTIVE_W,
    quiet_w: float = METER_QUIET_W,
) -> bool:
    """Return True when the power shows the result the command asked for."""
    if power is None:
        return False
    return power >= active_w if pending.expected else power <= quiet_w


class ChargerMeter:
    """Watch the charger power sensor while a charging command is pending."""

    def __init__(  # noqa: PLR0913 - the thresholds are keyword-only options
        self,
        hass: HomeAssistant,
        entity_id: str,
        get_pending: Callable[[], PendingCommand | None],
        on_confirmed: Callable[[PendingCommand], None],
        *,
        active_w: float = METER_ACTIVE_W,
        quiet_w: float = METER_QUIET_W,
    ) -> None:
        """Create the watcher; it is idle until ``async_update`` finds work."""
        self.hass = hass
        self.entity_id = entity_id
        self.active_w = active_w
        self.quiet_w = quiet_w
        self._get_pending = get_pending
        self._on_confirmed = on_confirmed
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._watching: PendingCommand | None = None

    def baseline(self) -> float | None:
        """Return the current charger power in W (baseline at acceptance)."""
        return power_watts(self.hass.states.get(self.entity_id))

    @callback
    def async_update(self) -> None:
        """Start or stop watching for the current pending charging command."""
        pending = self._get_pending()
        if pending is None or not meter_can_decide(
            pending, self.active_w, self.quiet_w
        ):
            self.async_stop()
            return
        if pending is self._watching:
            return
        self.async_stop()
        self._watching = pending
        self._unsub_state = async_track_state_change_event(
            self.hass, self.entity_id, self._async_state_changed
        )
        # The current value counts too (e.g. after a restart, or a sensor
        # that already reported): the baseline was the opposite, so it is
        # newer than the command.
        self._check(self.hass.states.get(self.entity_id))

    @callback
    def async_stop(self) -> None:
        """Stop watching and cancel a running hold timer."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        self._cancel_timer()
        self._watching = None

    def _cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        self._check(event.data["new_state"])

    @callback
    def _check(self, state: State | None) -> None:
        pending = self._watching
        if pending is None:  # pragma: no cover - unsubscribed before
            return
        if not target_reached(pending, power_watts(state), self.active_w, self.quiet_w):
            self._cancel_timer()
            return
        if self._unsub_timer is None:
            self._unsub_timer = async_call_later(
                self.hass, METER_HOLD_SECONDS, self._async_hold_done
            )

    @callback
    def _async_hold_done(self, _now: object) -> None:
        self._unsub_timer = None
        pending = self._watching
        if pending is None or pending is not self._get_pending():  # pragma: no cover
            return  # stopped or replaced; async_stop cancels the timer first
        self.async_stop()
        self._on_confirmed(pending)
