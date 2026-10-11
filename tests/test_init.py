"""Setting up, retrying, and unloading the config entry."""

from __future__ import annotations

import ssl
from unittest.mock import patch

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daikinone.api import DaikinOneClient
from custom_components.daikinone.coordinator import DaikinOneCoordinator
from tests.conftest import advance, setup_entry
from tests.fake_daikin import MAIN_ID, UPSTAIRS_ID, FakeDaikin, Reply

MAIN = f"/v1/devices/{MAIN_ID}"
UPSTAIRS = f"/v1/devices/{UPSTAIRS_ID}"


def is_loaded(entry: MockConfigEntry) -> bool:
    return entry.state is ConfigEntryState.LOADED


async def test_setup_fetches_once_and_loads(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, DaikinOneCoordinator)
    assert sorted((c.method, c.path) for c in daikin.calls) == sorted(
        [("POST", "/v1/token"), ("GET", "/v1/devices"), ("GET", MAIN), ("GET", UPSTAIRS)]
    )
    assert daikin.max_in_flight <= 3
    data = entry.runtime_data.data
    assert set(data) == {MAIN_ID, UPSTAIRS_ID}
    assert data[MAIN_ID].state is not None
    assert data[MAIN_ID].online is True


async def test_uses_the_shared_session_and_a_tls_1_3_context(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Without the test patch, the client gets Home Assistant's shared session."""
    captured: dict[str, object] = {}

    class Recorder(DaikinOneClient):
        def __init__(self, session: aiohttp.ClientSession, **kwargs: object) -> None:
            captured["session"] = session
            captured.update(kwargs)
            raise RuntimeError("stop before any request is made")

    with patch("custom_components.daikinone.DaikinOneClient", Recorder):
        await setup_entry(hass, entry)

    assert captured["session"] is async_get_clientsession(hass)
    context = captured["ssl_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.minimum_version is ssl.TLSVersion.TLSv1_3
    assert entry.state is ConfigEntryState.SETUP_ERROR


@pytest.mark.parametrize(
    "failure",
    [
        aiohttp.ClientConnectionError(),
        TimeoutError(),
        Reply(500, {"messages": "Internal error"}),
        Reply(429, None, {"Retry-After": "30"}),
        Reply(200, raw=b"<html>maintenance</html>"),
    ],
    ids=["connection", "timeout", "500", "429", "garbage"],
)
@pytest.mark.parametrize("path", ["/v1/token", "/v1/devices"])
async def test_failed_first_fetch_retries_on_its_own(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    failure: Reply | BaseException,
    path: str,
) -> None:
    method = "POST" if path == "/v1/token" else "GET"
    daikin.always(method, path, failure)
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    # Daikin recovers. Home Assistant's own retry timer brings the entry up
    # with no reload from the user.
    daikin.always(method, path, None)
    for _ in range(40):
        if is_loaded(entry):
            break
        await advance(hass, freezer, 30)
    assert is_loaded(entry)
    assert entry.runtime_data.data[MAIN_ID].state is not None


async def test_first_fetch_with_every_thermostat_unreadable_retries(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    daikin.always("GET", MAIN, aiohttp.ClientConnectionError())
    daikin.always("GET", UPSTAIRS, Reply(500))
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_first_fetch_with_one_thermostat_unreadable_still_loads(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    daikin.always("GET", MAIN, Reply(200, ["not", "an", "object"]))
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    data = entry.runtime_data.data
    assert data[MAIN_ID].state is None
    assert data[UPSTAIRS_ID].state is not None


async def test_account_with_no_thermostats_loads_empty(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    daikin.locations = []
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data == {}


async def test_unload_stops_all_requests(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, entry)
    await entry.runtime_data.async_set_schedule(MAIN_ID, enabled=True)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    sent = len(daikin.calls)
    await advance(hass, freezer, 3600)
    assert len(daikin.calls) == sent
