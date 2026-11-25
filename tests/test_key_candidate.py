"""Key change: the candidate is tried on the next regular GET."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.skoda_api_ha.const import (
    CONF_API_KEY_CANDIDATE,
    DOMAIN,
    ISSUE_KEY_REJECTED,
)
from custom_components.skoda_api_ha.key_change import (
    CandidateOutcome,
    judge_candidate,
)
from custom_components.skoda_api_ha.store import storage_key

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
from .test_store import add_listener

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
ACTIVE_EXPIRY = datetime(2027, 5, 1, tzinfo=UTC)
LONGER_EXPIRY = "2027-11-01T00:00:00Z"
# Setup at NOW: 11 left, reset in 30 min, reserve 8 -> next GET after 890 s.
REGULAR_DELAY = timedelta(seconds=895)


async def _setup_with_candidate(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Set up with the active key, then save a candidate (no request)."""
    freezer.move_to(NOW)
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, entry)
    add_listener(entry)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_API_KEY}
    )
    assert result["reason"] == "candidate_saved"
    aioclient_mock.clear_requests()


async def _regular_get(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delay: timedelta
) -> None:
    freezer.tick(delay)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _sent_key(aioclient_mock: AiohttpClientMocker) -> str:
    return aioclient_mock.mock_calls[-1][3]["X-API-Key"]


async def test_candidate_activated_on_regular_get(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """Longer validity: the regular GET switches the key, 0 extra requests."""
    issue_registry = ir.async_get(hass)
    await _setup_with_candidate(hass, aioclient_mock, config_entry, freezer)
    coordinator = config_entry.runtime_data.coordinator
    assert coordinator.key_expires_at == ACTIVE_EXPIRY
    # A stale rejection from an earlier try is cleared on success.
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_KEY_REJECTED,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="key_rejected_auth",
    )

    # Nothing is sent before the regular GET is due.
    await _regular_get(hass, freezer, timedelta(seconds=600))
    assert aioclient_mock.call_count == 0

    aioclient_mock.get(
        VEHICLE_URL,
        json=vehicle_body(),
        headers={**QUOTA_HEADERS, "X-API-Key-Expires-At": LONGER_EXPIRY},
    )
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await _regular_get(hass, freezer, REGULAR_DELAY - timedelta(seconds=600))
    assert aioclient_mock.call_count == 1
    assert _sent_key(aioclient_mock) == NEW_API_KEY
    reload.assert_not_called()

    assert config_entry.data[CONF_API_KEY] == NEW_API_KEY
    assert CONF_API_KEY_CANDIDATE not in config_entry.data
    assert config_entry.runtime_data.client.uses_key(NEW_API_KEY)
    new_expiry = datetime(2027, 11, 1, tzinfo=UTC)
    assert coordinator.key_expires_at == new_expiry
    assert coordinator.data.quota.key_expires_at == new_expiry
    assert coordinator.last_update_success
    saved = hass_storage[storage_key(config_entry.entry_id)]["data"]
    assert saved["key_expires_at"] == new_expiry.isoformat()
    assert "test-key" not in str(saved)
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_KEY_REJECTED) is None

    # The following GETs use the new key.
    aioclient_mock.clear_requests()
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await _regular_get(hass, freezer, timedelta(seconds=1800))
    assert _sent_key(aioclient_mock) == NEW_API_KEY


@pytest.mark.parametrize(
    "case",
    [
        (
            {
                "json": vehicle_body(),
                "headers": {
                    **QUOTA_HEADERS,
                    "X-API-Key-Expires-At": "2027-01-01T00:00:00Z",
                },
            },
            "key_rejected_shorter_validity",
        ),
        (
            # The same expiry is not an improvement.
            {"json": vehicle_body(), "headers": QUOTA_HEADERS},
            "key_rejected_shorter_validity",
        ),
        (
            {
                "json": vehicle_body(),
                "headers": {
                    **QUOTA_HEADERS,
                    "X-API-Key-Expires-At": "2026-11-01T00:00:00Z",
                },
            },
            "key_rejected_expired",
        ),
        ({"status": 401}, "key_rejected_auth"),
        ({"status": 403}, "key_rejected_auth"),
        (
            {"json": vehicle_body(vin=OTHER_VIN), "headers": QUOTA_HEADERS},
            "key_rejected_vin_mismatch",
        ),
    ],
)
async def test_candidate_rejected(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    case: tuple[dict, str],
) -> None:
    """A rejected candidate is dropped; the active key and data stay."""
    mock_kwargs, issue_key = case
    issue_registry = ir.async_get(hass)
    await _setup_with_candidate(hass, aioclient_mock, config_entry, freezer)
    coordinator = config_entry.runtime_data.coordinator
    aioclient_mock.get(VEHICLE_URL, **mock_kwargs)
    await _regular_get(hass, freezer, REGULAR_DELAY)
    assert aioclient_mock.call_count == 1
    assert _sent_key(aioclient_mock) == NEW_API_KEY

    assert config_entry.data[CONF_API_KEY] == API_KEY
    assert CONF_API_KEY_CANDIDATE not in config_entry.data
    assert config_entry.runtime_data.client.uses_key(API_KEY)
    assert config_entry.state is ConfigEntryState.LOADED
    assert coordinator.last_update_success
    assert coordinator.data.vin == VIN
    assert coordinator.key_expires_at == ACTIVE_EXPIRY
    assert coordinator.data.quota.key_expires_at == ACTIVE_EXPIRY
    issue = issue_registry.async_get_issue(DOMAIN, ISSUE_KEY_REJECTED)
    assert issue is not None
    assert issue.translation_key == issue_key
    # No reauth for a bad candidate: the active key still works.
    assert not list(config_entry.async_get_active_flows(hass, {"reauth"}))

    # The next regular GET uses the active key again.
    aioclient_mock.clear_requests()
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await _regular_get(hass, freezer, timedelta(seconds=1800))
    assert _sent_key(aioclient_mock) == API_KEY

    # A new candidate clears the issue.
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "test-key-third-not-real"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_KEY_REJECTED) is None


@pytest.mark.parametrize(
    "mock_kwargs",
    [
        # Missing expiry header: cannot be decided yet.
        {"json": vehicle_body(), "headers": {"RateLimit-Remaining": "10"}},
        # Missing VIN in the answer: cannot be decided yet.
        {"json": {"vehicle": {"name": "X"}}, "headers": QUOTA_HEADERS},
        # Temporary failures: try again on the next regular GET.
        {"status": 500},
        {"status": 429, "headers": {"Retry-After": "60"}},
        {"status": 404},
    ],
)
async def test_candidate_kept_for_next_get(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    mock_kwargs: dict,
) -> None:
    """Undecided or temporary: the candidate stays, the active key too."""
    issue_registry = ir.async_get(hass)
    await _setup_with_candidate(hass, aioclient_mock, config_entry, freezer)
    coordinator = config_entry.runtime_data.coordinator
    aioclient_mock.get(VEHICLE_URL, **mock_kwargs)
    await _regular_get(hass, freezer, REGULAR_DELAY)
    assert aioclient_mock.call_count == 1
    assert config_entry.data[CONF_API_KEY] == API_KEY
    assert config_entry.data[CONF_API_KEY_CANDIDATE] == NEW_API_KEY
    assert config_entry.runtime_data.client.uses_key(API_KEY)
    assert coordinator.key_expires_at == ACTIVE_EXPIRY
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_KEY_REJECTED) is None


async def test_extra_refresh_does_not_try_candidate(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A manual refresh is an extra request and uses the active key."""
    await _setup_with_candidate(hass, aioclient_mock, config_entry, freezer)
    freezer.tick(timedelta(seconds=120))
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await config_entry.runtime_data.coordinator.async_refresh()
    assert aioclient_mock.call_count == 1
    assert _sent_key(aioclient_mock) == API_KEY
    assert config_entry.data[CONF_API_KEY_CANDIDATE] == NEW_API_KEY


async def test_candidate_without_known_active_expiry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Unknown expiry of the active key does not block the change."""
    freezer.move_to(NOW)
    headers = {k: v for k, v in QUOTA_HEADERS.items() if k != "X-API-Key-Expires-At"}
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=headers)
    await setup_integration(hass, config_entry)
    add_listener(config_entry)
    coordinator = config_entry.runtime_data.coordinator
    assert coordinator.key_expires_at is None
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, CONF_API_KEY_CANDIDATE: NEW_API_KEY},
    )
    await hass.async_block_till_done()
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        VEHICLE_URL,
        json=vehicle_body(),
        headers={**QUOTA_HEADERS, "X-API-Key-Expires-At": LONGER_EXPIRY},
    )
    await _regular_get(hass, freezer, REGULAR_DELAY)
    assert config_entry.data[CONF_API_KEY] == NEW_API_KEY
    assert coordinator.key_expires_at == datetime(2027, 11, 1, tzinfo=UTC)


async def test_candidate_rejected_on_first_setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Without stored data a rejected candidate means: retry the setup later."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, CONF_API_KEY_CANDIDATE: NEW_API_KEY},
    )
    aioclient_mock.get(VEHICLE_URL, status=401)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert CONF_API_KEY_CANDIDATE not in config_entry.data
    assert config_entry.data[CONF_API_KEY] == API_KEY
    assert _sent_key(aioclient_mock) == NEW_API_KEY


async def test_key_expiry_survives_restart(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The active key's expiry is restored from Store (needed for the rule)."""
    freezer.move_to(NOW)
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 1
    assert config_entry.runtime_data.coordinator.key_expires_at == ACTIVE_EXPIRY


async def test_reauth_while_loaded_reloads_without_get(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Reauth of a loaded entry: the update listener reloads, using the flow GET."""
    freezer.move_to(NOW)
    aioclient_mock.get(VEHICLE_URL, json=vehicle_body(), headers=QUOTA_HEADERS)
    await setup_integration(hass, config_entry)
    freezer.tick(timedelta(seconds=120))
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        VEHICLE_URL,
        json=vehicle_body(),
        headers={**QUOTA_HEADERS, "X-API-Key-Expires-At": LONGER_EXPIRY},
    )
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_API_KEY}
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert aioclient_mock.call_count == 1
    assert config_entry.state is ConfigEntryState.LOADED
    runtime = config_entry.runtime_data
    assert runtime.client.uses_key(NEW_API_KEY)
    assert runtime.coordinator.key_expires_at == datetime(2027, 11, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("answered_vin", "new_expiry", "active_expiry", "outcome"),
    [
        (None, LONGER_EXPIRY, ACTIVE_EXPIRY, CandidateOutcome.WAIT),
        (OTHER_VIN, LONGER_EXPIRY, ACTIVE_EXPIRY, CandidateOutcome.REJECT_VIN_MISMATCH),
        (VIN, None, ACTIVE_EXPIRY, CandidateOutcome.WAIT),
        (VIN, "2026-11-23T10:00:00Z", None, CandidateOutcome.REJECT_EXPIRED),
        (
            VIN,
            "2027-05-01T00:00:00Z",
            ACTIVE_EXPIRY,
            CandidateOutcome.REJECT_SHORTER_VALIDITY,
        ),
        (VIN, LONGER_EXPIRY, ACTIVE_EXPIRY, CandidateOutcome.ACTIVATE),
        (VIN, LONGER_EXPIRY, None, CandidateOutcome.ACTIVATE),
    ],
)
def test_judge_candidate(
    answered_vin: str | None,
    new_expiry: str | None,
    active_expiry: datetime | None,
    outcome: CandidateOutcome,
) -> None:
    """The key change rule as a pure function."""
    assert (
        judge_candidate(
            vin=VIN,
            answered_vin=answered_vin,
            new_expiry=datetime.fromisoformat(new_expiry) if new_expiry else None,
            active_expiry=active_expiry,
            now=NOW,
        )
        is outcome
    )
