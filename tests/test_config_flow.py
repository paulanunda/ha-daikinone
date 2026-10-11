"""Config, re-authentication, reconfigure, and options flows."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daikinone.config_flow import account_unique_id, entry_title
from custom_components.daikinone.const import (
    CONF_INTEGRATOR_TOKEN,
    CONF_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)
from tests.conftest import ENTRY_DATA, TITLE, UNIQUE_ID, advance, setup_entry
from tests.fake_daikin import API_KEY, EMAIL, INTEGRATOR_TOKEN, MAIN_ID, FakeDaikin, Reply

MAIN = f"/v1/devices/{MAIN_ID}"
NEW_SECRETS = {CONF_API_KEY: "new-api-key-DDDD", CONF_INTEGRATOR_TOKEN: "new-integrator-EEEE"}


def reauth_flows(hass: HomeAssistant) -> list[Any]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == SOURCE_REAUTH
    ]


def rotate_credentials(daikin: FakeDaikin) -> None:
    """Daikin stops accepting the saved credentials and only takes NEW_SECRETS."""
    daikin.api_key = NEW_SECRETS[CONF_API_KEY]
    daikin.integrator_token = NEW_SECRETS[CONF_INTEGRATOR_TOKEN]
    daikin.expire_tokens()


# ------------------------------------------------------------------ helpers


def test_unique_id_and_title_do_not_reveal_the_email() -> None:
    assert account_unique_id(EMAIL) == UNIQUE_ID
    assert account_unique_id(f"  {EMAIL.upper()} ") == UNIQUE_ID
    assert entry_title(EMAIL) == TITLE
    local, domain = EMAIL.split("@")
    for text in (UNIQUE_ID, TITLE):
        assert local not in text
        assert domain not in text
        assert domain.lower() not in text


# --------------------------------------------------------------------- user


async def test_user_flow_asks_only_for_email_key_and_token(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    schema = result["data_schema"]
    assert schema is not None
    assert [str(key) for key in schema.schema] == [CONF_EMAIL, CONF_API_KEY, CONF_INTEGRATOR_TOKEN]


async def test_user_flow_validates_then_creates_the_entry(
    hass: HomeAssistant, daikin: FakeDaikin
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert daikin.calls == []
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        # Pasted values often carry stray whitespace. The email's case is kept.
        {
            CONF_EMAIL: f" {EMAIL} ",
            CONF_API_KEY: f"{API_KEY}\n",
            CONF_INTEGRATOR_TOKEN: f"\t{INTEGRATOR_TOKEN}",
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TITLE
    assert result["data"] == ENTRY_DATA
    entry = result["result"]
    assert entry.unique_id == UNIQUE_ID
    assert entry.state is ConfigEntryState.LOADED
    # Validation happened before the entry existed: a token, then the device list.
    assert [(c.method, c.path) for c in daikin.calls[:2]] == [
        ("POST", "/v1/token"),
        ("GET", "/v1/devices"),
    ]
    assert daikin.calls[0].json == {"email": EMAIL, "integratorToken": INTEGRATOR_TOKEN}


@pytest.mark.parametrize(
    ("user_input", "error"),
    [
        ({**ENTRY_DATA, CONF_INTEGRATOR_TOKEN: "wrong"}, "invalid_auth"),
        ({**ENTRY_DATA, CONF_API_KEY: "wrong"}, "invalid_auth"),
        ({**ENTRY_DATA, CONF_EMAIL: EMAIL.lower()}, "invalid_auth"),
    ],
    ids=["integrator-token", "api-key", "email-case"],
)
async def test_user_flow_rejected_credentials(
    hass: HomeAssistant, daikin: FakeDaikin, user_input: dict[str, str], error: str
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert hass.config_entries.async_entries(DOMAIN) == []

    # Correcting the input in the same form succeeds.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(ENTRY_DATA))
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("method", "path", "failure", "error"),
    [
        ("POST", "/v1/token", aiohttp.ClientConnectionError(), "cannot_connect"),
        ("POST", "/v1/token", TimeoutError(), "cannot_connect"),
        ("POST", "/v1/token", Reply(500), "cannot_connect"),
        ("POST", "/v1/token", Reply(429, None, {"Retry-After": "1"}), "rate_limited"),
        ("POST", "/v1/token", Reply(200, raw=b"garbage"), "unknown"),
        ("POST", "/v1/token", Reply(401), "invalid_auth"),
        ("GET", "/v1/devices", Reply(403), "invalid_auth"),
        ("GET", "/v1/devices", Reply(503), "cannot_connect"),
        ("GET", "/v1/devices", Reply(200, {"not": "a list"}), "unknown"),
        ("GET", "/v1/devices", Reply(400, {"messages": "Invalid request body"}), "unknown"),
    ],
)
async def test_user_flow_failures_show_an_error_and_create_nothing(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    method: str,
    path: str,
    failure: Reply | BaseException,
    error: str,
) -> None:
    daikin.always(method, path, failure)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(ENTRY_DATA))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize("email", [EMAIL, EMAIL.upper(), f" {EMAIL.lower()} "])
async def test_same_account_cannot_be_added_twice(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, email: str
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ENTRY_DATA, CONF_EMAIL: email}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert daikin.calls == []


# ------------------------------------------------------------------- reauth


async def test_rejected_credentials_at_startup_start_reauth(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    rotate_credentials(daikin)
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    (flow,) = reauth_flows(hass)
    assert flow["step_id"] == "reauth_confirm"
    assert flow["context"]["entry_id"] == entry.entry_id

    # It does not keep trying the rejected credentials.
    sent = len(daikin.calls)
    assert sent == 1
    await advance(hass, freezer, 3600)
    assert len(daikin.calls) == sent
    assert len(reauth_flows(hass)) == 1


@pytest.mark.parametrize(
    "rejection",
    [Reply(401, {"messages": "NotAuthorizedException"}), Reply(403, {"messages": "Forbidden"})],
    ids=["401", "403"],
)
async def test_rejected_credentials_during_polling_start_reauth_once_and_stop_polling(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    rejection: Reply,
) -> None:
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))
    assert reauth_flows(hass) == []

    daikin.always("GET", MAIN, rejection)
    await advance(hass, freezer, 180)
    assert coordinator.last_update_success is False
    assert len(reauth_flows(hass)) == 1

    sent = len(daikin.calls)
    await advance(hass, freezer, 7200)
    assert len(daikin.calls) == sent, "polling must stop until re-authentication"
    assert len(reauth_flows(hass)) == 1


async def test_rejected_credentials_during_a_write_start_reauth(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await setup_entry(hass, entry)
    rotate_credentials(daikin)
    with pytest.raises(HomeAssistantError) as err:
        await entry.runtime_data.async_set_schedule(MAIN_ID, enabled=True)
    assert err.value.translation_key == "auth_failed"
    await hass.async_block_till_done()
    assert len(reauth_flows(hass)) == 1


async def test_reauth_flow_updates_credentials_and_recovers(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    rotate_credentials(daikin)
    await setup_entry(hass, entry)
    (flow,) = reauth_flows(hass)

    result = await hass.config_entries.flow.async_configure(flow["flow_id"])
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"]
    assert schema is not None
    assert [str(key) for key in schema.schema] == [CONF_API_KEY, CONF_INTEGRATOR_TOKEN]
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["account"] == "O***@***.com"

    # Still-wrong credentials keep the form open and change nothing.
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_KEY: "nope", CONF_INTEGRATOR_TOKEN: "nope"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data == ENTRY_DATA

    result = await hass.config_entries.flow.async_configure(flow["flow_id"], dict(NEW_SECRETS))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {CONF_EMAIL: EMAIL, **NEW_SECRETS}
    assert entry.unique_id == UNIQUE_ID
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data[MAIN_ID].state is not None
    assert reauth_flows(hass) == []


# -------------------------------------------------------------- reconfigure


async def test_reconfigure_updates_credentials(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await setup_entry(hass, entry)
    rotate_credentials(daikin)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    schema = result["data_schema"]
    assert schema is not None
    suggested = {str(key): (key.description or {}).get("suggested_value") for key in schema.schema}
    # Only the email is pre-filled; saved secrets are never echoed into the form.
    assert suggested == {CONF_EMAIL: EMAIL, CONF_API_KEY: None, CONF_INTEGRATOR_TOKEN: None}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_API_KEY: "bad", CONF_INTEGRATOR_TOKEN: "bad"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data == ENTRY_DATA

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, **NEW_SECRETS}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_EMAIL: EMAIL, **NEW_SECRETS}
    assert entry.title == TITLE
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_refuses_a_different_account(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await setup_entry(hass, entry)
    sent = len(daikin.calls)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ENTRY_DATA, CONF_EMAIL: "someone.else@elsewhere.invalid"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "account_mismatch"
    assert entry.data == ENTRY_DATA
    assert len(daikin.calls) == sent


# ------------------------------------------------------------------ options


async def test_options_flow_changes_the_polling_interval(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    schema = result["data_schema"]
    assert schema is not None
    (key,) = schema.schema
    assert str(key) == CONF_SCAN_INTERVAL_MINUTES
    assert key.default() == 3

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL_MINUTES: 10}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_SCAN_INTERVAL_MINUTES: 10}
    assert type(entry.options[CONF_SCAN_INTERVAL_MINUTES]) is int
    # The entry was reloaded with the new interval.
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.update_interval is not None
    assert entry.runtime_data.update_interval.total_seconds() == 600

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]
    assert schema is not None
    (key,) = schema.schema
    assert key.default() == 10


@pytest.mark.parametrize("minutes", [2, 1, 0, -3, 2.9, 61, 1000])
async def test_options_flow_refuses_intervals_outside_the_limits(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, minutes: float
) -> None:
    await setup_entry(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL_MINUTES: minutes}
        )
    assert entry.options == {}
    assert entry.runtime_data.update_interval is not None
    assert entry.runtime_data.update_interval.total_seconds() == 180


# ------------------------------------------------------------------ secrets


async def test_flows_do_not_log_credentials_or_the_email(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ENTRY_DATA, CONF_API_KEY: "wrong-key-FFFF"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(ENTRY_DATA))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY

    assert "daikinone" in caplog.text
    for secret in (API_KEY, INTEGRATOR_TOKEN, "wrong-key-FFFF", EMAIL, EMAIL.lower(), "Owner.Name"):
        assert secret not in caplog.text
