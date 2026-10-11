"""Diagnostics download, with everything identifying removed.

Credentials, device ids, thermostat names, and location names never appear.
Values of fields this integration does not know are left out too, because
their content cannot be vouched for; only their names are listed.
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_API_KEY, CONF_EMAIL
from homeassistant.core import HomeAssistant

from .api import ThermostatState
from .api.models import THERMOSTAT_FIELDS
from .const import CONF_INTEGRATOR_TOKEN
from .coordinator import DaikinOneConfigEntry, ThermostatData

TO_REDACT = {CONF_EMAIL, CONF_API_KEY, CONF_INTEGRATOR_TOKEN}


def _parsed(state: ThermostatState) -> dict[str, Any]:
    """Return the parsed state with enum members shown by name."""
    values = dataclasses.asdict(state)
    values.pop("raw")
    return {
        key: value.name.lower() if isinstance(value, Enum) else value
        for key, value in values.items()
    }


def _received(state: ThermostatState) -> dict[str, Any]:
    """Return the documented fields exactly as Daikin sent them, if they are plain values."""
    return {
        name: state.raw[name]
        if isinstance(state.raw[name], (bool, int, float, type(None)))
        else f"<{type(state.raw[name]).__name__}>"
        for name in THERMOSTAT_FIELDS
        if name in state.raw
    }


def _thermostat(thermostat: ThermostatData, *, listed: bool) -> dict[str, Any]:
    info, state = thermostat.info, thermostat.state
    return {
        "id": REDACTED,
        "name": REDACTED,
        "location": REDACTED,
        "model": info.model,
        "firmware_version": info.firmware_version,
        "listed": listed,
        "online": thermostat.online,
        "problem": thermostat.problem,
        "parsed": _parsed(state) if state is not None else None,
        "received": _received(state) if state is not None else None,
        "missing_fields": sorted(set(THERMOSTAT_FIELDS) - set(state.raw)) if state else None,
        "unknown_fields": sorted(set(state.raw) - set(THERMOSTAT_FIELDS)) if state else None,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DaikinOneConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    interval = coordinator.update_interval
    error = coordinator.last_exception
    return {
        "entry": {
            "title": REDACTED,
            "unique_id": REDACTED,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": interval.total_seconds() if interval else None,
            "last_error": type(error).__name__ if error is not None else None,
        },
        "thermostats": [
            _thermostat(thermostat, listed=coordinator.is_listed(device_id))
            for device_id, thermostat in coordinator.data.items()
        ],
    }
