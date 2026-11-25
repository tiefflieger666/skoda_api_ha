"""Config flow for the Škoda API integration.

Steps: user, reauth, reconfigure and options. No YAML.
Every key check is exactly one ``GET /api/v1/vehicles/{vin}``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .api import (
    SkodaApiClient,
    SkodaAuthError,
    SkodaConnectionError,
    SkodaForbiddenError,
    SkodaRateLimitError,
    SkodaResponseError,
    VehicleResponse,
)
from .const import (
    CONF_API_ENABLED,
    CONF_API_KEY_CANDIDATE,
    CONF_CHARGER_ACTIVE_W,
    CONF_CHARGER_POWER_ENTITY,
    CONF_CHARGER_QUIET_W,
    CONF_CONFIRM_GET,
    CONF_DEVICE_NAME_SOURCE,
    CONF_POLLS_PER_HOUR,
    CONF_VIN,
    DEFAULT_API_ENABLED,
    DEFAULT_CHARGER_ACTIVE_W,
    DEFAULT_CHARGER_QUIET_W,
    DEFAULT_CONFIRM_GET,
    DEFAULT_NAME_SOURCE,
    DEFAULT_POLLS_PER_HOUR,
    DOMAIN,
    ISSUE_KEY_REJECTED,
    LOGGER,
    MAX_CHARGER_ACTIVE_W,
    MAX_CHARGER_QUIET_W,
    MAX_POLLS_PER_HOUR,
    MIN_POLLS_PER_HOUR,
    NAME_SOURCES,
)
from .coordinator import async_hand_over_flow_get, async_record_flow_get
from .naming import device_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .data import SkodaConfigEntry

# 17 characters, no I, O or Q (ISO 3779).
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

API_KEY_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
NAME_SOURCE_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=list(NAME_SOURCES),
        mode=SelectSelectorMode.LIST,
        translation_key=CONF_DEVICE_NAME_SOURCE,
    )
)

POWER_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain="sensor", device_class="power")
)


def _watts_selector(maximum: int) -> NumberSelector:
    """Threshold of the charger power sensor in W."""
    return NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=maximum,
            step=1,
            unit_of_measurement="W",
            mode=NumberSelectorMode.BOX,
        )
    )


def _power_entity_field(current: str | None) -> vol.Optional:
    """Return the optional charger power sensor field (may stay empty)."""
    return vol.Optional(
        CONF_CHARGER_POWER_ENTITY, description={"suggested_value": current}
    )


def _normalize_vin(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


# Order matters: SkodaForbiddenError is a subclass of SkodaAuthError.
_ERROR_CODES: tuple[tuple[type[Exception], str], ...] = (
    (SkodaForbiddenError, "vehicle_not_covered"),
    (SkodaAuthError, "invalid_auth"),
    (SkodaRateLimitError, "rate_limited"),
    (SkodaConnectionError, "cannot_connect"),
    (SkodaResponseError, "unexpected_response"),
)


def _error_code(err: Exception) -> str:
    """Map a GET failure to a form error."""
    for error_type, code in _ERROR_CODES:
        if isinstance(err, error_type):
            return code
    LOGGER.error("Unexpected error while checking the API key", exc_info=err)
    return "unknown"


class SkodaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Škoda API."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: SkodaConfigEntry) -> OptionsFlow:  # noqa: ARG004
        """Return the options flow."""
        return SkodaOptionsFlow()

    async def _async_validate(
        self,
        api_key: str,
        vin: str,
        errors: dict[str, str],
        entry: SkodaConfigEntry | None = None,
    ) -> VehicleResponse | None:
        """Check the key with one GET; fill ``errors`` on failure.

        For an existing entry (reauth, reconfigure) the GET is written to
        ``Store`` and the quota planner, whatever its outcome. For a new
        vehicle a successful GET is handed over to the first setup.
        """
        client = SkodaApiClient(async_get_clientsession(self.hass), api_key)
        started = dt_util.utcnow()
        response: VehicleResponse | None = None
        error: Exception | None = None
        try:
            response = await client.async_get_vehicle(vin)
        except Exception as err:  # noqa: BLE001
            error = err
            errors["base"] = _error_code(err)
        if entry is not None:
            await async_record_flow_get(self.hass, entry, started, response, error)
        elif response is not None:
            async_hand_over_flow_get(self.hass, vin, started, response)
        return response

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for API key, VIN and the device name source."""
        errors: dict[str, str] = {}
        if user_input is not None:
            vin = _normalize_vin(user_input[CONF_VIN])
            source = user_input[CONF_DEVICE_NAME_SOURCE]
            if not VIN_RE.match(vin):
                errors[CONF_VIN] = "invalid_vin"
            else:
                await self.async_set_unique_id(vin)
                self._abort_if_unique_id_configured()
                response = await self._async_validate(
                    user_input[CONF_API_KEY], vin, errors
                )
                if response is not None:
                    options: dict[str, Any] = {
                        CONF_DEVICE_NAME_SOURCE: source,
                        CONF_POLLS_PER_HOUR: DEFAULT_POLLS_PER_HOUR,
                        CONF_CONFIRM_GET: DEFAULT_CONFIRM_GET,
                    }
                    if power_entity := user_input.get(CONF_CHARGER_POWER_ENTITY):
                        options[CONF_CHARGER_POWER_ENTITY] = power_entity
                    return self.async_create_entry(
                        title=device_name(source, response, vin),
                        data={CONF_API_KEY: user_input[CONF_API_KEY], CONF_VIN: vin},
                        options=options,
                    )

        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(CONF_API_KEY): API_KEY_SELECTOR,
                vol.Required(CONF_VIN, default=defaults.get(CONF_VIN, "")): str,
                vol.Required(
                    CONF_DEVICE_NAME_SOURCE,
                    default=defaults.get(CONF_DEVICE_NAME_SOURCE, DEFAULT_NAME_SOURCE),
                ): NAME_SOURCE_SELECTOR,
                _power_entity_field(
                    defaults.get(CONF_CHARGER_POWER_ENTITY)
                ): POWER_ENTITY_SELECTOR,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],  # noqa: ARG002
    ) -> ConfigFlowResult:
        """Start reauthentication after 401/403."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            response = await self._async_validate(
                user_input[CONF_API_KEY], entry.data[CONF_VIN], errors, entry
            )
            if response is not None:
                updates = {CONF_API_KEY: user_input[CONF_API_KEY]}
                if entry.update_listeners:
                    # Loaded: the update listener reloads for the new key.
                    return self.async_update_and_abort(
                        entry, data_updates=updates, reason="reauth_successful"
                    )
                return self.async_update_reload_and_abort(entry, data_updates=updates)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): API_KEY_SELECTOR}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save a new API key as a candidate, without any request.

        The candidate is tried on the next regular GET (0 extra requests) and
        replaces the active key only if it answers for the same VIN and is
        valid longer. Otherwise the active key stays and a
        repair issue says why.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            if not api_key:
                errors["base"] = "invalid_auth"
            elif api_key == entry.data[CONF_API_KEY]:
                errors["base"] = "same_key"
            else:
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_KEY_REJECTED)
                return self.async_update_and_abort(
                    entry,
                    data_updates={CONF_API_KEY_CANDIDATE: api_key},
                    reason="candidate_saved",
                )
        return self.async_show_form(
            step_id=SOURCE_RECONFIGURE,
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): API_KEY_SELECTOR}),
            errors=errors,
        )


class SkodaOptionsFlow(OptionsFlow):
    """Device name source and polling chosen by the user.

    Saving the options never triggers a GET: the update listener changes the
    interval and the names in place.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_POLLS_PER_HOUR] = int(user_input[CONF_POLLS_PER_HOUR])
            user_input[CONF_CHARGER_ACTIVE_W] = int(user_input[CONF_CHARGER_ACTIVE_W])
            user_input[CONF_CHARGER_QUIET_W] = int(user_input[CONF_CHARGER_QUIET_W])
            if user_input[CONF_CHARGER_QUIET_W] >= user_input[CONF_CHARGER_ACTIVE_W]:
                errors[CONF_CHARGER_QUIET_W] = "charger_thresholds"
            else:
                # The API switch is an entity, not a field here: keep its state.
                user_input[CONF_API_ENABLED] = self.config_entry.options.get(
                    CONF_API_ENABLED, DEFAULT_API_ENABLED
                )
                return self.async_create_entry(data=user_input)

        options = {**self.config_entry.options, **(user_input or {})}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE_NAME_SOURCE,
                    default=options.get(CONF_DEVICE_NAME_SOURCE, DEFAULT_NAME_SOURCE),
                ): NAME_SOURCE_SELECTOR,
                vol.Required(
                    CONF_POLLS_PER_HOUR,
                    default=options.get(CONF_POLLS_PER_HOUR, DEFAULT_POLLS_PER_HOUR),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_POLLS_PER_HOUR,
                        max=MAX_POLLS_PER_HOUR,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_CONFIRM_GET,
                    default=options.get(CONF_CONFIRM_GET, DEFAULT_CONFIRM_GET),
                ): BooleanSelector(),
                _power_entity_field(
                    options.get(CONF_CHARGER_POWER_ENTITY)
                ): POWER_ENTITY_SELECTOR,
                vol.Required(
                    CONF_CHARGER_ACTIVE_W,
                    default=options.get(
                        CONF_CHARGER_ACTIVE_W, DEFAULT_CHARGER_ACTIVE_W
                    ),
                ): _watts_selector(MAX_CHARGER_ACTIVE_W),
                vol.Required(
                    CONF_CHARGER_QUIET_W,
                    default=options.get(CONF_CHARGER_QUIET_W, DEFAULT_CHARGER_QUIET_W),
                ): _watts_selector(MAX_CHARGER_QUIET_W),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
