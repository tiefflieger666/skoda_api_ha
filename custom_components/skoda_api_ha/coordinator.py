"""Data update coordinator for the Škoda API integration.

One regular ``GET /api/v1/vehicles/{vin}`` per planned interval. After every
request the next GET is planned from the ``RateLimit-*`` headers (see
``quota.py``) and the state is written to ``Store``, so that a restart of
Home Assistant does not cause an extra GET.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .api import (
    SkodaApiClient,
    SkodaApiError,
    SkodaAuthError,
    SkodaCommandRejectedError,
    SkodaRateLimitError,
    SkodaVehicleBusyError,
    SkodaVinMismatchError,
    VehicleResponse,
)
from .charger_meter import ChargerMeter
from .commands import (
    SNAPSHOT_GROUPS,
    Command,
    CommandError,
    CommandGroup,
    CommandResult,
    CommandState,
    PendingCommand,
    Verdict,
    evaluate,
    preflight,
)
from .const import (
    CONF_API_ENABLED,
    CONF_API_KEY_CANDIDATE,
    CONF_CHARGER_ACTIVE_W,
    CONF_CHARGER_POWER_ENTITY,
    CONF_CHARGER_QUIET_W,
    CONF_CONFIRM_GET,
    CONF_POLLS_PER_HOUR,
    CONF_VIN,
    DEFAULT_API_ENABLED,
    DEFAULT_CHARGER_ACTIVE_W,
    DEFAULT_CHARGER_QUIET_W,
    DEFAULT_CONFIRM_GET,
    DEFAULT_POLLS_PER_HOUR,
    DOMAIN,
    ISSUE_KEY_REJECTED,
    ISSUE_NO_COMMAND_QUOTA,
    LOGGER,
)
from .key_change import CandidateOutcome, judge_candidate
from .quota import (
    GOLDEN_RESERVE,
    PollPlan,
    QuotaState,
    can_spend,
    commands_possible,
    interval_seconds,
    plan_next_get,
    regular_gets_until_reset,
)
from .store import SkodaStore, StoredState

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from typing import Any

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

    from .data import SkodaConfigEntry


def quota_after_flow_get(
    state: QuotaState,
    started: datetime,
    now: datetime,
    response: VehicleResponse | None,
    error: BaseException | None,
) -> QuotaState:
    """Quota state after the one GET of a config flow (user, reauth).

    A successful GET returns fresh data and current quota headers, so it
    counts like the regular GET. Its key becomes the active key (user step,
    reauth), so its expiry is kept as the active key's expiry. A 429 starts
    the ``Retry-After`` cooldown.
    Any other failure is counted conservatively as one request taken from the
    reserve, without moving the regular GET schedule.
    """
    if response is not None:
        quota = response.quota
        return state.with_response(
            started, quota.limit, quota.remaining, quota.reset_at
        )
    if isinstance(error, SkodaRateLimitError):
        return state.with_rate_limit(
            started, now, error.retry_after, error.quota.reset_at
        )
    return state.with_extra_request(started)


async def async_record_flow_get(
    hass: HomeAssistant,
    entry: SkodaConfigEntry,
    started: datetime,
    response: VehicleResponse | None,
    error: BaseException | None,
) -> None:
    """Write the GET of a config flow to ``Store`` and the quota planner.

    A loaded entry updates its coordinator (in memory, replanned and
    persisted). Otherwise the stored state is updated directly, so the setup
    after the flow restores it and makes no extra GET.
    """
    if entry.state is ConfigEntryState.LOADED:
        await entry.runtime_data.coordinator.async_record_flow_get(
            started, response, error
        )
        return
    store = SkodaStore(hass, entry.entry_id)
    stored = await store.async_load()
    stored.quota = quota_after_flow_get(
        stored.quota, started, dt_util.utcnow(), response, error
    )
    if response is not None:
        stored.response = response
        stored.last_success_at = dt_util.utcnow()
        stored.key_expires_at = response.quota.key_expires_at
    await store.async_save(stored)


@dataclass(frozen=True, slots=True)
class FlowGet:
    """The successful GET of the user step, handed over to the first setup."""

    started: datetime
    response: VehicleResponse


# Short-lived hand-over from the user step to the first setup, keyed by VIN.
# The entry (and so its Store) does not exist yet while the user step runs;
# the setup that follows in the same flow takes the GET from here.
FLOW_GET_HANDOVER: HassKey[dict[str, FlowGet]] = HassKey(f"{DOMAIN}_flow_get")


def async_hand_over_flow_get(
    hass: HomeAssistant, vin: str, started: datetime, response: VehicleResponse
) -> None:
    """Keep the user step GET for the setup of the entry it creates."""
    hass.data.setdefault(FLOW_GET_HANDOVER, {})[vin] = FlowGet(started, response)


async def async_take_over_flow_get(
    hass: HomeAssistant, entry: SkodaConfigEntry
) -> None:
    """Write a handed-over user step GET to ``Store`` before the first setup.

    The first setup then restores it like after a restart and makes no GET.
    """
    handover = hass.data.get(FLOW_GET_HANDOVER)
    if not handover or (flow_get := handover.pop(entry.data[CONF_VIN], None)) is None:
        return
    if not handover:
        hass.data.pop(FLOW_GET_HANDOVER)
    await async_record_flow_get(hass, entry, flow_get.started, flow_get.response, None)


# The one confirmation GET after a command (option, off by default). The API
# shows a new charging state only after about 3-4 minutes.
CONFIRM_GET_DELAY_SECONDS = 180

# A scheduled refresh may fire a moment early; within this margin it still
# counts as the regular GET and not as an extra request.
REGULAR_TOLERANCE_SECONDS = 5


class SkodaDataUpdateCoordinator(DataUpdateCoordinator[VehicleResponse]):
    """Fetch ``GET /api/v1/vehicles/{vin}`` for one vehicle."""

    config_entry: SkodaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SkodaConfigEntry,
        client: SkodaApiClient,
        store: SkodaStore,
    ) -> None:
        """Create the coordinator; the real interval comes from the planner."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=interval_seconds(
                    entry.options.get(CONF_POLLS_PER_HOUR, DEFAULT_POLLS_PER_HOUR)
                )
            ),
            always_update=False,
        )
        self.client = client
        self.store = store
        self.vin: str = entry.data[CONF_VIN]
        self.quota_state = QuotaState()
        self.last_success_at: datetime | None = None
        # Expiry of the key in use (X-API-Key-Expires-At), kept in Store.
        self.key_expires_at: datetime | None = None
        self.plan: PollPlan | None = None
        # Commands: pending (waiting for confirmation), last accepted change
        # and last result per group. Kept in Store.
        self.commands = CommandState.empty()
        self._command_lock = asyncio.Lock()
        # Last send per command key (debounce); a restart clears it.
        self._last_sent: dict[str, datetime] = {}
        # Optional charger power sensor; confirmation GET timer.
        self._meter: ChargerMeter | None = None
        self._unsub_confirm_get: CALLBACK_TYPE | None = None

    @property
    def polls_per_hour(self) -> int:
        """Regular GETs per hour chosen by the user."""
        return int(
            self.config_entry.options.get(CONF_POLLS_PER_HOUR, DEFAULT_POLLS_PER_HOUR)
        )

    @property
    def api_enabled(self) -> bool:
        """Whether the integration may talk to the API at all (switch)."""
        return bool(
            self.config_entry.options.get(CONF_API_ENABLED, DEFAULT_API_ENABLED)
        )

    @property
    def regular_polling(self) -> bool:
        """Whether regular GETs run (API on and at least one per hour)."""
        return self.api_enabled and self.polls_per_hour > 0

    def _apply_plan(self, now: datetime) -> PollPlan:
        """Plan the next regular GET and use it as the refresh interval.

        Without regular polling there is no interval (no timer at all).
        """
        plan = plan_next_get(self.quota_state, now, self.polls_per_hour)
        self.plan = plan
        if self.regular_polling:
            self.update_interval = timedelta(seconds=max(1.0, plan.delay))
        else:
            self.update_interval = None
            self._async_unsub_refresh()
        return plan

    def _extra_allowed(self, now: datetime) -> bool:
        """Whether a GET outside the regular plan may be sent now.

        Normally only within the reserve (golden reserve). Without regular
        GETs (0 per hour) nothing else ever learns the new window, so after
        the quota has reset (and outside a 429 wait) a refresh is allowed.
        """
        state = self.quota_state
        if can_spend(state, now, self.polls_per_hour):
            return True
        return (
            not self.regular_polling
            and state.retry_delay(now) == 0
            and not state.window_open(now)
        )

    @callback
    def _update_quota_issue(self) -> None:
        """Warn when the chosen polling leaves no quota for commands."""
        if commands_possible(self.quota_state.limit, self.polls_per_hour):
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_COMMAND_QUOTA)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_NO_COMMAND_QUOTA,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_NO_COMMAND_QUOTA,
            translation_placeholders={"polls": str(self.polls_per_hour)},
        )

    async def async_restore(self) -> bool:
        """Load the stored state before the first refresh.

        Returns True when the stored response is used and no GET is due yet.
        Returns False when a regular GET is due now. Raises
        ``ConfigEntryNotReady`` (without any request) when nothing is stored
        but the quota says to wait.
        """
        stored = await self.store.async_load()
        self.quota_state = stored.quota
        self.last_success_at = stored.last_success_at
        self.key_expires_at = stored.key_expires_at
        self.commands = stored.commands
        self._setup_meter()
        self._update_quota_issue()
        plan = self._apply_plan(dt_util.utcnow())
        if not self.api_enabled:
            # API switched off: no request at all, also not at setup.
            if stored.response is not None:
                self.async_set_updated_data(stored.response)
            return True
        if not self.regular_polling and stored.response is not None:
            # No regular GETs: the stored answer stays until a manual refresh.
            self.async_set_updated_data(stored.response)
            return True
        if plan.delay <= REGULAR_TOLERANCE_SECONDS:
            return False
        if stored.response is None:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="quota_wait",
                translation_placeholders={"seconds": str(round(plan.delay))},
            )
        LOGGER.debug("Restored the last response; next GET in %.0f s", plan.delay)
        self.async_set_updated_data(stored.response)
        return True

    async def async_options_updated(self) -> None:
        """Replan after the user changed the polling options. No request."""
        self._setup_meter()
        self._update_quota_issue()
        self._apply_plan(dt_util.utcnow())
        if not self.api_enabled:
            self._cancel_confirm_get()
        if self._listeners:
            self._schedule_refresh()
        # Controls follow the API switch (available or not).
        self.async_update_listeners()

    async def async_record_flow_get(
        self,
        started: datetime,
        response: VehicleResponse | None,
        error: BaseException | None,
    ) -> None:
        """Count a reauth flow GET, replan and persist. No request."""
        now = dt_util.utcnow()
        self.quota_state = quota_after_flow_get(
            self.quota_state, started, now, response, error
        )
        self._apply_plan(now)
        if response is not None:
            # The flow key replaces the active key (reload follows).
            self.key_expires_at = response.quota.key_expires_at
            self.last_success_at = now
            # Also restarts the refresh timer with the new interval.
            self.async_set_updated_data(response)
        elif self._listeners:
            self._schedule_refresh()
        await self._async_persist()

    async def _async_persist(self, response: VehicleResponse | None = None) -> None:
        await self.store.async_save(
            StoredState(
                quota=self.quota_state,
                response=response if response is not None else self.data,
                last_success_at=self.last_success_at,
                key_expires_at=self.key_expires_at,
                commands=self.commands,
            )
        )

    def _keep_data(self, err: Exception) -> VehicleResponse:
        """After a rejected candidate: keep the last data (the old key is fine)."""
        if self.data is not None:
            return self.data
        raise UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="update_failed",
            translation_placeholders={"error": type(err).__name__},
        ) from err

    def _drop_candidate(self, api_key: str | None = None) -> None:
        """Remove the candidate from the entry; optionally make it the key."""
        entry = self.config_entry
        data = {k: v for k, v in entry.data.items() if k != CONF_API_KEY_CANDIDATE}
        if api_key is not None:
            data[CONF_API_KEY] = api_key
            # Before the entry update, so the update listener sees no new key
            # and does not reload.
            self.client.set_api_key(api_key)
        self.hass.config_entries.async_update_entry(entry, data=data)

    def _reject_candidate(self, outcome: CandidateOutcome) -> None:
        """Forget the candidate, keep the active key and tell the user."""
        self._drop_candidate()
        LOGGER.warning(
            "The new API key was rejected (%s); the current key stays in use",
            outcome.value,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_KEY_REJECTED,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=f"key_rejected_{outcome.value}",
        )

    def _finish_candidate(self, candidate: str, response: VehicleResponse) -> None:
        """Apply the key change rule after a successful trial GET."""
        new_expiry = response.quota.key_expires_at
        outcome = judge_candidate(
            vin=self.vin,
            answered_vin=response.vin,
            new_expiry=new_expiry,
            active_expiry=self.key_expires_at,
            now=dt_util.utcnow(),
        )
        if outcome is CandidateOutcome.WAIT:
            LOGGER.info("The new API key is checked again on the next regular update")
        elif outcome is CandidateOutcome.ACTIVATE:
            self._drop_candidate(candidate)
            self.key_expires_at = new_expiry
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_KEY_REJECTED)
            LOGGER.info("The new API key is now in use")
        else:
            self._reject_candidate(outcome)

    async def _async_update_data(self) -> VehicleResponse:
        """Exactly one GET when the planner allows it; no automatic retry.

        A key candidate from the reconfigure flow is tried only on a regular
        GET, never on an extra request.
        """
        started = dt_util.utcnow()
        plan = self._apply_plan(started)
        if not self.api_enabled:
            # API switched off: no request (a timer that fired anyway).
            if self.data is None:
                raise UpdateFailed(
                    translation_domain=DOMAIN, translation_key="api_disabled"
                )
            return self.data
        extra = (
            plan.delay > REGULAR_TOLERANCE_SECONDS or not self.regular_polling
        ) and self.data is not None
        source = "confirmation_get" if extra else "regular_get"
        if extra:
            # Not the regular GET (manual refresh, update_entity): it is
            # taken from the reserve, and only if the golden reserve holds.
            if not self._extra_allowed(started):
                LOGGER.debug("Refresh skipped to protect the quota reserve")
                return self.data
            self.quota_state = self.quota_state.with_extra_request(started)

        candidate: str | None = (
            None if extra else self.config_entry.data.get(CONF_API_KEY_CANDIDATE)
        )
        client = self.client.with_api_key(candidate) if candidate else self.client
        try:
            response = await client.async_get_vehicle(self.vin)
        except SkodaRateLimitError as err:
            self.quota_state = self.quota_state.with_rate_limit(
                started, dt_util.utcnow(), err.retry_after, err.quota.reset_at
            )
            await self._async_persist()
            plan = self._apply_plan(dt_util.utcnow())
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="rate_limited",
                retry_after=max(1.0, plan.delay),
            ) from err
        except SkodaAuthError as err:
            self.quota_state = self.quota_state.with_failed_get(started)
            await self._async_persist()
            self._apply_plan(dt_util.utcnow())
            if candidate:
                self._reject_candidate(CandidateOutcome.REJECT_AUTH)
                return self._keep_data(err)
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except SkodaApiError as err:
            self.quota_state = self.quota_state.with_failed_get(started)
            if isinstance(err, SkodaVehicleBusyError):
                # The vehicle declined; the quota headers are still current.
                self.quota_state = self.quota_state.with_command_response(
                    err.quota.limit, err.quota.remaining, err.quota.reset_at
                )
            await self._async_persist()
            self._apply_plan(dt_util.utcnow())
            if candidate and isinstance(err, SkodaVinMismatchError):
                self._reject_candidate(CandidateOutcome.REJECT_VIN_MISMATCH)
                return self._keep_data(err)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": type(err).__name__},
            ) from err

        quota = response.quota
        self.quota_state = self.quota_state.with_response(
            started, quota.limit, quota.remaining, quota.reset_at
        )
        self.last_success_at = dt_util.utcnow()
        if candidate:
            self._finish_candidate(candidate, response)
        elif quota.key_expires_at is not None:
            self.key_expires_at = quota.key_expires_at
        # The data always shows the expiry of the key in use.
        response = replace(
            response, quota=replace(quota, key_expires_at=self.key_expires_at)
        )
        self._confirm_pending(response, dt_util.utcnow(), source)
        await self._async_persist(response)
        self._apply_plan(dt_util.utcnow())
        return response

    # --- Commands --------------------------------------------------------------

    def _confirm_pending(
        self, response: VehicleResponse, now: datetime, source: str
    ) -> None:
        """Decide on accepted commands from a new GET (no extra request)."""
        for group, pending in list(self.commands.pending.items()):
            verdict = evaluate(
                pending, response.vehicle, now, interval_seconds(self.polls_per_hour)
            )
            if verdict is Verdict.PENDING:
                continue
            del self.commands.pending[group]
            self.commands.results[group] = CommandResult(
                pending.key, verdict.value, now, source
            )
            LOGGER.info("Command %s: %s", pending.key, verdict.value)
        if self._meter is not None:
            self._meter.async_update()

    def _setup_meter(self) -> None:
        """(Re)create the optional charger power watcher from the options."""
        if self._meter is not None:
            self._meter.async_stop()
            self._meter = None
        options = self.config_entry.options
        entity_id = options.get(CONF_CHARGER_POWER_ENTITY)
        if not entity_id:
            return
        self._meter = ChargerMeter(
            self.hass,
            entity_id,
            lambda: self.commands.pending.get(CommandGroup.CHARGING),
            self._on_meter_confirmed,
            active_w=float(
                options.get(CONF_CHARGER_ACTIVE_W, DEFAULT_CHARGER_ACTIVE_W)
            ),
            quiet_w=float(options.get(CONF_CHARGER_QUIET_W, DEFAULT_CHARGER_QUIET_W)),
        )
        self._meter.async_update()

    def _on_meter_confirmed(self, pending: PendingCommand) -> None:
        """Confirm a charging command by the charger power (no request)."""
        del self.commands.pending[CommandGroup.CHARGING]
        self.commands.results[CommandGroup.CHARGING] = CommandResult(
            pending.key, Verdict.CONFIRMED.value, dt_util.utcnow(), "charger_power"
        )
        LOGGER.info("Command %s: confirmed by the charger power", pending.key)
        self.config_entry.async_create_task(
            self.hass, self._async_persist(), "skoda_api_ha_persist"
        )
        self.async_update_listeners()

    def _schedule_confirm_get(self, pending: PendingCommand) -> None:
        """Plan the one confirmation GET after a command (if enabled).

        It is an extra request: when it is due it is sent only if the golden
        reserve holds and the command is still pending; otherwise the next
        regular GET (or the charger power) confirms.
        """
        if not self.config_entry.options.get(CONF_CONFIRM_GET, DEFAULT_CONFIRM_GET):
            return
        self._cancel_confirm_get()

        @callback
        def _due(_now: datetime) -> None:
            self._unsub_confirm_get = None
            if self.commands.pending.get(pending.group) is not pending:
                return
            self.config_entry.async_create_task(
                self.hass, self.async_refresh(), "skoda_api_ha_confirmation_get"
            )

        self._unsub_confirm_get = async_call_later(
            self.hass, CONFIRM_GET_DELAY_SECONDS, _due
        )

    def _cancel_confirm_get(self) -> None:
        if self._unsub_confirm_get is not None:
            self._unsub_confirm_get()
            self._unsub_confirm_get = None

    async def async_shutdown(self) -> None:
        """Stop the meter and the confirmation timer on unload."""
        self._cancel_confirm_get()
        if self._meter is not None:
            self._meter.async_stop()
        await super().async_shutdown()

    async def async_manual_refresh(self) -> None:
        """Refresh now (refresh button).

        Before the regular GET is due this is an extra request from the
        reserve: refused with a message when it would break the golden
        reserve (or during a 429 wait), instead of silently doing nothing.
        """
        if not self.api_enabled:
            raise _validation_error("api_disabled")
        now = dt_util.utcnow()
        plan = plan_next_get(self.quota_state, now, self.polls_per_hour)
        regular_due = self.regular_polling and plan.delay <= REGULAR_TOLERANCE_SECONDS
        if not regular_due and not self._extra_allowed(now):
            raise _validation_error("refresh_reserve")
        await self.async_refresh()
        if not self.last_update_success:
            raise _command_error("refresh_failed")

    async def async_clear_pending(self) -> None:
        """Forget every pending command (button; 0 requests).

        The way out when a command can never be decided, e.g. the vehicle
        ignored it and reports nothing new. The next GET shows the real state.
        """
        now = dt_util.utcnow()
        for group, pending in list(self.commands.pending.items()):
            del self.commands.pending[group]
            self.commands.results[group] = CommandResult(
                pending.key, "cleared", now, "manual"
            )
            LOGGER.info("Command %s: pending state cleared", pending.key)
        self._cancel_confirm_get()
        await self._async_persist()
        if self._meter is not None:
            self._meter.async_update()
        self.async_update_listeners()

    def pending_command(self, group: CommandGroup) -> PendingCommand | None:
        """Return the accepted command of a group that waits for confirmation."""
        return self.commands.pending.get(group)

    async def async_send_command(
        self, build: Callable[[dict[str, Any]], Command]
    ) -> None:
        """Build, check and send exactly one command (POST or PUT).

        ``build`` gets the vehicle data of the last GET (see ``commands.py``).
        Nothing is retried. A 202 answer means accepted, not executed: the
        command becomes pending until a later GET confirms it.
        """
        if not self.api_enabled:
            raise _validation_error("api_disabled")
        if self._command_lock.locked():
            raise _validation_error("busy")
        async with self._command_lock:
            await self._async_send_command(build)

    def check_command(self, build: Callable[[dict[str, Any]], Command]) -> None:
        """Run every check of ``async_send_command`` without sending anything."""
        if not self.api_enabled:
            raise _validation_error("api_disabled")
        if self._command_lock.locked():
            raise _validation_error("busy")
        self._prepare_command(build, dt_util.utcnow())

    def quota_placeholders(self, now: datetime) -> dict[str, str]:
        """Return the requests left, those free for commands and the reset time."""
        state = self.quota_state
        if not state.window_open(now) or state.reset_at is None:
            return {"remaining": "?", "spare": "?", "reset": "?"}
        remaining = state.remaining or 0
        spare = (
            remaining
            - regular_gets_until_reset(state, now, self.polls_per_hour)
            - GOLDEN_RESERVE
        )
        return {
            "remaining": str(remaining),
            "spare": str(max(0, spare)),
            "reset": dt_util.as_local(state.reset_at).strftime("%H:%M"),
        }

    def _prepare_command(
        self, build: Callable[[dict[str, Any]], Command], now: datetime
    ) -> Command:
        """Build the command and run the pre-send checks (nothing is sent)."""
        if self.data is None:
            raise _validation_error("no_data")
        try:
            command = build(self.data.vehicle)
            preflight(
                command,
                now=now,
                quota=self.quota_state,
                polls_per_hour=self.polls_per_hour,
                last_success_at=self.last_success_at,
                state=self.commands,
                last_sent=self._last_sent.get(command.key),
            )
        except CommandError as err:
            placeholders = dict(err.placeholders)
            if err.reason == "quota_reserve":
                placeholders |= self.quota_placeholders(now)
            raise _validation_error(err.reason, placeholders) from err
        return command

    async def _async_send_command(
        self, build: Callable[[dict[str, Any]], Command]
    ) -> None:
        now = dt_util.utcnow()
        command = self._prepare_command(build, now)

        # Counted before sending: a lost answer may still have used a request.
        self.quota_state = self.quota_state.with_extra_request(now)
        self._last_sent[command.key] = now
        try:
            response = await self.client.async_send_command(
                self.vin, command.method, command.path, command.body
            )
        except SkodaVehicleBusyError as err:
            # Declined by the vehicle, not the quota: no cooldown, the
            # headers tell what is left.
            self.quota_state = self.quota_state.with_command_response(
                err.quota.limit, err.quota.remaining, err.quota.reset_at
            )
            await self._async_command_failed(command, "rejected", str(err))
            raise _command_error("vehicle_busy") from err
        except SkodaRateLimitError as err:
            self.quota_state = self.quota_state.with_command_rate_limit(
                dt_util.utcnow(), err.retry_after, err.quota.reset_at
            )
            await self._async_command_failed(command, "rejected", "http_429")
            raise _command_error("rate_limited") from err
        except SkodaAuthError as err:
            await self._async_command_failed(command, "rejected", "auth")
            self.config_entry.async_start_reauth(self.hass)
            raise _command_error("auth_failed") from err
        except SkodaCommandRejectedError as err:
            detail = err.problem or f"http_{err.status}"
            await self._async_command_failed(command, "rejected", detail)
            raise _command_error("rejected", {"error": detail}) from err
        except SkodaApiError as err:
            # No clear answer: the vehicle may have executed it. Treat a
            # settings change as accepted for the next body (never undo it).
            await self._async_command_failed(command, "uncertain", type(err).__name__)
            raise _command_error("uncertain") from err

        done = dt_util.utcnow()
        quota = response.quota
        self.quota_state = self.quota_state.with_command_response(
            quota.limit, quota.remaining, quota.reset_at
        )
        baseline = (
            self._meter.baseline()
            if self._meter is not None and command.group is CommandGroup.CHARGING
            else None
        )
        pending = PendingCommand.from_command(command, done, baseline)
        self.commands.pending[command.group] = pending
        if command.group in SNAPSHOT_GROUPS:
            self.commands.last_accepted[command.group] = done
        self.commands.results[command.group] = CommandResult(
            command.key, "accepted", done
        )
        LOGGER.info("Command %s accepted (HTTP %s)", command.key, response.status)
        if self._meter is not None:
            self._meter.async_update()
        self._schedule_confirm_get(pending)
        await self._async_after_command()

    async def _async_command_failed(
        self, command: Command, result: str, detail: str
    ) -> None:
        now = dt_util.utcnow()
        if result == "uncertain" and command.group in SNAPSHOT_GROUPS:
            self.commands.last_accepted[command.group] = now
        self.commands.results[command.group] = CommandResult(
            command.key, result, now, detail
        )
        LOGGER.warning("Command %s %s (%s)", command.key, result, detail)
        await self._async_after_command()

    async def _async_after_command(self) -> None:
        """Persist, replan the regular GET and show the new command state."""
        await self._async_persist()
        self._apply_plan(dt_util.utcnow())
        if self._listeners:
            self._schedule_refresh()
        self.async_update_listeners()


def _validation_error(
    reason: str, placeholders: dict[str, str] | None = None
) -> ServiceValidationError:
    """Return the error for a command refused before sending (nothing sent)."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=f"command_{reason}",
        translation_placeholders=placeholders or {},
    )


def _command_error(
    reason: str, placeholders: dict[str, str] | None = None
) -> HomeAssistantError:
    """Return the error for a command that was sent but not accepted."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=f"command_{reason}",
        translation_placeholders=placeholders or {},
    )
