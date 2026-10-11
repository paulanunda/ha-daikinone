"""The diagnostics download contains state but nothing identifying."""

from __future__ import annotations

import json

import aiohttp
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daikinone.const import CONF_SCAN_INTERVAL_MINUTES
from custom_components.daikinone.diagnostics import async_get_config_entry_diagnostics
from tests.conftest import TITLE, UNIQUE_ID, advance, setup_entry
from tests.fake_daikin import (
    ACCESS_TOKEN_PREFIX,
    API_KEY,
    EMAIL,
    INTEGRATOR_TOKEN,
    MAIN_ID,
    UPSTAIRS_ID,
    FakeDaikin,
)

IDENTIFYING = (
    API_KEY,
    INTEGRATOR_TOKEN,
    ACCESS_TOKEN_PREFIX,
    EMAIL,
    EMAIL.lower(),
    "Owner.Name",
    "Example.com",
    "O***",
    TITLE,
    UNIQUE_ID,
    MAIN_ID,
    UPSTAIRS_ID,
    "Main Floor",
    "Upstairs",
    "Home",
    "SERIAL-123",
)


async def test_diagnostics_redacts_everything_identifying(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    hass.config_entries.async_update_entry(entry, options={CONF_SCAN_INTERVAL_MINUTES: 5})
    daikin.states[MAIN_ID].update(
        {"equipmentStatus": 7, "serialNumber": "SERIAL-123", "owner": {"email": EMAIL}}
    )
    del daikin.states[MAIN_ID]["humOutdoor"]
    daikin.states[MAIN_ID]["tempIndoor"] = f"oops {MAIN_ID}"
    await setup_entry(hass, entry)

    result = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(result)  # must be JSON serializable
    for secret in IDENTIFYING:
        assert secret not in text

    assert result["entry"] == {
        "title": "**REDACTED**",
        "unique_id": "**REDACTED**",
        "data": {
            "email": "**REDACTED**",
            "api_key": "**REDACTED**",
            "integrator_token": "**REDACTED**",
        },
        "options": {"scan_interval_minutes": 5},
    }
    assert result["coordinator"] == {
        "last_update_success": True,
        "update_interval_seconds": 300.0,
        "last_error": None,
    }

    main, upstairs = result["thermostats"]
    assert (main["id"], main["name"], main["location"]) == ("**REDACTED**",) * 3
    assert (main["model"], main["firmware_version"]) == ("ONETOUCH", "3.2.19")
    assert (main["listed"], main["online"], main["problem"]) == (True, True, None)
    # Useful for debugging: what was parsed, what was received, what was odd.
    assert main["parsed"]["mode"] == "off"
    assert main["parsed"]["equipment_status"] is None
    assert main["parsed"]["temp_indoor"] is None
    assert main["received"]["equipmentStatus"] == 7
    assert main["received"]["tempIndoor"] == "<str>"
    assert main["missing_fields"] == ["humOutdoor"]
    assert main["unknown_fields"] == ["owner", "serialNumber"]
    assert upstairs["parsed"]["temp_indoor"] == 23.0
    assert upstairs["missing_fields"] == []
    assert upstairs["unknown_fields"] == []


async def test_diagnostics_with_an_unavailable_thermostat_and_a_failed_poll(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, entry)
    entry.async_on_unload(entry.runtime_data.async_add_listener(lambda: None))

    daikin.always("GET", f"/v1/devices/{MAIN_ID}", aiohttp.ClientConnectionError(API_KEY))
    await advance(hass, freezer, 180)
    result = await async_get_config_entry_diagnostics(hass, entry)
    main, upstairs = result["thermostats"]
    assert main["parsed"] is None
    assert main["received"] is None
    assert main["online"] is None
    assert main["problem"] == "ConnectionFailedError"
    assert upstairs["online"] is True

    daikin.always("GET", f"/v1/devices/{UPSTAIRS_ID}", aiohttp.ClientConnectionError(API_KEY))
    await advance(hass, freezer, 180)
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["coordinator"]["last_update_success"] is False
    assert result["coordinator"]["last_error"] == "UpdateFailed"
    text = json.dumps(result)
    for secret in IDENTIFYING:
        assert secret not in text
