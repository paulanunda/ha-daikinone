"""Shared test configuration."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import timedelta
from functools import partial
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_API_KEY, CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.daikinone.api import DaikinOneClient
from custom_components.daikinone.const import CONF_INTEGRATOR_TOKEN, DOMAIN
from tests.fake_daikin import API_KEY, EMAIL, INTEGRATOR_TOKEN, FakeDaikin

ENTRY_DATA = {
    CONF_EMAIL: EMAIL,
    CONF_API_KEY: API_KEY,
    CONF_INTEGRATOR_TOKEN: INTEGRATOR_TOKEN,
}
UNIQUE_ID = hashlib.sha256(EMAIL.lower().encode()).hexdigest()
TITLE = "Daikin One (O***@***.com)"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load the integration from custom_components/."""


def _frozen_clock() -> float:
    """A clock that follows the test's frozen time."""
    return dt_util.utcnow().timestamp()


@pytest.fixture
def daikin() -> Iterator[FakeDaikin]:
    """Route the integration's HTTP session to the in-memory fake.

    The client normally measures token lifetimes and rate-limit backoff with
    time.monotonic, which the test clock does not move. Here it is given a
    clock that follows the frozen time instead; nothing else is changed.
    """
    fake = FakeDaikin()
    with (
        patch(
            "custom_components.daikinone.async_get_clientsession",
            side_effect=lambda hass: fake.session(),
        ),
        patch(
            "custom_components.daikinone.config_flow.async_get_clientsession",
            side_effect=lambda hass: fake.session(),
        ),
        patch(
            "custom_components.daikinone.DaikinOneClient",
            partial(DaikinOneClient, monotonic=_frozen_clock),
        ),
    ):
        yield fake


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry for the fake account, not yet set up."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        data=dict(ENTRY_DATA),
        unique_id=UNIQUE_ID,
    )
    config_entry.add_to_hass(hass)
    return config_entry


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the entry up and wait for everything it started."""
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    """Move the clock forward and let every due timer and task run."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
