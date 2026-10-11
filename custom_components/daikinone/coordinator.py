"""Polling and write coordination for one Daikin account.

One coordinator serves every thermostat on the account. It owns the rules that
keep the integration inside Daikin's documented limits and that keep one bad
thermostat from affecting another:

* Each poll reads every thermostat. A thermostat whose read fails becomes
  unavailable on its own; the poll as a whole fails only when nothing could be
  read at all.
* Writes are serialized per thermostat, completed from the last known state,
  validated before sending, reflected immediately, and verified by a single
  read 15 seconds later.
* Nothing is ever removed from the device or entity registry here.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .api import (
    AuthenticationError,
    BadRequestError,
    DaikinOneClient,
    DaikinOneError,
    DeviceInfo,
    DeviceOfflineError,
    FanCirculate,
    FanCirculateSpeed,
    Mode,
    RateLimitError,
    ThermostatState,
    mask_id,
)
from .const import (
    CONF_SCAN_INTERVAL_MINUTES,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DEVICE_LIST_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
    VERIFY_DELAY,
)
from .setpoints import SetpointError, resolve_setpoints

_LOGGER = logging.getLogger(__name__)

type DaikinOneConfigEntry = ConfigEntry[DaikinOneCoordinator]


@dataclass(frozen=True, slots=True)
class ThermostatData:
    """What is currently known about one thermostat.

    ``state`` is None whenever the last read did not produce fresh data; entities
    are unavailable in that case and never fall back to older values.
    ``online`` is True after a successful read, False when Daikin reported the
    thermostat offline, and None when its status could not be determined.
    ``problem`` names the kind of failure, for diagnostics.
    """

    info: DeviceInfo
    state: ThermostatState | None = None
    online: bool | None = None
    problem: str | None = None


def scan_interval_from_options(options: Mapping[str, Any]) -> timedelta:
    """Return the polling interval, never shorter than Daikin's three minute floor."""
    minutes = options.get(CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES)
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        minutes = DEFAULT_SCAN_INTERVAL_MINUTES
    minutes = min(max(minutes, MIN_SCAN_INTERVAL_MINUTES), MAX_SCAN_INTERVAL_MINUTES)
    return timedelta(minutes=minutes)


class DaikinOneCoordinator(DataUpdateCoordinator[dict[str, ThermostatData]]):
    """Fetches thermostat state and carries out writes for one config entry."""

    config_entry: DaikinOneConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: DaikinOneConfigEntry, client: DaikinOneClient
    ) -> None:
        """Set up the coordinator; nothing is fetched until the first refresh."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=scan_interval_from_options(entry.options),
        )
        self.client = client
        # Every thermostat seen since startup. Entries are never dropped.
        self._devices: dict[str, DeviceInfo] = {}
        # Thermostats present in the most recent device list.
        self._listed: set[str] = set()
        self._list_fetched_at: datetime | None = None
        self._write_locks: dict[str, asyncio.Lock] = {}
        # Bumped on every write, so a read that started earlier can be discarded.
        self._write_counts: dict[str, int] = {}
        # Until this time a thermostat's optimistic state must not be overwritten.
        self._settling_until: dict[str, datetime] = {}
        self._cancel_verify: dict[str, CALLBACK_TYPE] = {}

    # ----------------------------------------------------------------- polling

    def is_listed(self, device_id: str) -> bool:
        """Return whether the thermostat was in the latest device list."""
        return device_id in self._listed

    async def _async_update_data(self) -> dict[str, ThermostatData]:
        """Read every thermostat once."""
        try:
            await self._async_refresh_device_list()
            device_ids = list(self._devices)
            results = await asyncio.gather(
                *(self._async_read(device_id) for device_id in device_ids),
                return_exceptions=True,
            )
            # Account-wide failures surface from individual reads. Credentials
            # being rejected matters more than being rate limited.
            failures = [result for result in results if isinstance(result, BaseException)]
            for kind in (AuthenticationError, BaseException):
                for failure in failures:
                    if isinstance(failure, kind):
                        raise failure
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except RateLimitError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="rate_limited",
                translation_placeholders={"seconds": str(round(err.retry_after))},
                retry_after=err.retry_after,
            ) from err
        except DaikinOneError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"reason": type(err).__name__},
            ) from err

        data = {
            device_id: result
            for device_id, result in zip(device_ids, results, strict=True)
            if isinstance(result, ThermostatData)
        }
        attempted = [d for d in data.values() if d.problem != "not_listed"]
        if attempted and all(d.state is None and d.online is None for d in attempted):
            # Nothing could be read and Daikin did not say the thermostats are
            # offline, so this is a failure to reach or understand the service.
            reasons = sorted({d.problem or "unknown" for d in attempted})
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"reason": ", ".join(reasons)},
            )
        self._log_availability_changes(data)
        return data

    async def _async_refresh_device_list(self) -> None:
        """Fetch the thermostat list when it is due.

        It is due at startup, once an hour after that, and on every poll while a
        known thermostat is missing from it.
        """
        now = dt_util.utcnow()
        missing = any(device_id not in self._listed for device_id in self._devices)
        due = self._list_fetched_at is None or now - self._list_fetched_at >= DEVICE_LIST_INTERVAL
        if not (due or missing):
            return
        try:
            devices = await self.client.list_devices()
        except AuthenticationError, RateLimitError:
            raise
        except DaikinOneError as err:
            if not self._devices:
                raise
            # Keep polling the thermostats already known and try the list again next time.
            _LOGGER.debug("Could not refresh the thermostat list (%s)", type(err).__name__)
            return
        self._list_fetched_at = now
        self._listed = {device.id for device in devices}
        for device in devices:
            self._devices[device.id] = device

    async def _async_read(self, device_id: str) -> ThermostatData:
        """Read one thermostat. Failures specific to it are returned, not raised."""
        info = self._devices[device_id]
        if device_id not in self._listed:
            return ThermostatData(info=info, problem="not_listed")
        current = (self.data or {}).get(device_id)
        if current is not None and self._is_settling(device_id):
            # A write happened less than 15 seconds ago; Daikin may still return
            # the old values. Keep what the write produced.
            return current

        writes_before = self._write_counts.get(device_id, 0)
        try:
            state = await self.client.get_thermostat(device_id)
        except AuthenticationError, RateLimitError:
            raise
        except DeviceOfflineError:
            result = ThermostatData(info=info, online=False, problem="offline")
        except DaikinOneError as err:
            result = ThermostatData(info=info, problem=type(err).__name__)
        else:
            result = ThermostatData(info=info, state=state, online=True)

        if self._write_counts.get(device_id, 0) != writes_before:
            # A write completed while this read was in flight, so the read is stale.
            latest = (self.data or {}).get(device_id)
            if latest is not None:
                return latest
        return result

    def _is_settling(self, device_id: str) -> bool:
        until = self._settling_until.get(device_id)
        return until is not None and dt_util.utcnow() < until

    def _log_availability_changes(self, data: dict[str, ThermostatData]) -> None:
        previous = self.data or {}
        for device_id, new in data.items():
            old = previous.get(device_id)
            was_ok = old is None or old.state is not None
            if new.state is None and was_ok:
                _LOGGER.warning(
                    "Thermostat %s is unavailable (%s)", mask_id(device_id), new.problem
                )
            elif new.state is not None and old is not None and old.state is None:
                _LOGGER.info("Thermostat %s is available again", mask_id(device_id))

    # ------------------------------------------------------------------ writes

    async def async_set_mode_setpoints(
        self,
        device_id: str,
        *,
        mode: Mode | None = None,
        heat_setpoint: float | None = None,
        cool_setpoint: float | None = None,
    ) -> None:
        """Change the mode and/or setpoints (Celsius).

        Daikin needs all three values on every call, so anything not given is
        taken from the last known state. A successful call turns off the
        thermostat's schedule, which is reflected here straight away.
        """
        async with self._write_lock(device_id):
            state = self._require_state(device_id)
            new_mode = mode if mode is not None else state.mode
            if new_mode is None:
                raise self._error("state_unknown", device_id)
            try:
                setpoints = resolve_setpoints(
                    state, new_mode, heat=heat_setpoint, cool=cool_setpoint
                )
            except SetpointError as err:
                raise self._setpoint_error(err, device_id) from None
            await self._async_send(
                device_id,
                self.client.set_mode_setpoints(
                    device_id,
                    mode=new_mode,
                    heat_setpoint=setpoints.heat,
                    cool_setpoint=setpoints.cool,
                ),
            )
            self._apply_write(
                device_id,
                mode=new_mode,
                heat_setpoint=setpoints.heat,
                cool_setpoint=setpoints.cool,
                schedule_enabled=False,
            )

    async def async_set_schedule(self, device_id: str, *, enabled: bool) -> None:
        """Enable or disable the thermostat's own schedule."""
        async with self._write_lock(device_id):
            self._require_state(device_id)
            await self._async_send(device_id, self.client.set_schedule(device_id, enabled=enabled))
            self._apply_write(device_id, schedule_enabled=enabled)

    async def async_set_fan(
        self,
        device_id: str,
        *,
        circulate: FanCirculate | None = None,
        speed: FanCirculateSpeed | None = None,
    ) -> None:
        """Change the fan circulation mode and/or speed.

        Daikin takes both values together, so the one not given is taken from
        the last known state.
        """
        async with self._write_lock(device_id):
            state = self._require_state(device_id)
            new_circulate = circulate if circulate is not None else state.fan_circulate
            new_speed = speed if speed is not None else state.fan_circulate_speed
            if new_circulate is None or new_speed is None:
                raise self._error("state_unknown", device_id)
            await self._async_send(
                device_id,
                self.client.set_fan(device_id, circulate=new_circulate, speed=new_speed),
            )
            self._apply_write(device_id, fan_circulate=new_circulate, fan_circulate_speed=new_speed)

    def _write_lock(self, device_id: str) -> asyncio.Lock:
        return self._write_locks.setdefault(device_id, asyncio.Lock())

    def _require_state(self, device_id: str) -> ThermostatState:
        """Return the thermostat's current state, or refuse the write."""
        thermostat = (self.data or {}).get(device_id)
        if not self.last_update_success or thermostat is None or thermostat.state is None:
            raise self._error("thermostat_unavailable", device_id)
        return thermostat.state

    async def _async_send(self, device_id: str, request: Awaitable[None]) -> None:
        """Send one write and turn client errors into translated ones."""
        try:
            await request
        except AuthenticationError:
            self.config_entry.async_start_reauth(self.hass)
            raise self._error("auth_failed", device_id) from None
        except RateLimitError as err:
            raise self._error(
                "rate_limited", device_id, seconds=str(round(err.retry_after))
            ) from None
        except DeviceOfflineError:
            self._replace(device_id, state=None, online=False, problem="offline")
            raise self._error("thermostat_offline", device_id) from None
        except BadRequestError:
            raise self._error("write_rejected", device_id) from None
        except DaikinOneError as err:
            raise self._error("cannot_connect", device_id, reason=type(err).__name__) from None

    def _apply_write(self, device_id: str, **changes: object) -> None:
        """Reflect a successful write now and schedule the verification read."""
        self._write_counts[device_id] = self._write_counts.get(device_id, 0) + 1
        self._settling_until[device_id] = dt_util.utcnow() + VERIFY_DELAY
        thermostat = (self.data or {}).get(device_id)
        if thermostat is not None and thermostat.state is not None:
            self._replace(device_id, state=dataclasses.replace(thermostat.state, **changes))  # type: ignore[arg-type]

        # One verification per thermostat, 15 seconds after its latest write.
        if cancel := self._cancel_verify.pop(device_id, None):
            cancel()

        @callback
        def _verify(_now: datetime) -> None:
            self._cancel_verify.pop(device_id, None)
            self.config_entry.async_create_background_task(
                self.hass, self._async_verify(device_id), name=f"{DOMAIN} verify write"
            )

        self._cancel_verify[device_id] = async_call_later(self.hass, VERIFY_DELAY, _verify)

    async def _async_verify(self, device_id: str) -> None:
        """Read one thermostat back after a write."""
        self._settling_until.pop(device_id, None)
        try:
            result = await self._async_read(device_id)
        except DaikinOneError as err:
            # The next regular poll reports account-wide problems properly.
            _LOGGER.debug("Verification read failed (%s)", type(err).__name__)
            return
        self._replace(device_id, state=result.state, online=result.online, problem=result.problem)

    def _replace(self, device_id: str, **changes: object) -> None:
        """Update one thermostat's data and tell its entities."""
        thermostat = (self.data or {}).get(device_id)
        if thermostat is None:
            return
        self.data = {**self.data, device_id: dataclasses.replace(thermostat, **changes)}  # type: ignore[arg-type]
        self.async_update_listeners()

    def _name(self, device_id: str) -> str:
        info = self._devices.get(device_id)
        return (info.name if info else None) or mask_id(device_id)

    def _error(
        self,
        key: str,
        device_id: str,
        *,
        invalid_input: bool = False,
        **placeholders: str,
    ) -> HomeAssistantError:
        """Build a translated error. ``invalid_input`` marks a mistake in the request itself."""
        error_type = ServiceValidationError if invalid_input else HomeAssistantError
        return error_type(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"name": self._name(device_id), **placeholders},
        )

    def _setpoint_error(self, err: SetpointError, device_id: str) -> HomeAssistantError:
        """Build the translated error for a refused setpoint, in the user's unit."""
        unit = self.hass.config.units.temperature_unit
        celsius = UnitOfTemperature.CELSIUS
        placeholders: dict[str, str] = {}
        if err.minimum is not None:
            placeholders["unit"] = str(unit)
        for name, value in (("minimum", err.minimum), ("maximum", err.maximum)):
            if value is not None:
                converted = TemperatureConverter.convert(value, celsius, unit)
                placeholders[name] = f"{converted:.1f}"
        if err.delta is not None:
            interval = TemperatureConverter.convert_interval(err.delta, celsius, unit)
            placeholders["delta"] = f"{interval:.1f}"
        # Limits or state the thermostat has not reported are not the caller's mistake.
        invalid_input = err.key not in ("limits_unknown", "state_unknown")
        return self._error(err.key, device_id, invalid_input=invalid_input, **placeholders)

    # ---------------------------------------------------------------- shutdown

    async def async_shutdown(self) -> None:
        """Cancel pending verification reads."""
        for cancel in self._cancel_verify.values():
            cancel()
        self._cancel_verify.clear()
        await super().async_shutdown()
