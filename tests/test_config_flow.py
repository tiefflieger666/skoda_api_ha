"""Config flow tests: user, reauth, reconfigure, options, device name source."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.data_entry_flow import FlowResultType

from custom_components.skoda_api_ha.const import (
    CONF_API_ENABLED,
    CONF_API_KEY_CANDIDATE,
    CONF_CHARGER_ACTIVE_W,
    CONF_CHARGER_POWER_ENTITY,
    CONF_CHARGER_QUIET_W,
    CONF_CONFIRM_GET,
    CONF_DEVICE_NAME_SOURCE,
    CONF_POLLS_PER_HOUR,
    CONF_VIN,
    DOMAIN,
    NAME_SOURCE_LICENSE_PLATE,
    NAME_SOURCE_VEHICLE_NAME,
    NAME_SOURCE_VIN,
)

from .conftest import setup_integration
from .const import (
    API_KEY,
    NEW_API_KEY,
    OTHER_VIN,
    QUOTA_HEADERS,
    VEHICLE_URL,
    VIN,
    vehicle_body,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )

USER_INPUT = {
    CONF_API_KEY: API_KEY,
    CONF_VIN: VIN,
    CONF_DEVICE_NAME_SOURCE: NAME_SOURCE_VEHICLE_NAME,
}


@pytest.mark.usefixtures("mock_setup_entry")
@pytest.mark.parametrize(
    ("source", "body", "title"),
    [
        (NAME_SOURCE_VEHICLE_NAME, vehicle_body(), "FICTION CAR"),
        (NAME_SOURCE_LICENSE_PLATE, vehicle_body(), "XX TEST-01"),
        (NAME_SOURCE_VIN, vehicle_body(), VIN),
        # Missing plate falls back to the vehicle name, never to the VIN.
        (NAME_SOURCE_LICENSE_PLATE, vehicle_body(plate=None), "FICTION CAR"),
        # Missing name falls back to a generic name.
        (NAME_SOURCE_VEHICLE_NAME, vehicle_body(name=None), "Škoda"),
    ],
)
async def test_user_flow_success(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    source: str,
    body: dict,
    title: str,
) -> None:
    """A valid key creates the entry with one GET and the chosen name."""
    aioclient_mock.get(VEHICLE_URL, json=body, headers=QUOTA_HEADERS)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **USER_INPUT,
            CONF_VIN: " tmbtest 0000000001 ",
            CONF_DEVICE_NAME_SOURCE: source,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == title
    assert result["data"] == {CONF_API_KEY: API_KEY, CONF_VIN: VIN}
    assert result["options"] == {
        CONF_DEVICE_NAME_SOURCE: source,
        CONF_POLLS_PER_HOUR: 12,
        CONF_CONFIRM_GET: False,
    }
    assert result["result"].unique_id == VIN
    assert aioclient_mock.call_count == 1
    # The key is sent only as a header.
    _, url, _, headers = aioclient_mock.mock_calls[0]
    assert API_KEY not in str(url)
    assert headers["X-API-Key"] == API_KEY


@pytest.mark.usefixtures("mock_setup_entry")
@pytest.mark.parametrize(
    ("mock_kwargs", "error"),
    [
        ({"status": 401}, "invalid_auth"),
        ({"status": 403}, "vehicle_not_covered"),
        ({"status": 429, "headers": {"Retry-After": "120"}}, "rate_limited"),
        ({"status": 500}, "cannot_connect"),
        ({"exc": TimeoutError()}, "cannot_connect"),
        ({"status": 404}, "unexpected_response"),
        ({"json": {"unexpected": True}}, "unexpected_response"),
        ({"json": vehicle_body(vin=OTHER_VIN)}, "unexpected_response"),
    ],
)
async def test_user_flow_errors_and_recovery(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_kwargs: dict,
    error: str,
) -> None:
    """Every API error shows a form error; the user can retry."""
    aioclient_mock.get(VEHICLE_URL, **mock_kwargs)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    aioclient_mock.clear_requests()
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_unknown_error(hass: HomeAssistant) -> None:
    """An unexpected exception is reported as unknown."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.skoda_api_ha.config_flow.SkodaApiClient.async_get_vehicle",
        side_effect=RuntimeError,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


@pytest.mark.usefixtures("mock_setup_entry")
@pytest.mark.parametrize(
    "vin", ["TOO-SHORT", "TMBTEST000000000I", "TMBTEST00000000012"]
)
async def test_user_flow_invalid_vin(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, vin: str
) -> None:
    """An invalid VIN is rejected without any request."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_VIN: vin}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_VIN: "invalid_vin"}
    assert aioclient_mock.call_count == 0


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_duplicate_vin(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """The same VIN twice is aborted before any request."""
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_VIN: VIN.lower()}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert aioclient_mock.call_count == 0


async def test_reauth_flow(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Reauth: error first, then the new key is stored."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    aioclient_mock.get(VEHICLE_URL, status=401)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_API_KEY}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_API_KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_KEY] == NEW_API_KEY
    assert config_entry.data[CONF_VIN] == VIN
    # The scheduled reload must finish inside the test: a setup still running
    # at teardown leaves platform timers behind (device_tracker).
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED


async def test_reauth_wrong_vin(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A key that answers with another VIN is not accepted."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(vin=OTHER_VIN))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_API_KEY}
    )
    assert result["errors"] == {"base": "unexpected_response"}
    assert config_entry.data[CONF_API_KEY] == API_KEY


async def test_reconfigure_saves_candidate_without_request(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Reconfigure only saves the candidate: no GET, no reload, key unchanged."""
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    aioclient_mock.clear_requests()

    result = await config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    # The active key and an empty key are refused.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: API_KEY}
    )
    assert result["errors"] == {"base": "same_key"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "   "}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: f" {NEW_API_KEY} "}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "candidate_saved"
    assert config_entry.data[CONF_API_KEY] == API_KEY
    assert config_entry.data[CONF_API_KEY_CANDIDATE] == NEW_API_KEY
    assert config_entry.state is ConfigEntryState.LOADED
    reload.assert_not_called()
    assert aioclient_mock.call_count == 0


async def test_reconfigure_not_loaded(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A candidate can be saved while the entry is not loaded."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_API_KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "candidate_saved"
    assert config_entry.data[CONF_API_KEY_CANDIDATE] == NEW_API_KEY
    assert aioclient_mock.call_count == 0


async def test_options_flow(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Options change polling and the name source without any GET."""
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    with patch("custom_components.skoda_api_ha.PLATFORMS", []):
        await setup_integration(hass, config_entry)
    calls_after_setup = aioclient_mock.call_count

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DEVICE_NAME_SOURCE: NAME_SOURCE_LICENSE_PLATE,
            CONF_POLLS_PER_HOUR: 2.0,
            CONF_CONFIRM_GET: True,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        CONF_DEVICE_NAME_SOURCE: NAME_SOURCE_LICENSE_PLATE,
        CONF_POLLS_PER_HOUR: 2,
        CONF_CONFIRM_GET: True,
        # Charger thresholds keep their defaults when not changed.
        CONF_CHARGER_ACTIVE_W: 500,
        CONF_CHARGER_QUIET_W: 10,
        # The API switch is not a field of the form; its state is kept.
        CONF_API_ENABLED: True,
    }
    coordinator = config_entry.runtime_data.coordinator
    # Replanned in place from the stored quota state, without a request.
    assert coordinator.plan.interval == 1800
    assert 1790 <= coordinator.update_interval.total_seconds() <= 1811
    assert config_entry.title == "XX TEST-01"
    assert aioclient_mock.call_count == calls_after_setup


POWER_ENTITY = "sensor.wallbox_power"


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_with_charger_power_entity(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The optional charger power sensor is stored in the options."""
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_CHARGER_POWER_ENTITY: POWER_ENTITY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_CHARGER_POWER_ENTITY] == POWER_ENTITY
    assert aioclient_mock.call_count == 1


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_keeps_power_entity_after_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """After an error the form suggests the chosen power sensor again."""
    aioclient_mock.get(VEHICLE_URL, status=401)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_CHARGER_POWER_ENTITY: POWER_ENTITY}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    field = next(
        key for key in result["data_schema"].schema if key == CONF_CHARGER_POWER_ENTITY
    )
    assert field.description == {"suggested_value": POWER_ENTITY}


async def test_options_flow_set_and_clear_power_entity(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """The power sensor can be added later and removed again, without a GET."""
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    with patch("custom_components.skoda_api_ha.PLATFORMS", []):
        await setup_integration(hass, config_entry)
    calls_after_setup = aioclient_mock.call_count
    base = {
        CONF_DEVICE_NAME_SOURCE: NAME_SOURCE_VEHICLE_NAME,
        CONF_POLLS_PER_HOUR: 12,
        CONF_CONFIRM_GET: False,
    }

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, CONF_CHARGER_POWER_ENTITY: POWER_ENTITY}
    )
    await hass.async_block_till_done()
    assert config_entry.options[CONF_CHARGER_POWER_ENTITY] == POWER_ENTITY

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    field = next(
        key for key in result["data_schema"].schema if key == CONF_CHARGER_POWER_ENTITY
    )
    assert field.description == {"suggested_value": POWER_ENTITY}
    result = await hass.config_entries.options.async_configure(result["flow_id"], base)
    await hass.async_block_till_done()
    assert CONF_CHARGER_POWER_ENTITY not in config_entry.options
    assert aioclient_mock.call_count == calls_after_setup


async def test_options_flow_charger_thresholds(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Thresholds per charger: idle must be below charging; used by the meter."""
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    with patch("custom_components.skoda_api_ha.PLATFORMS", []):
        await setup_integration(hass, config_entry)
    calls_after_setup = aioclient_mock.call_count
    base = {
        CONF_DEVICE_NAME_SOURCE: NAME_SOURCE_VEHICLE_NAME,
        CONF_POLLS_PER_HOUR: 12,
        CONF_CONFIRM_GET: False,
        CONF_CHARGER_POWER_ENTITY: POWER_ENTITY,
    }

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**base, CONF_CHARGER_ACTIVE_W: 300.0, CONF_CHARGER_QUIET_W: 300.0},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CHARGER_QUIET_W: "charger_thresholds"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**base, CONF_CHARGER_ACTIVE_W: 1200.0, CONF_CHARGER_QUIET_W: 50.0},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_CHARGER_ACTIVE_W] == 1200
    assert config_entry.options[CONF_CHARGER_QUIET_W] == 50
    meter = config_entry.runtime_data.coordinator._meter
    assert meter is not None
    assert (meter.active_w, meter.quiet_w) == (1200.0, 50.0)
    assert aioclient_mock.call_count == calls_after_setup
