"""Daikin One thermostats through Daikin's official Open API."""

from __future__ import annotations

from homeassistant.const import CONF_API_KEY, CONF_EMAIL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DaikinOneClient, create_ssl_context
from .const import CONF_INTEGRATOR_TOKEN, DOMAIN
from .coordinator import DaikinOneConfigEntry, DaikinOneCoordinator

PLATFORMS: list[Platform] = []


async def async_setup_entry(hass: HomeAssistant, entry: DaikinOneConfigEntry) -> bool:
    """Set up one Daikin account.

    If the first fetch fails, the coordinator raises ConfigEntryNotReady (or
    ConfigEntryAuthFailed for rejected credentials) and Home Assistant retries
    or starts re-authentication on its own.
    """
    # Building a TLS context reads certificates from disk, so do it off the event loop.
    ssl_context = await hass.async_add_executor_job(create_ssl_context)
    client = DaikinOneClient(
        async_get_clientsession(hass),
        email=entry.data[CONF_EMAIL],
        api_key=entry.data[CONF_API_KEY],
        integrator_token=entry.data[CONF_INTEGRATOR_TOKEN],
        ssl_context=ssl_context,
    )
    coordinator = DaikinOneCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options_change))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DaikinOneConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_on_options_change(hass: HomeAssistant, entry: DaikinOneConfigEntry) -> None:
    """Apply a changed polling interval by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: DaikinOneConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a device by hand, but only once Daikin no longer lists it.

    The integration never removes devices itself. This hook is what enables the
    "Delete" button on the device page.
    """
    coordinator = entry.runtime_data
    return not any(
        domain == DOMAIN and coordinator.is_listed(device_id)
        for domain, device_id in device.identifiers
    )
